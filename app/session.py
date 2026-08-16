"""Visitor isolation.

THE PROBLEM WITH THE SINGLE-USER DESIGN
---------------------------------------
The original demo reset by truncating the operation tables before every run.
On a public URL that is a correctness bug, not just impoliteness: one visitor
pressing Run would delete the rows another visitor's experiment was in the
middle of using.

THE FIX
-------
Isolation by identity instead of by deletion. Every run executes under its own
`tenant_id`, and `operations` carries UNIQUE (tenant_id, operation_ref) with
every engine query filtered on tenant. Two visitors -- and two runs by the same
visitor -- therefore cannot collide by construction, and nothing has to be
deleted for a run to start clean.

    tenant_id = "lab-<session>-<run>"

The session half is a per-browser opaque id, so a visitor's runs can be swept
together. The run half keeps `operation_ref` stable and human-readable
("refund-demo-001") while still giving every run a fresh namespace.

No IP address, and nothing personal, is stored or derived here.
"""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import config

__all__ = [
    "new_session_id",
    "new_run_id",
    "tenant_for",
    "is_valid_session_id",
    "sweep_old_rows",
]


def new_session_id() -> str:
    """Opaque per-browser id. Not derived from anything about the visitor."""
    return secrets.token_hex(8)


def new_run_id() -> str:
    return secrets.token_hex(6)


def is_valid_session_id(value: str | None) -> bool:
    """Cookies are attacker-controlled; treat one as an untrusted string.

    The value reaches a `tenant_id` column, so it is validated to an exact
    shape rather than merely escaped.
    """
    if not value or len(value) != 16:
        return False
    return all(c in "0123456789abcdef" for c in value)


def tenant_for(session_id: str, run_id: str) -> str:
    """Namespace one run. Fixed-width hex on both halves, so it cannot exceed
    the column's 128 characters and cannot contain anything but [a-f0-9-]."""
    return f"lab-{session_id}-{run_id}"


async def sweep_old_rows(sf: async_sessionmaker) -> dict[str, Any]:
    """Bounded cleanup of finished experiments.

    Deletes by AGE only, never by "everything except mine", so a run that is
    currently executing is never touched -- runs complete in well under a
    second and the retention window is measured in tens of minutes. The LIMIT
    keeps a sweep cheap and interruptible; leftovers go on the next pass.

    `operation_attempts` and `operation_events` carry ON DELETE CASCADE, so
    removing the operation row removes its children.
    """
    minutes = config.DATA_RETENTION_MINUTES
    async with sf() as s, s.begin():
        deleted = (
            await s.execute(
                text(
                    """
                    DELETE FROM operations
                     WHERE id IN (
                           SELECT id FROM operations
                            WHERE tenant_id LIKE 'lab-%'
                              AND created_at < now() - make_interval(mins => :m)
                            LIMIT 500
                     )
                 RETURNING id
                    """
                ),
                {"m": minutes},
            )
        ).rowcount
    return {"deleted_operations": int(deleted or 0), "older_than_minutes": minutes}
