from datetime import date, datetime
import pytest
from app.repricer_page_finance import project_operations, repricer_finance_scope, page_projection
from app.repricer_bff import aggregate_finance_report_rows

START = date(2026, 9, 29)
END = date(2026, 10, 5)


def operation(identity=1, **fields):
    return dict(rrdId=identity, nmId=101, docTypeName="Продажа", sellerOperName="Продажа", currency="RUB",
                quantity=1, retailAmount="50.01", retailPriceWithDisc="100.02", forPay="40.01",
                acquiringFee="2.00", saleDt="2026-09-29T10:00:00Z", rrDate="2026-09-29",
                reportId=77, reportType=1, bonusTypeName="", retailPrice="100.02", vendorCode="synthetic",
                sku="synthetic-sku", srid=None, commissionPercent=None, ppvzSalesCommission="0",
                salePercent=None, spp=None, deliveryService="0", paidStorage="0", paidAcceptance="0",
                penalty="0", deduction="0", additionalPayment="0", paymentSchedule="0",
                cashbackAmount="0", cashbackDiscount="0", cashbackCommissionChange="0", acquiringPercent=None) | fields


def test_trade_commission_is_seller_basis_and_buyer_payment_is_preserved():
    result = project_operations([operation()], START, END)["aggregates"]["101"]
    assert result["buyerRevenueKopecks"] == 5001
    assert result["sellerRevenueKopecks"] == 10002
    assert result["commissionKopecks"] + result["acquiringKopecks"] == 6001
    assert result["payableKopecks"] == 4001
    assert result["settlementPayableConfirmed"] is True


def test_compensation_is_not_sold_units_and_not_commission():
    row = operation(2)
    row.update(sellerOperName="Добровольная компенсация при возврате", retailAmount=0,
               retailPriceWithDisc=0, forPay="8.51", acquiringFee=0)
    result = project_operations([operation(), row], START, END)
    a = result["aggregates"]["101"]
    assert a["salesUnits"] == 1
    assert a["payableKopecks"] == 4001
    assert a["additionalPaymentKopecks"] == 851
    assert a["commissionKopecks"] == 5801
    assert result["compensationKopecks"] == 851


def test_returns_are_signed_and_net_units_match():
    r = operation(2)
    r.update(docTypeName="Возврат", sellerOperName="Возврат")
    a = project_operations([operation(), r], START, END)["aggregates"]["101"]
    assert a["salesUnits"] == a["returnsUnits"] == 1
    assert a["netSalesUnits"] == a["sellerRevenueKopecks"] == a["payableKopecks"] == 0


def test_moscow_service_boundary_and_posting_storage_date():
    row = operation()
    row.update(saleDt="2026-09-28T21:00:00Z", rrDate="2026-09-28", deliveryService="3.01", paidStorage="7.02")
    a = project_operations([row], START, END)["aggregates"]["101"]
    assert a["salesUnits"] == 1
    assert a["logisticsKopecks"] == 301
    assert a["storageKopecks"] == 0


def test_deduplication_and_conflicting_observations():
    a = project_operations([operation(), operation()], START, END)["aggregates"]["101"]
    assert a["salesUnits"] == 1
    conflict = operation(); conflict["forPay"] = "1"
    with pytest.raises(ValueError, match="CONFLICTING"):
        project_operations([operation(), conflict], START, END)


def test_old_operation_not_in_range_even_if_posted_in_range():
    row = operation(); row["saleDt"] = "2026-09-28T20:59:59Z"
    assert project_operations([row], START, END)["aggregates"] == {}


def test_legacy_reports_unchanged_and_scope_does_not_leak():
    legacy = aggregate_finance_report_rows([operation()], date_from=datetime(2026,9,29), date_to=datetime(2026,10,5))
    assert legacy["aggregates"]["101"]["commissionKopecks"] == 800
    @repricer_finance_scope
    def scoped():
        assert page_projection.get()
        raise ValueError("test")
    with pytest.raises(ValueError):
        scoped()
    assert not page_projection.get()


def test_account_storage_is_retained_without_fake_product():
    row = operation(); row.update(nmId=0, docTypeName="", sellerOperName="Хранение",
                                  quantity=0, retailAmount=0, retailPriceWithDisc=0, forPay=0,
                                  acquiringFee=0, paidStorage="12.34")
    r = project_operations([row], START, END)
    assert r["aggregates"] == {}
    assert r["diagnostics"]["rawExpenseTotals"]["storageKopecks"] == 1234


def test_daily_basis_is_consistent_with_period_basis():
    r = project_operations([operation()], START, END)
    day = r["dailyAggregates"][str(START)]["101"]
    for key in ("buyerRevenueKopecks", "commissionKopecks", "commissionSource", "sellerRevenueKopecks", "settlementPayableConfirmed"):
        assert day[key] == r["aggregates"]["101"][key]


def test_unknown_adjustment_blocks_profit_without_calling_it_compensation():
    row = operation(); row.update(sellerOperName="Новая неизвестная операция", quantity=0,
                                  retailAmount=0, retailPriceWithDisc=0)
    r = project_operations([row], START, END)
    assert r["compensationKopecks"] == 0
    assert r["projectionBlockers"] == ["WB_FINANCE_UNCLASSIFIED_PAYABLE"]
    assert r["aggregates"]["101"]["settlementComponentsConfirmed"] is False


def test_complete_empty_new_observation_replaces_old_rows(monkeypatch):
    from app import repricer_page_finance as page
    from app.repricer_cache import store
    from app.cabinet import store as cabinet
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda org: "test-credential-not-real")
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [
        dict(sourceKey="old", dateFrom=str(START), dateTo=str(END), fetchedAt="2026-10-06T01:00:00Z"),
        dict(sourceKey="new", dateFrom=str(START), dateTo=str(END), fetchedAt="2026-10-06T02:00:00Z"),
    ])
    monkeypatch.setattr(store, "get_source_cache", lambda org, key, **kw: {
        "sourceComplete": True, "operations": [operation()] if key == "old" else [],
    })
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    r = page.load_page_finance(2, START, END)
    assert r["sourceComplete"] is True
    assert r["coveredDays"] == 7
    assert r["aggregates"] == {}


def test_finance_cursor_resume_then_new_refresh_starts_at_zero(monkeypatch):
    from app import repricer_bff as bff
    from app import repricer_page_finance as page
    from fastapi import HTTPException
    saved = {}; cursors = []; reads = 0
    monkeypatch.setattr(bff, "get_source_cache", lambda org, key, **kw: saved.get(key, {}))
    monkeypatch.setattr(bff, "save_source_cache", lambda org, key, value, **kw: saved.__setitem__(key, value))
    monkeypatch.setattr(page, "save_operations", lambda *a, **kw: None)
    monkeypatch.setattr(bff, "build_wb_finance_client", lambda *a, **kw: object())
    monkeypatch.setattr(bff, "RateLimitedWbApiClient", lambda **kw: object())
    def request(client, req):
        nonlocal reads
        reads += 1; cursors.append(req.jsonBody["rrdId"])
        if reads == 1:
            return [operation()]
        if reads == 2:
            raise HTTPException(429, "test rate limit")
        return []
    monkeypatch.setattr(bff, "_request_or_raise_finance_report", request)
    kw = dict(wb_token="test-credential-not-real", date_from=datetime(2026,9,29), date_to=datetime(2026,10,5), repricer_operations_org=2)
    with pytest.raises(HTTPException):
        bff.fetch_finance_report_aggregates("complete", **kw)
    assert len(next(iter(saved.values()))["operations"]) == 1
    result = bff.fetch_finance_report_aggregates("complete", **kw)
    assert result["sourceComplete"] is True
    assert result["aggregates"]["101"]["salesUnits"] == 1
    bff.fetch_finance_report_aggregates("complete", **kw)
    assert cursors == [0, 1, 1, 0]
