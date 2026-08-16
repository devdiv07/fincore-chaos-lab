from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

DEMO_ROOT = Path(__file__).resolve().parents[1]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

# psycopg's async driver cannot run on Windows' default ProactorEventLoop.
# Must be set before pytest-asyncio creates any loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db import get_session_factory, migrate, truncate_demo_tables  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> None:
    """Bring the demo schema to head using the FLAGSHIP migrations, once."""
    migrate()


@pytest_asyncio.fixture
async def session_factory():
    await truncate_demo_tables()
    yield get_session_factory()


@pytest.fixture
def tenant():
    """A fresh per-run tenant, exactly as the API mints one per request.

    Isolation is by identity, not by deletion: two runs never share a namespace,
    so neither has to delete the other's rows to start clean.
    """
    from app.session import new_run_id, new_session_id, tenant_for

    def _make() -> str:
        return tenant_for(new_session_id(), new_run_id())

    return _make


@pytest_asyncio.fixture
async def scenario_result(session_factory, tenant):
    """One full two-lane run, shared by the assertions below."""
    from app.scenario import run_scenario

    return await run_scenario(session_factory, tenant())
