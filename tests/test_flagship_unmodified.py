"""The flagship checkout is a read-only dependency of this demo.

`git status --porcelain` in the flagship must stay empty while the demo runs.
This catches the two realistic accidents: an editable install writing build
artifacts, and a stray `__pycache__` from running the flagship's Alembic scripts.
"""

from __future__ import annotations

import subprocess

import pytest

from app.core_link import FLAGSHIP_ROOT, flagship_paths


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(FLAGSHIP_ROOT), *args],
        capture_output=True,
        text=True,
    )


def test_flagship_checkout_exists_and_is_a_repo():
    root, src, experiments = flagship_paths()
    assert root.is_dir()
    assert (src / "fincore" / "engine.py").is_file()
    assert (experiments / "agent_execution" / "baselines.py").is_file()
    assert (root / ".git").exists()


def test_flagship_working_tree_is_clean():
    proc = _git("status", "--porcelain")
    if proc.returncode != 0:  # pragma: no cover - git missing
        pytest.skip(f"git unavailable: {proc.stderr.strip()}")
    assert proc.stdout.strip() == "", (
        "the flagship checkout has uncommitted changes; this demo must never "
        f"write to it:\n{proc.stdout}"
    )


def test_flagship_tests_are_unchanged():
    """Specifically: the tests this demo visualizes still say what they said."""
    proc = _git("diff", "--stat", "HEAD", "--", "tests", "src", "experiments")
    if proc.returncode != 0:  # pragma: no cover - git missing
        pytest.skip("git unavailable")
    assert proc.stdout.strip() == "", proc.stdout


def test_demo_uses_the_real_runtime_not_a_copy():
    """`fincore` must resolve INTO the flagship checkout, not to a vendored copy."""
    import fincore
    import fincore.engine

    _, src, _ = flagship_paths()
    assert fincore.engine.__file__ is not None
    engine_path = fincore.engine.__file__
    assert str(src) in engine_path, engine_path

    # And it is the genuine entry point, not a shim.
    assert hasattr(fincore.OperationRuntime, "execute")
    assert hasattr(fincore.OperationRuntime, "recover")


def test_naive_baseline_is_the_flagship_reference_tool():
    from agent_execution.baselines import NaiveRefundTool

    _, _, experiments = flagship_paths()
    module = __import__("agent_execution.baselines", fromlist=["NaiveRefundTool"])
    assert str(experiments) in str(module.__file__)
    # The flagship labels it as a baseline; the demo does not get to relabel it.
    assert NaiveRefundTool.UNSAFE_BASELINE is True
