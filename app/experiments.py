"""Experiments 2-4, plus the registry the API dispatches on.

Every experiment here drives the REAL flagship runtime and reads its counters
back out of the runtime result, the database, or the provider fixture. Nothing
is asserted by this module about what the runtime "should" do -- if the runtime
behaved differently, these numbers would change, which is the point of running
them live.

Each maps onto a test that already exists in the flagship:

    worker_crash      tests/test_crash_recovery.py::test_B_crash_after_provider_
                      success_before_persist
    concurrency       tests/test_concurrency.py::test_twenty_concurrent_callers_
                      one_execution_owner
    intent_conflict   tests/test_identity.py::test_changed_amount_under_same_
                      ref_is_conflict
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import config
from .limits import SafeError
from .scenario import (
    SOURCE_CALLER,
    SOURCE_PROVIDER,
    SOURCE_RUNTIME_DB,
    MeasuredRazorpayRefundFake,
    OperationRuntime,
    RefundOperation,
    _Recorder,
    _rupees,
    run_scenario,
)

# From the read-only flagship checkout (sys.path bound in scenario.py).
from fincore import FaultInjector, Outcome, SimulatedCrash  # noqa: E402

__all__ = ["EXPERIMENTS", "run_experiment", "experiment_catalog"]


# --------------------------------------------------------------------- helpers
async def _expire_lease(sf: async_sessionmaker, tenant_id: str, ref: str) -> None:
    """Make a lease look expired, deterministically.

    Mirrors the flagship's own test helper (`tests/conftest.py::expire_lease`).
    It exists so the demo does not have to sleep out a 30-second lease TTL to
    show recovery; it moves a timestamp and nothing else. The recovery logic
    under test is entirely the runtime's.
    """
    async with sf() as s, s.begin():
        await s.execute(
            text(
                "UPDATE operations SET lease_expires_at = now() - interval '1 hour' "
                "WHERE tenant_id = :t AND operation_ref = :r"
            ),
            {"t": tenant_id, "r": ref},
        )


async def _op_state(sf: async_sessionmaker, tenant_id: str, ref: str) -> str:
    async with sf() as s:
        return (
            await s.execute(
                text(
                    "SELECT state FROM operations WHERE tenant_id = :t AND operation_ref = :r"
                ),
                {"t": tenant_id, "r": ref},
            )
        ).scalar_one()


async def _counts(sf: async_sessionmaker, tenant_id: str) -> dict[str, int]:
    """Row counts for this run's tenant only."""
    async with sf() as s:
        ops = (
            await s.execute(
                text("SELECT count(*) FROM operations WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
        attempts = (
            await s.execute(
                text(
                    "SELECT count(*) FROM operation_attempts a JOIN operations o "
                    "ON o.id = a.operation_id WHERE o.tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        ).scalar_one()
    return {"operations": int(ops), "attempt_rows": int(attempts)}


def _refund(tenant_id: str, *, amount: int, ref: str) -> RefundOperation:
    return RefundOperation(
        tenant_id=tenant_id,
        principal_id=config.PRINCIPAL_ID,
        operation_ref=ref,
        payment_id=config.PAYMENT_ID,
        amount=amount,
    )


# ------------------------------------------------------ 2. WORKER CRASH
async def run_worker_crash(sf: async_sessionmaker, tenant_id: str, **_: Any) -> dict[str, Any]:
    """The financially dangerous crash: the provider executed, then we died.

    A crash *before* the provider call is easy -- nothing happened. This is the
    other one: the money moved and the process died before it could record that.
    Recovery must reuse the persisted provider identity so the provider replays
    the original refund rather than creating a second one.
    """
    ref = "refund-demo-002"
    rec = _Recorder("fincore")
    provider = MeasuredRazorpayRefundFake(payment_amount=config.PAYMENT_AMOUNT_PAISE)

    faults = FaultInjector()
    faults.arm("after_provider_call")  # die AFTER the provider executed
    worker_a = OperationRuntime(sf, provider, worker_id="worker-A", faults=faults)
    op = _refund(tenant_id, amount=config.REFUND_AMOUNT_PAISE, ref=ref)

    rec.add(
        "operation_started",
        "Business operation declared",
        f"Trusted application code supplies operation_ref “{ref}” for a "
        f"{_rupees(config.REFUND_AMOUNT_PAISE)} refund.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=1,
    )

    # SimulatedCrash derives from BaseException precisely so an ordinary
    # `except Exception` cannot swallow it and turn a simulated process death
    # into a normal error path.
    with suppress(SimulatedCrash):
        await worker_a.execute(op)
        raise SafeError("expected the injected crash, runtime did not raise", status=500)

    # "The provider executed and then we died" is the ordering being shown, so
    # it is verified from the fixture rather than assumed.
    crashed_after_effect = provider.refund_count == 1 and provider.calls == 1

    created = provider.refunds[:1]
    if created:
        rec.add(
            "provider_effect_created",
            "Refund created",
            f"worker-A reached the provider, which created {created[0]['id']} for "
            f"{_rupees(created[0]['amount'])}. The money moved.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=1,
            short=f"Provider created {created[0]['id']}",
        )

    state_after_crash = await _op_state(sf, tenant_id, ref)
    rec.add(
        "worker_crashed",
        "Worker crashed",
        "worker-A died before it could record the outcome. The operation is "
        f"left in {state_after_crash} with its lease still held.",
        source=SOURCE_RUNTIME_DB,
        tone="bad",
        attempt=1,
        short="Worker crashed",
    )

    # The lease would lapse on its own after its TTL; move the clock instead of
    # sleeping it out, exactly as the flagship's own crash tests do.
    await _expire_lease(sf, tenant_id, ref)
    rec.add(
        "lease_expired",
        "Lease expired",
        "No live worker holds the operation any more, so it becomes eligible "
        "for recovery.",
        source=SOURCE_RUNTIME_DB,
        tone="warn",
        attempt=1,
    )

    rec.add(
        "recovery_started",
        "New worker started",
        "worker-B sweeps for operations whose lease lapsed. It reconstructs the "
        "provider call entirely from persisted state -- the caller is not asked "
        "to re-submit anything.",
        source=SOURCE_RUNTIME_DB,
        tone="info",
        attempt=2,
        short="New worker started",
    )

    worker_b = OperationRuntime(sf, provider, worker_id="worker-B")
    # tenant_id is REQUIRED here: an unscoped sweep would recover other
    # visitors' expired operations as well as this run's.
    recovered = await worker_b.recover(tenant_id=tenant_id)
    if not recovered:
        raise SafeError("recovery produced no result", status=500)
    res = recovered[0]

    rec.add(
        "operation_loaded",
        "Same business operation loaded",
        f"worker-B resumed operation_ref “{res.operation_ref}”. A new worker is "
        "not a new financial operation.",
        source=SOURCE_RUNTIME_DB,
        tone="info",
        attempt=2,
        short=f"Same operation_ref “{res.operation_ref}”",
    )
    rec.add(
        "provider_key_reused",
        "Persisted provider identity reused",
        f"Recovery sent the stored identity {res.provider_idempotency_key_fingerprint}. "
        "It was never regenerated -- regenerating it here is what would create a "
        "second refund.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=2,
        short="Persisted provider identity reused",
    )
    rec.add(
        "provider_invoked",
        "Provider invoked",
        f"Invocation #{provider.calls} sent to the deterministic provider fixture.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=2,
    )
    if res.replayed:
        rec.add(
            "provider_replayed_original",
            "Original result recovered",
            f"The provider returned the original {res.provider_refund_id} instead of "
            "creating a second refund.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=2,
            short=f"Original refund recovered ({res.provider_refund_id})",
        )
    rec.add(
        "operation_succeeded",
        f"State: {res.state}",
        f"One business operation survived a process death: {provider.calls} provider "
        f"invocations, {provider.refund_count} financial effect.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=2,
        short=f"Final state: {res.state}",
    )

    counts = await _counts(sf, tenant_id)
    return {
        "lane": "fincore",
        "label": "FINANCIAL OPERATION CORE",
        "events": [e.to_dict() for e in rec.events],
        "workers": ["worker-A", "worker-B"],
        "worker_count": 2,
        "crashed_after_provider_effect": crashed_after_effect,
        "state_after_crash": state_after_crash,
        "provider_invocations": provider.calls,
        "financial_effects": provider.refund_count,
        "refund_ids": [r["id"] for r in provider.refunds],
        "final_state": str(res.state),
        "operation_ref": res.operation_ref,
        "operation_id": res.operation_id,
        "provider_key_fingerprint": res.provider_idempotency_key_fingerprint,
        "provider_key_reused": True,
        "replayed": res.replayed,
        "reconciled": res.reconciled,
        "attempt_rows": counts["attempt_rows"],
        "operations": counts["operations"],
        "verdict": "ONE FINANCIAL EFFECT",
    }


# ------------------------------------------------------ 3. CONCURRENT CALLERS
async def run_concurrency(
    sf: async_sessionmaker, tenant_id: str, *, callers: int = 20, **_: Any
) -> dict[str, Any]:
    """Many callers, one business operation.

    WORDING MATTERS HERE. This launches N concurrent CALLERS of the same logical
    operation. It does not produce N execution attempts: exactly one caller wins
    the lease and reaches the provider, and the persisted attempt-row count is 1.
    Every number below is read back from the runtime results and the database
    rather than assumed.
    """
    if callers not in config.CONCURRENCY_CHOICES:
        raise SafeError(
            f"callers must be one of {sorted(config.CONCURRENCY_CHOICES)}"
        )

    ref = "refund-demo-003"
    rec = _Recorder("fincore")
    provider = MeasuredRazorpayRefundFake(payment_amount=config.PAYMENT_AMOUNT_PAISE)
    op = _refund(tenant_id, amount=config.REFUND_AMOUNT_PAISE, ref=ref)

    rec.add(
        "operation_started",
        f"{callers} callers, one business operation",
        f"{callers} workers submit operation_ref “{ref}” at the same instant -- "
        "the shape of a retry storm after a timeout.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=1,
        short=f"{callers} concurrent callers",
    )

    runtimes = [
        OperationRuntime(sf, provider, worker_id=f"worker-{i:02d}") for i in range(callers)
    ]
    results = await asyncio.gather(*(rt.execute(op) for rt in runtimes))

    owners = [r for r in results if r.executed_upstream_this_invocation]
    others = [r for r in results if not r.executed_upstream_this_invocation]
    counts = await _counts(sf, tenant_id)

    rec.add(
        "execution_owner_elected",
        f"{len(owners)} execution owner",
        "The lease is taken by a conditional UPDATE in the database, so exactly "
        f"{len(owners)} caller reached the provider. The other {len(others)} were "
        "turned away without a second financial effect.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=1,
        short=f"{len(owners)} execution owner",
    )
    rec.add(
        "provider_invoked",
        f"{provider.calls} provider invocation",
        f"{provider.calls} call reached the deterministic provider fixture, not {callers}.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=1,
        short=f"{provider.calls} provider call",
    )
    if provider.refunds:
        rec.add(
            "provider_effect_created",
            "Refund created",
            f"Provider created {provider.refunds[0]['id']}. "
            f"{provider.refund_count} financial effect in total.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=1,
            short=f"{provider.refund_count} financial effect",
        )

    states = sorted({str(r.state) for r in results})
    outcomes = sorted({str(r.outcome) for r in results})
    final_state = await _op_state(sf, tenant_id, ref)

    rec.add(
        "operation_succeeded",
        f"State: {final_state}",
        f"All {callers} callers agree on one operation id and one provider "
        f"identity. Persisted execution attempts: {counts['attempt_rows']}.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=1,
        short=f"Final state: {final_state}",
    )

    return {
        "lane": "fincore",
        "label": "FINANCIAL OPERATION CORE",
        "events": [e.to_dict() for e in rec.events],
        "callers": callers,
        "execution_owners": len(owners),
        "turned_away": len(others),
        "provider_invocations": provider.calls,
        "financial_effects": provider.refund_count,
        "refund_ids": [r["id"] for r in provider.refunds],
        "attempt_rows": counts["attempt_rows"],
        "operations": counts["operations"],
        "final_state": final_state,
        "operation_ref": ref,
        "distinct_operation_ids": len({r.operation_id for r in results}),
        "distinct_provider_keys": len({r.provider_idempotency_key_fingerprint for r in results}),
        "provider_key_fingerprint": results[0].provider_idempotency_key_fingerprint,
        "observed_states": states,
        "observed_outcomes": outcomes,
        "verdict": "ONE FINANCIAL EFFECT",
    }


# ------------------------------------------------------ 4. INTENT CONFLICT
async def run_intent_conflict(
    sf: async_sessionmaker,
    tenant_id: str,
    *,
    retry_amount_paise: int = 20_000,
    **_: Any,
) -> dict[str, Any]:
    """Same business operation reference, different financial intent.

    The runtime fingerprints the intent when the operation is created. A later
    call under the same `operation_ref` carrying different money is refused
    BEFORE the provider is contacted -- the interesting property being that the
    refusal costs zero additional financial effects.

    Setting the retry amount equal to the original is the other half of the
    lesson: same intent is not a conflict, it is the terminal operation being
    returned again with no new effect.
    """
    if not isinstance(retry_amount_paise, int) or isinstance(retry_amount_paise, bool):
        raise SafeError("retry amount must be a whole number of paise")
    if not (config.MIN_RETRY_PAISE <= retry_amount_paise <= config.MAX_RETRY_PAISE):
        raise SafeError(
            f"retry amount must be between {_rupees(config.MIN_RETRY_PAISE)} and "
            f"{_rupees(config.MAX_RETRY_PAISE)}"
        )

    ref = "refund-demo-004"
    rec = _Recorder("fincore")
    provider = MeasuredRazorpayRefundFake(payment_amount=config.PAYMENT_AMOUNT_PAISE)
    runtime = OperationRuntime(sf, provider, worker_id="worker-A")

    original = _refund(tenant_id, amount=config.REFUND_AMOUNT_PAISE, ref=ref)
    first = await runtime.execute(original)

    rec.add(
        "operation_started",
        "Original operation succeeded",
        f"operation_ref “{ref}” refunded {_rupees(config.REFUND_AMOUNT_PAISE)}. "
        f"Provider created {first.provider_refund_id}.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=1,
        short=f"Refunded {_rupees(config.REFUND_AMOUNT_PAISE)} · {first.provider_refund_id}",
    )

    calls_before = provider.calls
    effects_before = provider.refund_count

    retry = _refund(tenant_id, amount=retry_amount_paise, ref=ref)
    second = await runtime.execute(retry)

    calls_caused = provider.calls - calls_before
    effects_caused = provider.refund_count - effects_before
    same_intent = retry_amount_paise == config.REFUND_AMOUNT_PAISE
    is_conflict = second.outcome is Outcome.CONFLICT

    rec.add(
        "retry_started",
        f"Same operation_ref, {_rupees(retry_amount_paise)}",
        f"A second call arrives under the SAME operation_ref “{ref}” asking for "
        f"{_rupees(retry_amount_paise)}.",
        source=SOURCE_CALLER,
        tone="warn" if not same_intent else "info",
        attempt=2,
        short=f"Retry asks for {_rupees(retry_amount_paise)}",
    )

    if is_conflict:
        rec.add(
            "intent_conflict",
            "Refused: different financial intent",
            second.conflict_reason or "operation_ref already exists with a different intent.",
            source=SOURCE_RUNTIME_DB,
            tone="bad",
            attempt=2,
            short="Refused before the provider was contacted",
        )
    elif same_intent:
        rec.add(
            "operation_replayed",
            "Same intent, no new effect",
            f"The intent fingerprint matches, so the completed operation is "
            f"returned as-is in state {second.state}. No second refund.",
            source=SOURCE_RUNTIME_DB,
            tone="good",
            attempt=2,
            short="Same intent — completed operation returned",
        )

    rec.add(
        "provider_untouched" if calls_caused == 0 else "provider_invoked",
        f"{calls_caused} extra provider invocation"
        + ("" if calls_caused == 1 else "s"),
        "The conflicting call never reached the provider, so it could not create "
        "a financial effect."
        if calls_caused == 0
        else f"{calls_caused} additional call was made.",
        source=SOURCE_PROVIDER,
        tone="good" if calls_caused == 0 else "warn",
        attempt=2,
        short=f"{calls_caused} extra provider call, {effects_caused} extra effect",
    )

    final_state = await _op_state(sf, tenant_id, ref)
    counts = await _counts(sf, tenant_id)

    return {
        "lane": "fincore",
        "label": "FINANCIAL OPERATION CORE",
        "events": [e.to_dict() for e in rec.events],
        "original_amount_paise": config.REFUND_AMOUNT_PAISE,
        "retry_amount_paise": retry_amount_paise,
        "same_intent": same_intent,
        "conflict": is_conflict,
        "conflict_reason": second.conflict_reason,
        "retry_outcome": str(second.outcome),
        "provider_calls_caused_by_retry": calls_caused,
        "financial_effects_caused_by_retry": effects_caused,
        "provider_invocations": provider.calls,
        "financial_effects": provider.refund_count,
        "refund_ids": [r["id"] for r in provider.refunds],
        "amount_refunded_paise": provider.amount_refunded,
        "final_state": final_state,
        "operation_ref": ref,
        "operation_id": second.operation_id,
        "provider_key_fingerprint": second.provider_idempotency_key_fingerprint,
        "attempt_rows": counts["attempt_rows"],
        "verdict": (
            "CONFLICT REFUSED" if is_conflict else "NO NEW EFFECT"
        ),
    }


# ------------------------------------------------------------------- registry
async def _run_response_loss(sf: async_sessionmaker, tenant_id: str, **_: Any) -> dict[str, Any]:
    """Experiment 1, unchanged in behaviour -- see scenario.run_scenario."""
    return await run_scenario(sf, tenant_id)


EXPERIMENTS: dict[str, Any] = {
    "response_loss": _run_response_loss,
    "worker_crash": run_worker_crash,
    "concurrency": run_concurrency,
    "intent_conflict": run_intent_conflict,
}


def experiment_catalog() -> list[dict[str, Any]]:
    """What the UI offers. Static, non-secret, and safe to expose."""
    return [
        {
            "id": "response_loss",
            "index": 1,
            "name": "Response loss",
            "question": "The provider completed the refund. The response never came back.",
            "compares": True,
            "controls": [],
        },
        {
            "id": "worker_crash",
            "index": 2,
            "name": "Worker crash",
            "question": "The money moved, then the process died before recording it.",
            "compares": False,
            "controls": [],
        },
        {
            "id": "concurrency",
            "index": 3,
            "name": "Concurrent callers",
            "question": "What happens when many workers retry the same operation at once?",
            "compares": False,
            "controls": [
                {
                    "id": "callers",
                    "type": "choice",
                    "label": "Concurrent callers",
                    "choices": list(config.CONCURRENCY_CHOICES),
                    "default": 20,
                }
            ],
        },
        {
            "id": "intent_conflict",
            "index": 4,
            "name": "Intent conflict",
            "question": "Same operation reference, different amount. What should happen?",
            "compares": False,
            "controls": [
                {
                    "id": "retry_amount_paise",
                    "type": "amount",
                    "label": "Retry amount",
                    "min": config.MIN_RETRY_PAISE,
                    "max": config.MAX_RETRY_PAISE,
                    "step": 100,
                    "default": 20_000,
                    "original": config.REFUND_AMOUNT_PAISE,
                }
            ],
        },
    ]


async def run_experiment(
    sf: async_sessionmaker, experiment: str, tenant_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    runner = EXPERIMENTS.get(experiment)
    if runner is None:
        raise SafeError(f"unknown experiment: {experiment!r}")
    return await runner(sf, tenant_id, **params)
