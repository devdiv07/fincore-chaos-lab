"""In-process cleanup of expired demo runs.

SINGLE-PROCESS ASSUMPTION, STATED PLAINLY
-----------------------------------------
This janitor runs as a background task inside the one web process, and the
rate limiter in `limits.py` keeps its counters in that same process's memory.
Both are correct for the deployment this demo targets: one replica, one Uvicorn
worker. Run two replicas and you get two janitors (harmless -- the delete is
idempotent and bounded) and per-replica rate limits (weaker than intended).
Scaling out would mean moving the limiter to shared storage and the sweep to a
single scheduled job. That is deliberately out of scope for a hiring demo.

WHY NOT AN HTTP ENDPOINT
------------------------
An earlier revision exposed `POST /api/demo/sweep`. A public, unauthenticated
route whose only job is to delete rows is a liability with no upside, and the
browser has no business triggering maintenance. The function is called directly
here instead, and the public route is gone.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from . import config
from .session import sweep_old_rows

log = logging.getLogger("fincore.lab.janitor")

__all__ = ["run_once", "janitor_loop"]


async def run_once(sf: async_sessionmaker) -> dict[str, object]:
    """One bounded sweep. Never raises."""
    try:
        result = await sweep_old_rows(sf)
        if result["deleted_operations"]:
            # Counts only. No tenant ids, no session ids, no row contents.
            log.info(
                "janitor removed %s expired operations (older than %s minutes)",
                result["deleted_operations"],
                result["older_than_minutes"],
            )
        return result
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not matter
        # A cleanup failure is not a reason to take the demo down. Log the type
        # only; the message could carry database detail.
        log.warning("janitor sweep failed: %s", type(exc).__name__)
        return {"deleted_operations": 0, "error": type(exc).__name__}


async def janitor_loop(sf: async_sessionmaker) -> None:
    """Sweep on an interval until cancelled at shutdown."""
    interval = config.JANITOR_INTERVAL_SECONDS
    log.info("janitor started (every %ss, retention %smin)",
             interval, config.DATA_RETENTION_MINUTES)
    try:
        while True:
            await asyncio.sleep(interval)
            await run_once(sf)
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        log.info("janitor stopped")
        raise
