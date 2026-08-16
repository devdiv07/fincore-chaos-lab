"""The two-lane response-loss experiment.

Both lanes run against the SAME deterministic provider fixture class
(`MeasuredRazorpayRefundFake`) and the SAME failure injection: the provider
completes the refund, and only then does the response become unavailable to the
caller. Nothing here fabricates a number -- every counter in the API response is
read back off the fixture or the database after execution.

    LANE A  naive retry     -- flagship `NaiveRefundTool`, mints a fresh
                               provider idempotency key per attempt
    LANE B  fincore         -- flagship `OperationRuntime.execute()`, twice,
                               with the same durable `operation_ref`

WHAT THE PROVIDER FIXTURE IS
----------------------------
`MeasuredRazorpayRefundFake` is the flagship's deterministic test double. Its
branches reproduce only behaviour measured against Razorpay Test Mode on
2026-08-10 (the B0 experiments). It is NOT Razorpay, it performs no network I/O,
and results produced with it are never provider measurements.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import config
from .core_link import bind_core

bind_core()

# Imported AFTER bind_core(); these come from the read-only flagship checkout.
# The provider import is narrow by intent, but be precise about what that buys:
# importing `fincore.providers.fake` still executes `fincore.providers.__init__`,
# which imports the real adapter module. What the demo guarantees is that the
# real adapter is never INSTANTIATED and no HTTP client is ever opened --
# tests/test_no_external_calls.py proves that by booby-trapping both during a
# full run.
from agent_execution.baselines import NaiveRefundTool  # noqa: E402
from fincore import OperationRuntime, RefundOperation  # noqa: E402
from fincore.providers.fake import MeasuredRazorpayRefundFake  # noqa: E402

__all__ = ["run_scenario", "demo_info", "PROVIDER_FIXTURE_NAME"]

PROVIDER_FIXTURE_NAME = MeasuredRazorpayRefundFake.__name__

#: Where each event came from. Shown so the demo never implies that a line in
#: the timeline is a runtime audit record when it is really an observation of
#: the fixture.
SOURCE_RUNTIME_DB = "runtime_db"  # a row in operation_events, written by fincore
SOURCE_PROVIDER = "provider_fixture"  # observed state change on the fixture
SOURCE_CALLER = "caller"  # something the demo harness itself did


@dataclass
class Event:
    step: int
    type: str
    title: str
    detail: str
    lane: str
    source: str
    tone: str = "neutral"  # neutral | good | warn | bad | info
    attempt: int | None = None
    #: Condensed line for the end-of-run recap. Set only on the events that
    #: carry the argument; `None` means "not part of the recap". Written here
    #: rather than parsed out of `detail` in the browser, so the recap is
    #: backend-derived like everything else on screen.
    short: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Recorder:
    lane: str
    events: list[Event] = field(default_factory=list)

    def add(
        self,
        type: str,
        title: str,
        detail: str,
        *,
        source: str,
        tone: str = "neutral",
        attempt: int | None = None,
        short: str | None = None,
    ) -> None:
        self.events.append(
            Event(
                step=len(self.events) + 1,
                type=type,
                title=title,
                detail=detail,
                lane=self.lane,
                source=source,
                tone=tone,
                attempt=attempt,
                short=short,
            )
        )


def _key_fp(key: str) -> str:
    """Same shape the runtime uses in results: a fingerprint, never the key."""
    import hashlib

    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------- lane A
async def _run_naive() -> dict[str, Any]:
    """Deliberately weak retry strategy: a new provider identity per attempt.

    This is the flagship's own `NaiveRefundTool`, which it labels
    UNSAFE_BASELINE. It is included as a reference point, not as a claim about
    how real backends are written -- see the fairness note in the UI.
    """
    rec = _Recorder("naive")
    provider = MeasuredRazorpayRefundFake(payment_amount=config.PAYMENT_AMOUNT_PAISE)
    tool = NaiveRefundTool(provider)
    args = {"payment_id": config.PAYMENT_ID, "amount": config.REFUND_AMOUNT_PAISE}

    # The provider will complete the FIRST refund and then lose the response on
    # the way back. Ordering is a property of the fixture: it appends the refund
    # before raising the transport error.
    provider.drop_next_responses = 1

    rec.add(
        "operation_started",
        "Refund requested",
        f"Caller asks for a {_rupees(config.REFUND_AMOUNT_PAISE)} refund on "
        f"{config.PAYMENT_ID}. No durable business-operation identity is involved.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=1,
    )

    # ---- attempt 1 ----
    before_calls, before_refunds = provider.calls, provider.refund_count
    result_1, is_error_1 = await tool.call("naive_refund", args)
    key_1 = tool.keys_used[0]

    rec.add(
        "provider_key_created",
        "Fresh retry identity minted",
        f"Attempt #1 generated provider key {_key_fp(key_1)}.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=1,
    )
    rec.add(
        "provider_invoked",
        "Provider invoked",
        f"Invocation #{provider.calls} sent to the deterministic provider fixture.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=1,
    )
    created_1 = provider.refunds[before_refunds:]
    if created_1:
        rec.add(
            "provider_effect_created",
            "Refund created",
            f"Provider created {created_1[0]['id']} for "
            f"{_rupees(created_1[0]['amount'])}. The money moved.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=1,
            short=f"Provider created {created_1[0]['id']}",
        )
    lost_1 = is_error_1 and provider.refund_count > before_refunds
    if lost_1:
        rec.add(
            "response_lost",
            "Response lost",
            "The connection closed before the response came back. The caller "
            f"saw: {result_1.get('state')}.",
            source=SOURCE_CALLER,
            tone="bad",
            attempt=1,
            short="Response lost",
        )

    # ---- retry ----
    rec.add(
        "retry_started",
        "Caller retries",
        "The caller treats the lost response as a failed refund and issues a "
        "brand-new request.",
        source=SOURCE_CALLER,
        tone="warn",
        attempt=2,
    )
    before_refunds_2 = provider.refund_count
    result_2, _ = await tool.call("naive_refund", args)
    key_2 = tool.keys_used[1]

    rec.add(
        "provider_key_created",
        "New provider key on retry",
        f"Attempt #2 minted {_key_fp(key_2)} — a different identity, so the "
        "provider is correct to treat this as a new operation.",
        source=SOURCE_CALLER,
        tone="warn",
        attempt=2,
        short="New provider key",
    )
    rec.add(
        "provider_invoked",
        "Provider invoked",
        f"Invocation #{provider.calls} sent to the deterministic provider fixture.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=2,
    )
    created_2 = provider.refunds[before_refunds_2:]
    if created_2:
        rec.add(
            "duplicate_effect_created",
            "Second refund created",
            f"Provider created {created_2[0]['id']}. Two refunds now exist for "
            "one business intent.",
            source=SOURCE_PROVIDER,
            tone="bad",
            attempt=2,
            short=f"Provider created {created_2[0]['id']}",
        )

    return {
        "lane": "naive",
        "label": "NAIVE RETRY",
        "events": [e.to_dict() for e in rec.events],
        "provider_invocations": provider.calls,
        "financial_effects": provider.refund_count,
        "refund_ids": [r["id"] for r in provider.refunds],
        "amount_refunded_paise": provider.amount_refunded,
        "provider_key_fingerprints": [_key_fp(k) for k in tool.keys_used],
        "provider_key_reused": len(set(tool.keys_used)) == 1,
        "response_loss_after_effect": bool(lost_1),
        "caller_saw": [result_1.get("state"), result_2.get("state")],
        "final_state": result_2.get("state"),
        "verdict": (
            "POSSIBLE DUPLICATE EFFECT"
            if provider.refund_count > 1
            else "SINGLE EFFECT"
        ),
    }


# ---------------------------------------------------------------------- lane B
async def _db_events(sf: async_sessionmaker, operation_id: str) -> list[dict[str, Any]]:
    async with sf() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT sequence, event_type, payload FROM operation_events "
                    "WHERE operation_id = :i ORDER BY sequence"
                ),
                {"i": operation_id},
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _db_attempts(sf: async_sessionmaker, operation_id: str) -> list[dict[str, Any]]:
    async with sf() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, attempt_number, attempt_type, observed_outcome "
                    "FROM operation_attempts WHERE operation_id = :i "
                    "ORDER BY attempt_number"
                ),
                {"i": operation_id},
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _run_fincore(sf: async_sessionmaker, tenant_id: str) -> dict[str, Any]:
    """The real runtime, executed twice on one durable business operation."""
    rec = _Recorder("fincore")
    provider = MeasuredRazorpayRefundFake(payment_amount=config.PAYMENT_AMOUNT_PAISE)
    runtime = OperationRuntime(sf, provider, worker_id=config.WORKER_ID, lease_ttl_seconds=30)

    op = RefundOperation(
        tenant_id=tenant_id,
        principal_id=config.PRINCIPAL_ID,
        operation_ref=config.OPERATION_REF,
        payment_id=config.PAYMENT_ID,
        amount=config.REFUND_AMOUNT_PAISE,
    )

    provider.drop_next_responses = 1

    rec.add(
        "operation_started",
        "Business operation declared",
        f"Trusted application code supplies operation_ref “{config.OPERATION_REF}” "
        f"for a {_rupees(config.REFUND_AMOUNT_PAISE)} refund. The agent never invents this.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=1,
    )

    # ---- attempt 1: provider executes, response is lost ----
    before_refunds = provider.refund_count
    first = await runtime.execute(op)
    created_1 = provider.refunds[before_refunds:]

    rec.add(
        "attempt_started",
        "Attempt #1 claimed",
        f"Runtime took the execution lease as attempt #{first.attempt_number}. "
        "A new attempt is not a new business operation.",
        source=SOURCE_RUNTIME_DB,
        tone="info",
        attempt=1,
    )
    rec.add(
        "provider_key_created",
        "Provider identity persisted",
        f"Provider idempotency identity {first.provider_idempotency_key_fingerprint} "
        "was written once, with the operation row. The runtime owns it, not the caller.",
        source=SOURCE_RUNTIME_DB,
        tone="info",
        attempt=1,
    )
    rec.add(
        "provider_invoked",
        "Provider invoked",
        f"Invocation #{provider.calls} sent to the deterministic provider fixture.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=1,
    )
    if created_1:
        rec.add(
            "provider_effect_created",
            "Refund created",
            f"Provider created {created_1[0]['id']} for "
            f"{_rupees(created_1[0]['amount'])}. The money moved.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=1,
            short=f"Provider created {created_1[0]['id']}",
        )

    events_1 = await _db_events(sf, first.operation_id)
    types_1 = [e["event_type"] for e in events_1]
    response_loss_after_effect = bool(created_1) and "provider.outcome.unknown" in types_1

    if response_loss_after_effect:
        rec.add(
            "response_lost",
            "Response lost",
            "The connection closed before the response was received. The runtime "
            "cannot see what the provider did.",
            source=SOURCE_PROVIDER,
            tone="bad",
            attempt=1,
            short="Response lost",
        )
        rec.add(
            "operation_unknown",
            "State: UNKNOWN",
            "Not failed. Not succeeded. The runtime records "
            f"{first.state} — a first-class outcome — and refuses to guess.",
            source=SOURCE_RUNTIME_DB,
            tone="warn",
            attempt=1,
            short=f"Runtime state: {first.state}",
        )

    # ---- attempt 2: same business operation, reconciled ----
    rec.add(
        "retry_started",
        "Same business operation re-executed",
        f"The caller retries operation_ref “{config.OPERATION_REF}”. Same "
        "operation, new attempt.",
        source=SOURCE_CALLER,
        tone="info",
        attempt=2,
        short=f"Same operation_ref “{config.OPERATION_REF}”",
    )

    before_refunds_2 = provider.refund_count
    second = await runtime.execute(op)
    created_2 = provider.refunds[before_refunds_2:]

    key_reused = (
        second.provider_idempotency_key_fingerprint
        == first.provider_idempotency_key_fingerprint
    )
    if key_reused:
        rec.add(
            "provider_key_reused",
            "Persisted provider key reused",
            f"Attempt #{second.attempt_number} sent the SAME identity "
            f"{second.provider_idempotency_key_fingerprint}. It was never regenerated.",
            source=SOURCE_RUNTIME_DB,
            tone="good",
            attempt=2,
            short="Persisted provider key reused",
        )
    rec.add(
        "provider_invoked",
        "Provider invoked",
        f"Invocation #{provider.calls} sent to the deterministic provider fixture.",
        source=SOURCE_PROVIDER,
        tone="neutral",
        attempt=2,
    )
    if second.replayed and not created_2:
        rec.add(
            "provider_replayed_original",
            "Original refund replayed",
            f"Provider returned the original {second.provider_refund_id} instead of "
            "creating a second one. No new financial effect.",
            source=SOURCE_PROVIDER,
            tone="good",
            attempt=2,
        )

    events_2 = await _db_events(sf, second.operation_id)
    types_2 = [e["event_type"] for e in events_2]
    if second.reconciled and "reconciliation.resolved" in types_2:
        rec.add(
            "operation_reconciled",
            "Outcome reconciled",
            "The uncertain attempt was resolved against the provider's own record "
            f"of the original refund ({second.provider_refund_id}).",
            source=SOURCE_RUNTIME_DB,
            tone="good",
            attempt=2,
            short=f"Original refund reconciled ({second.provider_refund_id})",
        )
    rec.add(
        "operation_succeeded",
        f"State: {second.state}",
        f"One business operation, {provider.calls} provider invocations, "
        f"{provider.refund_count} financial effect.",
        source=SOURCE_RUNTIME_DB,
        tone="good",
        attempt=2,
    )

    attempts = await _db_attempts(sf, second.operation_id)

    return {
        "lane": "fincore",
        "label": "FINANCIAL OPERATION CORE",
        "events": [e.to_dict() for e in rec.events],
        "provider_invocations": provider.calls,
        "financial_effects": provider.refund_count,
        "refund_ids": [r["id"] for r in provider.refunds],
        "amount_refunded_paise": provider.amount_refunded,
        "final_state": str(second.state),
        "operation_ref": second.operation_ref,
        "operation_id": second.operation_id,
        "tenant_id": second.tenant_id,
        "attempt_ids": [str(a["id"]) for a in attempts],
        "attempt_summary": [
            {
                "attempt_number": a["attempt_number"],
                "attempt_type": a["attempt_type"],
                "observed_outcome": a["observed_outcome"],
            }
            for a in attempts
        ],
        "provider_key_fingerprint": second.provider_idempotency_key_fingerprint,
        "provider_key_reused": key_reused,
        "replayed": second.replayed,
        "reconciled": second.reconciled,
        "response_loss_after_effect": response_loss_after_effect,
        "runtime_event_types": types_2,
        "intermediate_state": str(first.state),
        "verdict": "ONE FINANCIAL EFFECT",
    }


# ------------------------------------------------------------------- public API
async def run_scenario(sf: async_sessionmaker, tenant_id: str | None = None) -> dict[str, Any]:
    """Execute both lanes and return everything the UI renders.

    Each run executes under its OWN `tenant_id`, which is what makes it start
    clean. The earlier design truncated the operation tables first; on a public
    URL that would delete rows belonging to another visitor's in-flight
    experiment. Isolation by identity costs nothing and cannot do that.

    Backend execution is not delayed anywhere -- the frontend does its own
    presentation pacing.
    """
    tenant_id = tenant_id or config.TENANT_ID

    naive = await _run_naive()
    fincore = await _run_fincore(sf, tenant_id)

    return {
        "scenario": "response_loss_then_retry",
        "tenant_id": tenant_id,
        "run_id": uuid.uuid4().hex[:12],
        "demo_provider": "deterministic_fixture",
        "demo_provider_fixture": PROVIDER_FIXTURE_NAME,
        "amount_paise": config.REFUND_AMOUNT_PAISE,
        "amount_display": _rupees(config.REFUND_AMOUNT_PAISE),
        "payment_id": config.PAYMENT_ID,
        "naive": naive,
        "fincore": fincore,
    }


def demo_info() -> dict[str, Any]:
    """Static, non-secret description of how this demo is wired."""
    from .core_link import FLAGSHIP_ROOT

    return {
        "demo": "Financial Operation Core Lab",
        "scenario": "response_loss_then_retry",
        "provider": {
            "kind": "deterministic_fixture",
            "fixture": PROVIDER_FIXTURE_NAME,
            "description": (
                "Deterministic provider fixture modeled from measured Razorpay "
                "Test Mode behavior."
            ),
            "network_calls": 0,
        },
        "runtime": {
            "uses_real_core_runtime": True,
            "entry_point": "fincore.engine.OperationRuntime.execute",
            "integration": "sys.path binding to a read-only sibling checkout",
            "flagship_path": str(FLAGSHIP_ROOT),
            "naive_baseline": (
                "agent_execution.baselines.NaiveRefundTool "
                "(the flagship's own UNSAFE_BASELINE reference tool)"
            ),
        },
        "database": {
            "engine": "postgresql",
            "schema": config.DEMO_SCHEMA,
            "migrations": "flagship alembic/versions (0001, 0002)",
            "scope": "local demo database only",
        },
        "external_calls": {"razorpay": 0, "openai": 0, "any_provider": 0},
        "operation_ref": config.OPERATION_REF,
    }
