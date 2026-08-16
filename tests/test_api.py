"""HTTP surface. Same numbers, delivered over the API the UI actually calls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"]["reachable"] is True
    assert body["external_calls"] == {"razorpay": 0, "openai": 0}


def test_info_declares_the_wiring(client):
    body = client.get("/api/demo/info").json()
    assert body["provider"]["kind"] == "deterministic_fixture"
    assert body["provider"]["fixture"] == "MeasuredRazorpayRefundFake"
    assert body["provider"]["network_calls"] == 0
    assert body["runtime"]["uses_real_core_runtime"] is True
    assert body["runtime"]["entry_point"] == "fincore.engine.OperationRuntime.execute"
    assert body["external_calls"] == {"razorpay": 0, "openai": 0, "any_provider": 0}


def test_reset(client):
    body = client.post("/api/demo/reset").json()
    assert body["status"] == "reset"


def test_run_returns_the_two_lane_result(client):
    body = client.post("/api/demo/run").json()

    assert body["scenario"] == "response_loss_then_retry"
    assert body["demo_provider"] == "deterministic_fixture"

    assert body["naive"]["provider_invocations"] == 2
    assert body["naive"]["financial_effects"] == 2

    assert body["fincore"]["provider_invocations"] == 2
    assert body["fincore"]["financial_effects"] == 1
    assert body["fincore"]["final_state"] == "SUCCEEDED"
    assert body["fincore"]["provider_key_reused"] is True

    for lane in ("naive", "fincore"):
        assert len(body[lane]["events"]) >= 6
        for ev in body[lane]["events"]:
            assert set(ev) >= {"step", "type", "title", "detail", "source"}


def test_run_leaks_no_secrets(client):
    """Key FINGERPRINTS may be shown. Keys themselves may not."""
    raw = client.post("/api/demo/run").text
    assert "fcop_" not in raw, "a raw provider idempotency key escaped into the API"
    body = client.post("/api/demo/run").json()
    assert body["fincore"]["provider_key_fingerprint"].startswith("sha256:")
    for fp in body["naive"]["provider_key_fingerprints"]:
        assert fp.startswith("sha256:")


def test_index_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "FINCORE" in res.text
    assert "Financial Operation Chaos Lab" in res.text
