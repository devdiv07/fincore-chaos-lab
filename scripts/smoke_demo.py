"""Headless smoke test.

Runs the same scenario the browser runs and prints the counters. Prints no
identifiers, no keys, and no internal objects.

    python scripts/smoke_demo.py
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
from app.scenario import run_scenario  # noqa: E402

EXPECTED = {
    "naive": {"provider_invocations": 2, "financial_effects": 2},
    "fincore": {"provider_invocations": 2, "financial_effects": 1, "final_state": "SUCCEEDED"},
}


async def _main() -> int:
    migrate()
    try:
        result = await run_scenario(get_session_factory())
    finally:
        await dispose()

    naive, fincore = result["naive"], result["fincore"]

    print("=== RESPONSE LOSS DEMO ===")
    print()
    print("NAIVE")
    print(f"provider invocations: {naive['provider_invocations']}")
    print(f"financial effects: {naive['financial_effects']}")
    print()
    print("FINANCIAL OPERATION CORE")
    print(f"provider invocations: {fincore['provider_invocations']}")
    print(f"financial effects: {fincore['financial_effects']}")
    print(f"final state: {fincore['final_state']}")
    print()

    ok = (
        all(naive[k] == v for k, v in EXPECTED["naive"].items())
        and all(fincore[k] == v for k, v in EXPECTED["fincore"].items())
        and fincore["provider_key_reused"] is True
        and naive["response_loss_after_effect"] is True
        and fincore["response_loss_after_effect"] is True
    )
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
