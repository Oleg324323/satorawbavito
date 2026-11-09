from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.avito import account_stats_http as http
from app.avito.account_stats import AccountStatsError, project_stats_result
from app.avito.account_stats_http import make_account_avito_stats_router
from app.avito.stats import (
    AvitoStatsAccount,
    AvitoStatsDailyPoint,
    AvitoStatsFetchResult,
    AvitoStatsItem,
)

START = END = date(2026, 9, 1)


def result():
    return AvitoStatsFetchResult(
        status="synced",
        accounts=[AvitoStatsAccount(accountId="123", accountName="123")],
        items=[
            AvitoStatsItem(
                itemId="account:123:totals",
                accountId="123",
                accountName="123",
                title="123",
                views=0,
                spendKopecks=9007199254740993,
            )
        ],
        diagnostics={"unsafe": "synthetic detail"},
    )


def test_projection_preserves_exact_metrics_nulls_without_diagnostics_names():
    value = project_stats_result(
        result(), external_id="123", date_from=START, date_to=END
    )
    assert value["rows"][0]["metrics"]["views"] == "0"
    assert value["rows"][0]["metrics"]["spendKopecks"] == "9007199254740993"
    assert value["rows"][0]["metrics"]["orders"] is None
    assert "diagnostics" not in value and "accountName" not in str(value)


@pytest.mark.parametrize("section", ["accounts", "items"])
def test_foreign_account_rejected(section):
    data = result()
    getattr(data, section)[0].accountId = "999"
    with pytest.raises(AccountStatsError):
        project_stats_result(data, external_id="123", date_from=START, date_to=END)


def test_blocked_does_not_return_provider_error():
    data = result()
    data.status = "blocked"
    with pytest.raises(AccountStatsError):
        project_stats_result(data, external_id="123", date_from=START, date_to=END)


def client(monkeypatch):
    monkeypatch.setattr(
        http, "get_marketplace_credential_actor", lambda request: "actor"
    )

    class Service:
        def fetch(self, actor, *, marketplace_account_id, date_from, date_to):
            assert actor == "actor" and marketplace_account_id == 2
            return {"marketplaceAccountId": 2, "provider": "avito"}

    app = FastAPI()
    app.include_router(
        make_account_avito_stats_router(service_dependency=lambda: Service())
    )
    return TestClient(app)


def test_route_explicit_dates_and_no_store(monkeypatch):
    response = client(monkeypatch).get(
        "/api/v2/avito/accounts/2/statistics?dateFrom=2026-09-01&dateTo=2026-09-01"
    )
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "dateFrom=bad&dateTo=2026-09-01",
        "dateFrom=2026-01-01&dateTo=2027-01-01",
        "dateFrom=2026-09-01&dateTo=2026-08-01",
        "dateFrom=2026-09-01&dateTo=2026-09-01&accountId=9",
    ],
)
def test_invalid_query_fixed_no_echo(monkeypatch, query):
    response = client(monkeypatch).get("/api/v2/avito/accounts/2/statistics?" + query)
    assert response.status_code == 422 and response.json() == {
        "detail": {"code": "AVITO_ACCOUNT_STATS_INVALID_REQUEST"}
    }


@pytest.mark.parametrize(
    "case",
    [
        "foreign_day",
        "outside_day",
        "duplicate_day",
        "bool_metric",
        "wrong_item",
        "many_rows",
    ],
)
def test_projection_refuses_cross_scope_and_lossy_or_duplicate_data(case):
    data = result()
    if case.endswith("day"):
        data.daily = [
            AvitoStatsDailyPoint(
                date=START, accountId="123", accountName="ignored", views=0
            )
        ]
        if case == "foreign_day":
            data.daily[0].accountId = "999"
        elif case == "outside_day":
            data.daily[0].date = date(2026, 8, 1)
        else:
            data.daily.append(data.daily[0])
    elif case == "bool_metric":
        data.items[0].views = True
    elif case == "wrong_item":
        data.items[0].itemId = "arbitrary-item"
    else:
        data.items.append(data.items[0])
    with pytest.raises(AccountStatsError):
        project_stats_result(data, external_id="123", date_from=START, date_to=END)


@pytest.mark.parametrize(
    "code,status",
    [
        ("AVITO_ACCOUNT_STATS_ACCESS_DENIED", 403),
        ("AVITO_ACCOUNT_STATS_DISABLED", 503),
        ("AVITO_ACCOUNT_STATS_UNAVAILABLE", 503),
    ],
)
def test_service_error_fixed_code_and_no_store(monkeypatch, code, status):
    monkeypatch.setattr(
        http, "get_marketplace_credential_actor", lambda request: "actor"
    )

    class Service:
        def fetch(self, *args, **kwargs):
            raise AccountStatsError(code)

    app = FastAPI()
    app.include_router(
        make_account_avito_stats_router(service_dependency=lambda: Service())
    )
    response = TestClient(app).get(
        "/api/v2/avito/accounts/2/statistics?dateFrom=2026-09-01&dateTo=2026-09-01"
    )
    assert response.status_code == status and response.json() == {
        "detail": {"code": code}
    }
    assert response.headers["cache-control"] == "no-store"


def test_auth_401_kept_but_raw_detail_not_exposed(monkeypatch):
    from fastapi import HTTPException

    def denied(request):
        raise HTTPException(401, "synthetic-private-auth-detail")

    monkeypatch.setattr(http, "get_marketplace_credential_actor", denied)
    app = FastAPI()
    app.include_router(
        make_account_avito_stats_router(
            service_dependency=lambda: pytest.fail("service created")
        )
    )
    response = TestClient(app).get(
        "/api/v2/avito/accounts/2/statistics?dateFrom=2026-09-01&dateTo=2026-09-01"
    )
    assert response.status_code == 401 and "synthetic" not in response.text
    assert response.headers["cache-control"] == "no-store"
