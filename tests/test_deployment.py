"""Deployment-shape tests.

The container is verified end-to-end by `scripts/verify_deployment.py`, which
builds the image and runs it against a clean database. These are the checks that
do not need Docker: the ones that stop a deployment regression being committed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core_link import pinned_commit, resolved_commit, verify_pinned_commit
from app.main import app

DEMO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (DEMO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (DEMO_ROOT / ".dockerignore").read_text(encoding="utf-8")
RUNTIME_REQS = (DEMO_ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")

PINNED = "3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf"


# --------------------------------------------------------------- pinned core
def test_flagship_commit_is_pinned_everywhere():
    assert config.PINNED_FLAGSHIP_COMMIT == PINNED
    assert pinned_commit() == PINNED
    assert PINNED in DOCKERFILE, "the Dockerfile must pin the same commit"
    # And it must be an exact checkout, never a branch.
    assert "git checkout --quiet \"${FINCORE_COMMIT}\"" in DOCKERFILE
    assert re.search(r"git\s+clone[^\n]*--branch\s+main", DOCKERFILE) is None


def test_build_fails_if_the_pin_is_not_applied():
    """The Dockerfile verifies the checkout, so a silent drift fails the BUILD."""
    assert 'test "$(git rev-parse HEAD)" = "${FINCORE_COMMIT}"' in DOCKERFILE
    assert 'test "$(cat /opt/fincore-core/PINNED_COMMIT)" = "${FINCORE_COMMIT}"' in DOCKERFILE


def test_runtime_can_report_which_revision_it_is_running():
    info = verify_pinned_commit(strict=False)
    assert info["expected"] == PINNED
    assert set(info) == {"expected", "actual", "matches"}
    # Locally this reads .git/HEAD; in the image it reads the PINNED_COMMIT stamp.
    assert resolved_commit() is None or re.fullmatch(r"[0-9a-f]{40}", resolved_commit())


def test_strict_mode_refuses_to_start_on_a_mismatch(monkeypatch):
    monkeypatch.setattr(config, "PINNED_FLAGSHIP_COMMIT", "0" * 40)
    with pytest.raises(RuntimeError, match="revision mismatch"):
        verify_pinned_commit(strict=True)


def test_no_local_sibling_path_is_required():
    """Nothing may hardcode the developer's sibling checkout.

    The image sets FINCORE_FLAGSHIP_PATH; the code must honour it rather than
    assuming `../financial-operation-core-public` exists.
    """
    assert "FINCORE_FLAGSHIP_PATH=/opt/fincore-core" in DOCKERFILE

    core_link = (DEMO_ROOT / "app" / "core_link.py").read_text(encoding="utf-8")
    assert 'os.environ.get(\n        "FINCORE_FLAGSHIP_PATH"' in core_link

    for py in (DEMO_ROOT / "app").rglob("*.py"):
        body = py.read_text(encoding="utf-8")
        assert "C:\\Users" not in body, f"{py} hardcodes a local path"
        assert "/Users/ASUS" not in body, f"{py} hardcodes a local path"


# ------------------------------------------------------------------ Dockerfile
def test_dockerfile_runtime_hygiene():
    assert "FROM python:3.12-slim-bookworm AS runtime" in DOCKERFILE
    assert "USER lab" in DOCKERFILE, "must not run as root"
    assert "useradd" in DOCKERFILE
    assert "--workers 1" in DOCKERFILE, "one worker for a single-process demo"
    assert "--reload" not in DOCKERFILE
    assert "--host 0.0.0.0" in DOCKERFILE
    assert "${PORT}" in DOCKERFILE, "PORT comes from the host"
    # git and the build toolchain stay in the builder stage.
    assert DOCKERFILE.count("apt-get install") == 1
    assert "rm -rf /build/core/.git" in DOCKERFILE


def test_no_secrets_or_env_files_in_the_image():
    assert not re.search(r"^COPY\s+\.env", DOCKERFILE, re.M)
    assert "ENV DATABASE_URL" not in DOCKERFILE
    for secret in ("rzp_", "sk-", "ghp_", "PASSWORD=", "SECRET"):
        assert secret not in DOCKERFILE, f"{secret} appears in the Dockerfile"


def test_only_application_code_is_copied_into_the_image():
    copies = re.findall(r"^COPY\s+(?:--\S+\s+)*(\S+)\s", DOCKERFILE, re.M)
    from_build = {c for c in copies if c.startswith("/")}
    from_context = set(copies) - from_build
    assert from_context <= {"app/", "scripts/smoke_demo.py", "requirements-runtime.txt"}, (
        f"unexpected paths copied into the image: {from_context}"
    )


@pytest.mark.parametrize(
    "entry",
    [".venv/", "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
     "qa/", "tests/", ".git/", ".env", "*.log", "playwright-report/", ".coverage"],
)
def test_dockerignore_excludes_local_state(entry):
    assert entry in DOCKERIGNORE


# ------------------------------------------------------- runtime dependencies
@pytest.mark.parametrize("pkg", ["playwright", "pytest", "pytest-asyncio", "openai",
                                 "anthropic", "razorpay"])
def test_runtime_requirements_exclude_test_and_model_packages(pkg):
    assert not re.search(rf"^{re.escape(pkg)}\b", RUNTIME_REQS, re.M | re.I)


def test_runtime_requirements_cover_what_the_app_imports():
    for pkg in ("fastapi", "uvicorn", "sqlalchemy", "psycopg", "alembic",
                "pydantic", "cryptography"):
        assert re.search(rf"^{pkg}\b", RUNTIME_REQS, re.M), f"{pkg} missing"


# ----------------------------------------------------------------- cloud config
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgresql+psycopg://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ],
)
def test_railway_style_database_urls_are_accepted(raw, expected):
    assert config.normalize_database_url(raw) == expected


def test_port_and_host_follow_the_platform(monkeypatch):
    """PORT is read from the environment; 0.0.0.0 only when containerised."""
    import importlib

    monkeypatch.setenv("PORT", "9123")
    monkeypatch.delenv("FINCORE_DEMO_PORT", raising=False)
    monkeypatch.delenv("FINCORE_DEMO_HOST", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.PORT == 9123
        assert reloaded.HOST == "0.0.0.0"
    finally:
        monkeypatch.delenv("PORT", raising=False)
        importlib.reload(config)


def test_local_mode_still_defaults_to_loopback():
    import importlib

    os.environ.pop("PORT", None)
    reloaded = importlib.reload(config)
    assert reloaded.HOST == "127.0.0.1"
    assert reloaded.PORT == 8000


def test_no_credentials_are_baked_into_defaults():
    """The local dev default is a local container; nothing else may be present."""
    text = (DEMO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "127.0.0.1:55433" in text, "local default should be the local container"
    for secret in ("rzp_live", "rzp_test", "sk-", "ghp_"):
        assert secret not in text


# ---------------------------------------------------------- health / readiness
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_is_tiny_and_reveals_nothing(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body == {"status": "ok"}, "liveness must not expose configuration"


def test_ready_reports_database_readiness(client):
    res = client.get("/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ready"}


def test_health_endpoints_leak_no_environment(client):
    for path in ("/health", "/ready", "/api/health"):
        body = client.get(path).text
        for leak in ("postgresql://", "password", "DATABASE_URL", "/opt/", "fincore:fincore"):
            assert leak not in body, f"{leak!r} leaked from {path}"


# ------------------------------------------------------------------- janitor
async def test_janitor_run_once_never_raises(session_factory):
    from app.janitor import run_once

    result = await run_once(session_factory)
    assert "deleted_operations" in result


async def test_janitor_tolerates_a_failing_sweep(monkeypatch, session_factory):
    """One cleanup failure must not take the web server down."""
    import app.janitor as janitor

    async def boom(_sf):
        raise RuntimeError("database went away")

    monkeypatch.setattr(janitor, "sweep_old_rows", boom)
    result = await janitor.run_once(session_factory)
    assert result["deleted_operations"] == 0
    assert result["error"] == "RuntimeError"


async def test_janitor_only_deletes_expired_runs(session_factory, tenant):
    from sqlalchemy import text

    from app.experiments import run_experiment
    from app.janitor import run_once

    fresh = tenant()
    await run_experiment(session_factory, "worker_crash", fresh, {})
    await run_once(session_factory)

    async with session_factory() as s:
        alive = (
            await s.execute(
                text("SELECT count(*) FROM operations WHERE tenant_id = :t"), {"t": fresh}
            )
        ).scalar_one()
    assert alive == 1, "a fresh run must survive the janitor"


def test_janitor_logs_no_sensitive_identifiers():
    body = (DEMO_ROOT / "app" / "janitor.py").read_text(encoding="utf-8")
    assert "tenant_id" not in body.split('"""')[-1], "tenant ids must not be logged"
    assert "single-process" in body.lower(), "the scaling assumption must be documented"


def test_cleanup_is_not_reachable_from_the_browser(client):
    assert client.post("/api/demo/sweep").status_code in (404, 405)


# ============================================================ Neon / managed DB
# Regression tests for a real Render + Neon startup failure: the migration URL
# was built by string concatenation, which corrupts a provider URL that already
# carries query parameters.

NEON = (
    "postgresql+psycopg://neondb_owner:npg_secret@"
    "ep-cool-darkness-a1b2c3.ap-southeast-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def test_migration_url_has_exactly_one_query_string():
    from app.db import build_migration_url

    url = build_migration_url(NEON, "demo")
    assert url.count("?") == 1, f"malformed URL: {url}"


def test_migration_url_preserves_every_provider_parameter():
    """sslmode and channel_binding must survive. Dropping either breaks Neon."""
    from sqlalchemy.engine import make_url

    from app.db import build_migration_url

    query = dict(make_url(build_migration_url(NEON, "demo")).query)
    assert query["sslmode"] == "require"
    assert query["channel_binding"] == "require"
    assert query["options"] == "-csearch_path=demo"


def test_the_concatenation_bug_cannot_come_back():
    """The exact corruption that produced OperationalError on Render.

    Concatenating swallowed the second `?` into the preceding value, giving
    channel_binding="require?options=-csearch_path=demo", which libpq rejects.
    """
    from sqlalchemy.engine import make_url

    from app.db import build_migration_url

    query = dict(make_url(build_migration_url(NEON, "demo")).query)
    for key, value in query.items():
        assert "?" not in str(value), f"{key} absorbed a query separator: {value!r}"


def test_migration_url_keeps_a_password_with_reserved_characters():
    from sqlalchemy.engine import make_url

    from app.db import build_migration_url

    tricky = "postgresql+psycopg://user:p%40ss%2Fword%3A1@host/db?sslmode=require"
    parsed = make_url(build_migration_url(tricky, "demo"))
    assert parsed.password == "p@ss/word:1"
    assert dict(parsed.query)["sslmode"] == "require"


def test_migration_url_works_without_any_query_string():
    """The local container URL carries no parameters; it must still work."""
    from sqlalchemy.engine import make_url

    from app.db import build_migration_url

    url = build_migration_url(
        "postgresql+psycopg://fincore:fincore@127.0.0.1:55433/fincore_demo", "demo"
    )
    assert url.count("?") == 1
    assert dict(make_url(url).query) == {"options": "-csearch_path=demo"}


def test_admin_dsn_is_libpq_shaped_and_keeps_parameters():
    from sqlalchemy.engine import make_url

    from app.db import build_admin_dsn

    dsn = build_admin_dsn(NEON)
    assert dsn.startswith("postgresql://"), "psycopg needs a driverless scheme"
    assert "+psycopg" not in dsn
    query = dict(make_url(dsn).query)
    assert query["sslmode"] == "require"
    assert query["channel_binding"] == "require"


def test_schema_name_is_not_attacker_controlled():
    """DEMO_SCHEMA reaches CREATE SCHEMA, so it must come from config only."""
    body = (DEMO_ROOT / "app" / "db.py").read_text(encoding="utf-8")
    assert 'CREATE SCHEMA IF NOT EXISTS "{DEMO_SCHEMA}"' in body.replace("f'", "'").replace(
        "f\"", "\""
    )
    assert re.match(r"^[a-z_][a-z0-9_]*$", config.DEMO_SCHEMA), config.DEMO_SCHEMA


def test_migration_failure_message_carries_no_credentials():
    """The message is logged by the platform; it must not leak the URL."""
    body = (DEMO_ROOT / "app" / "db.py").read_text(encoding="utf-8")
    failure = body[body.index("flagship alembic migration failed") :][:400]
    for leak in ("sync_database_url()", "url", "dsn", "password", "DATABASE_URL"):
        assert f"{{{leak}}}" not in failure, f"{leak} interpolated into the error"


def test_no_manual_query_string_assembly_anywhere():
    """The banned pattern, in every form it tends to reappear."""
    for py in (DEMO_ROOT / "app").rglob("*.py"):
        body = py.read_text(encoding="utf-8")
        stripped = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        assert '"&" if "?" in' not in stripped, py
        assert "'&' if '?' in" not in stripped, py
        assert '+ f"?options=' not in stripped, py
        assert "+ \"?options=" not in stripped, py


def test_neon_pooler_guidance_is_documented():
    """Neon pooled endpoints carry '-pooler' in the hostname and are not the
    right target for DDL. The guidance must be written down, not folklore."""
    deploy = (DEMO_ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "-pooler" in deploy
    assert "direct" in deploy.lower()
    # ...and no provider hostname may be hardcoded in executable code.
    # Docstrings may cite one as an example -- that is documentation, and the
    # explanation of the concatenation bug depends on showing a real URL shape.
    import ast

    for py in (DEMO_ROOT / "app").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert "neon.tech" not in node.value, f"{py}: hardcoded host"
