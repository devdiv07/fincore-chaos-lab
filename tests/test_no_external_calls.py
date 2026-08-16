"""The demo must be incapable of reaching a real provider or a model API.

These are structural assertions, not documentation. If someone later wires an
SDK in, these fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEMO_ROOT = Path(__file__).resolve().parents[1]
APP = DEMO_ROOT / "app"
SCRIPTS = DEMO_ROOT / "scripts"

#: Files that necessarily CONTAIN the strings they exist to forbid: the
#: scanners themselves, the deployment assertions, and the image audit (which
#: literally runs `python -c "import openai"` inside the image to prove the
#: module is absent).
_SCANNERS = {
    "test_no_external_calls.py",
    "test_claims.py",
    "test_deployment.py",
    "verify_deployment.py",
}

#: Every Python and JS file the demo ships, excluding the venv.
SOURCE_FILES = sorted(
    p
    for p in (
        [*APP.rglob("*.py")]
        + [*APP.rglob("*.js")]
        + [*SCRIPTS.rglob("*.py")]
        + [*(DEMO_ROOT / "tests").rglob("*.py")]
    )
    if p.name not in _SCANNERS
)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_demo_never_names_the_real_provider_adapter():
    from app.scenario import PROVIDER_FIXTURE_NAME

    assert PROVIDER_FIXTURE_NAME == "MeasuredRazorpayRefundFake"

    for p in SOURCE_FILES:
        body = _text(p)
        assert "RazorpayRefundAdapter" not in body, p
        assert "api.razorpay.com" not in body, p


async def test_demo_never_uses_real_provider(session_factory, monkeypatch):
    """Runtime proof, not a grep: a full run constructs no real provider client.

    `fincore.providers.razorpay` IS imported -- importing any submodule of
    `fincore.providers` executes that package's `__init__`, which pulls the
    adapter in. What matters is that nothing ever *instantiates* it, and that no
    HTTP client is opened. Both are booby-trapped here for the duration of a
    real run.
    """
    import httpx
    from fincore.providers import razorpay as razorpay_adapter

    from app.scenario import run_scenario

    def _explode(*_args, **_kwargs):
        raise AssertionError("the demo attempted to construct a real provider client")

    monkeypatch.setattr(razorpay_adapter.RazorpayRefundAdapter, "__init__", _explode)
    monkeypatch.setattr(httpx, "Client", _explode)
    monkeypatch.setattr(httpx, "AsyncClient", _explode)

    result = await run_scenario(session_factory)

    assert result["demo_provider"] == "deterministic_fixture"
    assert result["fincore"]["financial_effects"] == 1


def test_demo_has_no_openai_dependency_or_call_path():
    forbidden = re.compile(
        r"\b(import\s+openai|from\s+openai\b|anthropic|api\.openai\.com|"
        r"OPENAI_API_KEY|RAZORPAY_KEY_SECRET|RAZORPAY_KEY_ID)\b",
        re.IGNORECASE,
    )
    for p in SOURCE_FILES:
        m = forbidden.search(_text(p))
        assert m is None, f"{p}: {m.group(0) if m else ''}"

    requirements = _text(DEMO_ROOT / "requirements.txt").lower()
    for pkg in ("openai", "anthropic", "razorpay"):
        assert not re.search(rf"^{pkg}[><=~\s]", requirements, re.MULTILINE), pkg

    with pytest.raises(ImportError):
        __import__("openai")


def test_demo_makes_no_outbound_http_client():
    """The demo app itself opens no HTTP client. Only the DB driver talks out."""
    for p in [q for q in SOURCE_FILES if q.is_relative_to(APP)]:
        body = _text(p)
        assert "httpx.AsyncClient" not in body, p
        assert "requests.post" not in body, p
        # fetch() in app.js is same-origin only.
        for match in re.findall(r"fetch\(\s*['\"]([^'\"]+)", body):
            assert match.startswith("/api/"), f"{p}: non-local fetch {match}"


def test_frontend_loads_no_remote_assets():
    html = _text(APP / "static" / "index.html")
    for match in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html):
        if match.startswith("http"):
            # Evidence links are anchors the user clicks, never loaded assets.
            assert 'rel="noopener"' in html
            assert match.startswith("https://github.com/"), match
        else:
            # Relative, so the identical file works from the API and the
            # static site without a build step rewriting paths.
            assert not match.startswith("//"), match
            assert match in {"styles.css", "config.js", "app.js"} or match.startswith(
                "data:"
            ), match


def test_no_real_credentials_in_demo_source():
    """Placeholder/local-dev values only."""
    patterns = [
        (r"rzp_live_\w+", "live Razorpay key"),
        (r"rzp_test_[A-Za-z0-9]{10,}", "real-looking Razorpay test key"),
        (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
        (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    ]
    files = SOURCE_FILES + [
        DEMO_ROOT / "README.md",
        DEMO_ROOT / ".env.example",
        DEMO_ROOT / "docker-compose.yml",
        DEMO_ROOT / "start-demo.ps1",
        DEMO_ROOT / "stop-demo.ps1",
    ]
    for p in files:
        if not p.is_file():
            continue
        body = _text(p)
        for pattern, label in patterns:
            assert not re.search(pattern, body), f"{p}: possible {label}"


def test_demo_does_not_copy_flagship_env():
    assert not (DEMO_ROOT / ".env").exists(), "no .env may be committed or copied here"
