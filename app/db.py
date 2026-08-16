"""Demo database wiring.

The schema is NOT defined here. It is produced by running the flagship's own
Alembic migrations (`alembic/versions/0001,0002`) into a dedicated schema, so
the demo exercises the same tables and constraints the flagship's tests do.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .config import DATABASE_URL, DEMO_SCHEMA, sync_database_url
from .core_link import FLAGSHIP_ROOT

__all__ = ["get_engine", "get_session_factory", "migrate", "truncate_demo_tables", "dispose"]

DEMO_ROOT = Path(__file__).resolve().parents[1]

#: Tables owned by the flagship schema. Truncated between demo runs so every
#: recording starts from the same clean state.
_TABLES = (
    "operation_events",
    "operation_attempts",
    "authorization_attempts",
    "execution_grants",
    "operations",
)

# psycopg's async driver cannot run on Windows' default ProactorEventLoop.
# Set before any loop is created -- app/server.py does this too, for the case
# where uvicorn builds the loop before importing this module.
if sys.platform == "win32":  # pragma: no cover - platform specific
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            connect_args={"options": f"-csearch_path={DEMO_SCHEMA}"},
            pool_pre_ping=True,
            # The concurrency experiment launches up to 20 callers that each
            # hold a connection while contending for the same row. A default
            # pool of 5 + 10 overflow would stall instead of demonstrating the
            # lease, so the pool is sized above the largest offered choice.
            pool_size=30,
            max_overflow=20,
            pool_timeout=20,
        )
    return _engine


def get_session_factory() -> async_sessionmaker:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def build_migration_url(url: str, schema: str) -> str:
    """Add the migration search_path WITHOUT destroying the provider's own
    query parameters.

    This function exists because of a real deployment failure. The previous
    implementation appended `"?options=-csearch_path=demo"` by string
    concatenation. Against a local URL with no query string that happens to
    work; against a managed provider it does not. Neon hands out:

        postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require&channel_binding=require

    Concatenating produced a SECOND `?`, and the failure was quiet rather than
    obvious: the URL still parses, but the trailing text is absorbed into the
    preceding value, yielding

        channel_binding = "require?options=-csearch_path=demo"

    which libpq rejects — surfacing as `OperationalError` from Alembic with no
    hint about the cause.

    So the URL is parsed, the key is set structurally, and every parameter the
    provider supplied (`sslmode`, `channel_binding`, anything else) is carried
    through untouched. The password is preserved and URL-encoded correctly,
    which also fixes credentials containing reserved characters.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    with_schema = parsed.update_query_dict(
        {"options": f"-csearch_path={schema}"}, append=False
    )
    return with_schema.render_as_string(hide_password=False)


def build_admin_dsn(url: str) -> str:
    """A libpq DSN for the one statement Alembic cannot run for us.

    `CREATE SCHEMA` has to happen before the migration, on a connection that
    does not yet reference that schema. Only the driver name is dropped;
    query parameters and credentials survive intact.
    """
    from sqlalchemy.engine import make_url

    return (
        make_url(url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def migrate() -> None:
    """Create the demo schema and bring it to head with FLAGSHIP migrations.

    The migration scripts are located by ABSOLUTE path into whichever flagship
    checkout `core_link` resolved -- the sibling directory locally, the pinned
    clone inside the image in a container. The previous implementation shelled
    out to a demo-local `alembic.ini` carrying a *relative* `script_location`,
    which cannot survive the move into a container.

    Failure raises. It is never swallowed: a demo serving traffic against an
    unmigrated database would produce confusing errors instead of an obvious
    refusal to start.
    """
    import psycopg
    from alembic import command
    from alembic.config import Config

    with psycopg.connect(build_admin_dsn(sync_database_url()), autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{DEMO_SCHEMA}"')

    script_location = FLAGSHIP_ROOT / "alembic"
    if not (script_location / "env.py").is_file():
        raise RuntimeError(f"flagship migrations not found at {script_location}")

    # env.py reads this; it is the only channel it accepts.
    previous = os.environ.get("FINCORE_DATABASE_URL")
    # Structured, not concatenated. See build_migration_url.
    os.environ["FINCORE_DATABASE_URL"] = build_migration_url(
        sync_database_url(), DEMO_SCHEMA
    )
    sys.dont_write_bytecode = True
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(script_location))
        command.upgrade(cfg, "head")
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        # Deliberately no URL, host, or credential in this message: it is
        # logged by the platform and may be surfaced in build output.
        raise RuntimeError(
            f"flagship alembic migration failed ({type(exc).__name__}). "
            f"script_location={script_location}"
        ) from exc
    finally:
        if previous is None:
            os.environ.pop("FINCORE_DATABASE_URL", None)
        else:
            os.environ["FINCORE_DATABASE_URL"] = previous


async def truncate_demo_tables() -> None:
    """Reset demo state. Touches only the demo schema's own tables."""
    sf = get_session_factory()
    async with sf() as s, s.begin():
        qualified = ", ".join(f'"{DEMO_SCHEMA}"."{t}"' for t in _TABLES)
        await s.execute(text(f"TRUNCATE {qualified} RESTART IDENTITY CASCADE"))
