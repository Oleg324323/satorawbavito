from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.auth import router as auth_router
from app.routers.control_plane import router as control_plane_router
from app.routers.wb_repricer_sprint_c import router as sprint_c_router
from app.wb_api.client import FakeWbApiClient
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(control_plane_router)
    app.include_router(sprint_c_router)
    return TestClient(app)


def current_price_client() -> FakeWbApiClient:
    return FakeWbApiClient(
        fixtures={
            "/api/v2/list/goods/filter": {
                "data": {
                    "listGoods": [
                        {
                            "nmID": 123456,
                            "vendorCode": "FBBT_42",
                            "brand": "Satorna",
                            "subjectName": "Футболки",
                            "discount": 12,
                            "sizes": [
                                {
                                    "sizeID": 654321,
                                    "price": 139000,
                                    "discountedPrice": 122320,
                                    "buyerPriceNoWallet": 110088,
                                    "techSizeName": "L",
                                }
                            ],
                        }
                    ]
                },
                "error": False,
                "errorText": "",
            }
        }
    )


def economics_payload() -> dict:
    return {
        "cogsKopecks": 70000,
        "commissionPct": 20,
        "logisticsKopecks": 5000,
        "storageKopecks": 1000,
        "taxKopecks": 2000,
        "buyoutPct": 85,
        "stockUnits": 20,
        "promoActive": False,
    }


def dry_run_item_payload() -> dict:
    return {
        "articleId": "FBBT_42",
        "nmId": 123456,
        "candidateSellerPriceKopecks": 123000,
        "economics": economics_payload(),
    }


def test_sprint_c_catalog_defaults_include_disabled_4445_and_night_off():
    api = client()
    response = api.get(
        "/api/v1/wb-repricer/strategies/catalog",
        headers=auth_headers(api, "viewer"),
    )
    assert response.status_code == 200
    rows = {item["strategyId"]: item for item in response.json()["data"]}

    assert {"baskets_orders_4599", "revenue_dynamics_4600", "night_price_mode", "spp_turnover_4445"} <= set(rows)
    assert rows["night_price_mode"]["status"] == "disabled"
    assert rows["spp_turnover_4445"]["status"] == "discovery_required"
    assert rows["spp_turnover_4445"]["scopeAssignmentEnabled"] is True


def test_4599_dry_run_is_explainable_and_versioned():
    api = client()
    create_version = api.post(
        "/api/v1/wb-repricer/strategies/baskets_orders_4599/versions",
        json={"desiredStatus": "enabled", "capPct": 3.0, "reason": "enable 4599 for dry-run"},
        headers=auth_headers(api, "settings_editor"),
    )
    assert create_version.status_code == 200
    created = create_version.json()["data"]
    assert created["status"] == "enabled"
    assert created["formulaVersion"] == "frm-strategy-4599-v1"

    run = api.post(
        "/api/v1/wb-repricer/strategies/baskets_orders_4599/dry-run",
        json={
            "scenario": "complete",
            "items": [
                {
                    **dry_run_item_payload(),
                    "basketsTrend": "up",
                    "ordersTrend": "down",
                }
            ],
        },
        headers=auth_headers(api, "price_sender"),
    )
    assert run.status_code == 200
    report = run.json()["data"]
    item = report["items"][0]
    assert report["strategyId"] == "baskets_orders_4599"
    assert item["deltaPct"] == -1.0
    assert item["currentSellerPriceKopecks"] is not None
    expected_recommended = int(round(item["currentSellerPriceKopecks"] * 0.99))
    assert item["recommendedSellerPriceKopecks"] == expected_recommended
    assert "4599 matrix" in item["explanation"]
    assert item["formulaVersion"] == "frm-strategy-4599-v1"
    assert item["sourceDependencies"]


def test_4600_requires_mode_then_supports_percent_rub_floor(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_repricer_sprint_c.build_wb_client",
        lambda _scenario: current_price_client(),
    )
    api = client()

    mode_missing = api.post(
        "/api/v1/wb-repricer/strategies/revenue_dynamics_4600/versions",
        json={"desiredStatus": "enabled", "reason": "try enable without mode"},
        headers=auth_headers(api, "settings_editor"),
    )
    assert mode_missing.status_code == 200
    missing_row = mode_missing.json()["data"]
    assert missing_row["status"] == "draft"
    assert "revenue_mode_not_selected" in missing_row["blockedReasons"]

    enabled = api.post(
        "/api/v1/wb-repricer/strategies/revenue_dynamics_4600/versions",
        json={
            "desiredStatus": "enabled",
            "reason": "enable with rub floor mode",
            "revenueMode": "percent_rub_floor",
            "revenueMaxStepPct": 3.0,
            "revenueRubFloorKopecks": 500,
        },
        headers=auth_headers(api, "settings_editor"),
    )
    assert enabled.status_code == 200
    row = enabled.json()["data"]
    assert row["status"] == "enabled"

    run = api.post(
        "/api/v1/wb-repricer/strategies/revenue_dynamics_4600/dry-run",
        json={
            "scenario": "complete",
            "items": [{**dry_run_item_payload(), "revenueTrendPct": 0.4}],
        },
        headers=auth_headers(api, "price_sender"),
    )
    assert run.status_code == 200
    item = run.json()["data"]["items"][0]
    assert item["deltaKopecks"] == 500
    assert "rub floor applied" in item["explanation"]


def test_night_mode_schedule_requires_approval_and_dry_run_has_no_auto_commit():
    api = client()

    current = api.get("/api/v1/wb-repricer/night-schedule", headers=auth_headers(api, "viewer"))
    assert current.status_code == 200
    assert current.json()["data"]["enabled"] is False

    no_approval = api.put(
        "/api/v1/wb-repricer/night-schedule",
        json={
            "enabled": True,
            "windowStartHour": 1,
            "windowEndHour": 5,
            "timezoneName": "Europe/Moscow",
            "reason": "try without approval ref",
        },
        headers=auth_headers(api, "settings_editor"),
    )
    assert no_approval.status_code == 409

    with_approval = api.put(
        "/api/v1/wb-repricer/night-schedule",
        json={
            "enabled": True,
            "windowStartHour": 1,
            "windowEndHour": 5,
            "timezoneName": "Europe/Moscow",
            "approvalRef": "APR-NIGHT-C-001",
            "reason": "approved night schedule",
        },
        headers=auth_headers(api, "settings_editor"),
    )
    assert with_approval.status_code == 200
    assert with_approval.json()["data"]["status"] == "enabled"

    run = api.post(
        "/api/v1/wb-repricer/strategies/night_price_mode/dry-run",
        json={
            "scenario": "complete",
            "items": [{**dry_run_item_payload(), "nightTargetDeltaPct": 1.5}],
        },
        headers=auth_headers(api, "price_sender"),
    )
    assert run.status_code == 200
    item = run.json()["data"]["items"][0]
    assert item["autoCommitAllowed"] is False
    assert "night_mode_auto_commit_requires_separate_approval" in item["blockedReasons"]
    assert "Dry-run only" in item["explanation"]


def test_4445_is_discovery_required_and_cannot_create_production_draft():
    api = client()
    run = api.post(
        "/api/v1/wb-repricer/strategies/spp_turnover_4445/dry-run",
        json={"scenario": "complete", "items": [dry_run_item_payload()]},
        headers=auth_headers(api, "price_sender"),
    )
    assert run.status_code == 200
    report = run.json()["data"]
    item = report["items"][0]

    assert report["strategyStatus"] == "discovery_required"
    assert item["canCreateProductionDraft"] is False
    assert any("WB-25" in reason for reason in item["blockedReasons"])


def test_sku_assignment_is_active_and_non_sku_scope_is_blocked():
    api = client()
    sku_assignment = api.post(
        "/api/v1/wb-repricer/strategy-assignments",
        json={"strategyId": "baskets_orders_4599", "scope": "sku", "targetId": "FBBT_42"},
        headers=auth_headers(api, "settings_editor"),
    )
    assert sku_assignment.status_code == 200
    sku_item = sku_assignment.json()["data"]
    assert sku_item["status"] == "active"
    assert sku_item["blockerIds"] == []

    blocked_group_assignment = api.post(
        "/api/v1/wb-repricer/strategy-assignments",
        json={"strategyId": "baskets_orders_4599", "scope": "group", "targetId": "group-1"},
        headers=auth_headers(api, "settings_editor"),
    )
    assert blocked_group_assignment.status_code == 200
    blocked_item = blocked_group_assignment.json()["data"]
    assert blocked_item["status"] == "blocked"
    assert blocked_item["blockerIds"] == ["SCOPE_SKU_ONLY_V1"]

    version = api.post(
        "/api/v1/wb-repricer/strategies/baskets_orders_4599/versions",
        json={"desiredStatus": "draft", "reason": "audit visibility check"},
        headers=auth_headers(api, "settings_editor"),
    )
    assert version.status_code == 200

    audit = api.get(
        "/api/v1/audit/events",
        params={"actionPrefix": "strategy.version.create", "limit": 200},
        headers=auth_headers(api, "admin"),
    )
    assert audit.status_code == 200
    assert any("baskets_orders_4599" in event["objectId"] for event in audit.json()["items"])
