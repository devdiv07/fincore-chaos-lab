"""This demo is meant to be publicly reachable, so it is tested like it.

Assume the caller is hostile: bad cookies, bad bodies, oversized bodies, unknown
experiments, out-of-range numbers, and someone pressing Run in a loop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.limits import runs
from app.main import app
from app.session import is_valid_session_id, tenant_for


@pytest.fixture
def client():
    runs.reset()
    with TestClient(app) as c:
        yield c
    runs.reset()


# ------------------------------------------------------------------ validation
def test_unknown_experiment_is_rejected(client):
    res = client.post("/api/demo/run", json={"experiment": "../../etc/passwd"})
    assert res.status_code == 400
    assert "unknown experiment" in res.json()["error"]


@pytest.mark.parametrize("payload", [{"experiment": 5}, {"experiment": None}, {"experiment": []}])
def test_non_string_experiment_is_rejected(client, payload):
    assert client.post("/api/demo/run", json=payload).status_code == 400


@pytest.mark.parametrize("callers", [1, 3, 21, 9999, -1, "20; DROP TABLE operations"])
def test_concurrency_input_is_validated(client, callers):
    res = client.post(
        "/api/demo/run", json={"experiment": "concurrency", "callers": callers}
    )
    assert res.status_code == 400
    assert "error" in res.json()


@pytest.mark.parametrize("amount", [0, 99, 100_001, -1, "abc", None])
def test_retry_amount_is_validated(client, amount):
    res = client.post(
        "/api/demo/run",
        json={"experiment": "intent_conflict", "retry_amount_paise": amount},
    )
    assert res.status_code == 400


def test_oversized_body_is_rejected(client):
    res = client.post(
        "/api/demo/run",
        content=b'{"experiment":"response_loss","junk":"' + b"A" * 8000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413


def test_malformed_json_is_rejected(client):
    res = client.post(
        "/api/demo/run", content=b"not json", headers={"content-type": "application/json"}
    )
    assert res.status_code == 400


def test_error_responses_never_leak_internals(client):
    """No stack trace, no driver text, no schema or path names."""
    for payload in (
        {"experiment": "nope"},
        {"experiment": "concurrency", "callers": 7},
        {"experiment": "intent_conflict", "retry_amount_paise": -3},
    ):
        body = client.post("/api/demo/run", json=payload).text.lower()
        for leak in (
            "traceback",
            "sqlalchemy",
            "psycopg",
            "select ",
            "c:\\users",
            "/app/",
            "operations",
            "file \"",
        ):
            assert leak not in body, f"{leak!r} leaked in {body!r}"


# -------------------------------------------------------------- rate limiting
def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(runs, "_limit", 3)
    codes = []
    for _ in range(6):
        codes.append(
            client.post("/api/demo/run", json={"experiment": "intent_conflict"}).status_code
        )
    assert 429 in codes, f"expected a rate limit, got {codes}"
    res = client.post("/api/demo/run", json={"experiment": "intent_conflict"})
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert "error" in res.json()


def test_rate_limit_is_per_session(client, monkeypatch):
    monkeypatch.setattr(runs, "_limit", 2)
    for _ in range(3):
        client.post("/api/demo/run", json={"experiment": "intent_conflict"})
    blocked = client.post("/api/demo/run", json={"experiment": "intent_conflict"})
    assert blocked.status_code == 429

    # A different session is unaffected.
    fresh = TestClient(app)
    ok = fresh.post("/api/demo/run", json={"experiment": "intent_conflict"})
    assert ok.status_code == 200


def test_limiter_does_not_grow_without_bound():
    from app.limits import RateLimiter

    rl = RateLimiter(limit=1, window_seconds=0.001)
    for i in range(5000):
        rl.check(f"session-{i}")
    assert len(rl._hits) < 5000, "stale sessions must be pruned"


# ----------------------------------------------------------- session isolation
def test_session_cookie_is_set_and_scoped(client):
    res = client.post("/api/demo/run", json={"experiment": "intent_conflict"})
    assert res.status_code == 200
    cookie = res.cookies.get("fincore_lab_session")
    assert cookie is not None
    assert is_valid_session_id(cookie)
    header = res.headers.get("set-cookie", "").lower()
    assert "httponly" in header
    assert "samesite=lax" in header


@pytest.mark.parametrize(
    "value",
    ["", "x", "../../etc", "'; DROP TABLE operations; --", "g" * 16, "A" * 200, None],
)
def test_malicious_session_cookies_are_rejected(value):
    """The cookie reaches a tenant_id column, so it is shape-validated."""
    assert is_valid_session_id(value) is False


def test_forged_cookie_does_not_reach_the_database(client):
    """An invalid cookie is replaced by a fresh id rather than being trusted."""
    injected = "'; DELETE FROM operations; --"
    client.cookies.set("fincore_lab_session", injected)
    res = client.post("/api/demo/run", json={"experiment": "intent_conflict"})
    assert res.status_code == 200
    assert injected not in res.text


def test_two_sessions_do_not_share_results(client):
    a = client.post("/api/demo/run", json={"experiment": "worker_crash"}).json()
    other = TestClient(app)
    b = other.post("/api/demo/run", json={"experiment": "worker_crash"}).json()

    assert a["operation_id"] != b["operation_id"]
    assert a["financial_effects"] == b["financial_effects"] == 1


def test_tenant_namespaces_are_distinct_and_bounded():
    t = tenant_for("0123456789abcdef", "0123456789ab")
    assert t.startswith("lab-")
    assert len(t) <= 128
    assert set(t) <= set("abcdef0123456789-lab")


async def test_a_run_never_deletes_another_visitors_rows(session_factory, tenant):
    """The regression this design exists to prevent.

    The previous implementation truncated the operation tables at the start of
    every run. On a public URL that means visitor B pressing Run destroys the
    rows visitor A's experiment is still using. A run must only ever ADD.
    """
    from sqlalchemy import text

    from app.experiments import run_experiment

    victim = tenant()
    await run_experiment(session_factory, "worker_crash", victim, {})

    async def victim_rows() -> int:
        async with session_factory() as s:
            return (
                await s.execute(
                    text("SELECT count(*) FROM operations WHERE tenant_id = :t"),
                    {"t": victim},
                )
            ).scalar_one()

    assert await victim_rows() == 1

    # Five other visitors run every experiment.
    for _ in range(2):
        await run_experiment(session_factory, "worker_crash", tenant(), {})
        await run_experiment(session_factory, "concurrency", tenant(), {"callers": 5})
        await run_experiment(session_factory, "response_loss", tenant(), {})

    assert await victim_rows() == 1, "another visitor's run destroyed these rows"


# ------------------------------------------------------------------- headers
def test_security_headers_present(client):
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in res.headers["Cache-Control"]
    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_cleanup_is_bounded_and_spares_active_runs(session_factory, tenant):
    """Cleanup deletes by AGE only, so a run in progress is never a candidate.

    The public POST /api/demo/sweep route was removed for deployment: an
    unauthenticated endpoint whose only job is to delete rows is a liability
    with no upside, and the browser has no business triggering maintenance.
    The function is exercised directly, the way the janitor calls it.
    """
    from sqlalchemy import text

    from app.experiments import run_experiment
    from app.janitor import run_once

    active = tenant()
    await run_experiment(session_factory, "worker_crash", active, {})

    result = await run_once(session_factory)
    assert "deleted_operations" in result
    assert result.get("error") is None

    async with session_factory() as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM operations WHERE tenant_id = :t"), {"t": active}
            )
        ).scalar_one()
    assert n == 1, "a run younger than the retention window must survive cleanup"


def test_public_sweep_route_no_longer_exists(client):
    for method in ("post", "get"):
        res = getattr(client, method)("/api/demo/sweep")
        assert res.status_code in (404, 405), f"{method} /api/demo/sweep is still reachable"
