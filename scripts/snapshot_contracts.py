"""Regenerate the frozen backend response snapshots.

    python scripts/snapshot_contracts.py

Run this ONLY when a contract change is intended. The snapshots are what stop
the frontend and backend drifting apart silently.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db import dispose, get_session_factory, migrate  # noqa: E402
from app.experiments import run_experiment  # noqa: E402
from app.session import new_run_id, new_session_id, tenant_for  # noqa: E402
from tests.contract_util import save  # noqa: E402

CASES = [
    ("response_loss", {}),
    ("worker_crash", {}),
    ("concurrency", {"callers": 20}),
    ("intent_conflict", {"retry_amount_paise": 20_000}),
    ("intent_conflict_same", {"retry_amount_paise": 10_000}),
]


async def _main() -> int:
    migrate()
    sf = get_session_factory()
    session = new_session_id()
    try:
        for name, params in CASES:
            experiment = "intent_conflict" if name == "intent_conflict_same" else name
            result = await run_experiment(
                sf, experiment, tenant_for(session, new_run_id()), params
            )
            save(name, result)
            print(f"wrote contracts/{name}.json")
    finally:
        await dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
