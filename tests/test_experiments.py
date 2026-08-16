"""Experiments 2-4.

Each mirrors a flagship test, and each asserts the property that makes the
experiment worth showing -- not merely that it returned something.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.experiments import experiment_catalog, run_experiment
from app.limits import SafeError


# ------------------------------------------------------------- 2. worker crash
async def test_worker_crash_produces_one_effect(session_factory, tenant):
    r = await run_experiment(session_factory, "worker_crash", tenant(), {})

    assert r["worker_count"] == 2
    assert r["provider_invocations"] == 2
    assert r["financial_effects"] == 1
    assert r["final_state"] == "SUCCEEDED"
    assert r["replayed"] is True
    assert len(r["refund_ids"]) == 1


async def test_worker_crash_happens_after_the_provider_effect(session_factory, tenant):
    """The dangerous ordering, verified -- not the easy one.

    A crash BEFORE the provider call is uninteresting: nothing happened. This
    experiment only means something if the money moved first.
    """
    r = await run_experiment(session_factory, "worker_crash", tenant(), {})

    assert r["crashed_after_provider_effect"] is True
    assert r["state_after_crash"] == "EXECUTING", "the crash must leave work mid-flight"

    types = [e["type"] for e in r["events"]]
    assert types.index("provider_effect_created") < types.index("worker_crashed")


async def test_worker_crash_recovery_reuses_the_persisted_identity(session_factory, tenant):
    r = await run_experiment(session_factory, "worker_crash", tenant(), {})

    assert r["provider_key_reused"] is True
    assert r["provider_key_fingerprint"].startswith("sha256:")
    types = [e["type"] for e in r["events"]]
    assert "provider_key_reused" in types
    assert "provider_replayed_original" in types


async def test_worker_crash_recovery_is_scoped_to_its_own_tenant(session_factory, tenant):
    """A recovery sweep must not pick up another visitor's expired operation.

    `recover()` accepts a tenant filter; if the demo ever stopped passing it,
    one visitor's crash experiment would recover another's work. Two crash runs
    interleaved must still produce one effect each.
    """
    a, b = tenant(), tenant()
    ra = await run_experiment(session_factory, "worker_crash", a, {})
    rb = await run_experiment(session_factory, "worker_crash", b, {})

    assert ra["financial_effects"] == 1
    assert rb["financial_effects"] == 1
    assert ra["operation_id"] != rb["operation_id"]

    async with session_factory() as s:
        for t in (a, b):
            n = (
                await s.execute(
                    text("SELECT count(*) FROM operations WHERE tenant_id = :t"), {"t": t}
                )
            ).scalar_one()
            assert n == 1


# --------------------------------------------------------- 3. concurrent callers
@pytest.mark.parametrize("callers", [2, 5, 10, 20])
async def test_concurrent_callers_elect_one_execution_owner(session_factory, tenant, callers):
    """N concurrent CALLERS is not N attempts.

    Exactly one caller wins the lease and reaches the provider, and the
    persisted attempt-row count is 1 -- describing this as "N attempts" would
    misstate what the runtime did.
    """
    r = await run_experiment(session_factory, "concurrency", tenant(), {"callers": callers})

    assert r["callers"] == callers
    assert r["execution_owners"] == 1
    assert r["turned_away"] == callers - 1
    assert r["provider_invocations"] == 1
    assert r["financial_effects"] == 1
    assert r["attempt_rows"] == 1
    assert r["operations"] == 1
    assert r["final_state"] == "SUCCEEDED"


async def test_concurrent_callers_agree_on_one_identity(session_factory, tenant):
    r = await run_experiment(session_factory, "concurrency", tenant(), {"callers": 20})

    assert r["distinct_operation_ids"] == 1
    assert r["distinct_provider_keys"] == 1


async def test_concurrency_rejects_values_outside_the_allow_list(session_factory, tenant):
    for bad in (1, 3, 21, 1000, -5, 0):
        with pytest.raises(SafeError):
            await run_experiment(session_factory, "concurrency", tenant(), {"callers": bad})


# ------------------------------------------------------------ 4. intent conflict
async def test_intent_conflict_is_refused_before_the_provider(session_factory, tenant):
    r = await run_experiment(
        session_factory, "intent_conflict", tenant(), {"retry_amount_paise": 20_000}
    )

    assert r["conflict"] is True
    assert r["retry_outcome"] == "CONFLICT"
    # THE property: the refusal cost nothing financially.
    assert r["provider_calls_caused_by_retry"] == 0
    assert r["financial_effects_caused_by_retry"] == 0
    assert r["financial_effects"] == 1
    assert r["amount_refunded_paise"] == 10_000


async def test_same_amount_is_not_a_conflict_and_creates_no_new_effect(session_factory, tenant):
    """The other half of the lesson: same intent is a replay, not a conflict."""
    r = await run_experiment(
        session_factory, "intent_conflict", tenant(), {"retry_amount_paise": 10_000}
    )

    assert r["conflict"] is False
    assert r["same_intent"] is True
    assert r["financial_effects"] == 1
    assert r["financial_effects_caused_by_retry"] == 0
    assert r["final_state"] == "SUCCEEDED"


@pytest.mark.parametrize("amount", [0, 99, 100_001, -100, 10**9])
async def test_intent_conflict_rejects_out_of_range_amounts(session_factory, tenant, amount):
    with pytest.raises(SafeError):
        await run_experiment(
            session_factory, "intent_conflict", tenant(), {"retry_amount_paise": amount}
        )


@pytest.mark.parametrize("amount", [True, "200", 1.5, None, [1]])
async def test_intent_conflict_rejects_non_integer_amounts(session_factory, tenant, amount):
    with pytest.raises(SafeError):
        await run_experiment(
            session_factory, "intent_conflict", tenant(), {"retry_amount_paise": amount}
        )


# ------------------------------------------------------------------- registry
async def test_unknown_experiment_is_refused(session_factory, tenant):
    with pytest.raises(SafeError):
        await run_experiment(session_factory, "rm -rf", tenant(), {})


def test_catalog_matches_the_registry():
    from app.experiments import EXPERIMENTS

    catalog_ids = [e["id"] for e in experiment_catalog()]
    assert catalog_ids == ["response_loss", "worker_crash", "concurrency", "intent_conflict"]
    assert set(catalog_ids) == set(EXPERIMENTS)


async def test_every_experiment_reports_backend_derived_counters(session_factory, tenant):
    """No experiment may return a counter the UI would have to invent."""
    for exp, params in [
        ("worker_crash", {}),
        ("concurrency", {"callers": 5}),
        ("intent_conflict", {"retry_amount_paise": 20_000}),
    ]:
        r = await run_experiment(session_factory, exp, tenant(), params)
        assert isinstance(r["provider_invocations"], int)
        assert isinstance(r["financial_effects"], int)
        assert r["final_state"]
        assert r["events"], "every experiment must return its own timeline"
        for ev in r["events"]:
            assert set(ev) >= {"step", "type", "title", "detail", "source"}
            assert ev["source"] in {"runtime_db", "provider_fixture", "caller"}
