"""Import path binding to the read-only Financial Operation Core checkout.

WHY NOT `pip install -e ../financial-operation-core-public`
----------------------------------------------------------
Two reasons, both concrete:

1. The naive baseline this demo compares against lives in
   `experiments/agent_execution/baselines.py`, which the flagship deliberately
   keeps OUT of the `fincore` package ("proof-harness code, not product"). An
   editable install would expose `fincore` and nothing else, so the naive lane
   would have to be re-implemented here -- i.e. invented business logic, which
   this demo is not allowed to do.

2. An editable install runs the flagship's build backend against its source
   tree. This module instead only prepends two directories to `sys.path` and
   disables bytecode writing, so the flagship checkout is never written to at
   all -- not even a `__pycache__` entry.

The demo constructs only `MeasuredRazorpayRefundFake`. The real HTTP adapter is
never instantiated and no HTTP client is ever opened;
`tests/test_no_external_calls.py` proves that at runtime rather than by grep.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["FLAGSHIP_ROOT", "bind_core", "flagship_paths"]

#: Sibling read-only checkout. Override for CI or a relocated checkout.
FLAGSHIP_ROOT = Path(
    os.environ.get(
        "FINCORE_FLAGSHIP_PATH",
        str(Path(__file__).resolve().parents[2] / "financial-operation-core-public"),
    )
).resolve()

_SRC = FLAGSHIP_ROOT / "src"
_EXPERIMENTS = FLAGSHIP_ROOT / "experiments"


def flagship_paths() -> tuple[Path, Path, Path]:
    return FLAGSHIP_ROOT, _SRC, _EXPERIMENTS


def pinned_commit() -> str:
    """The flagship revision this build is pinned to."""
    from . import config

    return config.PINNED_FLAGSHIP_COMMIT


def resolved_commit() -> str | None:
    """The revision actually present, read without invoking git.

    The deployed image contains a checkout produced by `git clone` + `git
    checkout <sha>`, but the runtime image has no git binary and runs as a
    non-root user, so the revision is read from a provenance file written at
    build time, falling back to `.git/HEAD` for the local sibling checkout.
    """
    stamp = FLAGSHIP_ROOT / "PINNED_COMMIT"
    if stamp.is_file():
        return stamp.read_text(encoding="utf-8").strip() or None

    head = FLAGSHIP_ROOT / ".git" / "HEAD"
    if not head.is_file():
        return None
    ref = head.read_text(encoding="utf-8").strip()
    if ref.startswith("ref: "):
        target = FLAGSHIP_ROOT / ".git" / ref[5:]
        if target.is_file():
            return target.read_text(encoding="utf-8").strip()
        packed = FLAGSHIP_ROOT / ".git" / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref[5:]):
                    return line.split(" ", 1)[0]
        return None
    return ref


def verify_pinned_commit(*, strict: bool = False) -> dict[str, object]:
    """Report whether the checkout on disk is the revision we intended.

    Deployed builds set `strict` so a mismatched or unidentifiable core fails
    startup rather than quietly serving results from unknown code. Locally it
    is advisory: a developer may legitimately have the sibling checkout on a
    different commit.
    """
    expected = pinned_commit()
    actual = resolved_commit()
    matches = actual == expected
    result: dict[str, object] = {
        "expected": expected,
        "actual": actual,
        "matches": matches,
    }
    if strict and not matches:
        raise RuntimeError(
            "Financial Operation Core revision mismatch: expected "
            f"{expected}, found {actual or 'unknown'}. Refusing to start."
        )
    return result


def bind_core() -> Path:
    """Make `fincore` and the flagship's experiment baselines importable.

    Returns the flagship root. Raises if the checkout is missing or does not
    look like the expected project -- a silent fallback to some other `fincore`
    on the path would undermine the whole point of the demo.
    """
    missing = [str(p) for p in (FLAGSHIP_ROOT, _SRC, _EXPERIMENTS) if not p.is_dir()]
    if missing:
        raise RuntimeError(
            "Financial Operation Core checkout not found. Expected a sibling "
            f"directory at {FLAGSHIP_ROOT}. Missing: {', '.join(missing)}. "
            "Set FINCORE_FLAGSHIP_PATH to point at it."
        )
    if not (_SRC / "fincore" / "engine.py").is_file():
        raise RuntimeError(f"{_SRC / 'fincore'} does not look like the fincore package")

    # The flagship must stay byte-identical, including no new __pycache__ dirs.
    sys.dont_write_bytecode = True

    for path in (_SRC, _EXPERIMENTS):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)

    return FLAGSHIP_ROOT
