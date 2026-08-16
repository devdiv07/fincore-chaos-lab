"""What the demo asserts on screen must be what the backend actually produced.

These mirror `tests/test_unknown.py::test_unknown_is_reconciled_with_the_same_provider_identity`
in the flagship, which is the load-bearing test this demo visualizes.
"""

from __future__ import annotations

import pytest


async def test_naive_response_loss_creates_two_effects(scenario_result):
    naive = scenario_result["naive"]
    assert naive["provider_invocations"] == 2
    assert naive["financial_effects"] == 2
    assert len(naive["refund_ids"]) == 2
    assert len(set(naive["refund_ids"])) == 2, "two DISTINCT refunds, not one replayed"


async def test_fincore_response_loss_creates_one_effect(scenario_result):
    fc = scenario_result["fincore"]
    assert fc["provider_invocations"] == 2
    assert fc["financial_effects"] == 1
    assert fc["final_state"] == "SUCCEEDED"
    assert len(fc["refund_ids"]) == 1


async def test_fincore_reuses_provider_identity(scenario_result):
    fc = scenario_result["fincore"]
    assert fc["provider_key_reused"] is True
    assert fc["replayed"] is True, "the provider must replay the original, not create"
    assert fc["reconciled"] is True
    assert any(e["type"] == "provider_key_reused" for e in fc["events"])


async def test_naive_lane_regenerates_provider_identity(scenario_result):
    """The baseline must be weak for the stated reason, not some other one."""
    naive = scenario_result["naive"]
    fps = naive["provider_key_fingerprints"]
    assert len(fps) == 2
    assert fps[0] != fps[1]
    assert naive["provider_key_reused"] is False


@pytest.mark.parametrize("lane", ["naive", "fincore"])
async def test_response_loss_occurs_after_provider_effect(scenario_result, lane):
    """Ordering is the whole point: effect first, THEN the response disappears.

    The alternative failure mode -- the network dying before the provider ever
    saw the request -- is a different problem and is not what is being shown.
    """
    data = scenario_result[lane]
    assert data["response_loss_after_effect"] is True

    types = [e["type"] for e in data["events"]]
    assert "provider_effect_created" in types
    assert "response_lost" in types
    assert types.index("provider_effect_created") < types.index("response_lost"), (
        "the timeline must show the financial effect BEFORE the response loss"
    )


async def test_fincore_passes_through_unknown_before_succeeding(scenario_result):
    fc = scenario_result["fincore"]
    assert fc["intermediate_state"] == "UNKNOWN"
    types = [e["type"] for e in fc["events"]]
    assert types.index("operation_unknown") < types.index("operation_succeeded")
    # And the runtime's own durable audit trail agrees.
    assert "provider.outcome.unknown" in fc["runtime_event_types"]
    assert "reconciliation.resolved" in fc["runtime_event_types"]


async def test_fincore_uses_one_business_operation_across_two_attempts(scenario_result):
    fc = scenario_result["fincore"]
    assert fc["operation_ref"] == "refund-demo-001"
    assert len(fc["attempt_ids"]) == 2
    assert len(set(fc["attempt_ids"])) == 2, "two distinct attempts"
    assert [a["attempt_type"] for a in fc["attempt_summary"]] == [
        "execution",
        "reconciliation",
    ]
    assert [a["observed_outcome"] for a in fc["attempt_summary"]] == [
        "UNKNOWN",
        "SUCCEEDED",
    ]


async def test_events_declare_their_source(scenario_result):
    """No event may imply it is a runtime audit record when it is not."""
    allowed = {"runtime_db", "provider_fixture", "caller"}
    for lane in ("naive", "fincore"):
        for ev in scenario_result[lane]["events"]:
            assert ev["source"] in allowed, ev

    # The naive lane has no durable runtime state, so it must claim none.
    assert all(e["source"] != "runtime_db" for e in scenario_result["naive"]["events"])
    # The fincore lane's key facts come from the database, not from the harness.
    fc_by_type = {e["type"]: e for e in scenario_result["fincore"]["events"]}
    assert fc_by_type["provider_key_reused"]["source"] == "runtime_db"
    assert fc_by_type["operation_succeeded"]["source"] == "runtime_db"


async def test_both_lanes_start_from_equivalent_intent(scenario_result):
    """Same money, same payment, same failure injection. Only retry differs."""
    assert scenario_result["amount_paise"] == 10_000
    naive_total = scenario_result["naive"]["amount_refunded_paise"]
    fincore_total = scenario_result["fincore"]["amount_refunded_paise"]
    assert fincore_total == 10_000
    assert naive_total == 20_000, "the duplicate path really did move twice the money"


async def test_recap_lines_are_backend_derived(scenario_result):
    """The end-of-run recap is composed from backend `short` fields.

    The browser must not have to parse prose out of `detail` to build the final
    frame, because that frame is the one that gets recorded.
    """
    naive_short = [e["short"] for e in scenario_result["naive"]["events"] if e["short"]]
    fincore_short = [e["short"] for e in scenario_result["fincore"]["events"] if e["short"]]

    assert naive_short == [
        "Provider created rfnd_FAKE001",
        "Response lost",
        "New provider key",
        "Provider created rfnd_FAKE002",
    ]
    assert fincore_short == [
        "Provider created rfnd_FAKE001",
        "Response lost",
        "Runtime state: UNKNOWN",
        "Same operation_ref “refund-demo-001”",
        "Persisted provider key reused",
        "Original refund reconciled (rfnd_FAKE001)",
    ]

    # Each recap line must be attributable to an attempt, so the recap can group
    # them the way the timeline does.
    for lane in ("naive", "fincore"):
        for ev in scenario_result[lane]["events"]:
            if ev["short"]:
                assert ev["attempt"] in (1, 2), ev


async def test_reset_produces_clean_scenario(session_factory, tenant):
    """Two consecutive runs are identical; state does not leak between them.

    Each run gets its own tenant namespace, which is what makes the second run
    start clean -- no truncation, and therefore nothing that could disturb a
    concurrent visitor.
    """
    from sqlalchemy import text

    from app.scenario import run_scenario

    tenant_a, tenant_b = tenant(), tenant()
    first = await run_scenario(session_factory, tenant_a)
    second = await run_scenario(session_factory, tenant_b)

    for lane in ("naive", "fincore"):
        assert (
            first[lane]["provider_invocations"] == second[lane]["provider_invocations"] == 2
        )
        assert first[lane]["financial_effects"] == second[lane]["financial_effects"]
        assert [e["type"] for e in first[lane]["events"]] == [
            e["type"] for e in second[lane]["events"]
        ]

    assert second["fincore"]["final_state"] == "SUCCEEDED"

    # Each run owns exactly one business operation, in its own namespace.
    async with session_factory() as s:
        for t in (tenant_a, tenant_b):
            n = (
                await s.execute(
                    text("SELECT count(*) FROM operations WHERE tenant_id = :t"), {"t": t}
                )
            ).scalar_one()
            assert n == 1, f"tenant {t} should own exactly one operation"
