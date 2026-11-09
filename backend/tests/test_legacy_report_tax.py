from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app import wb_reports_sprint_d as reports
from app.routers import wb_reports_bff as bff
from tests.test_legacy_finance_tax import _cache, _fact, _policy, tax_db  # noqa: F401
from vella_wb_19_05.models import ManualCost


@pytest.fixture
def report_cache(monkeypatch, tax_db):
    from app.platform.economics.costs import CostsService
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=0,
        value_state='configured', effective_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        source='fixture', source_reference='explicit-zero-cost', evidence_status='dated')
    sources, stored = {}, {}
    def read_period(org, prefix, start, end, **kwargs):
        value = deepcopy(sources.get(prefix, {}))
        if prefix == "finance" and value:
            from app.repricer_page_finance import VERSION, REALIZATION_VERSION
            value.update(projectionVersion=VERSION, realizationVersion=REALIZATION_VERSION,
                realizationDateBasis="rrDate", realizationMoneyBasis="retailAmount")
            value.setdefault("sourceComplete", True)
            value.setdefault("missingDates", [])
            value["reportMetricVersion"] = "wb-product-report-v3"
            for fact in value.get("aggregates", {}).values():
                fact.setdefault("financeAdSpendAuthoritative", True)
            value["financialAdvertisingComplete"] = all(f.get("financeAdSpendAuthoritative") for f in value.get("aggregates", {}).values())
            # Tests explicitly providing legacy page counts exercise absent
            # date metadata too; do not repair that scenario in the fixture.
            if "pagesLoaded" not in value:
                value.setdefault("dateFrom", str(start))
                value.setdefault("dateTo", str(end))
        return value
    monkeypatch.setattr(reports, "_period_cache", read_period)
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **_kwargs: deepcopy(stored.get((org, key))))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: stored.update({(org, key): {**deepcopy(value), "fetchedAt": datetime.now(timezone.utc).isoformat()}}))
    monkeypatch.setattr(reports, "list_cached_goods", lambda org: [{"nmID": 123, "vendorCode": "TEST", "brand": "Synthetic"}])
    monkeypatch.setattr(reports, "load_runtime_state", lambda org: {"skuSettingsOverrides": {"TEST": {"cogsKopecks": 0}}})
    monkeypatch.setattr(reports, "load_algorithm_settings", lambda org: {"taxPct": 99, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0})
    monkeypatch.setattr(reports, "list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    return sources, stored


@pytest.mark.parametrize("case,tax,profit", [
    ("unconfirmed_august", None, None), ("september", 6375, 78625),
    ("two_rates", 13500, 186500), ("zero", 0, 0), ("returns", -4, -56),
])
@pytest.mark.parametrize("group", ["sku", "brand"])
def test_legacy_abc_pnl_share_dated_tax_and_keep_unknown_totals(tax_db, report_cache, case, tax, profit, group):
    sources, _stored = report_cache
    _policy(tax_db, "2026-08-01T00:00:00", 600, confirmed=case == "two_rates", reference="august")
    _policy(tax_db, "2026-08-31T21:00:00", 750)
    start = date(2026, 8, 31) if case in {"unconfirmed_august", "two_rates"} else date(2026, 9, 1)
    end = date(2026, 9, 1)
    if case in {"unconfirmed_august", "two_rates"}:
        finance = _cache(_fact(200000, 2), {"2026-08-31": _fact(100000), "2026-09-01": _fact(100000)})
    else:
        fact = _fact(0 if case == "zero" else -60 if case == "returns" else 85000,
                     0 if case in {"zero", "returns"} else 1, 1 if case == "returns" else 0)
        finance = _cache(fact, {"2026-09-01": fact})
    sources.update(finance=finance, ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    original = deepcopy(finance)
    abc = reports.build_abc_report(start, end, group, "", True, 1)
    pnl = reports.build_pnl_report(start, end, group, "preliminary", True, 1)
    assert abc.rows[0]["taxKopecks"] == tax
    assert abc.rows[0]["netTotalKopecks"] == abc.filteredSummary.profitKopecks == profit
    assert pnl.rows[0].taxKopecks == tax
    assert pnl.rows[0].netProfitKopecks == pnl.totals.netProfitKopecks == profit
    assert next(cost.amountKopecks for cost in pnl.manualCosts if cost.costId == "tax") == tax
    if tax is None:
        assert abc.rows[0]["marginPct"] is None
        assert pnl.rows[0].marginPct is pnl.totals.marginPct is None
        mapped = bff._map_pnl_to_report_response(pnl, {"from": str(start), "to": str(end)})
        assert [row["value"] for row in mapped["kpis"]][1:] == ["—", "—"]
        assert mapped["chart"]["points"] == []
    assert sources["finance"] == original


@pytest.mark.parametrize("state,fact,net,expected", [
    ("missing", None, 1000, None), ("configured", None, 1000, None),
    ("configured", 0, 1000, 0), ("configured", 2500, 1000, 2500),
    (None, None, 1000, 1000),
])
def test_abc_fallback_and_week_do_not_replace_factual_null_or_zero(state, fact, net, expected):
    row = bff._repricer_row_to_abc_row({"meta": {"articleId": "TEST", "nmId": 123}, "analytics": {
        "factTaxState": state, "factTaxReason": "tax_policy_unconfirmed" if state == "missing" else None,
        "factNetProfitKopecks": fact, "netProfitKopecks": net, "taxKopecks": None,
        "marginPct": 99, "marginKopecks": 9900, "revenueKopecks": 10000,
    }})
    assert row["netTotalKopecks"] == expected
    assert row["marginPct"] == (None if expected is None else expected / 100 if state else 99)
    enriched = bff._week_rows_with_period_stats([row], {"123": {"ordersUnits": 1, "salesUnits": 1, "revenueKopecks": 10000}})[0]
    assert enriched["netTotalKopecks"] == expected
    week = bff._week_rows_from_abc_rows([enriched], [row])[0]
    assert week["profit"]["kopecks"] == expected
    assert week["profit"]["deltaPct"] == (0 if expected else None)


def test_pnl_cache_rebuilds_after_dated_policy_change(tax_db, report_cache):
    sources, _stored = report_cache
    _policy(tax_db, "2026-08-31T21:00:00", 750)
    sources.update(finance=_cache(_fact(100000)), ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    args = (date(2026, 9, 1), date(2026, 9, 1), "sku", "preliminary", True, 1)
    assert reports.build_pnl_report(*args).rows[0].taxKopecks == 7500
    _policy(tax_db, "2026-08-31T21:00:00", 900, reference="corrected-confirmation")
    assert reports.build_pnl_report(*args).rows[0].taxKopecks == 9000


def test_pnl_cost_update_rebuilds_from_database_without_tax_or_marketplace_reload(tax_db, report_cache):
    from app.platform.economics.costs import CostsService
    sources, _stored = report_cache
    sources.update(finance=_cache(_fact(200000), {'2026-09-01': _fact(200000)}))
    args = (date(2026, 9, 1), date(2026, 9, 1), 'sku', 'preliminary', True, 1)
    first = reports.build_pnl_report(*args)
    assert first.rows[0].cogsKopecks == 0
    assert 'WB_PNL_DATED_COST_MISSING' not in first.blockerIds
    assert first.rows[0].taxKopecks is first.rows[0].netProfitKopecks is None
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=80000,
        value_state='configured', effective_from=datetime(2021, 1, 1, tzinfo=timezone.utc),
        source='fixture', source_reference='corrected-cost', evidence_status='dated')
    updated = reports.build_pnl_report(*args)
    assert updated.rows[0].cogsKopecks == 80000
    assert updated.rows[0].taxKopecks is updated.rows[0].netProfitKopecks is None
    assert 'WB_PNL_DATED_COST_MISSING' not in updated.blockerIds


@pytest.mark.parametrize('source,expected', [('user-example', 80000), ('unverified-import', None)])
def test_only_explicit_user_examples_produce_provisional_math(tax_db, report_cache, source, expected):
    from app.platform.economics.costs import CostsService
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=80000, value_state='assumed',
        effective_from=datetime(2021, 1, 1, tzinfo=timezone.utc), source=source,
        source_reference='example', evidence_status='dated')
    sources.update(finance=_cache(_fact(200000), {'2026-09-01': _fact(200000)}),
                   ads={'aggregates': {'123': {'adSpendKopecks': 0}}})
    args = (date(2026, 9, 1), date(2026, 9, 1))
    pnl = reports.build_pnl_report(*args, 'sku', 'final', True, 1)
    abc = reports.build_abc_report(*args, 'sku', '', True, 1)
    assert pnl.rows[0].cogsKopecks == expected
    assert pnl.rows[0].taxKopecks == 44000
    assert pnl.rows[0].netProfitKopecks == (76000 if expected else None)
    assert abc.rows[0]['netTotalKopecks'] == pnl.rows[0].netProfitKopecks
    assert pnl.reportState != 'final'
    if expected:
        assert 'WB_USER_EXAMPLE_COSTS' in pnl.blockerIds
        assert 'WB_USER_EXAMPLE_COSTS' in abc.blockerIds
        mapped = bff._map_pnl_to_report_response(pnl, {'from': '2026-09-01', 'to': '2026-09-01'},
            {'status': 'disabled'}, finance_allowed=True)
        assert mapped['rows'][0]['netProfitKopecks'] == 76000
        assert mapped['financialConfirmationStatus'] == 'pending'
        assert 'ONE_C_DISABLED' in mapped['blockerIds']
        assert 'не фактическая' in mapped['warning']
        restricted = bff._map_pnl_to_report_response(pnl, {'from': '2026-09-01', 'to': '2026-09-01'},
            {'status': 'disabled'}, finance_allowed=False)
        assert restricted['rows'][0]['netProfitKopecks'] is None
        assert restricted['rows'][0]['overheadKopecks'] is None
        assert restricted['rows'][0]['marginPct'] is None
        assert restricted['chart']['points'] == []
        assert restricted['kpis'][1]['value'] == restricted['kpis'][2]['value'] == '—'


@pytest.mark.parametrize('cash_status', ['not_loaded', 'pending', 'processing', 'failed'])
@pytest.mark.parametrize('finance_allowed', [True, False])
def test_unloaded_operational_costs_never_confirm_final_profit(tax_db, report_cache, cash_status, finance_allowed):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    sources.update(finance=_cache(_fact(200000)), ads={'aggregates': {'123': {'adSpendKopecks': 0}}})
    pnl = reports.build_pnl_report(date(2026, 9, 1), date(2026, 9, 1), 'sku', 'final', True, 1)
    mapped = bff._map_pnl_to_report_response(pnl, {'from': '2026-09-01', 'to': '2026-09-01'},
        {'status': cash_status}, finance_allowed=finance_allowed)
    assert mapped['financialConfirmationStatus'] == 'pending'
    assert mapped['rows'][0]['netProfitKopecks'] is None
    assert mapped['rows'][0]['marginPct'] is None
    assert mapped['chart']['points'] == []
    assert mapped['kpis'][1]['value'] == mapped['kpis'][2]['value'] == '—'
    assert mapped['warning']


@pytest.mark.parametrize('authoritative,ads_present', [(True, True), (False, True), (True, False)])
def test_abc_pnl_share_signed_fees_and_authoritative_advertising(tax_db, report_cache, authoritative, ads_present):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    fact = {**_fact(200000), 'reportedCommissionRows': 1, 'commissionKopecks': -1180,
            'logisticsKopecks': 10000, 'storageKopecks': -500, 'acquiringKopecks': 1500,
            'deductionKopecks': 200, 'additionalPaymentKopecks': 100,
            'adSpendKopecks': 6000, 'financeAdSpendAuthoritative': authoritative}
    sources.update(finance=_cache(fact, {'2026-09-01': fact}),
                   ads={'aggregates': {'123': {'adSpendKopecks': 2000}}} if ads_present else {})
    args = (date(2026, 9, 1), date(2026, 9, 1))
    p = reports.build_pnl_report(*args, 'sku', 'preliminary', True, 1)
    a = reports.build_abc_report(*args, 'sku', '', True, 1)
    row = p.rows[0]
    assert row.adSpendKopecks == (6000 if authoritative else None)
    assert row.commissionKopecks == -1180
    assert row.storageKopecks == -500
    assert row.overheadKopecks == 1600
    assert row.netProfitKopecks == a.rows[0]['netTotalKopecks']
    assert a.filteredSummary.profitKopecks == p.totals.netProfitKopecks
    assert a.rows[0]['adSpendSource'] == ('wb_finance_report' if authoritative else None)
    if authoritative:
        assert row.netProfitKopecks == row.revenueKopecks - sum(getattr(row, key) for key in (
            'cogsKopecks', 'commissionKopecks', 'logisticsKopecks', 'storageKopecks',
            'adSpendKopecks', 'taxKopecks', 'overheadKopecks'))
    else:
        assert row.netProfitKopecks is None  # campaign spend cannot fill missing Finance attribution
    from app.contracts.vella_wb_19_05_generated import PnlReportResponse
    TypeAdapter(PnlReportResponse).validate_python(p.model_dump(mode='json'))


@pytest.mark.parametrize('evidence,expected', [('complete', 0), ('partial', None), ('undated', None)])
def test_abc_no_settlement_requires_complete_exact_ledger(tax_db, report_cache, monkeypatch, evidence, expected):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    monkeypatch.setattr(reports, 'list_cached_goods', lambda org: [
        {'nmID': 123, 'vendorCode': 'TEST'}, {'nmID': 456, 'vendorCode': 'NO-SALES'}])
    finance = _cache(_fact(200000), {'2026-09-01': _fact(200000)})
    finance.update(pagesLoaded=1, rowsCount=1, sourceComplete=evidence != 'partial')
    if evidence != 'undated':
        finance.update(dateFrom='2026-09-01', dateTo='2026-09-01')
    sources.update(finance=finance, ads={'aggregates': {}})
    report = reports.build_abc_report(date(2026, 9, 1), date(2026, 9, 1), 'sku', '', True, 1)
    row = next(r for r in report.rows if r['nmId'] == 456)
    assert row['netTotalKopecks'] == expected
    assert (report.filteredSummary.profitKopecks is not None) == (evidence == 'complete')


def test_partial_finance_with_fully_known_rows_cannot_be_final(tax_db, report_cache):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    finance = _cache(_fact(200000), {'2026-09-01': _fact(200000)})
    finance['sourceComplete'] = False
    sources.update(finance=finance, ads={'aggregates': {'123': {'adSpendKopecks': 0}}})
    args = (date(2026, 9, 1), date(2026, 9, 1))
    pnl = reports.build_pnl_report(*args, 'sku', 'final', True, 1)
    abc = reports.build_abc_report(*args, 'sku', '', True, 1)
    assert pnl.reportState == 'preliminary'
    assert pnl.sourceStatus == abc.sourceStatus == 'partial'
    assert 'WB_FINANCE_SOURCE_INCOMPLETE' in pnl.blockerIds
    assert 'WB_FINANCE_SOURCE_INCOMPLETE' in abc.blockerIds


def test_all_history_22_percent_includes_delayed_operations_of_exact_report(tax_db, report_cache):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history-confirmation')
    finance = _cache(_fact(100000, 2), {'2026-08-28': _fact(60000), '2026-09-01': _fact(40000)})
    finance.update(dateFrom='2026-09-01', dateTo='2026-09-01')
    sources.update(finance=finance, ads={'aggregates': {'123': {'adSpendKopecks': 0}}})
    pnl = reports.build_pnl_report(date(2026, 9, 1), date(2026, 9, 1), 'sku', 'preliminary', True, 1)
    assert pnl.rows[0].taxKopecks == 22000
    assert pnl.rows[0].netProfitKopecks == 78000


@pytest.mark.parametrize('complete', [True, False])
def test_pnl_keeps_ads_only_products_and_never_guesses_missing_sales(tax_db, report_cache, complete):
    sources, _ = report_cache
    _policy(tax_db, '1970-01-01T00:00:00', 2200, reference='all-history')
    finance = _cache(_fact(200000), {'2026-09-01': _fact(200000)})
    finance.update(pagesLoaded=1, rowsCount=1, sourceComplete=complete,
                   dateFrom='2026-09-01', dateTo='2026-09-01')
    finance['aggregates']['456'] = {**_fact(0, 0), 'adSpendKopecks': 5000, 'financeAdSpendAuthoritative': True}
    finance['dailyAggregates']['2026-09-01']['456'] = dict(finance['aggregates']['456'])
    sources.update(finance=finance, ads={'aggregates': {'456': {'adSpendKopecks': 5000}}})
    args = (date(2026, 9, 1), date(2026, 9, 1))
    pnl = reports.build_pnl_report(*args, 'sku', 'preliminary', True, 1)
    abc = reports.build_abc_report(*args, 'sku', '', True, 1)
    row = next(r for r in pnl.rows if r.rowId == 'nm-456')
    assert row.adSpendKopecks == 5000
    assert row.netProfitKopecks is None  # no cost/tax configuration for unmatched nmId 456
    assert row.taxKopecks is None
    assert pnl.totals.netProfitKopecks == abc.filteredSummary.profitKopecks


def test_signed_return_tax_survives_runtime_response_model(tax_db, report_cache, monkeypatch):
    from app.routers import wb_reports_sprint_d as router
    sources, _stored = report_cache
    _policy(tax_db, "2026-08-31T21:00:00", 750)
    sources.update(finance=_cache(_fact(-60, 0, 1)), ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    report = reports.build_pnl_report(date(2026, 9, 1), date(2026, 9, 1), "sku", "preliminary", True, 1)
    monkeypatch.setattr(router, "build_pnl_report", lambda **kwargs: report)
    monkeypatch.setattr(router, "actor_from_request", lambda request: SimpleNamespace(organization_id=1))
    monkeypatch.setattr(router, "assert_permission_or_audit", lambda **kwargs: None)
    monkeypatch.setattr(router, "record_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(router, "has_permission", lambda *args: True)
    api = FastAPI()
    api.include_router(router.router)
    payload = TestClient(api).get("/api/v1/wb-reports/pnl").json()
    assert payload["rows"][0]["taxKopecks"] == -4
    assert payload["rows"][0]["revenueKopecks"] == payload["totals"]["revenueKopecks"] == -60
    assert payload["rows"][0]["marginPct"] is payload["totals"]["marginPct"] is None
    assert payload["totals"]["netProfitKopecks"] == -56
    assert next(item["amountKopecks"] for item in payload["manualCosts"] if item["costId"] == "tax") == -4
    with pytest.raises(ValidationError):
        ManualCost(costId="unverified-manual", label="Other", amountKopecks=-4, allocationBase="sku", sourceStatus="fresh", blockerIds=[])


def test_generated_contract_allows_signed_return_revenue_and_tax_only(tax_db, report_cache):
    from app.contracts.vella_wb_19_05_generated import PnlReportResponse
    sources, _stored = report_cache
    _policy(tax_db, "2026-08-31T21:00:00", 750)
    sources.update(finance=_cache(_fact(-60, 0, 1)), ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    payload = reports.build_pnl_report(date(2026, 9, 1), date(2026, 9, 1), "sku", "preliminary", True, 1).model_dump(mode="json")
    contract = TypeAdapter(PnlReportResponse)
    assert contract.validate_python(payload).rows[0].revenueKopecks == -60
    payload["manualCosts"][0].update(costId="unverified-manual", amountKopecks=-4)
    with pytest.raises(ValidationError):
        contract.validate_python(payload)


@pytest.mark.parametrize("transient", [True, False])
def test_pnl_tax_resolver_failure_retries_but_unconfirmed_policy_is_cached(report_cache, monkeypatch, transient):
    sources, _stored = report_cache
    sources.update(finance=_cache(_fact(100000)), ads={"aggregates": {"123": {"adSpendKopecks": 0}}})
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "stable")
    calls = []
    reason = "tax_policy_unavailable" if transient else "tax_policy_unconfirmed"
    def taxes(*args, **kwargs):
        calls.append(True)
        return {"123": {"taxKopecks": None if len(calls) == 1 else 7500,
                        "factTaxState": "missing" if len(calls) == 1 else "configured",
                        "factTaxReason": reason if len(calls) == 1 else None}}
    monkeypatch.setattr(reports, "get_legacy_finance_taxes", taxes)
    args = (date(2026, 9, 1), date(2026, 9, 1), "sku", "preliminary", True, 1)
    first = reports.build_pnl_report(*args)
    assert first.rows[0].taxKopecks is None and reason in first.blockerIds
    assert reports.build_pnl_report(*args).rows[0].taxKopecks == (7500 if transient else None)
    assert len(calls) == (2 if transient else 1)


@pytest.mark.parametrize("report_id,previous", [("abc", False), ("pnl", False), ("week-over-week", False), ("week-over-week", True)])
def test_exact_report_cache_never_accepts_transient_tax_failure(monkeypatch, report_id, previous):
    monkeypatch.setattr(bff, "legacy_finance_tax_revision", lambda org: "stable")
    monkeypatch.setattr(bff, "_abc_economics_version", lambda org: "economics")
    monkeypatch.setattr(bff, "save_source_cache", lambda *args: None)
    missing = {"nmId": 123, "netTotalKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_policy_unavailable"}
    configured = {"nmId": 123, "netTotalKopecks": 0, "factTaxState": "configured"}
    rows = bff._week_rows_from_abc_rows([configured], [missing]) if previous else [missing]
    versions = {"abc": bff.ABC_REPORT_PAYLOAD_VERSION, "pnl": bff.PNL_REPORT_PAYLOAD_VERSION,
                "week-over-week": bff.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION}
    payload = {"rows": rows, "cacheVersion": versions[report_id],
               "blockerIds": ["tax_policy_unavailable"] if report_id == "pnl" else []}
    cache = bff._save_exact_report_payload_cache(organization_id=1, report_id=report_id,
        date_from=date(2026, 9, 1), date_to=date(2026, 9, 1), group_by="sku", source="operational",
        report=payload, finance_allowed=True)
    # Preserve the settings revision so the same-period partial snapshot can
    # be displayed, but never accept a transient calculation failure as fresh.
    assert cache["taxRevision"] == "stable"
    assert bff._report_tax_unavailable(cache["report"])
    assert not bff._report_payload_cache_is_usable(report_id, cache, organization_id=1)


@pytest.mark.parametrize("profit,expected", [(None, "—"), (0, "0"), (-56, "-56")])
def test_digest_preserves_nullable_shared_profit(profit, expected):
    result = bff._build_digest_payload(
        {"from": "2026-09-01", "to": "2026-09-01"},
        SimpleNamespace(orders=[], sales=[], stocks=[], source_status="cached"),
        SimpleNamespace(rows=[], totals={}, source_status="cached", blocker_ids=[]),
        SimpleNamespace(rows=[], blockerIds=[]),
        report_summary={"marginKopecks": profit, "factTaxState": "missing" if profit is None else "configured"},
    )
    assert next(kpi["value"] for kpi in result["kpis"] if kpi["id"] == "margin_profit") == expected


def test_raw_week_snapshot_has_no_complete_profit_basis():
    snapshot = SimpleNamespace(revenue_by_nm_kopecks={123: 100000}, seller_payout_by_nm_kopecks={123: 80000},
                               commission_cost_by_nm_kopecks={123: 20000})
    assert bff._profit_by_nm(snapshot, {})[123] == {"profit": None, "margin": None}


def test_abc_pnl_share_report_revenue_and_commission_basis(tax_db, report_cache):
    from app.repricer_page_finance import project_realization_operations
    from tests.test_repricer_page_finance import operation
    sources, _ = report_cache
    _policy(tax_db, "2026-08-31T21:00:00", 600)
    day = date(2026, 9, 29)
    finance = project_realization_operations([operation(nmId=123)], day, day)
    finance.update(sourceComplete=True, dateFrom=str(day), dateTo=str(day),
                   revenueBasis="retailPriceWithDisc*quantity", financeSchemaVersion="v6")
    sources.update(finance=finance, ads=dict(aggregates={"123": dict(adSpendKopecks=0)}))
    abc = reports.build_abc_report(day, day, "sku", "", True, 1)
    pnl = reports.build_pnl_report(day, day, "sku", "preliminary", True, 1)
    row = abc.rows[0]
    assert row["salesKopecks"] == pnl.rows[0].revenueKopecks == 5001
    assert row["taxKopecks"] == pnl.rows[0].taxKopecks == 300
    assert row["netTotalKopecks"] == pnl.rows[0].netProfitKopecks == 3701
    assert row["grossSalesKopecks"] == 5001


@pytest.mark.parametrize("reasons", [("tax_policy_unavailable", "tax_policy_unconfirmed"), ("tax_policy_unconfirmed", "tax_policy_unavailable")])
def test_grouped_abc_preserves_transient_tax_failure(reasons):
    rows = [{"brand": "Synthetic", "taxKopecks": None, "netTotalKopecks": None,
             "factTaxState": "missing", "factTaxReason": reason} for reason in reasons]
    grouped = reports._abc_group_rows(rows, "brand")
    assert len(grouped) == 1 and grouped[0]["factTaxReason"] == "tax_policy_unavailable"
    assert bff._report_tax_unavailable({"rows": grouped})


@pytest.mark.parametrize("sales,returns,revenue,expected", [(4, 1, 85000, 3), (0, 1, -60, -1), (1, 1, 0, 0)])
def test_repricer_abc_fallback_sales_are_net_returns(sales, returns, revenue, expected):
    row = bff._repricer_row_to_abc_row({"analytics": {"salesUnits": sales, "returnsUnits": returns,
        "sellerRevenueKopecks": revenue, "revenueKopecks": 100000, "factTaxState": "configured", "factNetProfitKopecks": 0}})
    assert row["salesComposite"]["units"] == expected
    assert row["salesComposite"]["kopecks"] == revenue
    assert row["marginPct"] == (0 if revenue > 0 else None)
