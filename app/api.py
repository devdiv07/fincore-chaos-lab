"""Public HTTP surface for the Lab.

Everything a browser can reach is here, and every one of these endpoints is
written on the assumption that the caller is hostile:

  * the experiment name is looked up in a registry, never used to import or
    call something derived from the request;
  * every parameter is range-checked and type-checked before it reaches the
    runtime, and rejected with a message that describes the RULE, not the
    internals;
  * the session cookie is validated to an exact shape before it is allowed
    anywhere near a database column;
  * unexpected exceptions become an opaque error plus a run id, so a stack
    trace, a driver message, or a schema name can never reach the page.

There is no endpoint that evaluates input, reads a caller-supplied path, or
takes a URL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from . import config
from .db import get_session_factory
from .experiments import experiment_catalog, run_experiment
from .limits import SafeError, runs
from .scenario import PROVIDER_FIXTURE_NAME, demo_info
from .session import is_valid_session_id, new_run_id, new_session_id, tenant_for

log = logging.getLogger("fincore.lab")
router = APIRouter()

#: Largest JSON body accepted on a run request. The real payloads are tens of
#: bytes; this exists so a large body cannot be used to burn memory.
MAX_BODY_BYTES = 4096


def _session_id(request: Request) -> str:
    """The opaque demo session, from the header first, then the cookie.

    The static shell sends the header because its fetches are cross-site and a
    SameSite=Lax cookie would not travel. The cookie path stays for the
    same-origin case, where the API also serves the UI.

    Either way the value is attacker-supplied and is validated to an exact
    shape before it can reach a `tenant_id` column. Note plainly what this id
    is: a namespace for demo rows and a rate-limit bucket. It is not a
    credential, it authorises nothing, and it was equally client-controlled
    when it lived in a cookie -- so a caller determined to evade the rate limit
    could always rotate it. Real abuse protection would have to live in front
    of the app.
    """
    raw = request.headers.get(config.SESSION_HEADER) or request.cookies.get(
        config.SESSION_COOKIE
    )
    return raw if is_valid_session_id(raw) else new_session_id()


def _error(status: int, message: str, run_id: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if run_id:
        body["run_id"] = run_id
    return JSONResponse(body, status_code=status)


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise SafeError("request body too large", status=413)
    if not raw:
        return {}
    try:
        import json

        parsed = json.loads(raw)
    except ValueError:
        raise SafeError("request body must be JSON") from None
    if not isinstance(parsed, dict):
        raise SafeError("request body must be a JSON object")
    return parsed


def _coerce_params(experiment: str, body: dict[str, Any]) -> dict[str, Any]:
    """Whitelist and type-check the per-experiment controls.

    Unknown keys are dropped rather than forwarded: the runtime's own signature
    should never be reachable from a request body.
    """
    params: dict[str, Any] = {}

    if experiment == "concurrency":
        raw = body.get("callers", 20)
        try:
            callers = int(raw)
        except (TypeError, ValueError):
            raise SafeError("callers must be a whole number") from None
        if callers not in config.CONCURRENCY_CHOICES:
            raise SafeError(f"callers must be one of {sorted(config.CONCURRENCY_CHOICES)}")
        params["callers"] = callers

    elif experiment == "intent_conflict":
        raw = body.get("retry_amount_paise", 20_000)
        if isinstance(raw, bool):
            raise SafeError("retry amount must be a whole number of paise")
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            raise SafeError("retry amount must be a whole number of paise") from None
        if not (config.MIN_RETRY_PAISE <= amount <= config.MAX_RETRY_PAISE):
            raise SafeError(
                f"retry amount must be between {config.MIN_RETRY_PAISE // 100} and "
                f"{config.MAX_RETRY_PAISE // 100} rupees"
            )
        params["retry_amount_paise"] = amount

    return params


# ---------------------------------------------------------------------- routes
@router.get("/api/health")
async def health() -> dict[str, Any]:
    from sqlalchemy import text

    try:
        async with get_session_factory()() as s:
            await s.execute(text("SELECT 1"))
        db_ok, db_error = True, None
    except Exception as exc:  # pragma: no cover - only on a broken database
        log.warning("health check database failure: %s", exc)
        db_ok, db_error = False, "unavailable"

    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"reachable": db_ok, "error": db_error},
        "provider": "deterministic_fixture",
        "external_calls": {"razorpay": 0, "openai": 0},
    }


@router.get("/api/demo/info")
async def info() -> dict[str, Any]:
    payload = demo_info()
    payload["experiments"] = experiment_catalog()
    payload["pinned_flagship_commit"] = config.PINNED_FLAGSHIP_COMMIT
    payload["limits"] = {
        "runs_per_window": config.RATE_LIMIT_RUNS,
        "window_seconds": config.RATE_LIMIT_WINDOW_SECONDS,
    }
    return payload


@router.post("/api/demo/reset")
async def reset(request: Request, response: Response) -> dict[str, Any]:
    """Clear the caller's own view.

    Deliberately does NOT delete anything: every run already executes in its own
    tenant namespace, so "reset" is a fresh namespace for the next run rather
    than a destructive operation that could touch another visitor's rows.
    """
    session_id = _session_id(request)
    response.set_cookie(
        config.SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=86_400,
    )
    return {"status": "reset"}


@router.post("/api/demo/run")
async def run(request: Request) -> Response:
    session_id = _session_id(request)
    run_id = new_run_id()

    allowed, retry_after = runs.check(session_id)
    if not allowed:
        res = _error(429, "Too many runs. Please wait a moment and try again.")
        res.headers["Retry-After"] = str(int(retry_after) + 1)
        return res

    try:
        body = await _json_body(request)
        experiment = body.get("experiment", "response_loss")
        if not isinstance(experiment, str) or experiment not in dict(
            (e["id"], e) for e in experiment_catalog()
        ):
            raise SafeError("unknown experiment")
        params = _coerce_params(experiment, body)
    except SafeError as exc:
        return _error(exc.status, exc.message)

    tenant_id = tenant_for(session_id, run_id)

    try:
        async with asyncio.timeout(config.RUN_TIMEOUT_SECONDS):
            result = await run_experiment(
                get_session_factory(), experiment, tenant_id, params
            )
    except SafeError as exc:
        return _error(exc.status, exc.message, run_id)
    except TimeoutError:
        log.warning("run %s timed out (experiment=%s)", run_id, experiment)
        return _error(504, "The experiment took too long and was stopped.", run_id)
    except Exception:
        # The detail goes to the server log; the browser gets a correlation id.
        log.exception("run %s failed (experiment=%s)", run_id, experiment)
        return _error(500, "The experiment could not be completed.", run_id)

    result["run_id"] = run_id
    result["experiment"] = experiment
    result["demo_provider"] = "deterministic_fixture"
    result["demo_provider_fixture"] = PROVIDER_FIXTURE_NAME

    response = JSONResponse(result)
    response.set_cookie(
        config.SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=86_400,
    )
    return response




@router.get("/health")
async def health_live() -> dict[str, str]:
    """Liveness. Process is up and serving. Deliberately does not touch the
    database: a liveness probe that fails on a database blip would restart a
    perfectly healthy process."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness. The database is reachable and migrated, so an experiment can
    run right now. No configuration, versions, or connection details."""
    from sqlalchemy import text

    try:
        async with get_session_factory()() as s:
            await s.execute(text("SELECT 1 FROM operations LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness check failed: %s", type(exc).__name__)
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})
