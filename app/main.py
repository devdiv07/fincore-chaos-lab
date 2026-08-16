"""Financial Operation Core Lab -- FastAPI application.

The API executes each experiment as fast as the database allows and returns
structured results. It never sleeps for effect: all pacing lives in the
frontend, so the numbers on screen are the numbers the backend produced.

The public HTTP surface lives in `api.py`. This module is composition only:
lifespan, middleware, and static hosting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config
from .api import router
from .db import dispose, get_session_factory, migrate
from .janitor import janitor_loop

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("fincore.lab")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Everything the first reviewer request must NOT have to wait for.

    Migrations and provenance checks happen here, before the app accepts
    traffic, so a cold start never pays for schema work. Any failure propagates:
    a Lab serving experiments against an unmigrated database, or against a core
    of unknown provenance, would be worse than one that refuses to start.
    """
    from . import config
    from .core_link import verify_pinned_commit

    # Deployed builds pin the core; a local sibling checkout may legitimately
    # sit on another commit, so strictness follows the deployment flag.
    strict = os.environ.get("FINCORE_STRICT_PIN", "1" if config.IS_CLOUD else "0") == "1"
    provenance = verify_pinned_commit(strict=strict)
    log.info(
        "core revision %s (expected %s)%s",
        provenance["actual"] or "unknown",
        provenance["expected"],
        "" if provenance["matches"] else "  MISMATCH",
    )

    # Idempotent: creates the demo schema if absent, then brings it to head
    # using the flagship's migrations. Raises loudly on failure.
    await asyncio.to_thread(migrate)
    log.info("database migrated to head")

    task: asyncio.Task | None = None
    if config.JANITOR_ENABLED:
        task = asyncio.create_task(janitor_loop(get_session_factory()))

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await dispose()


app = FastAPI(
    title="Financial Operation Core Lab",
    description="Deterministic. No provider or model API is contacted.",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def _headers(request: Request, call_next: Any) -> Any:
    """No-store, plus the baseline headers a public page should carry.

    No-store because this demo's UI gets edited between recording takes, and a
    cached bundle is a nasty failure mode: the page looks like it ignored a fix
    and the mistake only surfaces in the recording.

    The CSP is deliberately strict and can afford to be -- the page loads no
    remote asset of any kind, so nothing legitimate is blocked by forbidding
    everything off-origin.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return response


# --------------------------------------------------------------------- CORS
# The static shell is a different origin from this API, so browsers require an
# explicit grant. The allowlist is exact -- never "*" -- and credentials are
# NOT enabled: the session id travels in a header, so no cookie needs to cross
# origins, and a wildcard-plus-credentials combination cannot arise.
#
# With no FINCORE_ALLOWED_ORIGIN set (local development) no CORS middleware is
# installed at all, leaving the API strictly same-origin.
if config.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", config.SESSION_HEADER],
        max_age=600,
    )
    log.info("CORS enabled for %d origin(s)", len(config.ALLOWED_ORIGINS))

app.include_router(router)


# ------------------------------------------------------------------- frontend
# Mounted at the ROOT, and last, so the API routes above win. Serving the shell
# here with the same relative asset paths the static site uses means both
# surfaces serve byte-identical files -- there is no second copy of the
# frontend to drift out of sync with this one.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
