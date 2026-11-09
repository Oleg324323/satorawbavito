from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.platform.finance.service import (
    FinanceNormalizationError,
    normalize_operation,
    normalize_operations,
    snapshot_checksum,
)
from app.platform.period import Period

PERIOD = Period(date(2026, 8, 17), date(2026, 8, 23))


def raw_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "rrdId": 101,
        "reportId": 820347930,
        "reportType": 1,
        "nmId": 453200669,
        "vendorCode": "FBBT_1133",
        "docTypeName": "Продажа",
        "quantity": 1,
        "retailAmount": "1208.005",
        "ppvzSalesCommission": "100.10",
        "deliveryService": "53.76",
        "paidStorage": "1.01",
        "paidAcceptance": "2.02",
        "penalty": "3.03",
        "deduction": "4.04",
        "additionalPayment": "5.05",
        "paymentSchedule": "9.09",
        "acquiringFee": "6.06",
        "saleDt": "2026-08-20T10:00:00+03:00",
        "rrDate": "2026-08-23",
        "srid": "sale-1",
    }
    row.update(overrides)
    row.setdefault("sellerOperName", row.get("docTypeName"))
    return row


def test_normalizer_uses_exact_kopecks_and_explicit_report_semantics() -> None:
    main = normalize_operation(raw_row(), 2, 31, PERIOD)
    redemption_return = normalize_operation(
        raw_row(
            rrdId=102,
            reportId=820347933,
            reportType=2,
            docTypeName="Возврат",
            retailAmount="100.25",
            quantity=2,
        ),
        2,
        31,
        PERIOD,
    )

    assert main.source_identity == "rrd:101"
    assert main.report_type == "main"
    assert main.revenue_kopecks == 120_801
    assert main.units == 1
    assert main.sign == 1
    assert main.correction_at == datetime(2026, 8, 22, 21, tzinfo=timezone.utc)
    assert main.additional_payment_kopecks == -1_414  # Both adjustments are costs.
    assert redemption_return.report_type == "redemptions"
    assert redemption_return.revenue_kopecks == -10_025
    assert redemption_return.commission_kopecks == -10_010
    assert redemption_return.acquiring_kopecks == -606
    assert redemption_return.units == -2
    assert redemption_return.sign == -1


def test_late_correction_is_orthogonal_to_report_type() -> None:
    operation = normalize_operation(
        raw_row(
            rrdId=103,
            reportType=1,
            sellerOperName="Корректировка продаж",
            saleDt="2026-08-20",
            rrDate="2026-08-26",
        ),
        2,
        31,
        PERIOD,
    )

    assert operation.report_type == "main"
    assert operation.operation_kind == "correction"
    assert operation.is_late_correction is True


def test_fallback_identity_is_stable_and_conflicting_rrd_payload_is_rejected() -> None:
    without_rrd = raw_row(rrdId=None)
    first = normalize_operation(without_rrd, 2, 31, PERIOD)
    second = normalize_operation(
        dict(reversed(list(without_rrd.items()))), 2, 31, PERIOD
    )

    assert first.source_identity.startswith("fp:")
    assert first == second
    with pytest.raises(FinanceNormalizationError, match="conflicting payloads"):
        normalize_operations(
            [raw_row(), raw_row(retailAmount="1209.00")],
            organization_id=2,
            marketplace_account_id=31,
            period=PERIOD,
        )


def test_snapshot_checksum_is_order_independent_and_exact_duplicates_are_removed() -> (
    None
):
    first = raw_row(rrdId=1)
    second = raw_row(rrdId=2, reportType=99)

    left = normalize_operations(
        [first, second, first],
        organization_id=2,
        marketplace_account_id=31,
        period=PERIOD,
    )
    right = normalize_operations(
        [second, first],
        organization_id=2,
        marketplace_account_id=31,
        period=PERIOD,
    )

    assert len(left) == 2
    assert {operation.report_type for operation in left} == {"main", "unknown"}
    assert snapshot_checksum(left) == snapshot_checksum(right)


def test_loyalty_money_preserves_missing_zero_and_signed_source_values() -> None:
    missing = normalize_operation(raw_row(), 2, 31, PERIOD)
    explicit = normalize_operation(
        raw_row(
            cashback_amount="0",
            cashback_discount="12.34",
            cashback_commission_change="-1.25",
        ),
        2,
        31,
        PERIOD,
    )

    assert (
        missing.cashback_amount_kopecks,
        missing.cashback_discount_kopecks,
        missing.cashback_commission_change_kopecks,
    ) == (None, None, None)
    assert (
        explicit.cashback_amount_kopecks,
        explicit.cashback_discount_kopecks,
        explicit.cashback_commission_change_kopecks,
    ) == (0, 1_234, -125)
    assert missing.source_identity == explicit.source_identity
    assert missing.payload_checksum != explicit.payload_checksum
    assert missing.operation_id != explicit.operation_id


@pytest.mark.parametrize(
    "field",
    ("cashbackAmount", "cashbackDiscount", "cashbackCommissionChange"),
)
def test_loyalty_money_rejects_boolean_values(field: str) -> None:
    with pytest.raises(FinanceNormalizationError, match="invalid money"):
        normalize_operation(raw_row(**{field: True}), 2, 31, PERIOD)


@pytest.mark.parametrize("document", ["Продажа", "Возврат"])
@pytest.mark.parametrize("missing", [None, ""])
def test_trade_revenue_is_required(document: str, missing: object) -> None:
    with pytest.raises(FinanceNormalizationError, match="missing trade revenue"):
        normalize_operation(
            raw_row(docTypeName=document, retailAmount=missing), 2, 31, PERIOD
        )


@pytest.mark.parametrize("field", [
    "retailAmount", "ppvzSalesCommission", "acquiringFee", "deliveryService",
    "paidStorage", "paidAcceptance", "penalty", "deduction",
    "additionalPayment", "paymentSchedule", "forPay",
])
@pytest.mark.parametrize("value", [True, False])
def test_all_money_fields_reject_booleans(field: str, value: bool) -> None:
    with pytest.raises(FinanceNormalizationError, match="invalid money"):
        normalize_operation(raw_row(**{field: value}), 2, 31, PERIOD)


def test_zero_trade_revenue_and_non_trade_rows_remain_valid() -> None:
    assert normalize_operation(raw_row(retailAmount=0), 2, 31, PERIOD).revenue_kopecks == 0
    assert normalize_operation(
        raw_row(docTypeName="", retailAmount=None, paidStorage="12.34"), 2, 31, PERIOD
    ).storage_kopecks == 1_234
