"""Normalisation for the frozen backend response contracts.

Snapshots exist so the FRONTEND cannot silently drift from the backend: if a
field the UI reads is renamed, retyped, or dropped, the contract test fails
before anyone notices a blank number on screen.

Values that legitimately change between runs (ids, fingerprints) are replaced
with placeholders. Values that carry the argument (counts, states, verdicts,
event types and order) are compared exactly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CONTRACT_DIR = Path(__file__).resolve().parent / "contracts"

#: Fields whose VALUE is unstable per run but whose presence and type matter.
VOLATILE_KEYS = {
    "run_id",
    "tenant_id",
    "operation_id",
    "attempt_ids",
    "provider_key_fingerprint",
    "provider_key_fingerprints",
}

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_FP = re.compile(r"sha256:[0-9a-f]+")


def _scrub(text: str) -> str:
    return _FP.sub("sha256:<fp>", _UUID.sub("<uuid>", text))


def normalize(value: Any, key: str | None = None) -> Any:
    if key in VOLATILE_KEYS:
        if isinstance(value, list):
            return [f"<{key}>" for _ in value]
        return f"<{key}>"
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return _scrub(value)
    return value


def snapshot_path(name: str) -> Path:
    return CONTRACT_DIR / f"{name}.json"


def load(name: str) -> Any:
    return json.loads(snapshot_path(name).read_text(encoding="utf-8"))


def save(name: str, payload: Any) -> None:
    CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path(name).write_text(
        json.dumps(normalize(payload), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def shape(value: Any) -> Any:
    """Structure and types only -- used for the looser 'fields exist' check."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__
