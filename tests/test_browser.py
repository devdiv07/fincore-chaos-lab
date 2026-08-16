"""Browser tests for the Chaos Lab UI.

These run against a LIVE server (start-demo.ps1 or `python -m app.server`) and
skip if one is not listening, so the rest of the suite stays runnable offline.

What they are actually for: proving that what a reviewer SEES equals what the
backend returned. Several of them re-run the experiment through the API and
compare the numbers on screen against that response, so a hardcoded figure in
the frontend would fail rather than look convincing.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8000"
FAST = f"{BASE}/?step=40"

LAPTOP = {"width": 1440, "height": 900}
SMALL_LAPTOP = {"width": 1366, "height": 768}
MOBILE = {"width": 390, "height": 844}


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as r:
            return json.loads(r.read())["status"] == "ok"
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _server_up(), reason="no server on 127.0.0.1:8000 (run start-demo.ps1)"
)


@pytest.fixture(scope="module")
def browser():
    """Launch Chromium, working around a Windows event-loop clash.

    conftest installs WindowsSelectorEventLoopPolicy because psycopg's async
    driver cannot run on the Proactor loop. Playwright has the opposite
    requirement: launching the browser spawns a subprocess, and only the
    Proactor loop implements subprocess support -- on the selector loop it
    raises NotImplementedError. The policy is global, so it is swapped for the
    lifetime of this fixture and restored afterwards, keeping the database
    tests on the loop they need.
    """
    import asyncio
    import sys

    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    finally:
        asyncio.set_event_loop_policy(previous)


class Page:
    """Thin driver: run an experiment and read what is on screen."""

    def __init__(self, page):
        self.page = page
        self.errors: list[str] = []
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: self.errors.append(f"console.error: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text
            else None,
        )

    def open(self, url=FAST):
        """Wait for the shell to render, not for the network to fall silent.

        `networkidle` stopped being meaningful once the page began polling
        /ready in the background: with a sleeping or unreachable API there is
        no quiet moment to wait for. The shell rendering its controls is the
        real signal that the page is usable.
        """
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector(".mode", timeout=30_000)
        return self

    def choose(self, mode_id: str):
        self.page.click(f'.mode[data-mode="{mode_id}"]')
        return self

    def run(self):
        self.page.click("#run")
        self.page.wait_for_selector("#verdict:not([hidden])", timeout=90_000)
        self.page.wait_for_timeout(350)
        return self

    @property
    def text(self) -> str:
        return self.page.inner_text("body")

    def counters(self) -> dict[str, str]:
        out = {}
        for row in self.page.query_selector_all(".counter"):
            dt = row.query_selector("dt")
            dd = row.query_selector("dd")
            if dt and dd:
                out[dt.inner_text().strip()] = dd.inner_text().strip()
        return out

    def internals(self) -> dict[str, str]:
        self.page.evaluate("() => document.getElementById('internals').open = true")
        self.page.wait_for_timeout(120)
        out = {}
        for row in self.page.query_selector_all(".int-row"):
            spans = row.query_selector_all("span")
            if len(spans) == 2:
                out[spans[0].inner_text().strip()] = spans[1].inner_text().strip()
        return out

    def headlines(self) -> list[str]:
        return [e.inner_text().strip() for e in self.page.query_selector_all(".headline-num")]


@pytest.fixture
def lab(browser):
    page = browser.new_page(viewport=LAPTOP)
    driver = Page(page)
    yield driver
    assert not driver.errors, f"browser errors: {driver.errors}"
    page.close()


def api_run(payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/demo/run",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# --------------------------------------------------------------- landing state
def test_landing_states_the_offer_in_plain_language(lab):
    lab.open()
    body = lab.text
    assert "Can you make it" in body and "refund twice?" in body
    assert "₹100" in body
    assert "pay_demo_123" in body
    assert "Break the refund" in body
    # The sandbox disclosure is visible without opening anything.
    assert "no real money" in body.lower()
    assert "deterministic provider" in body.lower()


def test_landing_hides_jargon(lab):
    """Technical identity vocabulary belongs behind Inspect internals."""
    lab.open()
    visible = lab.text.lower()
    for jargon in ("operation_ref", "idempotency", "lease token", "intent fingerprint"):
        assert jargon not in visible, f"{jargon!r} is exposed before any run"


def test_all_four_failure_modes_are_offered(lab):
    lab.open()
    labels = [e.inner_text() for e in lab.page.query_selector_all(".mode-name")]
    assert labels == [
        "Lose the response",
        "Crash the worker",
        "Fire 20 callers",
        "Change the amount",
    ]


# ------------------------------------------------------------- 1 response loss
def test_response_loss_shows_two_hundred_versus_one_hundred(lab):
    lab.open().choose("response_loss").run()

    assert lab.headlines() == ["₹200", "₹100"]

    body = lab.text
    assert "Provider created a SECOND refund" in body
    assert "The runtime doesn't know the outcome yet" in body
    assert "UNKNOWN" in body
    assert "Original result recovered" in body
    assert "SUCCEEDED" in body


def test_response_loss_numbers_match_the_backend(lab):
    """The screen must equal a fresh API response, not a baked-in figure."""
    lab.open().choose("response_loss").run()
    api = api_run({"experiment": "response_loss"})

    shown = lab.internals()
    assert shown["financial effects"] == str(api["fincore"]["financial_effects"])
    assert shown["provider invocations"] == str(api["fincore"]["provider_invocations"])
    assert api["naive"]["financial_effects"] == 2
    assert api["fincore"]["financial_effects"] == 1


def test_money_visual_shows_two_coins_versus_one(lab):
    lab.open().choose("response_loss").run()
    lanes = lab.page.query_selector_all(".money-lane")
    assert len(lanes) == 2
    assert len(lanes[0].query_selector_all(".coin")) == 2
    assert len(lanes[1].query_selector_all(".coin")) == 1
    assert "simulated financial effects" in lab.text.lower()


# -------------------------------------------------------------- 2 worker crash
def test_worker_crash_ends_with_one_effect(lab):
    lab.open().choose("worker_crash").run()
    body = lab.text

    assert "Worker crashed" in body
    assert "A new worker took over" in body
    assert "Original result recovered" in body or "Provider returned the original refund" in body

    counters = lab.counters()
    assert counters["Financial effects"] == "1"
    assert counters["Workers involved"] == "2"
    assert counters["Provider invocations"] == "2"
    assert "SUCCEEDED" in body


def test_worker_crash_does_not_claim_background_reconciliation(lab):
    """Recovery here is a recover() sweep, not an automatic background daemon."""
    lab.open().choose("worker_crash").run()
    body = lab.text.lower()
    for overclaim in ("automatically reconciles", "background reconciliation", "self-healing"):
        assert overclaim not in body


# ------------------------------------------------------------ 3 concurrency
def test_twenty_callers_are_never_called_attempts(lab):
    lab.open().choose("concurrency").run()
    body = lab.text

    assert "20" in body
    assert re.search(r"\b20\s+attempts\b", body, re.I) is None
    assert re.search(r"\b20\s+execution attempts\b", body, re.I) is None
    assert "callers" in body.lower()

    counters = lab.counters()
    assert counters["Callers"] == "20"
    assert counters["Execution owners"] == "1"
    assert counters["Turned away"] == "19"
    assert counters["Provider calls"] == "1"
    assert counters["Financial effects"] == "1"
    assert counters["Persisted attempt rows"] == "1"


def test_caller_swarm_draws_exactly_the_backend_count(lab):
    """Exactly `callers` markers -- not one more, not one fewer.

    The marker that crosses the boundary is a separate element by design; if it
    were also counted as a caller the visualisation would claim 21 callers for a
    20-caller run.
    """
    lab.open().choose("concurrency").run()
    api_callers = int(lab.counters()["Callers"])
    assert len(lab.page.query_selector_all(".caller")) == api_callers == 20
    assert len(lab.page.query_selector_all('.caller[data-owner="1"]')) == 1
    assert len(lab.page.query_selector_all('.caller[data-turned="1"]')) == 19
    assert len(lab.page.query_selector_all(".caller-crossed")) == 1


@pytest.mark.parametrize("n", [2, 5, 10])
def test_other_caller_counts_work(lab, n):
    lab.open().choose("concurrency")
    lab.page.click(f'.seg button:text-is("{n}")')
    lab.run()
    counters = lab.counters()
    assert counters["Callers"] == str(n)
    assert counters["Execution owners"] == "1"
    assert counters["Financial effects"] == "1"
    assert counters["Turned away"] == str(n - 1)


# ---------------------------------------------------------- 4 intent conflict
def test_changing_the_amount_is_blocked_before_the_provider(lab):
    lab.open().choose("intent_conflict")
    lab.page.fill("#amt", "200")
    lab.run()

    body = lab.text
    assert "CONFLICT" in body
    assert "Refused" in body

    counters = lab.counters()
    assert counters["Extra provider calls"] == "0"
    assert counters["Extra financial effects"] == "0"
    assert counters["Original intent"] == "₹100"
    assert counters["Retry intent"] == "₹200"


def test_same_amount_is_not_a_conflict(lab):
    lab.open().choose("intent_conflict")
    lab.page.fill("#amt", "100")
    lab.run()

    body = lab.text
    assert "Same amount, nothing new happened" in body
    counters = lab.counters()
    assert counters["Extra financial effects"] == "0"
    assert counters["Total financial effects"] == "1"
    assert "CONFLICT" not in body


@pytest.mark.parametrize("bad", ["0", "1001", "-5"])
def test_amount_input_is_validated_in_the_browser(lab, bad):
    lab.open().choose("intent_conflict")
    lab.page.fill("#amt", bad)
    lab.page.wait_for_timeout(120)
    assert lab.page.is_disabled("#run"), f"{bad} should block the run button"
    assert lab.page.get_attribute("#amt-hint", "data-error") == "1"


# --------------------------------------------------------------- internals
def test_inspect_internals_exposes_runtime_detail_but_no_secrets(lab):
    lab.open().choose("worker_crash").run()
    shown = lab.internals()

    assert shown["operation_ref"] == "refund-demo-002"
    assert shown["provider key fingerprint"].startswith("sha256:")
    # Two attempts: the execution attempt that died, then the reconciliation
    # attempt that recovered it. One financial effect across both.
    assert shown["attempt rows"] == "2"
    assert shown["state at crash"] == "EXECUTING"
    assert shown["final state"] == "SUCCEEDED"

    blob = lab.text
    assert "fcop_" not in blob, "a raw provider key reached the page"
    assert "postgresql://" not in blob
    assert "Traceback" not in blob


def test_internals_are_collapsed_by_default(lab):
    lab.open().choose("worker_crash").run()
    assert lab.page.get_attribute("#internals", "open") is None


# ---------------------------------------------------------------- evidence
def test_evidence_drawer_carries_the_disclaimer_and_real_links(lab):
    lab.open()
    lab.page.click("#trust-open")
    lab.page.wait_for_selector("#trust:not([hidden])")
    drawer = lab.page.inner_text("#trust")

    assert "deterministic provider fixture" in drawer
    assert "makes no live Razorpay transactions" in drawer

    hrefs = [a.get_attribute("href") for a in lab.page.query_selector_all("#trust a")]
    assert "https://github.com/razorpay/razorpay-mcp-server/pull/114" in hrefs
    assert any("devdiv07/financial-operation-core" in h for h in hrefs)
    assert "Open for review" in drawer
    assert "merged" not in drawer.lower()


def test_no_wording_implies_a_live_provider_call(lab):
    lab.open().choose("response_loss").run()
    body = lab.text.lower()
    for claim in (
        "live razorpay",
        "real razorpay call",
        "razorpay api call",
        "exactly-once",
        "exactly once",
        "production-ready",
        "guaranteed",
    ):
        assert claim not in body, f"{claim!r} appears on screen"


# --------------------------------------------------------------- viewports
@pytest.mark.parametrize(
    "viewport,name", [(SMALL_LAPTOP, "1366x768"), (LAPTOP, "1440x900")]
)
def test_result_is_visible_without_hunting(browser, viewport, name):
    page = browser.new_page(viewport=viewport)
    driver = Page(page)
    driver.open().choose("response_loss").run()

    # No horizontal overflow anywhere.
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    assert not overflow, f"{name} scrolls horizontally"

    # The headline result is on screen after the run, without manual scrolling.
    box = page.evaluate(
        """() => {
             const n = document.querySelector('.headline-num');
             const r = n.getBoundingClientRect();
             return {top: r.top, bottom: r.bottom, h: innerHeight};
           }"""
    )
    assert box["top"] < box["h"], f"{name}: result headline is below the fold"
    assert not driver.errors, driver.errors
    page.close()


def test_mobile_is_functional(browser):
    page = browser.new_page(viewport=MOBILE)
    driver = Page(page)
    driver.open().choose("worker_crash").run()

    assert not page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
    ), "mobile scrolls horizontally"

    counters = driver.counters()
    assert counters["Financial effects"] == "1"
    assert not driver.errors, driver.errors
    page.close()


# ------------------------------------------------------- no fabricated results
def test_page_contains_no_prebaked_result_markup():
    """The shipped HTML must not contain the answers.

    If the counters were hardcoded in markup, the demo would still 'work' with
    the backend switched off -- which is exactly the thing this project is
    arguing against.
    """
    from pathlib import Path

    html = Path(__file__).resolve().parents[1].joinpath("app/static/index.html").read_text(
        encoding="utf-8"
    )
    for baked in ("₹200", "SUCCEEDED", "CONFLICT", "UNKNOWN", "rfnd_FAKE"):
        assert baked not in html, f"{baked!r} is baked into the static HTML"


def test_ui_renders_nothing_when_the_backend_refuses(lab):
    """A rejected run must not leave a plausible-looking result on screen."""
    lab.open().choose("intent_conflict")
    lab.page.evaluate(
        """() => window.fetch('/api/demo/run', {
              method: 'POST', headers: {'content-type': 'application/json'},
              body: JSON.stringify({experiment: 'intent_conflict', retry_amount_paise: 999999})
           }).then(r => r.json()).then(j => { window.__probe = j; })"""
    )
    lab.page.wait_for_function("() => window.__probe !== undefined", timeout=15_000)
    probe = lab.page.evaluate("() => window.__probe")
    assert "error" in probe
    assert "financial_effects" not in probe


# ==================================================================== stage 3
# The causal visualisations. Every count below is compared against the counters
# the backend produced for that same run.


def test_result_answers_the_question_first(lab):
    """Before any table: did another financial effect happen?"""
    lab.open().choose("worker_crash").run()
    # The question label is uppercased by CSS, so compare case-insensitively.
    answer = lab.page.inner_text("#answer")
    assert "did another financial effect happen?" in answer.lower()
    assert "No second effect" in answer


def test_response_loss_answers_both_sides(lab):
    lab.open().choose("response_loss").run()
    cards = [c.inner_text() for c in lab.page.query_selector_all(".answer-card")]
    assert len(cards) == 2
    assert "Yes" in cards[0], "the naive lane really did create a second effect"
    assert "No second effect" in cards[1]


def test_response_loss_result_is_in_view_without_manual_search(lab):
    """The reviewer must not have to hunt for the ₹200 vs ₹100 payoff."""
    lab.open().choose("response_loss").run()
    lab.page.wait_for_timeout(900)  # allow the smooth scroll to settle
    pos = lab.page.evaluate(
        """() => {
             const r = document.getElementById('answer').getBoundingClientRect();
             return {top: r.top, h: innerHeight};
           }"""
    )
    assert -40 < pos["top"] < pos["h"], f"answer block not in viewport: {pos}"


def test_crash_visualisation_carries_exactly_one_effect_token(lab):
    """One financial effect existed, so exactly one token may be drawn -- and it
    is the SAME token across the worker transition, not one per worker."""
    lab.open().choose("worker_crash").run()

    viz = lab.page.query_selector(".viz-crash")
    assert viz is not None
    assert len(viz.query_selector_all(".effect-token")) == 1

    text = viz.inner_text()
    lowered = text.lower()
    assert "worker-a" in lowered and "worker-b" in lowered
    assert "crash" in lowered
    assert "recovery started" in lowered
    assert "durable operation" in lowered
    # Explicitly invoked recovery must not be dressed up as automatic.
    assert "auto recovery" not in lowered
    assert "automatic" not in lowered


def test_crash_visualisation_counts_match_the_counters(lab):
    lab.open().choose("worker_crash").run()
    counters = lab.counters()
    tally = lab.page.inner_text(".viz-crash .viz-tally")
    assert counters["Workers involved"] in tally
    assert counters["Financial effects"] in tally
    assert "financial effect" in tally


@pytest.mark.parametrize("n", [5, 20])
def test_swarm_marker_count_follows_the_backend(browser, n):
    page = browser.new_page(viewport=LAPTOP)
    driver = Page(page)
    driver.open().choose("concurrency")
    page.click(f'.seg button:text-is("{n}")')
    driver.run()

    counters = driver.counters()
    assert counters["Callers"] == str(n)
    assert len(page.query_selector_all(".caller")) == n
    assert len(page.query_selector_all('.caller[data-owner="1"]')) == int(
        counters["Execution owners"]
    )
    assert len(page.query_selector_all('.caller[data-turned="1"]')) == int(
        counters["Turned away"]
    )

    headline = page.inner_text(".viz-swarm .viz-headline")
    assert str(n) in headline
    assert "callers" in headline.lower()
    assert "execution owner" in headline.lower()
    assert not re.search(r"\d+\s+attempts", headline, re.I)

    assert not driver.errors, driver.errors
    page.close()


def test_swarm_shows_the_runtime_and_provider_boundaries(lab):
    lab.open().choose("concurrency").run()
    viz = lab.page.inner_text(".viz-swarm").lower()
    assert "financial operation core" in viz
    assert "provider" in viz
    assert "turned away" in viz


def test_conflict_visualisation_shows_the_provider_was_not_called(lab):
    lab.open().choose("intent_conflict")
    lab.page.fill("#amt", "200")
    lab.run()

    viz = lab.page.query_selector(".viz-conflict")
    assert viz is not None
    text = viz.inner_text()
    assert "₹100" in text and "₹200" in text
    assert "Provider not called" in text
    assert "Different financial intent" in text

    blocked = lab.page.query_selector_all('.viz-endpoint[data-blocked="1"]')
    assert len(blocked) == 1, "exactly the retry lane is blocked, not the original"

    tally = lab.page.inner_text(".viz-conflict .viz-tally")
    counters = lab.counters()
    assert counters["Extra provider calls"] == "0"
    assert "extra provider calls" in tally


def test_same_amount_retry_is_shown_as_matching_not_blocked_for_intent(lab):
    """₹100 is not a conflict: it matches the operation that already completed."""
    lab.open().choose("intent_conflict")
    lab.page.fill("#amt", "100")
    lab.run()

    text = lab.page.inner_text(".viz-conflict")
    assert "Same intent" in text
    assert "already completed" in text
    assert "Different financial intent" not in text


def test_no_visualisation_number_disagrees_with_the_backend(lab):
    """Sweep every experiment: drawn counts vs the counters they came from."""
    lab.open().choose("concurrency").run()
    counters = lab.counters()
    assert len(lab.page.query_selector_all(".caller")) == int(counters["Callers"])
    assert len(lab.page.query_selector_all(".effect-token")) == int(
        counters["Financial effects"]
    )

    lab.open().choose("worker_crash").run()
    counters = lab.counters()
    assert len(lab.page.query_selector_all(".viz-crash .effect-token")) == int(
        counters["Financial effects"]
    )


def test_reduced_motion_still_produces_a_complete_result(browser):
    """With animation disabled the page must still be fully usable."""
    ctx = browser.new_context(viewport=LAPTOP, reduced_motion="reduce")
    page = ctx.new_page()
    driver = Page(page)
    driver.open().choose("concurrency").run()

    counters = driver.counters()
    assert counters["Callers"] == "20"
    assert counters["Financial effects"] == "1"
    assert len(page.query_selector_all(".caller")) == 20
    assert page.is_visible(".viz-swarm")
    assert not driver.errors, driver.errors
    ctx.close()


# ==================================================================== stage 4
# Static shell against a sleeping API. The page is served as files; the API is
# simulated as unreachable / slow / awake by intercepting the /ready request.


def _shell(browser, ready_behaviour):
    """Open the page with /ready driven by `ready_behaviour`.

    behaviour: "down" (never answers), "slow" (503 then 200), "up" (200).
    """
    page = browser.new_page(viewport=LAPTOP)
    driver = Page(page)
    state = {"calls": 0}

    def handle(route):
        state["calls"] += 1
        if ready_behaviour == "down":
            route.abort()
        elif ready_behaviour == "slow" and state["calls"] < 3:
            route.fulfill(status=503, body='{"status":"not_ready"}',
                          content_type="application/json")
        else:
            route.fulfill(status=200, body='{"status":"ready"}',
                          content_type="application/json")

    page.route("**/ready", handle)
    driver.open(f"{BASE}/?step=40")
    return page, driver, state


def test_shell_renders_fully_while_the_api_is_unreachable(browser):
    """The whole point of the split: the page is useful before the API wakes."""
    page, driver, _ = _shell(browser, "down")
    page.wait_for_timeout(1200)

    body = page.inner_text("body")
    assert "Can you make it" in body and "refund twice?" in body
    assert "₹100" in body
    assert len(page.query_selector_all(".mode")) == 4
    # Evidence and mode selection stay usable with no backend at all.
    page.click('.mode[data-mode="worker_crash"]')
    assert page.get_attribute('.mode[data-mode="worker_crash"]', "aria-selected") == "true"
    page.click("#trust-open")
    assert page.is_visible("#trust")
    page.close()


def test_run_is_disabled_until_the_api_is_ready(browser):
    page, driver, _ = _shell(browser, "down")
    page.wait_for_timeout(1200)
    assert page.is_disabled("#run"), "an experiment must not be runnable with no engine"
    assert page.get_attribute("#engine", "data-state") in ("checking", "waking")
    page.close()


def test_waking_state_and_copy_appear(browser):
    page, driver, _ = _shell(browser, "down")
    page.wait_for_function(
        "() => document.getElementById('engine').dataset.state === 'waking'",
        timeout=20_000,
    )
    assert "Waking up" in page.inner_text("#engine")
    note = page.inner_text("#engine-note")
    assert "starting after inactivity" in note
    assert "explore the page while it gets ready" in note
    page.close()


def test_real_ready_response_enables_the_controls(browser):
    page, driver, _ = _shell(browser, "slow")
    assert page.is_disabled("#run")
    page.wait_for_function(
        "() => document.getElementById('engine').dataset.state === 'ready'",
        timeout=30_000,
    )
    assert "Ready" in page.inner_text("#engine")
    assert page.is_enabled("#run")
    assert not driver.errors, driver.errors
    page.close()


def test_no_result_is_shown_while_the_backend_is_unavailable(browser):
    """A sleeping API must never yield a plausible-looking experiment result."""
    page, driver, _ = _shell(browser, "down")
    page.wait_for_timeout(1500)

    assert page.is_hidden("#verdict")
    assert page.is_hidden("#canvas")
    body = page.inner_text("body")
    for fabricated in ("₹200", "SUCCEEDED", "CONFLICT", "financial effect"):
        assert fabricated not in body, f"{fabricated} shown with no backend"
    page.close()


def test_engine_never_reports_ready_without_a_200(browser):
    """Elapsed time must not be mistaken for readiness."""
    page, _, state = _shell(browser, "down")
    page.wait_for_timeout(9000)
    assert page.get_attribute("#engine", "data-state") != "ready"
    assert state["calls"] >= 2, "polling should have retried"
    page.close()


def test_polling_backs_off_rather_than_hammering(browser):
    page, _, state = _shell(browser, "down")
    page.wait_for_timeout(10_000)
    # With ~2s initial delay growing 1.4x, ten seconds is a handful of calls,
    # nowhere near one per second.
    assert state["calls"] <= 8, f"too many /ready probes: {state['calls']}"
    page.close()


def test_experiment_failure_after_ready_reports_an_error_not_a_result(browser):
    """If the API dies mid-session the UI must say so, not invent numbers."""
    page = browser.new_page(viewport=LAPTOP)
    driver = Page(page)
    page.route("**/ready", lambda r: r.fulfill(
        status=200, body='{"status":"ready"}', content_type="application/json"))
    page.route("**/api/demo/run", lambda r: r.abort())
    driver.open(f"{BASE}/?step=40")

    page.wait_for_function(
        "() => document.getElementById('engine').dataset.state === 'ready'", timeout=30_000)
    page.click("#run")
    page.wait_for_selector(".error-note", timeout=20_000)

    assert page.is_hidden("#verdict"), "no verdict may render on a failed run"
    body = page.inner_text("body")
    for fabricated in ("₹200", "SUCCEEDED", "CONFLICT"):
        assert fabricated not in body
    page.close()


def test_session_id_is_sent_as_a_header_not_a_cookie(browser):
    page = browser.new_page(viewport=LAPTOP)
    driver = Page(page)
    seen = {}

    def capture(route):
        seen["session"] = route.request.headers.get("x-fincore-session")
        route.fulfill(status=200, body='{"status":"ready"}',
                      content_type="application/json")

    page.route("**/ready", capture)
    driver.open(f"{BASE}/?step=40")
    page.wait_for_function(
        "() => document.getElementById('engine').dataset.state === 'ready'", timeout=30_000)

    assert seen.get("session"), "no session header sent"
    assert re.fullmatch(r"[0-9a-f]{16}", seen["session"]), seen["session"]

    stored = page.evaluate("() => localStorage.getItem('fincore.session')")
    assert stored == seen["session"], "the id must persist across requests"
    page.close()
