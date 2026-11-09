from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import repricer_bff as repricer_bff_module
from app.repricer_sprint_b import _buyer_price_for_accounting
from tests.auth_helpers import auth_headers


@pytest.fixture(autouse=True)
def fake_user_wb_token(monkeypatch):
    monkeypatch.setenv("VELLA_WB_API_MODE", "fake")
    monkeypatch.setattr("app.routers.wb_repricer_sprint_b.get_user_wb_token_secret", lambda _user_id: "synthetic-sprint-b-token")


def client() -> TestClient:
    return TestClient(create_app())


def _valid_draft_payload() -> dict:
    return {
        "scenario": "complete",
        "articleId": "FBBT_42",
        "nmId": 123456,
        "candidateSellerPriceKopecks": 129500,
        "minPriceKopecks": 100000,
        "pMaxKopecks": 180000,
        "economics": {
            "cogsKopecks": 59000,
            "commissionPct": 20,
            "logisticsKopecks": 5000,
            "storageKopecks": 1000,
            "taxKopecks": 2000,
            "buyoutPct": 85,
            "stockUnits": 12,
            "promoActive": False,
        },
        "reason": "manual correction",
    }


def test_recommendation_includes_formula_version_and_source_snapshot():
    api = client()
    response = api.post(
        "/api/v1/wb-repricer/recommendation",
        headers=auth_headers(api, "price_sender"),
        json={
            "scenario": "complete",
            "articleId": "FBBT_42",
            "nmId": 123456,
            "candidateSellerPriceKopecks": 123000,
            "minPriceKopecks": 100000,
            "economics": {
                "cogsKopecks": 60000,
                "commissionPct": 20,
                "logisticsKopecks": 5000,
                "storageKopecks": 1000,
                "taxKopecks": 2000,
                "buyoutPct": 80,
                "stockUnits": 10,
                "promoActive": False,
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["formulaVersion"] == "sprint-b-repricer-v1"
    assert payload["guardFormulaVersion"] == "sprint-b-guards-v1"
    assert payload["sourceSnapshot"]["snapshotVersion"] == "wb23-runtime-v1"
    assert payload["normalized"]["currentSppPct"] is not None


def test_candidate_buyer_price_can_include_configured_wallet():
    previous = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE.update({"sppAccountingMode": "spp_plus_wallet", "wbWalletType": 4})
    try:
        assert _buyer_price_for_accounting(100_000, 26) == 71_040
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous)


def test_draft_blocks_when_required_inputs_are_missing():
    api = client()
    payload = _valid_draft_payload()
    payload["economics"]["cogsKopecks"] = None

    response = api.post("/api/v1/wb-repricer/drafts", json=payload, headers=auth_headers(api, "price_sender"))
    assert response.status_code == 200
    draft = response.json()["data"]
    assert draft["state"] == "blocked"
    assert draft["guardReport"]["canApply"] is False
    assert "WB-23" in draft["guardReport"]["blockers"]


def test_draft_approval_and_apply_flow_reaches_accepted_with_feature_flag_enabled():
    previous = os.environ.get("VELLA_REAL_PRICE_APPLY_ENABLED")
    os.environ["VELLA_REAL_PRICE_APPLY_ENABLED"] = "true"
    try:
        api = client()
        create_response = api.post("/api/v1/wb-repricer/drafts", json=_valid_draft_payload(), headers=auth_headers(api, "price_sender"))
        assert create_response.status_code == 200
        draft = create_response.json()["data"]
        assert draft["state"] == "draft"

        draft_id = draft["draftId"]
        approve = api.post(
            f"/api/v1/wb-repricer/drafts/{draft_id}/approve",
            json={"approvalRef": "APR-SPRINT-B-001", "reason": "price manager approval"},
            headers=auth_headers(api, "price_sender"),
        )
        assert approve.status_code == 200
        assert approve.json()["data"]["state"] == "approved"

        apply_response = api.post(
            f"/api/v1/wb-repricer/drafts/{draft_id}/apply",
            json={"scenario": "complete", "pollMaxAttempts": 3, "pollDelaySeconds": 0},
            headers=auth_headers(api, "price_sender"),
        )
        assert apply_response.status_code == 200
        job = apply_response.json()["data"]
        assert job["state"] == "accepted"
        assert job["wbUploadId"] is not None

    finally:
        if previous is None:
            os.environ.pop("VELLA_REAL_PRICE_APPLY_ENABLED", None)
        else:
            os.environ["VELLA_REAL_PRICE_APPLY_ENABLED"] = previous


def test_apply_failure_has_retry_and_retry_can_recover():
    previous = os.environ.get("VELLA_REAL_PRICE_APPLY_ENABLED")
    os.environ["VELLA_REAL_PRICE_APPLY_ENABLED"] = "true"
    try:
        api = client()
        created = api.post("/api/v1/wb-repricer/drafts", json=_valid_draft_payload(), headers=auth_headers(api, "price_sender"))
        assert created.status_code == 200
        draft_id = created.json()["data"]["draftId"]

        approved = api.post(
            f"/api/v1/wb-repricer/drafts/{draft_id}/approve",
            json={"approvalRef": "APR-SPRINT-B-002"},
            headers=auth_headers(api, "price_sender"),
        )
        assert approved.status_code == 200

        failed_apply = api.post(
            f"/api/v1/wb-repricer/drafts/{draft_id}/apply",
            json={"scenario": "server_error"},
            headers=auth_headers(api, "price_sender"),
        )
        assert failed_apply.status_code == 200
        failed_job = failed_apply.json()["data"]
        assert failed_job["state"] == "needs_attention"
        assert failed_job["retryCount"] == 1
        assert failed_job["nextRetryAt"] is not None

        retried = api.post(
            f"/api/v1/wb-repricer/jobs/{failed_job['jobId']}/retry",
            json={"scenario": "complete"},
            headers=auth_headers(api, "price_sender"),
        )
        assert retried.status_code == 200
        recovered = retried.json()["data"]
        assert recovered["state"] in {"accepted", "sent"}

    finally:
        if previous is None:
            os.environ.pop("VELLA_REAL_PRICE_APPLY_ENABLED", None)
        else:
            os.environ["VELLA_REAL_PRICE_APPLY_ENABLED"] = previous


def test_legacy_direct_apply_endpoint_is_blocked():
    api = client()
    response = api.post(
        "/api/v1/wb-repricer/actions/price-apply",
        json={
            "scenario": "complete",
            "dryRun": False,
            "rows": [{"nmId": 123456, "priceKopecks": 139000, "discountPct": 12}],
        },
        headers=auth_headers(api, "price_sender"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HTTP_409"
