"""Claim discipline.

Certain phrases are overclaims for what this demo measures. They must not appear
in demo source, UI copy, or demo docs as active claims. Quoted refusals (e.g.
the flagship's own "does NOT provide exactly-once") are fine, so the check is
scoped to the demo's own files.
"""

from __future__ import annotations

import re
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = [
    r"exactly[- ]once",
    r"production[- ]ready",
    r"secure (financial )?agents?",
    r"safe (financial )?agents?",
    r"prompt[- ]injection[- ]?proof",
    r"tamper[- ]?proof",
    r"razorpay vulnerabilit",
    r"razorpay bug",
    r"razorpay security gap",
    r"industry first",
    r"first ever",
    r"guarantees? no duplicate refunds",
    r"razorpay simulator",
]

SCANNED = sorted(
    [p for p in (DEMO_ROOT / "app").rglob("*") if p.suffix in {".py", ".js", ".html", ".css"}]
    + [p for p in (DEMO_ROOT / "scripts").rglob("*.py")]
    + [p for p in (DEMO_ROOT / "tests").rglob("*.py")]
    + [
        DEMO_ROOT / "README.md",
        DEMO_ROOT / "start-demo.ps1",
        DEMO_ROOT / "stop-demo.ps1",
        DEMO_ROOT / "docker-compose.yml",
        DEMO_ROOT / ".env.example",
    ]
)


def test_no_forbidden_claims():
    hits = []
    for p in SCANNED:
        if not p.is_file() or p.name in {"test_claims.py", "test_browser.py"}:
            continue
        body = p.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            for m in re.finditer(pattern, body, re.IGNORECASE):
                line = body[: m.start()].count("\n") + 1
                hits.append(f"{p.relative_to(DEMO_ROOT)}:{line}: {m.group(0)!r}")
    assert not hits, "forbidden claim(s):\n" + "\n".join(hits)


def test_pr_114_is_described_as_open():
    html = (DEMO_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "pull/114" in html
    assert "Open for review" in html
    assert not re.search(r"merged", html, re.IGNORECASE)


def test_ui_states_the_provider_is_a_fixture():
    """The sandbox disclosure is visible WITHOUT opening anything."""
    html = (DEMO_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    flat = " ".join(html.split())

    # Always on screen, in the masthead.
    assert "Sandbox · deterministic provider · no real money" in flat

    # And stated in full in the evidence drawer.
    assert (
        "deterministic provider fixture modelled from measured Razorpay Test Mode "
        "behaviour. It makes no live Razorpay transactions and moves no real money." in flat
    )


def test_ui_separates_invocations_from_effects():
    """Provider calls and financial effects are never collapsed into one number.

    The counters are rendered by app.js from backend fields, so the labels live
    there rather than in the static markup.
    """
    js = (DEMO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "'Provider calls'" in js or '"Provider calls"' in js
    assert "'Provider invocations'" in js or '"Provider invocations"' in js
    assert "'Financial effects'" in js or '"Financial effects"' in js
    # And they are read from separate backend fields, never derived one from the other.
    assert "provider_invocations" in js
    assert "financial_effects" in js


def test_ui_carries_the_fair_baseline_disclaimer():
    """The naive lane must not be presented as how competent backends work."""
    flat = " ".join(
        (DEMO_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8").split()
    )
    assert "deliberately weak retry strategy that regenerates the provider identity" in flat
    assert "A disciplined caller that persists and reuses that key" in flat
    assert "binds it to durable business-operation state across attempts and recovery" in flat
    # And it must not blame the provider.
    assert not re.search(r"razorpay duplicates", flat, re.IGNORECASE)


def test_the_core_principle_is_stated_once_and_prominently():
    """The sentence the whole product exists to make concrete."""
    flat = " ".join(
        (DEMO_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8").split()
    )
    sentence = (
        "A retry can be a new execution attempt without becoming a new financial operation."
    )
    assert sentence in flat
    assert flat.count(sentence) == 1, "stated once, not repeated everywhere"


def test_identity_chain_is_present_as_technical_detail():
    """The four identities appear as quiet detail under the principle.

    Not as a headline: on the landing screen this vocabulary is jargon. It earns
    its place only next to the sentence it explains.
    """
    flat = " ".join(
        (DEMO_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8").split()
    )
    for node in (
        "Business operation",
        "Execution attempt",
        "Tool / transport request",
        "Provider idempotency identity",
    ):
        assert node in flat, node


def test_no_fabricated_transport_identity_is_claimed():
    """MCP is not in this demo's execution path, so no MCP id may be shown."""
    from app.scenario import demo_info

    assert "mcp" not in str(demo_info()).lower()

    js = (DEMO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "mcp_request" not in js.lower()
