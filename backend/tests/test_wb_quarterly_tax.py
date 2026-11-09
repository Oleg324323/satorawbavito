"""Provider-free quarterly tax tests; all writes go to disposable SQLite."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform.economics import wb_quarterly_tax as tax
from app.platform.economics.legacy_tax import legacy_finance_tax_revision
from app.platform.economics.policies import EconomicsService
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
from tests.test_legacy_finance_tax import tax_db, _policy, _cache, _fact, _resolve  # noqa: F401
from tests.test_legacy_report_tax import report_cache  # noqa: F401
from tests.test_abc_pnl_service import session as canonical_session  # noqa: F401
from tests.test_abc_pnl_v2 import api as canonical_api  # noqa: F401


def settings(db, org=1, rate=6):
    row = db.get(WbRepricerAlgorithmSettingsRow, org)
    if row is None:
        row = WbRepricerAlgorithmSettingsRow(organization_id=org,
            settings_payload={"taxPct": rate, "otherExpensePricePct": 5, "marker": "preserve"})
        db.add(row)
        db.commit()
    return tax.settings_in_session(db, org)


def save(db, year, rates, org=1):
    payload = settings(db, org)
    return tax.save_year(db, org, year=year, rates=rates, expected_revision=tax.revision(payload))


@pytest.mark.parametrize("rate", [0, 6, 7.5, 7.55, None])
def test_initialization_is_read_only_preserves_zero_and_legacy_history(tax_db, rate):
    payload = settings(tax_db, rate=rate)
    assert tax.year_view(payload, 2026)["rates"] == [rate] * 4
    assert tax.year_view(payload, 2026)["configured"] == [False] * 4
    assert tax.SETTINGS_KEY not in tax.settings_in_session(tax_db, 1)
    assert tax.selected_basis_points(payload, date(2026, 10, 1)) is None


def test_save_reload_idempotence_unrelated_fields_and_account_isolation(tax_db):
    original = settings(tax_db, rate=7.5)
    settings(tax_db, org=2, rate=0)
    view = save(tax_db, 2026, [7.5, 0, 8.25, 12])
    assert view["rates"] == [7.5, 0, 8.25, 12]
    assert view["configured"] == [False, True, True, True]
    saved = tax.settings_in_session(tax_db, 1)
    assert {key: saved[key] for key in original} == original
    assert tax.year_view(saved, 2027)["rates"] == [7.5] * 4
    assert tax.year_view(tax.settings_in_session(tax_db, 2), 2026)["rates"] == [0] * 4
    assert save(tax_db, 2026, view["rates"])["revision"] == view["revision"]
    assert tax.settings_in_session(tax_db, 1) == saved
    with pytest.raises(tax.QuarterlyTaxConflict):
        tax.save_year(tax_db, 1, year=2026, rates=[1, 2, 3, 4], expected_revision=tax.revision(original))
    tax_db.rollback()
    assert tax.settings_in_session(tax_db, 1) == saved


def test_explicit_confirmation_accepts_saved_rate_without_changing_other_quarters(tax_db):
    original = settings(tax_db, rate=22)
    view = tax.save_year(tax_db, 1, year=2026, rates=[22] * 4,
        expected_revision=tax.revision(original), confirmed_quarters=(3,))
    assert view["rates"] == [22] * 4
    assert view["configured"] == [False, False, False, True]
    saved = tax.settings_in_session(tax_db, 1)
    assert saved["marker"] == "preserve" and saved["otherExpensePricePct"] == 5
    assert tax.selected_basis_points(saved, date(2026, 9, 30)) is None
    assert tax.selected_basis_points(saved, date(2026, 10, 12)) == 2200
    tax.save_year(tax_db, 1, year=2026, rates=[22] * 4,
        expected_revision=view["revision"], confirmed_quarters=(3,))
    assert tax.settings_in_session(tax_db, 1) == saved


@pytest.mark.parametrize("index", [-1, 4, True, "3"])
def test_invalid_explicit_quarter_does_not_write(tax_db, index):
    original = settings(tax_db)
    with pytest.raises(ValueError):
        tax.save_year(tax_db, 1, year=2026, rates=[6] * 4,
            expected_revision=tax.revision(original), confirmed_quarters=(index,))
    assert tax.settings_in_session(tax_db, 1) == original


@pytest.mark.parametrize("bad", [-1, 101, True, "NaN", "Infinity", "oops", 7.501, {}, []])
def test_invalid_rates_never_write(tax_db, bad):
    original = settings(tax_db)
    with pytest.raises(ValueError):
        save(tax_db, 2026, [6, 6, 6, bad])
    tax_db.rollback()
    assert tax.settings_in_session(tax_db, 1) == original


def test_missing_quarter_does_not_become_zero(tax_db):
    settings(tax_db, rate=None)
    save(tax_db, 2026, [None, None, None, 0])
    payload = tax.settings_in_session(tax_db, 1)
    assert tax.selected_basis_points(payload, date(2026, 9, 30)) is None
    assert tax.selected_basis_points(payload, date(2026, 10, 1)) == 0
    with pytest.raises(ValueError):
        save(tax_db, 2026, [None, None, None, None])


@pytest.mark.parametrize("instant,rate", [
    ("2026-03-31T20:59:59+00:00", 100), ("2026-03-31T21:00:00+00:00", 200),
    ("2026-06-30T21:00:00+00:00", 300), ("2026-09-30T21:00:00+00:00", 400),
    ("2026-12-31T21:00:00+00:00", None), ("2025-12-31T20:59:59+00:00", None),
])
def test_quarter_boundaries_use_moscow_and_exact_calendar_year(tax_db, instant, rate):
    save(tax_db, 2026, [1, 2, 3, 4])
    assert tax.selected_basis_points(tax.settings_in_session(tax_db, 1), datetime.fromisoformat(instant)) == rate


def test_same_initial_rates_leave_legacy_tax_and_rounding_unchanged(tax_db):
    _policy(tax_db, "2020-01-01T00:00:00", 750)
    settings(tax_db, rate=7.5)
    cache = _cache(_fact(14, 2), {"2026-09-30": _fact(7), "2026-10-01": _fact(7)})
    before = _resolve(cache, "2026-09-30", "2026-10-01")
    revision_before = legacy_finance_tax_revision(1)
    save(tax_db, 2026, [7.5] * 4)
    assert _resolve(cache, "2026-09-30", "2026-10-01") == before
    assert before["taxKopecks"] == 1
    assert legacy_finance_tax_revision(1) == revision_before


def test_cross_quarter_and_year_daily_signed_tax_preserves_sources_and_shared_policy(tax_db):
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    save(tax_db, 2026, [6, 6, 7.5, 10])
    save(tax_db, 2027, [0, 6, 6, 6])
    cache = _cache(_fact(200000, 2), {"2026-09-30": _fact(100000), "2026-10-01": _fact(100000)})
    original = deepcopy(cache)
    assert _resolve(cache, "2026-09-30", "2026-10-01")["taxKopecks"] == 17500
    assert _resolve(_cache(_fact(200000, 2), {"2026-12-31": _fact(100000), "2027-01-01": _fact(100000)}),
                    "2026-12-31", "2027-01-01")["taxKopecks"] == 10000
    assert _resolve(_cache(_fact(-60)), "2026-10-01")["taxKopecks"] == -6
    assert cache == original
    point = (11, datetime(2026, 10, 1, tzinfo=timezone.utc))
    # Shared economics service (including any Avito consumer) is unchanged.
    shared = EconomicsService(tax_db, 1).get_policies_for_points([point])[point]
    wb = tax.wb_policies_for_points(tax_db, 1, [point])[point]
    assert shared.tax_basis_points == 600 and wb.tax_basis_points == 1000
    for field in ("other_expense_price_basis_points", "other_expense_per_sale_kopecks"):
        assert getattr(shared, field) == getattr(wb, field)


def test_delayed_operation_uses_existing_recognition_date_not_report_date(tax_db):
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    save(tax_db, 2026, [6, 6, 7.5, 10])
    cache = {**_cache(_fact(100000), {"2026-09-30": _fact(100000)}),
             "dateFrom": "2026-10-07", "dateTo": "2026-10-11"}
    assert _resolve(cache, "2026-10-07", "2026-10-11")["taxKopecks"] == 7500
    assert _resolve(_cache(_fact(100000)), "2026-09-30", "2026-10-01")["factTaxReason"] == "tax_daily_basis_missing"


def test_quarter_save_changes_only_tax_revision_and_blocks_stale_report_publication(tax_db, monkeypatch):
    from app.routers import wb_reports_bff as reports
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    settings(tax_db)
    before = legacy_finance_tax_revision(1)
    save(tax_db, 2026, [6, 6, 6, 10])
    assert legacy_finance_tax_revision(1) != before
    writes = []
    monkeypatch.setattr(reports, "save_source_cache", lambda *args, **kwargs: writes.append(args))

    @tax.tax_consistent_report
    def outdated_job(organization_id):
        save(tax_db, 2026, [6, 6, 6, 12])
        reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="pnl",
            date_from=date(2026, 10, 1), date_to=date(2026, 10, 1), group_by="sku", source="operational", report={"rows": []})

    with pytest.raises(tax.QuarterlyTaxConflict):
        outdated_job(1)
    assert writes == []


def test_pnl_abc_reuse_sources_and_recalculate_totals_after_save(tax_db, report_cache, monkeypatch):
    from app.platform.economics import wb_quarterly_tax as profile_tax
    planning = profile_tax.planning_tax_pct
    monkeypatch.setattr(profile_tax, 'planning_tax_pct', lambda values: planning(values, date(2026, 11, 1)))
    from app import wb_reports_sprint_d as reports
    import httpx
    def no_http(*args, **kwargs):
        raise AssertionError("A tax change must not call WB")
    monkeypatch.setattr(httpx.Client, "send", no_http)
    sources, stored = report_cache
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    settings(tax_db)
    sources.update(finance=_cache(_fact(200000, 2), {"2026-09-30": _fact(100000), "2026-10-01": _fact(100000)}),
                   ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    original = deepcopy(sources)
    args = (date(2026, 9, 30), date(2026, 10, 1), "sku", "preliminary", True, 1)
    assert reports.build_pnl_report(*args).rows[0].taxKopecks == 12000
    save(tax_db, 2026, [6, 6, 6, 10])
    pnl = reports.build_pnl_report(*args)
    abc = reports.build_abc_report(args[0], args[1], "sku", "", True, 1)
    assert pnl.rows[0].taxKopecks == abc.rows[0]["taxKopecks"] == 20000
    assert next(cost.amountKopecks for cost in pnl.manualCosts if cost.costId == "tax") == 20000
    assert pnl.rows[0].netProfitKopecks == pnl.totals.netProfitKopecks == abc.filteredSummary.profitKopecks
    assert sources == original
    assert stored


def test_both_week_periods_use_their_own_quarter(tax_db):
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    save(tax_db, 2026, [6, 6, 7.5, 10])
    previous = _resolve(_cache(_fact(100000)), "2026-09-24", "2026-09-30")
    selected = _resolve(_cache(_fact(100000)), "2026-10-01", "2026-10-07")
    assert previous["taxKopecks"] == 7500
    assert selected["taxKopecks"] == 10000


def test_legacy_settings_writer_cannot_overwrite_quarterly_settings(tax_db, monkeypatch):
    from app.repricer_persistence import store
    stale = settings(tax_db)
    save(tax_db, 2026, [6, 6, 6, 10])
    quarterly = tax.settings_in_session(tax_db, 1)[tax.SETTINGS_KEY]
    monkeypatch.setattr(store, "_run_db", lambda function: function(tax_db))
    monkeypatch.setattr(store, "_save_file_algorithm_settings", lambda *args: True)
    assert store.save_algorithm_settings(1, {**stale, "marker": "updated", tax.SETTINGS_KEY: {}})
    stored = tax.settings_in_session(tax_db, 1)
    assert stored[tax.SETTINGS_KEY] == quarterly
    assert stored["marker"] == "updated"


def test_planning_settings_select_quarter_without_changing_cogs_or_other_expenses(tax_db, monkeypatch):
    from app import repricer_bff as repricer
    from app.wb_reports_sprint_d import _sku_settings_from_state
    _policy(tax_db, "2020-01-01T00:00:00", 600)
    save(tax_db, datetime.now(tax.MOSCOW).year, [1, 2, 3, 4])
    payload = {**repricer.DEFAULT_ALGORITHM_SETTINGS, **tax.settings_in_session(tax_db, 1)}
    monkeypatch.setattr(repricer, "ALGORITHM_SETTINGS_STATE", payload)
    current = datetime.now(tax.MOSCOW).date()
    expected = (current.month - 1) // 3 + 1
    before = repricer._sku_cost_settings("F_SYNTHETIC", use_demo_data=False)
    assert before["taxPct"] == expected
    assert _sku_settings_from_state("F_SYNTHETIC", runtime={}, algorithm=payload, effective_on=current)["taxPct"] == expected
    monkeypatch.setattr(repricer, "ALGORITHM_SETTINGS_STATE", {key: value for key, value in payload.items() if key != tax.SETTINGS_KEY})
    previous = repricer._sku_cost_settings("F_SYNTHETIC", use_demo_data=False)
    assert {key: value for key, value in before.items() if key != "taxPct"} == {key: value for key, value in previous.items() if key != "taxPct"}


def test_postgresql_json_cas_uses_semantic_jsonb_equality():
    from types import SimpleNamespace
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    query = select(WbRepricerAlgorithmSettingsRow.organization_id).where(tax.settings_match(session, {"taxPct": 0}))
    assert str(query.compile(dialect=postgresql.dialect())).count("JSONB") == 2


def test_canonical_product_summary_metadata_and_xlsx_keep_the_same_quarterly_tax(canonical_session, monkeypatch):
    import asyncio
    import io
    import json
    from xml.etree import ElementTree
    from zipfile import ZipFile
    from starlette.requests import Request
    from app.control_plane.auth import ActorContext
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    from app.platform.finance.service import FinanceService
    from app.routers.wb_pnl_export import export_pnl_table
    from tests.test_abc_pnl_service import add_dated_costs, add_dated_economics, settlement_rows, NOW, PERIOD
    monkeypatch.setattr("app.platform.economics.legacy_tax.get_session_factory", lambda: lambda: canonical_session)
    add_dated_costs(canonical_session)
    add_dated_economics(canonical_session)
    FinanceService(canonical_session, 1, now=lambda: NOW).ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    service = WbAbcPnlService(canonical_session, 1, now=lambda: NOW)
    original = service.get_page(31, PERIOD, limit=100, offset=0)
    save(canonical_session, 2026, [6, 6, 7.5, 6])
    page = service.get_page(31, PERIOD, limit=100, offset=0)
    row = page.items[0]
    assert row.tax_kopecks == page.summary.tax_kopecks == 6000
    assert original.items[0].tax_kopecks == 4800
    assert page.economics_revision != original.economics_revision and page.economics_revision < 2**53
    assert row.cogs_kopecks == original.items[0].cogs_kopecks
    assert row.finance_expenses_kopecks == original.items[0].finance_expenses_kopecks
    assert row.profit_before_ads_and_loyalty_kopecks == original.items[0].profit_before_ads_and_loyalty_kopecks - 1200
    body = json.dumps({"reportKind": "pnl", "sourceKind": "loaded-pnl", "dateFrom": str(PERIOD.date_from),
        "dateTo": str(PERIOD.date_to), "sourceState": "partial", "warning": "Synthetic fixture",
        "filterDescription": "All", "headers": ["Товар", "Налог", "Прибыль"],
        "rows": [["SKU", row.tax_kopecks / 100, row.profit_before_ads_and_loyalty_kopecks / 100]]}).encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({"type": "http", "method": "POST", "headers": []}, receive=receive)
    actor = ActorContext("test", "test", 1, "owner", frozenset({"finance:read"}))
    response = asyncio.run(export_pnl_table(request, actor))
    with ZipFile(io.BytesIO(response.body)) as workbook:
        xml = workbook.read("xl/worksheets/sheet1.xml").decode()
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells = {cell.attrib["r"]: float(cell.find("x:v", namespace).text)
             for cell in ElementTree.fromstring(xml).findall(".//x:c", namespace)
             if cell.find("x:v", namespace) is not None}
    assert cells["B2"] == 60
    assert cells["C2"] == 223


def test_profile_endpoints_enforce_scope_permission_and_validation(tax_db, monkeypatch):
    from app.control_plane.auth import ActorContext
    from app.routers import wb_repricer_bff as api
    from app.infra.db import get_db_session
    actor = ActorContext("test", "test", 1, "owner", frozenset({"settings:read", "settings:write"}))
    monkeypatch.setattr(api, "actor_from_request", lambda request: actor)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db_session] = lambda: tax_db
    settings(tax_db, rate=0)
    with TestClient(app) as client:
        view = client.get("/api/v1/wb-repricer/quarterly-tax?year=2026").json()
        assert view["rates"] == [0] * 4
        response = client.put("/api/v1/wb-repricer/quarterly-tax", json={"year": 2026, "rates": [0, 1, 2, 3.25], "revision": view["revision"]})
        assert response.status_code == 200 and response.json()["rates"][-1] == 3.25
        actor = ActorContext("test", "test", 2, "viewer", frozenset({"settings:read"}))
        assert client.get("/api/v1/wb-repricer/quarterly-tax?year=2026").json()["rates"] == [None] * 4
        assert client.put("/api/v1/wb-repricer/quarterly-tax", json={"year": 2026, "rates": [1] * 4, "revision": view["revision"]}).status_code == 403


def test_canonical_api_reports_concurrent_tax_change_as_retryable_conflict(canonical_api, monkeypatch):
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    from tests.test_abc_pnl_v2 import headers, params
    def superseded(*args, **kwargs):
        raise tax.QuarterlyTaxConflict("Налог изменился во время расчёта.")
    monkeypatch.setattr(WbAbcPnlService, "get_page", superseded)
    response = canonical_api.get("/api/v2/wb/reports/abc-pnl", params=params(), headers=headers("finance-all"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WB_TAX_SETTINGS_CHANGED"
