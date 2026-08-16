"""The backend response contracts are FROZEN.

The frontend reads these fields by name. If one is renamed, retyped, dropped, or
if a counter changes, that must fail here rather than turning into a blank
number on a public page.

To change a contract deliberately:  python scripts/snapshot_contracts.py
"""

from __future__ import annotations

import pytest

from app.experiments import run_experiment
from tests.contract_util import load, normalize, shape

CASES = [
    ("response_loss", "response_loss", {}),
    ("worker_crash", "worker_crash", {}),
    ("concurrency", "concurrency", {"callers": 20}),
    ("intent_conflict", "intent_conflict", {"retry_amount_paise": 20_000}),
    ("intent_conflict_same", "intent_conflict", {"retry_amount_paise": 10_000}),
]


@pytest.mark.parametrize("name,experiment,params", CASES)
async def test_response_matches_frozen_contract(
    session_factory, tenant, name, experiment, params
):
    result = await run_experiment(session_factory, experiment, tenant(), params)
    assert normalize(result) == load(name), (
        f"{name} response changed. If intended, regenerate with "
        f"scripts/snapshot_contracts.py and review the diff."
    )


@pytest.mark.parametrize("name,experiment,params", CASES)
async def test_response_shape_is_stable(session_factory, tenant, name, experiment, params):
    """Types too -- a count arriving as a string would still render, badly."""
    result = await run_experiment(session_factory, experiment, tenant(), params)
    assert shape(result) == shape(load(name))


#: Exactly the fields the UI is allowed to read. Adding to this list is a
#: deliberate act; the frontend must never depend on a field not listed here.
FRONTEND_FIELDS = {
    "response_loss": {
        "naive": {"provider_invocations", "financial_effects", "refund_ids", "events",
                  "verdict", "amount_refunded_paise", "final_state"},
        "fincore": {"provider_invocations", "financial_effects", "refund_ids", "events",
                    "verdict", "amount_refunded_paise", "final_state", "operation_ref",
                    "provider_key_fingerprint", "provider_key_reused", "replayed",
                    "reconciled", "attempt_ids", "intermediate_state"},
    },
    "worker_crash": {
        "_": {"provider_invocations", "financial_effects", "final_state", "events",
              "worker_count", "workers", "replayed", "provider_key_reused",
              "state_after_crash", "crashed_after_provider_effect", "attempt_rows",
              "operation_ref", "provider_key_fingerprint", "refund_ids", "verdict"}
    },
    "concurrency": {
        "_": {"callers", "execution_owners", "turned_away", "provider_invocations",
              "financial_effects", "attempt_rows", "operations", "final_state",
              "events", "operation_ref", "distinct_operation_ids",
              "distinct_provider_keys", "provider_key_fingerprint", "verdict"}
    },
    "intent_conflict": {
        "_": {"conflict", "same_intent", "retry_outcome", "conflict_reason",
              "original_amount_paise", "retry_amount_paise",
              "provider_calls_caused_by_retry", "financial_effects_caused_by_retry",
              "provider_invocations", "financial_effects", "final_state", "events",
              "operation_ref", "amount_refunded_paise", "verdict"}
    },
}


@pytest.mark.parametrize("name", list(FRONTEND_FIELDS))
def test_every_field_the_ui_reads_exists_in_the_contract(name):
    snapshot = load(name)
    for section, fields in FRONTEND_FIELDS[name].items():
        target = snapshot if section == "_" else snapshot[section]
        missing = fields - set(target)
        assert not missing, f"{name}.{section} is missing UI fields: {sorted(missing)}"


def test_concurrency_contract_cannot_describe_callers_as_attempts():
    """The wording correction, enforced at the contract level.

    20 concurrent callers produce ONE execution owner and ONE persisted attempt
    row. A field that reported 20 attempts would be false, so the contract keeps
    `callers` and `attempt_rows` separate and pins both.
    """
    snap = load("concurrency")
    assert snap["callers"] == 20
    assert snap["execution_owners"] == 1
    assert snap["turned_away"] == 19
    assert snap["attempt_rows"] == 1
    assert snap["provider_invocations"] == 1
    assert snap["financial_effects"] == 1
    assert "attempts" not in {k.lower() for k in snap}

    blob = " ".join(
        f"{e['title']} {e['detail']} {e['short'] or ''}" for e in snap["events"]
    ).lower()
    assert "20 attempts" not in blob
    assert "20 execution attempts" not in blob
    assert "20 concurrent callers" in blob


def test_response_loss_contract_supports_the_money_visual():
    """The UI shows ₹200 of effects vs ₹100. That must come from real amounts."""
    snap = load("response_loss")
    assert snap["naive"]["amount_refunded_paise"] == 20_000
    assert snap["fincore"]["amount_refunded_paise"] == 10_000
    assert snap["naive"]["financial_effects"] == 2
    assert snap["fincore"]["financial_effects"] == 1


def test_conflict_contract_supports_the_zero_cost_claim():
    snap = load("intent_conflict")
    assert snap["conflict"] is True
    assert snap["provider_calls_caused_by_retry"] == 0
    assert snap["financial_effects_caused_by_retry"] == 0
    assert snap["original_amount_paise"] == 10_000
    assert snap["retry_amount_paise"] == 20_000

    same = load("intent_conflict_same")
    assert same["conflict"] is False
    assert same["same_intent"] is True
    assert same["financial_effects"] == 1


def test_no_snapshot_leaks_a_secret():
    import re

    for name, _, _ in CASES:
        blob = str(load(name))
        assert "fcop_" not in blob, f"{name} leaked a raw provider key"
        assert not re.search(r"postgresql://", blob)
        assert "password" not in blob.lower()
