from __future__ import annotations

from datetime import date, datetime, timezone

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
from app.platform.finance.service import FinanceService
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period
from app.routers.finance_v2 import get_finance_actor

PERIOD = Period(date(2026, 8, 17), date(2026, 8, 23))


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
            ]
        )
        session.commit()
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
                    "saleDt": "2026-08-20",
                    "rrDate": "2026-08-23",
                }
            ],
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


def test_finance_v2_is_cache_only_and_returns_one_consistent_snapshot(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved_now = datetime(2026, 9, 1, 12, 34, 56, tzinfo=timezone.utc)
    monkeypatch.setattr("app.routers.finance_v2.utc_now", lambda: resolved_now)
    monkeypatch.setattr(
        "app.repricer_bff.fetch_finance_report_aggregates",
        lambda *_args, **_kwargs: pytest.fail("HTTP read path must not call WB"),
    )

    response = api.get(
        "/api/v2/wb/finance", params=params(), headers=headers("finance-all")
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == len(payload["items"]) == 1
    assert payload["summary"]["totalRevenueKopecks"] == 120_800
    assert sum(row["revenueKopecks"] for row in payload["items"]) == 120_800
    assert payload["meta"]["state"] == "ready"
    assert payload["meta"]["period"] == {
        "dateFrom": "2026-08-17",
        "dateTo": "2026-08-23",
        "timezone": "Europe/Moscow",
        "startAt": "2026-08-16T21:00:00Z",
        "endExclusiveAt": "2026-08-23T21:00:00Z",
        "days": 7,
        "temporalState": "complete",
    }
    assert payload["meta"]["snapshot"]["operationCount"] == 1
    assert payload["meta"]["snapshot"]["formulaVersion"] == "wb-finance-v4-rrdate"
    assert payload["timestamp"] == "2026-09-01T12:34:56Z"
    assert "token" not in str(payload).lower()


def test_finance_v2_requires_permission_and_account_scope(api: TestClient) -> None:
    assert api.get("/api/v2/wb/finance", params=params()).status_code == 401
    denied = api.get("/api/v2/wb/finance", params=params(), headers=headers("viewer"))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NO_ACCESS"

    scoped = api.get(
        "/api/v2/wb/finance",
        params=params(33),
        headers=headers("finance-scoped"),
    )
    assert scoped.status_code == 403
    assert scoped.json()["error"]["code"] == "ACCOUNT_SCOPE_DENIED"

    cross_tenant = api.get(
        "/api/v2/wb/finance",
        params=params(32),
        headers=headers("finance-all"),
    )
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["error"]["code"] == "WB_ACCOUNT_NOT_FOUND"


def test_finance_v2_contract_and_period_validation(api: TestClient) -> None:
    schema = api.get("/openapi.json").json()
    assert "/api/v2/wb/finance" in schema["paths"]
    assert "FinancePageView" in schema["components"]["schemas"]

    invalid = api.get(
        "/api/v2/wb/finance",
        params={"marketplaceAccountId": 31, "dateFrom": "2026-08-17"},
        headers=headers("finance-all"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_PERIOD"
