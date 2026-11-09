from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cabinet.orm import LkOrganizationRow, LkUserRow
from app.cabinet.permissions import permissions_from_profile
from app.control_plane.auth import ActorContext
from app.infra.db import get_db_session
from app.infra.models import Base
from app.main import create_app
from app.platform.advertising.service import AdvertisingService
from app.platform.catalog.orm import (
    CatalogSkuRow,
    MarketplaceOfferRow,
    MarketplaceProductRow,
)
from app.platform.economics.costs import CostsService
from app.platform.economics.policies import EconomicsService
from app.platform.finance.service import FinanceService
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period
from app.routers.finance_v2 import get_finance_actor

PERIOD = Period(date(2026, 8, 17), date(2026, 8, 23))
GLOBAL_BLOCKERS = {
    "WB_PNL_CLASSIFICATION_NOT_CANONICAL",
}


def raw_advertising_bundle() -> dict[str, object]:
    bundle = json.loads(
        (
            Path(__file__).parent / "fixtures/wb_advertising_raw_sanitized.json"
        ).read_text()
    )
    date_from = PERIOD.date_from.isoformat()
    date_to = PERIOD.date_to.isoformat()
    business_day = "2026-08-20"
    bundle["period"] = {"dateFrom": date_from, "dateTo": date_to}
    response = bundle["fullstats"][0]
    response.update(
        dateFrom=date_from,
        dateTo=date_to,
        campaignIds=[1001],
        payload=response["payload"][:1],
    )
    campaign = response["payload"][0]
    campaign["sum"] = 123.0
    campaign["days"] = campaign["days"][:1]
    day = campaign["days"][0]
    day.update(date=f"{business_day}T00:00:00Z", sum=123.0)
    day["apps"] = day["apps"][:1]
    app = day["apps"][0]
    app["sum"] = 123.0
    app["nms"] = app["nms"][:1]
    app["nms"][0].update(nmId=453200669, sum=123.0)
    for page in bundle["upd"]:
        page.update(dateFrom=date_from, dateTo=date_to)
        for index, row in enumerate(page["payload"]):
            row["updTime"] = f"{business_day}T12:{index:02d}:00+03:00"
    for item in bundle["manifest"]:
        request = item.get("request", {})
        for key in ("from", "beginDate"):
            if key in request:
                request[key] = date_from
        for key in ("to", "endDate"):
            if key in request:
                request[key] = date_to
        if "ids" in request:
            request["ids"] = "1001"
    return bundle


@pytest.fixture
def api() -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    users = {
        "finance-all": ("finance_viewer", "all", []),
        "finance-scoped": ("finance_viewer", "selected", [31]),
        "viewer": ("viewer", "all", []),
    }
    with factory() as session:
        session.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                *[
                    LkUserRow(
                        user_id=user_id,
                        organization_id=1,
                        email=f"{user_id}@example.local",
                        password_hash="unused",
                        full_name=user_id,
                        permission_profile=profile,
                    )
                    for user_id, (profile, _scope, _accounts) in users.items()
                ],
                *[
                    IamMembershipRow(
                        membership_id=index,
                        organization_id=1,
                        user_id=user_id,
                        role=profile,
                        permissions=[],
                        scope_mode=scope,
                        allowed_account_ids=accounts,
                    )
                    for index, (user_id, (profile, scope, accounts)) in enumerate(
                        users.items(), 101
                    )
                ],
                MarketplaceAccountRow(
                    marketplace_account_id=31,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=33,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one-extra",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=32,
                    organization_id=2,
                    marketplace="wb",
                    external_account_id="wb-two",
                    status="connected",
                ),
                CatalogSkuRow(
                    catalog_sku_id=11,
                    organization_id=1,
                    code="FBBT_1133",
                ),
            ]
        )
        session.flush()
        session.add(
            MarketplaceProductRow(
                marketplace_product_id=1011,
                organization_id=1,
                marketplace_account_id=31,
                external_product_id="453200669",
                seller_article="FBBT_1133",
            )
        )
        session.flush()
        session.add(
            MarketplaceOfferRow(
                marketplace_offer_id=2011,
                organization_id=1,
                marketplace_account_id=31,
                marketplace_product_id=1011,
                external_offer_key="FBBT_1133",
                catalog_sku_id=11,
            )
        )
        session.commit()
        CostsService(session, 1).set_cost(
            catalog_sku_id=11,
            amount_kopecks=20_000,
            value_state="configured",
            effective_from=datetime(2026, 8, 16, 21, tzinfo=timezone.utc),
            source="fixture",
            source_reference="abc-api-cost",
            evidence_status="dated",
        )
        EconomicsService(session, 1).set_organization_policy(
            tax_basis_points=600,
            other_expense_price_basis_points=500,
            other_expense_per_sale_kopecks=1_000,
            value_state="configured",
            effective_from=datetime(2026, 8, 16, 21, tzinfo=timezone.utc),
            source="fixture",
            source_reference="abc-api-economics",
            evidence_status="dated",
        )
        FinanceService(session, 1).ingest_snapshot(
            31,
            PERIOD,
            [
                {
                    "rrdId": 1,
                    "reportId": 820347930,
                    "reportType": 1,
                    "nmId": 453200669,
                    "vendorCode": "FBBT_1133",
                    "docTypeName": "Продажа",
                    "sellerOperName": "Продажа",
                    "quantity": 1,
                    "retailAmount": "1208.00",
                    "forPay": "1208.00",
                    "cashbackAmount": "10.00",
                    "cashbackDiscount": "3.50",
                    "cashbackCommissionChange": "-1.25",
                    "saleDt": "2026-08-20",
                    "rrDate": "2026-08-23",
                    "deduction": "123.00",
                    "bonusTypeName": "WB Продвижение",
                }
            ],
            observed_at=datetime(2026, 8, 26, 3, 50, tzinfo=timezone.utc),
        )
        AdvertisingService(session, 1).ingest_raw_payload(
            31,
            PERIOD,
            raw_advertising_bundle(),
            source_reference="abc-api-advertising",
            observed_at=datetime(2026, 8, 26, 3, 50, tzinfo=timezone.utc),
        )

    app = create_app()

    def override_session():
        with factory() as session:
            yield session

    def override_actor(request: Request) -> ActorContext:
        user_id = request.headers.get("x-test-user")
        if user_id not in users:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        profile = users[user_id][0]
        return ActorContext(
            actor_id=user_id,
            user_id=user_id,
            organization_id=1,
            permission_profile=profile,
            permissions=permissions_from_profile(profile),
        )

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_finance_actor] = override_actor
    return TestClient(app)


def headers(user_id: str) -> dict[str, str]:
    return {"X-Test-User": user_id}


def params(account_id: int = 31) -> dict[str, object]:
    return {
        "marketplaceAccountId": account_id,
        "dateFrom": "2026-08-17",
        "dateTo": "2026-08-23",
    }


def test_abc_pnl_v2_is_cache_only_and_reconciles(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.repricer_bff.fetch_finance_report_aggregates",
        lambda *_args, **_kwargs: pytest.fail("HTTP read path must not call WB"),
    )

    response = api.get(
        "/api/v2/wb/reports/abc-pnl",
        params=params(),
        headers=headers("finance-all"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == len(payload["items"]) == 1
    assert payload["summary"]["revenueKopecks"] == 120_800
    assert payload["summary"]["cogsKopecks"] == 20_000
    assert payload["summary"]["settlementProfitKopecks"] == 100_800
    assert payload["summary"]["taxKopecks"] == 7_248
    assert payload["summary"]["otherExpensesKopecks"] == 0
    assert payload["summary"]["profitBeforeAdsAndLoyaltyKopecks"] == 93_552
    assert payload["summary"]["advertisingSpendKopecks"] == 12_300
    assert payload["summary"]["unattributedAdvertisingSpendKopecks"] == 0
    assert payload["summary"]["profitBeforeLoyaltyKopecks"] == 81_252
    assert payload["summary"]["cashbackAmountKopecks"] == 1_000
    assert payload["summary"]["cashbackDiscountKopecks"] == 350
    assert payload["summary"]["cashbackCommissionChangeKopecks"] == -125
    assert payload["summary"]["loyaltyNetCostKopecks"] == 875
    assert payload["summary"]["profitAfterLoyaltyKopecks"] == 80_377
    assert payload["summary"]["netProfitKopecks"] == 80_377
    assert payload["summary"]["profitBeforeInternalExpensesKopecks"] == 80_377
    assert payload["summary"]["internalExpensesKopecks"] == 0
    assert payload["items"][0]["settlementProfitKopecks"] == 100_800
    assert payload["items"][0]["economicsValueState"] == "configured"
    assert payload["items"][0]["economicsEvidenceStatus"] == "dated"
    assert payload["items"][0]["advertisingSpendKopecks"] == 12_300
    assert payload["items"][0]["profitBeforeLoyaltyKopecks"] == 81_252
    assert payload["items"][0]["cashbackAmountKopecks"] == 1_000
    assert payload["items"][0]["cashbackDiscountKopecks"] == 350
    assert payload["items"][0]["cashbackCommissionChangeKopecks"] == -125
    assert payload["items"][0]["loyaltyNetCostKopecks"] == 875
    assert payload["items"][0]["profitAfterLoyaltyKopecks"] == 80_377
    assert payload["items"][0]["netProfitKopecks"] == 80_377
    assert payload["items"][0]["profitBeforeInternalExpensesKopecks"] == 80_377
    assert payload["items"][0]["internalExpensesKopecks"] == 0
    assert payload["items"][0]["profitClass"] is None
    assert payload["items"][0]["abcCode"] is None
    assert payload["meta"]["state"] == "partial"
    assert payload["meta"]["formulaVersion"] == "wb-abc-pnl-finance-rrdate-v4"
    assert payload["meta"]["costLedgerRevision"] == 1
    assert payload["meta"]["economicsRevision"] == 1
    assert payload["meta"]["advertisingSource"] == "finance_promotion"
    assert len(payload["meta"]["advertisingSnapshotChecksum"]) == 64
    assert payload["meta"]["advertisingEvidenceStatus"] == "raw"
    assert payload["meta"]["blockerIds"] == [
        "WB_PNL_CLASSIFICATION_NOT_CANONICAL",
            ]
    assert set(payload["meta"]["blockerIds"]) == GLOBAL_BLOCKERS
    assert payload["meta"]["snapshot"]["operationCount"] == 1
    assert payload["timestamp"].endswith("Z")


def test_abc_pnl_v2_requires_permission_and_account_scope(api: TestClient) -> None:
    path = "/api/v2/wb/reports/abc-pnl"
    assert api.get(path, params=params()).status_code == 401
    denied = api.get(path, params=params(), headers=headers("viewer"))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NO_ACCESS"

    scoped = api.get(path, params=params(33), headers=headers("finance-scoped"))
    assert scoped.status_code == 403
    assert scoped.json()["error"]["code"] == "ACCOUNT_SCOPE_DENIED"

    cross_tenant = api.get(path, params=params(32), headers=headers("finance-all"))
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["error"]["code"] == "WB_ACCOUNT_NOT_FOUND"


def test_abc_pnl_v2_contract_and_period_validation(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()
    assert "/api/v2/wb/reports/abc-pnl" in schema["paths"]
    assert "AbcPnlPageView" in schema["components"]["schemas"]

    invalid = api.get(
        "/api/v2/wb/reports/abc-pnl",
        params={"marketplaceAccountId": 31, "dateFrom": "2026-08-17"},
        headers=headers("finance-all"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_PERIOD"
