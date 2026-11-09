from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    return TestClient(create_app())


def test_1c_cash_flow_rejects_wrong_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.one_c_cash_flow.LOG_PATH", tmp_path / "1c_logs.json")

    response = client().post(
        "/api/1c/cash-flow",
        headers={"Authorization": "Bearer wrong"},
        json={"amount": 123},
    )

    assert response.status_code == 401


def test_1c_cash_flow_accepts_payload_and_exposes_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.one_c_cash_flow.LOG_PATH", tmp_path / "1c_logs.json")
    api = client()

    accepted = api.post(
        "/api/1c/cash-flow",
        headers={"Authorization": "Bearer change-me"},
        json={"date": "2026-07-03", "amount": 12500, "comment": "test payment"},
    )

    assert accepted.status_code == 200
    accepted_payload = accepted.json()
    assert accepted_payload["status"] == "ok"
    assert accepted_payload["id"]

    unauthorized_logs = api.get("/api/1c/cash-flow/logs")
    assert unauthorized_logs.status_code == 401

    forbidden_logs = api.get("/api/1c/cash-flow/logs", headers=auth_headers(api, "viewer"))
    assert forbidden_logs.status_code == 403

    logs = api.get("/api/1c/cash-flow/logs", headers=auth_headers(api, "finance_viewer"))
    assert logs.status_code == 200
    payload = logs.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == accepted_payload["id"]
    assert payload["items"][0]["payload"]["amount"] == 12500
    assert payload["items"][0]["payloadKeys"] == ["amount", "comment", "date"]


def test_cash_flow_job_queue_roundtrip_and_pnl_attachment(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    monkeypatch.setattr("app.routers.one_c_cash_flow.LOG_PATH", tmp_path / "1c_logs.json")
    monkeypatch.setattr("app.routers.one_c_cash_flow.JOBS_PATH", tmp_path / "1c_jobs.json")
    report_cache: dict[tuple[int, str], dict] = {}
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.save_source_cache",
        lambda organization_id, key, payload: report_cache.__setitem__((organization_id, key), payload) or payload,
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.get_source_cache",
        lambda organization_id, key, **_kwargs: report_cache.get((organization_id, key)),
    )
    api = client()
    headers = auth_headers(api, "finance_viewer")

    requested = api.get(
        "/api/wb/reports/cash-flow",
        params={"from": "2026-06-01", "to": "2026-06-30"},
        headers=headers,
    )
    assert requested.status_code == 200
    pending = requested.json()
    assert pending["status"] == "pending"
    job_id = pending["job_id"]

    jobs = api.get("/api/1c/jobs", headers={"Authorization": "Bearer change-me"})
    assert jobs.status_code == 200
    assert jobs.json()["jobs"] == [
        {
            "job_id": job_id,
            "type": "cash_flow",
            "period_from": "2026-06-01",
            "period_to": "2026-06-30",
        }
    ]

    result = api.post(
        f"/api/1c/jobs/{job_id}/result",
        headers={"Authorization": "Bearer change-me"},
        json={
            "balances": [{"opening_balance": 1000, "turnover": 700, "closing_balance": 1300}],
            "income": [
                {"article": "Оплата от покупателей", "amount": 150},
                {"article": "Перемещение между кассами, счетами", "amount": 5000},
            ],
            "expense": [
                {"article": "Аренда помещений", "amount": 1200},
                {"article": "Заработная плата", "amountKopecks": 34_000},
                {"article": "Возврат кредита /займа/ФК ОБОРОТЫ", "amount": 999},
                {"article": "Дивиденды", "amount": 888},
                {"article": "Перемещение между кассами, счетами", "amount": 777},
                {"article": "Оплата поставщикам_Султанов", "amount": 666},
                {"article": "Оплата поставщикам_расходники", "amount": 100},
            ],
        },
    )
    assert result.status_code == 200
    assert result.json()["rows"] == 9

    ready = api.get(
        "/api/wb/reports/cash-flow",
        params={"from": "2026-06-01", "to": "2026-06-30"},
        headers=headers,
    )
    assert ready.status_code == 200
    ready_payload = ready.json()
    assert ready_payload["status"] == "ready"
    assert ready_payload["data"]["totals"]["expenseKopecks"] == 497_000
    assert ready_payload["data"]["totals"]["operationalExpenseKopecks"] == 164_000
    assert ready_payload["data"]["totals"]["incomeKopecks"] == 515_000
    assert ready_payload["data"]["balances"]["closingBalanceKopecks"] == 130_000
    operational_articles = [
        row["article"]
        for row in ready_payload["data"]["rows"]
        if row["operationalExpense"]
    ]
    assert operational_articles == ["Аренда помещений", "Заработная плата", "Оплата поставщикам_расходники"]

    from app import repricer_tasks

    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("P&L cache build must not refresh WB"))
    repricer_tasks.build_report_for_org.run(
        1,
        "finance-viewer@vella.local",
        "pnl",
        "2026-06-01",
        "2026-06-30",
        "sku",
        "financial",
        True,
        None,
    )
    pnl = api.get(
        "/api/wb/reports/pnl",
        params={"preset": "custom", "from": "2026-06-01", "to": "2026-06-30", "source": "financial", "groupBy": "sku"},
        headers=headers,
    )
    assert pnl.status_code == 200
    assert pnl.json()["cashFlow"]["status"] == "ready"
    assert pnl.json()["cashFlow"]["data"]["totals"]["operationalExpenseKopecks"] == 164_000

    expenses = api.get(
        "/api/wb/reports/expenses",
        params={"preset": "custom", "from": "2026-06-01", "to": "2026-06-30", "groupBy": "sku"},
        headers=headers,
    )
    assert expenses.status_code == 200
    expenses_payload = expenses.json()
    assert expenses_payload["meta"]["id"] == "expenses"
    assert expenses_payload["cashFlow"]["status"] == "ready"
    assert expenses_payload["kpis"][0]["value"] == "3"
    assert expenses_payload["rows"] == [
        {
            "id": "cf_2",
            "category": "Аренда помещений",
            "article": "Аренда помещений",
            "amountKopecks": 120_000,
            "sourceLabel": "1С cash-flow",
            "flowType": "expense",
            "periodLabel": "2026-06-01 — 2026-06-30",
            "approvalStatusLabel": "готово к P&L",
            "allocationBaseLabel": "статья ДДС",
            "allocationCoverageLabel": "100% статьи",
            "owner": "1С",
            "pending": "нет",
            "comment": "Операционная статья ДДС из 1С cash-flow.",
        },
        {
            "id": "cf_3",
            "category": "Заработная плата",
            "article": "Заработная плата",
            "amountKopecks": 34_000,
            "sourceLabel": "1С cash-flow",
            "flowType": "expense",
            "periodLabel": "2026-06-01 — 2026-06-30",
            "approvalStatusLabel": "готово к P&L",
            "allocationBaseLabel": "статья ДДС",
            "allocationCoverageLabel": "100% статьи",
            "owner": "1С",
            "pending": "нет",
            "comment": "Операционная статья ДДС из 1С cash-flow.",
        },
        {
            "id": "cf_8",
            "category": "Оплата поставщикам_расходники",
            "article": "Оплата поставщикам_расходники",
            "amountKopecks": 10_000,
            "sourceLabel": "1С cash-flow",
            "flowType": "expense",
            "periodLabel": "2026-06-01 — 2026-06-30",
            "approvalStatusLabel": "готово к P&L",
            "allocationBaseLabel": "статья ДДС",
            "allocationCoverageLabel": "100% статьи",
            "owner": "1С",
            "pending": "нет",
            "comment": "Операционная статья ДДС из 1С cash-flow.",
        },
    ]


def test_cash_flow_next_job_endpoint_returns_single_pending_job(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.one_c_cash_flow.LOG_PATH", tmp_path / "1c_logs.json")
    monkeypatch.setattr("app.routers.one_c_cash_flow.JOBS_PATH", tmp_path / "1c_jobs.json")
    api = client()

    requested = api.get(
        "/api/wb/reports/cash-flow",
        params={"from": "2026-06-01", "to": "2026-07-07"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert requested.status_code == 200
    job_id = requested.json()["job_id"]

    next_job = api.get("/api/1c/jobs/next", headers={"Authorization": "Bearer change-me"})

    assert next_job.status_code == 200
    assert next_job.json() == {
        "has_job": True,
        "job_id": job_id,
        "type": "cash_flow",
        "period_from": "2026-06-01",
        "period_to": "2026-07-07",
    }

    empty = api.get("/api/1c/jobs/next", headers={"Authorization": "Bearer change-me"})

    assert empty.status_code == 200
    assert empty.json() == {
        "has_job": False,
        "job_id": None,
        "type": None,
        "period_from": None,
        "period_to": None,
    }


def test_cash_flow_database_store_is_shared_between_request_and_claim(tmp_path, monkeypatch):
    from app.cash_flow import store
    from app.cash_flow.orm import OneCCashFlowJobRow
    from app.routers import one_c_cash_flow

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cash-flow.db'}")
    OneCCashFlowJobRow.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(one_c_cash_flow, "JOBS_PATH", one_c_cash_flow.DEFAULT_JOBS_PATH)

    created = one_c_cash_flow.get_cash_flow_for_period(
        organization_id=77,
        period_from=date(2026, 5, 1),
        period_to=date(2026, 5, 31),
        requested_by="finance@example.test",
    )
    claimed = one_c_cash_flow._claim_jobs(1)
    observed = one_c_cash_flow.get_cash_flow_for_period(
        organization_id=77,
        period_from=date(2026, 5, 1),
        period_to=date(2026, 5, 31),
        requested_by="finance@example.test",
    )

    assert created["status"] == "pending"
    assert claimed[0]["id"] == created["job_id"]
    assert claimed[0]["status"] == "processing"
    assert observed == {"status": "processing", "job_id": created["job_id"]}
