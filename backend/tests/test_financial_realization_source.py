"""Synthetic Finance API contract tests. No credentials or marketplace traffic."""
from copy import deepcopy
from datetime import date, datetime

import pytest
from fastapi import HTTPException

from app import repricer_bff as bff, repricer_page_finance as page
from app.wb_api.client import FakeWbApiClient, RateLimitedWbApiClient
from app.wb_api.financial_reports import (
    CORE_FIELDS, DETAIL_FIELDS, DETAIL_PATH, LIST_PATH, SOURCE_VERSION,
    SUMMARY_MONEY_FIELDS, fetch_report_list, raw_source_compatible, summarize_reports,
)
from tests.test_repricer_page_finance import operation

START, END = date(2026, 9, 29), date(2026, 10, 5)


def report(identity, **values):
    return {"reportId": identity, "reportType": 1, "currency": "RUB",
            "dateFrom": str(START), "dateTo": str(END),
            **dict.fromkeys(SUMMARY_MONEY_FIELDS, "0"), **values}


def test_report_list_paginates_both_types_and_month_split_without_first_row_substitution():
    rows = [report(1, retailAmountSum="100.01", dateTo="2026-09-30"),
            report(2, retailAmountSum="50.02", dateFrom="2026-10-01"),
            report(3, retailAmountSum="-3.01", reportType=2)]
    provider = FakeWbApiClient({LIST_PATH: {"data": rows}})
    result = fetch_report_list(RateLimitedWbApiClient(inner=provider), START, END, limit=2)
    assert result == rows
    assert [req.jsonBody["offset"] for req in provider.requests] == [0, 2]
    assert {req.jsonBody["period"] for req in provider.requests} == {"weekly"}
    assert summarize_reports(result)["RUB"]["totals"]["retailAmountSum"] == 14702
    assert summarize_reports(result)["RUB"]["reportIds"] == [1, 2, 3]


def test_summary_preserves_null_zero_currency_and_exact_large_identifiers():
    large = 9_007_199_254_740_993
    missing = report(large, retailAmountSum=None)
    result = summarize_reports([missing, report(2), missing, report(3, currency="BYN", retailAmountSum="7.12")])
    assert result["RUB"]["totals"]["retailAmountSum"] is None
    assert result["RUB"]["totals"]["penaltySum"] == 0
    assert result["RUB"]["reportIds"] == [large, 2]
    assert result["BYN"]["totals"]["retailAmountSum"] == 712


def test_changed_report_duplicate_is_not_silently_discarded():
    with pytest.raises(HTTPException, match="CONFLICTING_REPORTS"):
        summarize_reports([report(1), report(1, forPaySum="1")])


def test_short_list_page_is_not_a_detail_cursor_terminal_rule():
    provider = FakeWbApiClient({LIST_PATH: {"data": [report(1)]}})
    assert len(fetch_report_list(RateLimitedWbApiClient(inner=provider), START, END)) == 1
    assert len(provider.requests) == 1


def test_repeated_full_report_list_page_cannot_prove_complete_coverage(monkeypatch):
    monkeypatch.setattr(bff, "_request_or_raise_finance_report", lambda *a: [report(1)])
    with pytest.raises(HTTPException, match="LIST_STALLED"):
        fetch_report_list(object(), START, END, limit=1)


def test_wrong_exact_report_is_rejected_before_persistence(monkeypatch):
    monkeypatch.setattr(bff, "_request_or_raise_finance_report", lambda *a: [operation(reportId=88)])
    monkeypatch.setattr(bff, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(bff, "save_source_cache", lambda *a, **kw: pytest.fail("unverified report persisted"))
    with pytest.raises(HTTPException, match="REPORT_MISMATCH"):
        bff.fetch_finance_report_aggregates("complete", wb_token="synthetic",
            date_from=datetime(2026, 9, 29), date_to=datetime(2026, 10, 5),
            report_id=77, repricer_operations_org=2, _client=object())


def collect(monkeypatch, rows, *, report_id=None, period="daily", org=None):
    provider = FakeWbApiClient({DETAIL_PATH: {"data": rows}})
    monkeypatch.setattr(bff, "build_wb_finance_client", lambda *a, **kw: provider)
    return bff.fetch_finance_report_aggregates(
        "complete", wb_token="synthetic-test-credential", date_from=datetime.combine(START, datetime.min.time()),
        date_to=datetime.combine(END, datetime.min.time()), report_id=report_id,
        report_period=period, repricer_operations_org=org), provider


def test_exact_report_is_read_by_id_and_persisted_outside_daily_coverage(monkeypatch):
    saved = {}
    monkeypatch.setattr(bff, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(bff, "save_source_cache", lambda org, key, payload, **kw: saved.__setitem__(key, payload))
    monkeypatch.setattr("app.repricer_cache.store.save_source_cache", lambda org, key, payload, **kw: saved.__setitem__(key, payload))
    result, provider = collect(monkeypatch, [operation(reportId=77), operation(2, reportId=88)],
                               report_id=77, period="weekly", org=2)
    assert result["sourceScope"] == "report" and result["reportId"] == 77
    assert result["rowsCount"] == 1
    assert all(req.path == DETAIL_PATH + "/77" for req in provider.requests)
    assert all(not {"dateFrom", "dateTo", "period"} & req.jsonBody.keys() for req in provider.requests)
    assert not any(key.startswith(("repricer_finance_", "finance_")) for key in saved)
    raw = next(value for key, value in saved.items() if key.startswith("wb_realization_report_"))
    assert raw["reportId"] == 77 and raw["reportPeriod"] == "weekly"
    assert raw_source_compatible(raw) is False


def test_extended_fields_keep_existing_contract_and_source_metadata(monkeypatch):
    result, provider = collect(monkeypatch, [operation()])
    assert CORE_FIELDS <= set(provider.requests[0].jsonBody["fields"])
    assert {"vw", "vwNds", "rebillLogisticCost"} <= set(result["requestedFields"])
    assert result["aggregates"]["101"]["sellerRevenueKopecks"] == 10002
    assert result["sourceVersion"] == SOURCE_VERSION
    assert result["sourceScope"] == "period" and result["reportPeriod"] == "daily"


@pytest.mark.parametrize("currency", [None, "", "BYN", "USD"])
def test_unknown_or_foreign_currency_cannot_be_published_as_rubles(monkeypatch, currency):
    row = operation(); row["currency"] = currency
    with pytest.raises(HTTPException, match="CURRENCY_UNAVAILABLE"):
        collect(monkeypatch, [row])


@pytest.mark.parametrize("identity", [0, -1, True, 1.5, "01", "../prices", "١"])
def test_invalid_exact_report_never_reaches_provider(monkeypatch, identity):
    monkeypatch.setattr(bff, "build_wb_finance_client", lambda *a, **kw: pytest.fail("provider constructed"))
    with pytest.raises(HTTPException):
        bff.fetch_finance_report_aggregates("complete", date_from=datetime(2026, 10, 1), report_id=identity)


def test_raw_coverage_checks_required_fields_and_daily_representation():
    raw = dict(sourceComplete=True, operations=[operation()])
    assert raw_source_compatible(raw)
    for changes in (dict(requestedFields=["rrdId", "nmId"]), dict(reportPeriod="weekly"),
                    dict(reportId=77), dict(sourceComplete=False)):
        assert not raw_source_compatible(raw | changes)
    unknown = deepcopy(raw); unknown["operations"][0].pop("currency")
    assert not raw_source_compatible(unknown)
    assert raw_source_compatible(dict(sourceComplete=True, operations=[]))


def test_new_complete_receipt_reuses_database_without_decoding_raw_rows(monkeypatch):
    from app.repricer_cache import store
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [
        dict(sourceKey="current", dateFrom=str(START), dateTo=str(END))])
    monkeypatch.setattr(store, "get_source_cache_completion_flags", lambda *a: dict(
        sourceComplete=True, requestedFields=list(DETAIL_FIELDS), operationCount=100_000,
        sourceVersion=SOURCE_VERSION, reportPeriod="daily", currencies=["RUB"], availableFields=list(DETAIL_FIELDS)))
    monkeypatch.setattr(store, "get_source_cache", lambda *a, **kw: pytest.fail("unnecessary large raw read"))
    assert page.missing_page_ranges(2, "synthetic-test-credential", START, END) == []


def test_requested_field_mask_does_not_certify_a_missing_fee(monkeypatch):
    from app.repricer_cache import store
    saved = {}
    monkeypatch.setattr(store, "save_source_cache", lambda org, key, payload, **kw: saved.update(payload) or payload)
    row = operation()
    row.pop("penalty")
    page.save_operations(2, "synthetic", START, END, [row], requested_fields=DETAIL_FIELDS)
    assert "penalty" in saved["requestedFields"]
    assert "penalty" not in saved["availableFields"]
    assert not raw_source_compatible(saved)


def test_optional_operation_description_does_not_trigger_endless_reload():
    row = operation()
    row.pop("bonusTypeName")
    assert raw_source_compatible(dict(sourceComplete=True, operations=[row]))


@pytest.mark.parametrize("value", [None, False])
def test_unavailable_fee_is_not_a_confirmed_zero(monkeypatch, value):
    from app.repricer_cache import store
    saved = {}
    monkeypatch.setattr(store, "save_source_cache", lambda org, key, payload, **kw: saved.update(payload) or payload)
    row = operation(penalty=value)
    assert not raw_source_compatible(dict(sourceComplete=True, operations=[row]))
    page.save_operations(2, "synthetic", START, END, [row], requested_fields=DETAIL_FIELDS)
    assert not raw_source_compatible(saved)
    assert raw_source_compatible(dict(sourceComplete=True, operations=[operation(penalty=0)]))


def test_previous_derived_financial_schema_is_not_reused_as_current():
    from app.repricer_cache.store import FINANCE_REVENUE_BASIS, finance_cache_uses_current_revenue_basis
    assert not finance_cache_uses_current_revenue_basis(dict(revenueBasis=FINANCE_REVENUE_BASIS,
                                                            financeSchemaVersion="v5"))


@pytest.mark.parametrize("count", [False, True, 1])
def test_inconsistent_empty_receipt_is_not_complete(count):
    assert not raw_source_compatible(dict(sourceComplete=True, sourceVersion=SOURCE_VERSION,
                                         operations=[], operationCount=count))


def test_receipt_queries_only_small_metadata_and_isolated_account(monkeypatch):
    from datetime import timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.cabinet.orm import LkOrganizationRow
    from app.repricer_cache import store
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    LkOrganizationRow.__table__.create(engine)
    store.WbRepricerSourceCacheRow.__table__.create(engine)
    payload = dict(sourceComplete=True, requestedFields=list(DETAIL_FIELDS),
                   sourceVersion=SOURCE_VERSION, operationCount=1, currencies=["RUB"],
                   reportPeriod="daily", operations=[operation()], availableFields=list(CORE_FIELDS))
    with Session(engine) as session:
        session.add_all([LkOrganizationRow(organization_id=org, slug=f"synthetic-{org}", name="Synthetic")
                         for org in (2, 3)])
        session.flush()
        session.add(store.WbRepricerSourceCacheRow(organization_id=2, source_key="current",
                    payload=payload, fetched_at=datetime.now(timezone.utc)))
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        receipt = store.get_source_cache_completion_flags(2, "current")
        assert "operations" not in receipt and raw_source_compatible(receipt)
        assert store.get_source_cache_completion_flags(3, "current") == {}
    engine.dispose()


def test_weekly_and_daily_operations_are_never_added_together(monkeypatch):
    from app.cabinet import store as cabinet
    from app.repricer_cache import store
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda org: "synthetic")
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [
        dict(sourceKey=key, dateFrom=str(START), dateTo=str(END), fetchedAt=key)
        for key in ("daily", "weekly")])
    monkeypatch.setattr(store, "get_source_cache", lambda org, key, **kw: dict(
        sourceComplete=True, reportPeriod=key, operations=[operation()], fetchedAt=key))
    result = page.load_page_finance(2, START, END, projector=page.project_operations)
    assert result["sourceComplete"] is True
    assert result["aggregates"]["101"]["salesUnits"] == 1


def test_revenue_only_still_requests_currency_without_changing_money_basis(monkeypatch):
    provider = FakeWbApiClient({DETAIL_PATH: {"data": [operation()]}})
    monkeypatch.setattr(bff, "build_wb_finance_client", lambda *a, **kw: provider)
    result = bff.fetch_finance_report_aggregates("complete", date_from=datetime(2026, 9, 29),
                                               date_to=datetime(2026, 10, 5), revenue_only=True)
    assert "currency" in provider.requests[0].jsonBody["fields"]
    assert result["revenueOnly"] is True
    assert result["aggregates"]["101"]["sellerRevenueKopecks"] == 10002


def test_runtime_merchandise_returns_and_compensation_are_not_the_same_money(monkeypatch):
    from app.wb_api.reports_sources_runtime import _load_realization_rows
    sale = operation(reportId=77, ppvzSalesCommission="1.00", deliveryService="3.00")
    returned = sale | {"rrdId": 2, "docTypeName": "Возврат", "sellerOperName": "Возврат"}
    compensation = sale | {"rrdId": 3, "sellerOperName": "Компенсация товара", "retailAmount": "0",
                           "retailPriceWithDisc": "0", "forPay": "8.51", "ppvzSalesCommission": "0", "deliveryService": "0"}
    provider = FakeWbApiClient({DETAIL_PATH: {"data": [sale, returned, compensation]}})
    envelope, rows = _load_realization_rows(RateLimitedWbApiClient(inner=provider), date_from=START, date_to=END)
    assert envelope.ok
    assert sum(row.retail_amount_kopecks for row in rows) == 0
    assert sum(row.seller_payout_kopecks for row in rows) == 851
    assert sum(row.commission_kopecks for row in rows) == 0
    assert sum(row.logistics_kopecks for row in rows) == 600
    assert not any(req.method == "GET" for req in provider.requests)


def test_exact_report_read_allowlist_does_not_enable_writes_or_other_hosts():
    import importlib.util
    from pathlib import Path
    import httpx
    spec = importlib.util.spec_from_file_location("finance_read_guard", Path(__file__).parents[1] / "ops/load_local_wb_readonly.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    base = "https://finance-api.wildberries.ru"
    assert guard.allowed_request(httpx.Request("POST", base + DETAIL_PATH + "/77"))
    for method, url in (("DELETE", base + DETAIL_PATH + "/77"),
                        ("POST", base + DETAIL_PATH + "/0"),
                        ("POST", base + DETAIL_PATH + "/77/apply"),
                        ("POST", "https://example.com" + DETAIL_PATH + "/77"),
                        ("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task")):
        assert not guard.allowed_request(httpx.Request(method, url))


def test_loyalty_returns_cancel_charges_once_in_both_existing_projections():
    sale = operation(cashbackAmount="3.00", cashbackDiscount="2.00", cashbackCommissionChange="1.00")
    returned = sale | {"rrdId": 2, "docTypeName": "Возврат", "sellerOperName": "Возврат"}
    for result in (bff.aggregate_finance_report_rows([sale, returned],
                   date_from=datetime(2026, 9, 29), date_to=datetime(2026, 10, 5)),
                   page.project_operations([sale, returned], START, END)):
        row = result["aggregates"]["101"]
        assert row["cashbackAmountKopecks"] == 0
        assert row["cashbackDiscountKopecks"] == 0
        assert row["cashbackCommissionChangeKopecks"] == 0
        assert row["loyaltyCostKopecks"] == 0
        assert row["netSalesUnits"] == 0
