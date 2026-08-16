"""Static shell + sleeping API.

The frontend is served from a static host; the execution API is a free-tier
service that sleeps. These tests cover the seam: CORS, the cross-origin session
id, and the guarantee that the shell never fabricates readiness or results.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DEMO_ROOT = Path(__file__).resolve().parents[1]
STATIC = DEMO_ROOT / "app" / "static"
CONFIG_JS = (STATIC / "config.js").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")

STATIC_ORIGIN = "https://fincore-chaos-lab-ui.onrender.com"
EVIL_ORIGIN = "https://not-our-site.example"


def _app(monkeypatch, origins):
    """Rebuild the app with a given allowlist, as a deployment would."""
    import app.config as config

    if origins is None:
        monkeypatch.delenv("FINCORE_ALLOWED_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("FINCORE_ALLOWED_ORIGIN", origins)
    importlib.reload(config)
    import app.api
    import app.main

    importlib.reload(app.api)
    return importlib.reload(app.main).app


@pytest.fixture
def restore_app():
    yield
    import app.config

    importlib.reload(app.config)
    import app.api
    import app.main

    importlib.reload(app.api)
    importlib.reload(app.main)


# ------------------------------------------------------------------- API base
def test_api_base_is_centralised_in_one_file():
    """The deployed API origin must appear in config.js and nowhere else."""
    assert "onrender.com" in CONFIG_JS
    assert "onrender.com" not in APP_JS, "app.js must not know the API's location"
    assert "onrender.com" not in INDEX


def test_every_api_call_goes_through_the_helper():
    """No bare same-origin fetch may survive; it would break on the static host."""
    assert "function api(path" in APP_JS
    assert "CFG.apiBase + path" in APP_JS
    for call in re.findall(r"fetch\(\s*['\"]([^'\"]+)", APP_JS):
        assert not call.startswith("/api"), f"bare same-origin fetch: {call}"


def test_static_assets_contain_no_database_or_secret():
    for name in ("config.js", "app.js", "index.html", "styles.css"):
        body = (STATIC / name).read_text(encoding="utf-8").lower()
        for leak in ("neon.tech", "postgres", "database_url", "sslmode",
                     "npg_", "sk-", "ghp_"):
            assert leak not in body, f"{name} leaks {leak!r}"


def test_shell_assets_are_relative_so_both_hosts_serve_them():
    for asset in ("styles.css", "config.js", "app.js"):
        assert '"' + asset + '"' in INDEX
    assert "/static/" not in INDEX


# ------------------------------------------------------------------ readiness
def test_ready_is_only_set_from_a_real_probe():
    """READY must never be inferred from elapsed time."""
    assert APP_JS.count("setEngine('ready')") == 1
    window = APP_JS[max(0, APP_JS.index("setEngine('ready')") - 220):]
    assert "probeReady()" in window[:280], "ready must follow a /ready probe"
    assert "res.status === 200" in APP_JS


def test_run_is_blocked_until_the_engine_is_ready():
    assert "if (busy || !engineReady) return;" in APP_JS
    assert "$('run').disabled = !engineReady" in APP_JS


def test_polling_backs_off_and_gives_up_visibly():
    assert "delay * 1.4" in APP_JS
    assert "90_000" in APP_JS
    assert "Taking longer than expected" in APP_JS
    assert "Try again" in APP_JS


def _user_facing_strings(js: str) -> list[str]:
    """Quoted literals that plausibly reach the screen.

    Checking raw source would flag identifiers like `renderVerdict` or
    `downloaded`; only string content is shown to a reviewer.
    """
    return re.findall(r"'([^'\n]{4,})'|\"([^\"\n]{4,})\"", js) and [
        a or b for a, b in re.findall(r"'([^'\n]{4,})'|\"([^\"\n]{4,})\"", js)
    ]


def test_no_fake_progress_percentage():
    """A percentage would have to be measured to be honest, and none is."""
    for text in _user_facing_strings(APP_JS):
        assert not re.search(r"\d+\s*%", text), "invented percentage: " + text
        assert "% loaded" not in text.lower()
        assert "% complete" not in text.lower()


def test_waking_copy_is_present_and_provider_neutral():
    """The reviewer is told what is happening, not who is hosting it.

    Scanned over string literals only: `renderVerdict` and friends are
    identifiers, not copy.
    """
    assert "The interactive sandbox is starting after inactivity." in APP_JS
    for text in _user_facing_strings(APP_JS):
        low = text.lower()
        for vendor in ("onrender", "free tier", "freetier", "heroku", "railway"):
            assert vendor not in low, vendor + " named in user-facing copy: " + text


# ----------------------------------------------------------------------- CORS
def test_cors_allows_the_approved_origin(monkeypatch, restore_app):
    with TestClient(_app(monkeypatch, STATIC_ORIGIN)) as c:
        res = c.get("/ready", headers={"Origin": STATIC_ORIGIN})
        assert res.status_code == 200
        assert res.headers["access-control-allow-origin"] == STATIC_ORIGIN


def test_cors_rejects_an_unapproved_origin(monkeypatch, restore_app):
    with TestClient(_app(monkeypatch, STATIC_ORIGIN)) as c:
        res = c.get("/ready", headers={"Origin": EVIL_ORIGIN})
        # No grant header means the browser blocks the response.
        assert "access-control-allow-origin" not in res.headers


def test_preflight_is_answered_for_the_approved_origin(monkeypatch, restore_app):
    with TestClient(_app(monkeypatch, STATIC_ORIGIN)) as c:
        res = c.options(
            "/api/demo/run",
            headers={
                "Origin": STATIC_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-fincore-session",
            },
        )
        assert res.status_code in (200, 204)
        assert res.headers["access-control-allow-origin"] == STATIC_ORIGIN
        allowed = res.headers.get("access-control-allow-headers", "").lower()
        assert "x-fincore-session" in allowed


def test_cors_never_uses_a_wildcard_or_credentials(monkeypatch, restore_app):
    main_src = (DEMO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'allow_origins=["*"]' not in main_src
    assert "allow_credentials=False" in main_src

    with TestClient(_app(monkeypatch, STATIC_ORIGIN)) as c:
        res = c.get("/ready", headers={"Origin": STATIC_ORIGIN})
        assert res.headers.get("access-control-allow-origin") != "*"
        assert "access-control-allow-credentials" not in res.headers


def test_no_cors_middleware_when_unconfigured(monkeypatch, restore_app):
    """Local development stays strictly same-origin."""
    with TestClient(_app(monkeypatch, None)) as c:
        res = c.get("/ready", headers={"Origin": EVIL_ORIGIN})
        assert "access-control-allow-origin" not in res.headers


# ------------------------------------------------- cross-origin session id
def test_session_header_is_accepted_and_isolates_runs(monkeypatch, restore_app):
    """The header replaces the cookie cross-origin without changing isolation."""
    application = _app(monkeypatch, STATIC_ORIGIN)
    with TestClient(application) as c:
        a = c.post(
            "/api/demo/run",
            json={"experiment": "worker_crash"},
            headers={"Origin": STATIC_ORIGIN, "X-Fincore-Session": "a1b2c3d4e5f60718"},
        ).json()
        b = c.post(
            "/api/demo/run",
            json={"experiment": "worker_crash"},
            headers={"Origin": STATIC_ORIGIN, "X-Fincore-Session": "0f1e2d3c4b5a6978"},
        ).json()

    assert a["financial_effects"] == b["financial_effects"] == 1
    assert a["operation_id"] != b["operation_id"], "sessions must not share state"


def test_same_session_header_reuses_the_rate_limit_bucket(monkeypatch, restore_app):
    from app.limits import runs

    runs.reset()
    monkeypatch.setattr(runs, "_limit", 2)
    application = _app(monkeypatch, STATIC_ORIGIN)
    headers = {"Origin": STATIC_ORIGIN, "X-Fincore-Session": "aaaabbbbccccdddd"}
    try:
        with TestClient(application) as c:
            codes = [
                c.post(
                    "/api/demo/run",
                    json={"experiment": "intent_conflict"},
                    headers=headers,
                ).status_code
                for _ in range(4)
            ]
        assert 429 in codes, "header session must key the rate limit, got " + str(codes)
    finally:
        runs.reset()


@pytest.mark.parametrize(
    "bad",
    ["x", "ZZZZZZZZZZZZZZZZ", "not-a-session", "a" * 200,
     "'; DROP TABLE operations; --"],
)
def test_malformed_session_header_is_replaced_not_trusted(monkeypatch, restore_app, bad):
    """A bad header must be discarded and a fresh id minted server-side.

    Asserting the raw value is absent from the whole response would be
    meaningless for a value like "x", which occurs in ordinary prose. What
    matters is that it never becomes part of a tenant namespace.
    """
    application = _app(monkeypatch, STATIC_ORIGIN)
    with TestClient(application) as c:
        res = c.post(
            "/api/demo/run",
            json={"experiment": "intent_conflict"},
            headers={"Origin": STATIC_ORIGIN, "X-Fincore-Session": bad},
        )
    assert res.status_code == 200
    assert "lab-" + bad not in res.text, "an invalid session id reached a tenant"
    from app.session import is_valid_session_id

    assert is_valid_session_id(bad) is False


def test_client_session_id_shape_matches_the_server_validator():
    """The browser mints the id, so it must produce what the server accepts."""
    assert "/^[0-9a-f]{16}$/" in APP_JS
    from app.session import is_valid_session_id

    assert is_valid_session_id("a1b2c3d4e5f60718") is True
    assert is_valid_session_id("a1b2c3d4e5f6071") is False
