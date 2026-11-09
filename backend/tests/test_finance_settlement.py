from datetime import date

import pytest

from app.platform.finance.orm import WbFinanceOperationRow, WbFinanceSyncRunRow
from app.platform.finance.service import FinanceService, normalize_operation
from app.platform.period import Period
from tests.test_finance_pnl_rollup import NOW, PERIOD, session, settlement_rows


@pytest.mark.parametrize("fee,credit", [("10", -1000), ("-10", 1000), ("0", 0)])
def test_legacy_producer_marks_signed_withdrawal_schema(monkeypatch, fee, credit):
    from app.repricer_cache.store import FINANCE_SCHEMA_VERSION, finance_cache_uses_current_revenue_basis
    from tests.test_finance_fetch_completeness import fetch, row

    result, cursors = fetch(monkeypatch, [(200, [row(retailAmount="100", paymentSchedule=fee, saleDt="2026-08-17")]), (204, None)])
    assert FINANCE_SCHEMA_VERSION == "v6"
    assert result["financeSchemaVersion"] == FINANCE_SCHEMA_VERSION
    assert finance_cache_uses_current_revenue_basis(result)
    assert result["aggregates"]["101"]["additionalPaymentKopecks"] == credit
    assert result["dailyAggregates"]["2026-08-17"]["101"]["additionalPaymentKopecks"] == credit
    assert cursors == [0, 1]


@pytest.mark.parametrize("document,sign", [("Продажа", 1), ("Возврат", -1)])
def test_payable_is_preserved_with_document_direction(document, sign):
    raw = settlement_rows()[0] | {"docTypeName": document, "sellerOperName": document, "forPay": "863.00"}
    operation = normalize_operation(raw, 1, 31, PERIOD)

    assert getattr(operation, "payable_kopecks", None) == sign * 86_300
    assert operation.commission_kopecks == sign * 10_000  # Source evidence retained.


def test_pnl_reconciles_payable_in_exact_daily_and_raw_reads(session):
    rows = settlement_rows()
    rows[0]["forPay"] = "863.00"
    rows[1]["forPay"] = "172.60"
    service = FinanceService(session, 1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)

    fact = service.get_pnl_source(31, PERIOD).facts[0]
    assert fact.commission_kopecks == 9_760
    assert fact.payable_kopecks == 69_040
    assert (
        fact.revenue_kopecks - fact.commission_kopecks - fact.acquiring_kopecks
        == 69_040
    )
    day = service.get_pnl_source(
        31, Period(date(2026, 8, 20), date(2026, 8, 20))
    ).facts[0]
    assert (day.commission_kopecks, day.payable_kopecks) == (12_200, 86_300)

    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    run.is_pnl_rollup_materialized = run.is_daily_pnl_rollup_materialized = False
    session.commit()
    assert service.get_pnl_source(31, PERIOD).facts[0] == fact
    operation = session.query(WbFinanceOperationRow).filter_by(rrd_id=1).one()
    assert operation.commission_kopecks == 10_000
    assert operation.payable_kopecks == 86_300
    from app.platform.finance.service import FINANCE_FORMULA_VERSION
    assert snapshot.formula_version == FINANCE_FORMULA_VERSION


def test_missing_payable_remains_unknown_and_zero_is_evidence(session):
    rows = settlement_rows()
    rows[0]["forPay"] = "0"
    service = FinanceService(session, 1, now=lambda: NOW)
    service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)

    total = service.get_pnl_source(31, PERIOD).facts[0]
    assert getattr(total, "payable_kopecks", "absent") is None
    day = service.get_pnl_source(
        31, Period(date(2026, 8, 20), date(2026, 8, 20))
    ).facts[0]
    assert day.payable_kopecks == 0
    assert day.commission_kopecks == 98_500


def test_payable_correction_is_a_new_immutable_observation(session):
    rows = [settlement_rows()[0] | {"forPay": "863.00"}]
    service = FinanceService(session, 1, now=lambda: NOW)
    first = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    second = service.ingest_snapshot(
        31, PERIOD, [rows[0] | {"forPay": "860.00"}], observed_at=NOW
    )

    assert first.sync_run_id != second.sync_run_id
    assert session.query(WbFinanceOperationRow).count() == 2
    assert {row.payable_kopecks for row in session.query(WbFinanceOperationRow)} == {
        86_300,
        86_000,
    }
    retry = service.ingest_snapshot(
        31, PERIOD, [rows[0] | {"forPay": "860.00"}], observed_at=NOW
    )
    assert retry.sync_run_id == second.sync_run_id


@pytest.mark.parametrize("field", ["paymentSchedule", "payment_schedule"])
@pytest.mark.parametrize("fee,credit", [("10.00", -1_000), ("-10.00", 1_000)])
def test_withdraw_now_fee_reduces_profit_in_both_finance_paths(field, fee, credit):
    from app.repricer_bff import _finance_expense_diagnostic, _finance_row_costs

    raw = {"docTypeName": "", field: fee}
    canonical = normalize_operation(raw, 1, 31, PERIOD)
    legacy = _finance_row_costs(raw)

    assert canonical.additional_payment_kopecks == credit
    assert legacy["additionalPaymentKopecks"] == credit
    assert _finance_expense_diagnostic(legacy)["expensesWithoutTaxKopecks"] == -credit


@pytest.mark.parametrize("read_path", ["exact", "daily", "raw"])
@pytest.mark.parametrize(
    "scenario,payable,loyalty",
    [
        ("complete", 9_500, 0),
        ("mixed", None, None),
        ("zero", 0, 0),
        ("return", -9_500, 0),
        ("missing_cashback", 9_500, None),
        ("v2", None, -1_000),
    ],
)
def test_payable_reconciliation_does_not_credit_loyalty_compensation_twice(
    session, read_path, scenario, payable, loyalty
):
    common = {
        "cashbackAmount": "0",
        "cashbackCommissionChange": "0",
        "saleDt": "2026-08-20",
        "rrDate": "2026-08-20",
    }
    rows = [
        common
        | {
            "rrdId": 1,
            "nmId": 101,
            "docTypeName": "Продажа",
            "sellerOperName": "Продажа",
            "quantity": 1,
            "retailAmount": "100",
            "forPay": "85",
            "acquiringFee": "1",
            "cashbackDiscount": "0",
        },
        common
        | {
            "rrdId": 2,
            "nmId": 101,
            "docTypeName": "",
            "forPay": "10",
            "cashbackDiscount": "10",
        },
    ]
    if scenario == "mixed":
        rows.append(common | {"rrdId": 3, "nmId": 101, "cashbackDiscount": "0"})
    elif scenario == "zero":
        for row in rows:
            row["forPay"] = "0"
    elif scenario == "return":
        rows[0].update(docTypeName="Возврат", sellerOperName="Возврат")
        rows[1].update(forPay="-10", cashbackDiscount="-10")
    elif scenario == "missing_cashback":
        rows[1].pop("cashbackCommissionChange")
    elif scenario == "v2":
        for row in rows:
            row.pop("forPay")
    service = FinanceService(session, 1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    if scenario == "v2":
        run.formula_version = "wb-finance-v2"
    if read_path == "raw":
        run.is_pnl_rollup_materialized = run.is_daily_pnl_rollup_materialized = False
    session.commit()
    period = (
        Period(date(2026, 8, 20), date(2026, 8, 20)) if read_path == "daily" else PERIOD
    )
    result = service.get_pnl_source(31, period)
    if scenario == "v2":
        assert result.state == "missing" and result.facts == []
        return  # legacy contracts are never silently promoted to v4
    fact = result.facts[0]

    assert fact.cashback_discount_kopecks == (-1_000 if scenario == "return" else 1_000)
    assert fact.payable_kopecks == payable
    assert fact.loyalty_net_cost_kopecks == loyalty
    if payable is not None and loyalty is not None:
        assert (
            fact.revenue_kopecks
            - fact.commission_kopecks
            - fact.acquiring_kopecks
            - loyalty
            + fact.additional_payment_kopecks
            == payable
        )
