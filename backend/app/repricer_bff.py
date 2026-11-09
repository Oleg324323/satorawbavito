from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from itertools import count
from math import ceil, floor, isfinite
from typing import Any, Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.config import get_settings
from app.modules.wb_repricing_calculation import settings_minimum_price_kopecks
from app.repricer_cache.store import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION, get_source_cache, save_source_cache
from app.repricer_promotion_status import MEMBERSHIP_VERSION, product_id, promotion_membership
from app.wb_api.client import (
    FakeWbApiClient,
    RateLimitedWbApiClient,
    WbApiRequest,
    WbApiResponseEnvelope,
    build_wb_analytics_client,
    build_wb_ads_client,
    build_wb_client,
    build_wb_common_client,
    build_wb_content_client,
    build_wb_finance_client,
    build_wb_promotions_client,
    build_wb_statistics_client,
)
from app.wb_api.price_units import wb_goods_price_to_kopecks

logger = logging.getLogger(__name__)

_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

_PROMOTION_LIST_PAGE_SIZE = 100
_PROMOTION_NOMENCLATURES_PAGE_SIZE = 1000
_PROMOTION_DETAILS_BATCH_SIZE = 20
_PROMOTIONS_CALENDAR_MIN_INTERVAL_S = 1.1
_PROMOTIONS_CALENDAR_MAX_ATTEMPTS = 6
_FINANCE_REPORT_MIN_INTERVAL_S = 61.0
_FINANCE_REPORT_MAX_ATTEMPTS = 4
_FINANCE_REPORT_MAX_RETRY_AFTER_S = 120.0
_STATISTICS_REPORT_MIN_INTERVAL_S = 61.0
_STATISTICS_REPORT_MAX_ATTEMPTS = 4
_STATISTICS_REPORT_MAX_RETRY_AFTER_S = 180.0
_ADS_FULLSTATS_MIN_INTERVAL_S = 23.0
_ADS_FULLSTATS_MAX_ATTEMPTS = 8
_ADS_FULLSTATS_MAX_RETRY_AFTER_S = 300.0
_ADS_FULLSTATS_TRANSIENT_MAX_ATTEMPTS = 4
_SALES_FUNNEL_PRODUCTS_MIN_INTERVAL_S = 22.0
_SALES_FUNNEL_PRODUCTS_MAX_ATTEMPTS = 5
_SALES_FUNNEL_PRODUCTS_MAX_RETRY_AFTER_S = 180.0
_ADS_FULLSTATS_TRANSIENT_ERROR_MARKERS = (
    "sqlstate 40001",
    "conflict with recovery",
    "canceling statement",
    "service.getavgpositions failed",
    "code = internal",
)
_WB_TRANSPORT_MAX_ATTEMPTS = 3
_WB_TRANSPORT_RETRY_DELAYS_S = (2.0, 5.0)
_STOCK_REPORT_MAX_ATTEMPTS = 4
_STOCK_REPORT_RETRY_DELAYS_S = (15.0, 30.0, 60.0)
_STOCK_REPORT_MIN_INTERVAL_S = 65.0
_CATALOG_GOODS_MAX_ATTEMPTS = 2
_CATALOG_GOODS_DEFAULT_RETRY_AFTER_S = 65.0
_CATALOG_GOODS_MAX_RETRY_AFTER_S = 120.0
_CONTENT_CARDS_MAX_ATTEMPTS = 20
_CONTENT_CARDS_DEFAULT_RETRY_AFTER_S = 5.0
_CONTENT_CARDS_MAX_RETRY_AFTER_S = 120.0
_promotions_calendar_last_request_at = 0.0
_finance_report_last_request_at = 0.0
_statistics_report_last_request_at = 0.0
_ads_fullstats_last_request_at = 0.0
_sales_funnel_products_last_request_at = 0.0
_stock_report_lock = threading.Lock()
_stock_report_last_request_at = 0.0
_ads_fullstats_lock = threading.Lock()
_statistics_report_lock = threading.Lock()
_sales_funnel_products_lock = threading.Lock()
_FINANCE_ADJUSTMENT_WORDS = ("штраф", "компенса", "коррект", "возврат")
_FINANCE_DIAGNOSTIC_ROW_LIMIT = 5000
_FINANCE_DIAGNOSTIC_RAW_FIELDS = (
    "rrdId",
    "rrd_id",
    "nmId",
    "nmID",
    "vendorCode",
    "sku",
    "docTypeName",
    "sellerOperName",
    "bonusTypeName",
    "quantity",
    "retailAmount",
    "retailPrice",
    "retailPriceWithDisc",
    "commissionPercent",
    "ppvzSalesCommission",
    "rebillLogisticCost",
    "deliveryAmount",
    "deliveryService",
    "paidStorage",
    "paidAcceptance",
    "penalty",
    "deduction",
    "additionalPayment",
    "paymentSchedule",
    "cashbackAmount",
    "cashbackDiscount",
    "cashbackCommissionChange",
    "acquiringFee",
    "forPay",
    "rrDate",
    "saleDt",
    "srid",
)


class WbSalesFunnelDeferred(RuntimeError):
    def __init__(self, *, retry_after_seconds: float, message: str = "WB Sales Funnel long retry") -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def log_finance_adjustment(
    *,
    nm_id: int,
    rrd_id: Any,
    seller_oper_name: str,
    bonus_type_name: str,
    penalty_kopecks: int,
    deduction_kopecks: int,
    additional_payment_kopecks: int,
) -> None:
    logger.info(
        "WB finance adjustment row detected",
        extra={
            "wbFinanceAdjustment": {
                "nmId": nm_id,
                "rrdId": rrd_id,
                "sellerOperName": seller_oper_name,
                "bonusTypeName": bonus_type_name,
                "penaltyKopecks": penalty_kopecks,
                "deductionKopecks": deduction_kopecks,
                "additionalPaymentKopecks": additional_payment_kopecks,
            }
        },
    )


def _finance_raw_first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item.get(key) not in (None, ""):
            return item.get(key)
    return None


def _finance_diagnostic_raw_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in _FINANCE_DIAGNOSTIC_RAW_FIELDS
        if key in item and item.get(key) is not None
    }


def _finance_adjustment_reasons(
    *,
    seller_oper_name: str,
    bonus_type_name: str,
    penalty_kopecks: int,
    deduction_kopecks: int,
    additional_payment_kopecks: int,
) -> list[str]:
    reasons: list[str] = []
    if penalty_kopecks < 0:
        reasons.append("negative_penalty")
    if deduction_kopecks < 0:
        reasons.append("negative_deduction")
    if additional_payment_kopecks != 0:
        reasons.append("additional_payment")
    adjustment_text = f"{seller_oper_name} {bonus_type_name}".lower()
    matched_words = [word for word in _FINANCE_ADJUSTMENT_WORDS if word in adjustment_text]
    if matched_words:
        reasons.extend(f"text:{word}" for word in matched_words)
    return reasons


def _finance_diagnostic_row(
    item: dict[str, Any],
    *,
    nm_id: int,
    seller_oper_name: str,
    bonus_type_name: str,
    penalty_kopecks: int,
    deduction_kopecks: int,
    additional_payment_kopecks: int,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "nmId": nm_id,
        "rrdId": _finance_raw_first(item, "rrdId", "rrd_id"),
        "vendorCode": _finance_raw_first(item, "vendorCode", "vendor_code"),
        "sku": _finance_raw_first(item, "sku"),
        "rrDate": _finance_raw_first(item, "rrDate", "rr_date"),
        "saleDt": _finance_raw_first(item, "saleDt", "sale_dt"),
        "docTypeName": _finance_raw_first(item, "docTypeName", "doc_type_name"),
        "sellerOperName": seller_oper_name,
        "bonusTypeName": bonus_type_name,
        "reasons": reasons,
        "raw": _finance_diagnostic_raw_fields(item),
        "normalized": {
            "penaltyKopecks": penalty_kopecks,
            "penaltyDirection": "return" if penalty_kopecks < 0 else ("charge" if penalty_kopecks > 0 else "zero"),
            "deductionKopecks": deduction_kopecks,
            "deductionDirection": "compensation" if deduction_kopecks < 0 else ("charge" if deduction_kopecks > 0 else "zero"),
            "additionalPaymentKopecks": additional_payment_kopecks,
            "additionalPaymentEffect": "signed_expense" if additional_payment_kopecks else "zero",
            "expenseFormulaContributionKopecks": penalty_kopecks + deduction_kopecks + additional_payment_kopecks,
        },
    }


def _finance_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _finance_expense_diagnostic(row: dict[str, Any]) -> dict[str, Any]:
    commission_formula_kopecks = _finance_int(row.get("commissionFormulaKopecks"))
    finance_commission_kopecks = _finance_int(row.get("commissionKopecks"))
    commission_kopecks = finance_commission_kopecks
    additional_payment_kopecks = _finance_int(row.get("additionalPaymentKopecks"))
    components = {
        "commissionKopecks": commission_kopecks,
        "commissionCalcMode": "finance_actual",
        "financeCommissionKopecks": finance_commission_kopecks,
        "commissionFormulaKopecks": commission_formula_kopecks,
        "logisticsKopecks": _finance_int(row.get("logisticsKopecks")),
        "storageKopecks": _finance_int(row.get("storageKopecks")),
        "acceptanceKopecks": _finance_int(row.get("acceptanceKopecks")),
        "penaltyKopecks": _finance_int(row.get("penaltyKopecks")),
        "deductionKopecks": _finance_int(row.get("deductionKopecks")),
        "acquiringKopecks": _finance_int(row.get("acquiringKopecks")),
        "additionalPaymentKopecks": additional_payment_kopecks,
        "rewardAdjustmentKopecks": _finance_int(row.get("rewardAdjustmentKopecks")),
        "paymentScheduleKopecks": _finance_int(row.get("paymentScheduleKopecks")),
        "loyaltyCostKopecks": _finance_int(row.get("loyaltyCostKopecks")),
    }
    expenses_without_tax_kopecks = (
        components["commissionKopecks"]
        + components["logisticsKopecks"]
        + components["storageKopecks"]
        + components["acceptanceKopecks"]
        + components["penaltyKopecks"]
        + components["deductionKopecks"]
        + components["acquiringKopecks"]
        + components["loyaltyCostKopecks"]
        - additional_payment_kopecks
    )
    return {
        **components,
        "expensesWithoutTaxKopecks": expenses_without_tax_kopecks,
    }


def _finance_field_requested(requested_fields: list[str] | None, *names: str) -> bool | None:
    if requested_fields is None:
        return None
    normalized = {str(field).lower() for field in requested_fields}
    return any(name.lower() in normalized for name in names)


def _finance_storage_acceptance_diagnostics(
    aggregates: dict[str, dict[str, Any]],
    *,
    raw_rows: list[dict[str, Any]] | None,
    requested_fields: list[str] | None,
) -> dict[str, Any]:
    storage_aggregate_sum_kopecks = sum(_finance_int(row.get("storageKopecks")) for row in aggregates.values())
    acceptance_aggregate_sum_kopecks = sum(_finance_int(row.get("acceptanceKopecks")) for row in aggregates.values())
    payload: dict[str, Any] = {
        "rawRowsAvailable": raw_rows is not None,
        "rawRowsCount": len(raw_rows) if raw_rows is not None else None,
        "requestedFields": requested_fields or [],
        "paidStorageFieldRequested": _finance_field_requested(requested_fields, "paidStorage", "storageFee", "storage_fee"),
        "paidAcceptanceFieldRequested": _finance_field_requested(requested_fields, "paidAcceptance", "acceptance"),
        "paidStorageAggregateSumKopecks": storage_aggregate_sum_kopecks,
        "paidAcceptanceAggregateSumKopecks": acceptance_aggregate_sum_kopecks,
        "paidStorageNonzeroSkuCount": sum(
            1 for row in aggregates.values() if _finance_int(row.get("storageKopecks")) != 0
        ),
        "paidAcceptanceNonzeroSkuCount": sum(
            1 for row in aggregates.values() if _finance_int(row.get("acceptanceKopecks")) != 0
        ),
    }
    if raw_rows is None:
        payload.update(
            {
                "source": "aggregates",
                "paidStorageRowsWithField": None,
                "paidStorageNonzeroRows": None,
                "paidStorageSum": storage_aggregate_sum_kopecks,
                "paidStorageSumKopecks": storage_aggregate_sum_kopecks,
                "paidAcceptanceRowsWithField": None,
                "paidAcceptanceNonzeroRows": None,
                "paidAcceptanceSum": acceptance_aggregate_sum_kopecks,
                "paidAcceptanceSumKopecks": acceptance_aggregate_sum_kopecks,
                "note": "Raw Finance detailed rows are stripped from cache; row-level paidStorage/paidAcceptance zeros require refresh=true.",
            }
        )
        return payload

    paid_storage_sum_kopecks = 0
    paid_acceptance_sum_kopecks = 0
    paid_storage_rows_with_field = 0
    paid_acceptance_rows_with_field = 0
    paid_storage_nonzero_rows = 0
    paid_acceptance_nonzero_rows = 0
    paid_storage_missing_nm_rows = 0
    paid_storage_missing_nm_sum_kopecks = 0
    paid_acceptance_missing_nm_rows = 0
    paid_acceptance_missing_nm_sum_kopecks = 0
    for item in raw_rows:
        nm_id = int(_number_or_none(item.get("nmId") or item.get("nmID") or item.get("nm_id")) or 0)
        has_storage_field = any(key in item for key in ("storage_fee", "storageFee", "paidStorage"))
        has_acceptance_field = any(key in item for key in ("acceptance", "paidAcceptance"))
        if has_storage_field:
            paid_storage_rows_with_field += 1
        if has_acceptance_field:
            paid_acceptance_rows_with_field += 1
        paid_storage_kopecks = _first_kopecks(item, "storage_fee", "storageFee", "paidStorage")
        paid_acceptance_kopecks = _first_kopecks(item, "acceptance", "paidAcceptance")
        paid_storage_sum_kopecks += paid_storage_kopecks
        paid_acceptance_sum_kopecks += paid_acceptance_kopecks
        if paid_storage_kopecks != 0:
            paid_storage_nonzero_rows += 1
            if nm_id <= 0:
                paid_storage_missing_nm_rows += 1
                paid_storage_missing_nm_sum_kopecks += paid_storage_kopecks
        if paid_acceptance_kopecks != 0:
            paid_acceptance_nonzero_rows += 1
            if nm_id <= 0:
                paid_acceptance_missing_nm_rows += 1
                paid_acceptance_missing_nm_sum_kopecks += paid_acceptance_kopecks

    payload.update(
        {
            "source": "raw_rows",
            "paidStorageRowsWithField": paid_storage_rows_with_field,
            "paidStorageNonzeroRows": paid_storage_nonzero_rows,
            "paidStorageSum": paid_storage_sum_kopecks,
            "paidStorageSumKopecks": paid_storage_sum_kopecks,
            "paidStorageMissingNmRows": paid_storage_missing_nm_rows,
            "paidStorageMissingNmSumKopecks": paid_storage_missing_nm_sum_kopecks,
            "paidAcceptanceRowsWithField": paid_acceptance_rows_with_field,
            "paidAcceptanceNonzeroRows": paid_acceptance_nonzero_rows,
            "paidAcceptanceSum": paid_acceptance_sum_kopecks,
            "paidAcceptanceSumKopecks": paid_acceptance_sum_kopecks,
            "paidAcceptanceMissingNmRows": paid_acceptance_missing_nm_rows,
            "paidAcceptanceMissingNmSumKopecks": paid_acceptance_missing_nm_sum_kopecks,
            "note": "Raw Finance detailed row counters from current WB response.",
        }
    )
    return payload


def _finance_row_costs(item: dict[str, Any]) -> dict[str, int]:
    deduction = _first_kopecks(item, "deduction", "deductionRub")
    is_wb_promotion = "wb продвижение" in str(
        item.get("bonusTypeName") or item.get("bonus_type_name") or ""
    ).casefold()
    cashback_amount = _first_kopecks(item, "cashback_amount", "cashbackAmount")
    cashback_commission_change = _first_kopecks(
        item, "cashback_commission_change", "cashbackCommissionChange"
    )
    reward_adjustment = _first_kopecks(item, "additional_payment", "additionalPayment")
    payment_schedule = _first_kopecks(item, "payment_schedule", "paymentSchedule")
    doc_type = str(item.get("docTypeName") or item.get("doc_type_name") or "").strip().casefold()
    document_sign = -1 if doc_type == "возврат" else 1
    return {
        "adSpendKopecks": deduction if is_wb_promotion else 0,
        "logisticsKopecks": _finance_logistics_kopecks(item),
        "storageKopecks": _first_kopecks(item, "storage_fee", "storageFee", "paidStorage"),
        "acceptanceKopecks": _first_kopecks(item, "acceptance", "paidAcceptance"),
        "penaltyKopecks": _first_kopecks(item, "penalty", "penaltyRub"),
        "deductionKopecks": 0 if is_wb_promotion else deduction,
        "rewardAdjustmentKopecks": reward_adjustment,
        "paymentScheduleKopecks": payment_schedule,
        "additionalPaymentKopecks": -payment_schedule - reward_adjustment,
        "cashbackAmountKopecks": document_sign * cashback_amount,
        "cashbackDiscountKopecks": document_sign * _first_kopecks(item, "cashback_discount", "cashbackDiscount"),
        "cashbackCommissionChangeKopecks": document_sign * cashback_commission_change,
        "loyaltyCostKopecks": document_sign * (cashback_amount + cashback_commission_change),
        "acquiringKopecks": document_sign
        * _first_kopecks(item, "acquiring_fee", "acquiringFee"),
    }


def _allocate_signed_total(total: int, weights: dict[str, int]) -> dict[str, int]:
    if not total or not weights:
        return {key: 0 for key in weights}
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        weights = {key: 1 for key in weights}
        weight_sum = len(weights)
    magnitude = abs(total)
    shares = {key: magnitude * weight // weight_sum for key, weight in weights.items()}
    remainder = magnitude - sum(shares.values())
    order = sorted(weights, key=lambda key: (-(magnitude * weights[key] % weight_sum), key))
    for key in order[:remainder]:
        shares[key] += 1
    sign = -1 if total < 0 else 1
    return {key: sign * value for key, value in shares.items()}


def _allocate_global_finance_costs(
    aggregates: dict[str, dict[str, Any]],
    global_rows: list[dict[str, Any]],
    *,
    fallback_aggregates: dict[str, dict[str, Any]] | None = None,
) -> None:
    if not global_rows:
        return
    weight_source = aggregates or fallback_aggregates or {}
    weights = {
        key: max(0, _finance_int(row.get("sellerRevenueKopecks")))
        for key, row in weight_source.items()
        if _finance_int(key) > 0 and isinstance(row, dict)
    }
    if not weights:
        return
    totals = {key: 0 for key in _finance_row_costs({})}
    for item in global_rows:
        for key, amount in _finance_row_costs(item).items():
            totals[key] += amount
    for key, total in totals.items():
        if not total:
            continue
        for nm_id, amount in _allocate_signed_total(total, weights).items():
            source = weight_source[nm_id]
            row = aggregates.setdefault(
                nm_id,
                {
                    "vendorCode": source.get("vendorCode"),
                    "rowsCount": 0,
                    "source": "finance_sales_reports_detailed",
                },
            )
            row[key] = _finance_int(row.get(key)) + amount
            if key == "penaltyKopecks":
                breakdown_key = "penaltyChargedKopecks" if amount >= 0 else "penaltyReturnedKopecks"
                row[breakdown_key] = _finance_int(row.get(breakdown_key)) + abs(amount)
            elif key == "deductionKopecks":
                breakdown_key = "deductionChargedKopecks" if amount >= 0 else "deductionCompensationKopecks"
                row[breakdown_key] = _finance_int(row.get(breakdown_key)) + abs(amount)
            row["globalCostAllocation"] = "sellerRevenue"


def _finance_raw_expense_totals(raw_rows: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "adSpendKopecks": 0,
        "logisticsKopecks": 0,
        "storageKopecks": 0,
        "acceptanceKopecks": 0,
        "penaltyKopecks": 0,
        "deductionKopecks": 0,
        "additionalPaymentKopecks": 0,
        "rewardAdjustmentKopecks": 0,
        "paymentScheduleKopecks": 0,
        "cashbackAmountKopecks": 0,
        "cashbackDiscountKopecks": 0,
        "cashbackCommissionChangeKopecks": 0,
        "loyaltyCostKopecks": 0,
        "acquiringKopecks": 0,
    }
    for item in raw_rows:
        for key, amount in _finance_row_costs(item).items():
            totals[key] += amount
    return totals


def project_finance_tax_diagnostics(
    diagnostics: dict[str, Any], finance_taxes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve every diagnostic tax view from the same dated period index."""
    result = deepcopy(diagnostics)
    taxes = finance_taxes or {}
    missing = {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_context_missing"}
    rows = result.get("skuSummaries") or []
    entries = list(taxes.values()) + [missing for row in rows if str(row.get("nmId")) not in taxes]
    reasons = {
        str(entry.get("factTaxReason") or "tax_context_missing") for entry in entries
        if entry.get("factTaxState") != "configured" or type(entry.get("taxKopecks")) is not int
    }
    if not entries:
        reasons.add("tax_context_missing")
    total_tax = None if reasons else sum(entry["taxKopecks"] for entry in entries)
    result["tax"] = {
        **(result.get("tax") or {}), "taxPct": None, "taxKopecks": total_tax,
        "factTaxState": "missing" if reasons else "configured",
        "factTaxReason": sorted(reasons)[0] if reasons else None,
        "source": "dated_economics",
    }
    totals = result.setdefault("totals", {})
    totals["taxKopecks"] = total_tax
    expenses = totals.get("expensesWithoutTaxKopecks")
    totals["expensesIfTaxIncludedKopecks"] = expenses + total_tax if type(expenses) is int and total_tax is not None else None
    for row in rows:
        tax = taxes.get(str(row.get("nmId"))) or missing
        amount = tax.get("taxKopecks") if tax.get("factTaxState") == "configured" else None
        amount = amount if type(amount) is int else None
        expenses = row.get("expensesWithoutTaxKopecks")
        row.update(
            taxPct=None, taxKopecks=amount,
            factTaxState="configured" if amount is not None else "missing",
            factTaxReason=None if amount is not None else tax.get("factTaxReason") or "tax_context_missing",
            expensesIfTaxIncludedKopecks=expenses + amount if type(expenses) is int and amount is not None else None,
            marginDeltaFromTaxRemovalKopecks=amount,
        )
    return result


def build_finance_diagnostics_from_aggregates(
    aggregates: dict[str, dict[str, Any]],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
    requested_fields: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    finance_taxes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    totals = {
        "adSpendKopecks": 0,
        "sellerRevenueKopecks": 0,
        "buyerRevenueKopecks": 0,
        "salesUnits": 0,
        "returnsUnits": 0,
        "expensesWithoutTaxKopecks": 0,
        "commissionKopecks": 0,
        "logisticsKopecks": 0,
        "penaltyKopecks": 0,
        "penaltyChargedKopecks": 0,
        "penaltyReturnedKopecks": 0,
        "deductionKopecks": 0,
        "deductionChargedKopecks": 0,
        "deductionCompensationKopecks": 0,
        "additionalPaymentKopecks": 0,
        "rewardAdjustmentKopecks": 0,
        "paymentScheduleKopecks": 0,
        "cashbackAmountKopecks": 0,
        "cashbackDiscountKopecks": 0,
        "cashbackCommissionChangeKopecks": 0,
        "loyaltyCostKopecks": 0,
        "storageKopecks": 0,
        "acceptanceKopecks": 0,
        "acquiringKopecks": 0,
    }
    sku_summaries: list[dict[str, Any]] = []
    for nm_id, row in sorted(aggregates.items(), key=lambda item: str(item[0])):
        seller_revenue_kopecks = _finance_int(row.get("sellerRevenueKopecks"))
        buyer_revenue_kopecks = _finance_int(row.get("buyerRevenueKopecks"))
        expense = _finance_expense_diagnostic(row)
        expenses_without_tax_kopecks = int(expense["expensesWithoutTaxKopecks"])
        summary = {
            "nmId": nm_id,
            "rowsCount": _finance_int(row.get("rowsCount")),
            "salesUnits": _finance_int(row.get("salesUnits")),
            "returnsUnits": _finance_int(row.get("returnsUnits")),
            "sellerRevenueKopecks": seller_revenue_kopecks,
            "buyerRevenueKopecks": buyer_revenue_kopecks,
            "taxBaseKopecks": seller_revenue_kopecks,
            "taxIncludedInExpenses": False,
            "taxIncludedInNetProfit": True,
            "expensesWithoutTaxKopecks": expenses_without_tax_kopecks,
            **expense,
            "penaltyChargedKopecks": _finance_int(row.get("penaltyChargedKopecks")),
            "penaltyReturnedKopecks": _finance_int(row.get("penaltyReturnedKopecks")),
            "deductionChargedKopecks": _finance_int(row.get("deductionChargedKopecks")),
            "deductionCompensationKopecks": _finance_int(row.get("deductionCompensationKopecks")),
        }
        sku_summaries.append(summary)
        totals["sellerRevenueKopecks"] += seller_revenue_kopecks
        totals["buyerRevenueKopecks"] += buyer_revenue_kopecks
        totals["salesUnits"] += _finance_int(row.get("salesUnits"))
        totals["returnsUnits"] += _finance_int(row.get("returnsUnits"))
        totals["expensesWithoutTaxKopecks"] += expenses_without_tax_kopecks
        totals["commissionKopecks"] += _finance_int(row.get("commissionKopecks"))
        totals["logisticsKopecks"] += _finance_int(row.get("logisticsKopecks"))
        totals["penaltyKopecks"] += _finance_int(row.get("penaltyKopecks"))
        totals["penaltyChargedKopecks"] += _finance_int(row.get("penaltyChargedKopecks"))
        totals["penaltyReturnedKopecks"] += _finance_int(row.get("penaltyReturnedKopecks"))
        totals["deductionKopecks"] += _finance_int(row.get("deductionKopecks"))
        totals["deductionChargedKopecks"] += _finance_int(row.get("deductionChargedKopecks"))
        totals["deductionCompensationKopecks"] += _finance_int(row.get("deductionCompensationKopecks"))
        totals["additionalPaymentKopecks"] += _finance_int(row.get("additionalPaymentKopecks"))
        totals["rewardAdjustmentKopecks"] += _finance_int(row.get("rewardAdjustmentKopecks"))
        totals["paymentScheduleKopecks"] += _finance_int(row.get("paymentScheduleKopecks"))
        totals["cashbackAmountKopecks"] += _finance_int(row.get("cashbackAmountKopecks"))
        totals["cashbackDiscountKopecks"] += _finance_int(row.get("cashbackDiscountKopecks"))
        totals["cashbackCommissionChangeKopecks"] += _finance_int(row.get("cashbackCommissionChangeKopecks"))
        totals["loyaltyCostKopecks"] += _finance_int(row.get("loyaltyCostKopecks"))
        totals["storageKopecks"] += _finance_int(row.get("storageKopecks"))
        totals["acceptanceKopecks"] += _finance_int(row.get("acceptanceKopecks"))
        totals["acquiringKopecks"] += _finance_int(row.get("acquiringKopecks"))
        totals["adSpendKopecks"] += _finance_int(row.get("adSpendKopecks"))

    if any(type(row.get("adSpendKopecks")) is not int for row in aggregates.values()):
        totals["adSpendKopecks"] = None

    adjustment_rows: list[dict[str, Any]] = []
    if raw_rows is not None:
        for item in raw_rows:
            nm_id = int(item.get("nmId") or item.get("nmID") or item.get("nm_id") or 0)
            if nm_id <= 0:
                continue
            penalty_kopecks = _first_kopecks(item, "penalty", "penaltyRub")
            deduction_kopecks = _first_kopecks(item, "deduction", "deductionRub")
            additional_payment_kopecks = _first_kopecks(item, "additional_payment", "additionalPayment")
            seller_oper_name = str(item.get("sellerOperName") or item.get("seller_oper_name") or "").strip()
            bonus_type_name = str(item.get("bonusTypeName") or item.get("bonus_type_name") or "").strip()
            reasons = _finance_adjustment_reasons(
                seller_oper_name=seller_oper_name,
                bonus_type_name=bonus_type_name,
                penalty_kopecks=penalty_kopecks,
                deduction_kopecks=deduction_kopecks,
                additional_payment_kopecks=additional_payment_kopecks,
            )
            if not reasons:
                continue
            adjustment_rows.append(
                _finance_diagnostic_row(
                    item,
                    nm_id=nm_id,
                    seller_oper_name=seller_oper_name,
                    bonus_type_name=bonus_type_name,
                    penalty_kopecks=penalty_kopecks,
                    deduction_kopecks=deduction_kopecks,
                    additional_payment_kopecks=additional_payment_kopecks,
                    reasons=reasons,
                )
            )

    adjustment_rows_total = len(adjustment_rows)
    stored_adjustment_rows = adjustment_rows[:_FINANCE_DIAGNOSTIC_ROW_LIMIT]
    storage_acceptance = _finance_storage_acceptance_diagnostics(
        aggregates,
        raw_rows=raw_rows,
        requested_fields=requested_fields,
    )
    return project_finance_tax_diagnostics({
        "state": "ok" if raw_rows is not None else "aggregate_only",
        "dateFrom": date_from.date().isoformat() if date_from else None,
        "dateTo": date_to.date().isoformat() if date_to else None,
        "rawRowsAvailable": raw_rows is not None,
        "rawRowsStoredInFinanceCache": False,
        "requestedFields": requested_fields or [],
        "rawExpenseTotals": _finance_raw_expense_totals(raw_rows) if raw_rows is not None else None,
        "storageAcceptance": storage_acceptance,
        "paidStorageNonzeroRows": storage_acceptance.get("paidStorageNonzeroRows"),
        "paidStorageSum": storage_acceptance.get("paidStorageSum"),
        "paidAcceptanceNonzeroRows": storage_acceptance.get("paidAcceptanceNonzeroRows"),
        "paidAcceptanceSum": storage_acceptance.get("paidAcceptanceSum"),
        "tax": {
            "taxBase": "sellerRevenueKopecks",
            "taxIncludedInExpenses": False,
            "taxIncludedInNetProfit": True,
            "note": "Налог учитывается в netProfit/margin и показывается отдельно от расходов WB.",
        },
        "formula": {
            "financeExpensesWithoutTax": (
                "commission + logistics + storage + acceptance + penalty + deduction + acquiring + loyaltyCost - additionalPayment"
            ),
            "pnlExpensesWithoutTax": (
                "commission + logistics + storage + acceptance + penalty + deduction + acquiring + loyaltyCost + ads - additionalPayment"
            ),
            "netProfit": "buyerRevenue + workReturn - cogs - pnlExpensesWithoutTax - tax",
            "taxIncludedInExpenses": False,
            "signedFields": ["penalty", "deduction"],
            "subtractFields": ["additionalPayment"],
            "additionalPaymentNormalization": "-paymentSchedule - rewardAdjustment(additionalPayment raw)",
            "forbiddenNormalizers": [
                "absolute value for penalty",
                "absolute value for deduction",
                "positive-only clamp for penalty",
                "positive-only clamp for deduction",
            ],
        },
        "totals": totals,
        "skuSummaries": sku_summaries,
        "adjustmentRows": stored_adjustment_rows,
        "adjustmentRowsTotal": adjustment_rows_total,
        "adjustmentRowsStored": len(stored_adjustment_rows),
        "adjustmentRowsTruncated": adjustment_rows_total > len(stored_adjustment_rows),
    }, finance_taxes)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _days_between(target: datetime, now: datetime) -> int:
    return ceil((target - now).total_seconds() / 86400)


def _article_type(article_id: str) -> str:
    normalized = article_id.strip().upper()
    first = normalized[:1] or "F"
    markers = {
        "Л": "L",
        "Ф": "F",
        "Х": "H",
        "L": "L",
        "F": "F",
        "H": "H",
    }
    if first in markers:
        return markers[first]
    for marker in normalized:
        if marker in "ЛФХ":
            return markers[marker]
    for index, marker in enumerate(normalized):
        if marker in "LFH" and (index == 0 or not normalized[index - 1].isalpha()):
            return marker
    return first


ARTICLE_TYPE_FALLBACK_SUBJECTS: dict[str, str] = {
    "F": "Футболки",
    "H": "Худи",
    "L": "Лонгсливы",
}


TYPE_DEFAULTS: dict[str, dict[str, int | float]] = {
    "F": {
        "cogsKopecks": 45000,
        "logisticsKopecks": 5000,
        "minMarginPct": 15,
        "pMaxKopecks": 190000,
    },
    "H": {
        "cogsKopecks": 85000,
        "logisticsKopecks": 5500,
        "minMarginPct": 15,
        "pMaxKopecks": 320000,
    },
    "L": {
        "cogsKopecks": 44000,
        "logisticsKopecks": 4100,
        "minMarginPct": 15,
        "pMaxKopecks": 240000,
    },
}

DEFAULT_ALGORITHM_SETTINGS: dict[str, Any] = {
    "targetMarginPct": 25,
    "priceStepPct": 6,
    "maxPriceChangeDailyPct": 20,
    "syncIntervalHours": 1,
    "syncIntervalMinutes": 60,
    "fullSyncIntervalMinutes": 60,
    "fullSyncPromotionsEnabled": True,
    "workerAutoApplyPricesEnabled": False,
    "sppAccountingMode": "spp_only",
    "wbWalletType": 4,
    "marginCalcMode": "from-discount",
    "storageCostPer60Days": 21,
    "acquiringPct": 3.31,
    "taxPct": 6,
    "workReturnPerSaleRub": 0,
    "otherExpensePricePct": 5,
    "otherExpensePerSaleRub": 0,
    "logisticsCoefficient": 1.0,
    "localizationIndex": 1.0,
    "nightMedianEnabled": True,
    "nightMedianMode": "conservative",
    "nightMedianGlobal": True,
    "nightMedianAutoEnableAllSkus": True,
    "nightMedianDefaultAllSkusVersion": 1,
    "nightMedianAutoApplyEnabled": False,
    "minPriceSyncEnabled": True,
    "promoMarginThresholdPct": 10,
    "priceJumpProtectionEnabled": True,
    "priceJumpStockValueMinPct": -20,
    "priceJumpSppMinPct": -40,
    "priceJumpStockQtyMinPct": -15,
    "csvMaxCostDropPct": 20,
    "csvMaxPriceDropPct": 20,
    "discountStepEnabled": False,
    "discountStepPct": 1,
    "priceRoundingEnabled": False,
    "basketSignalMode": "matrix",
    "cartHighBasketsThreshold": 30,
    "cartLowBasketsThreshold": 5,
    "cartComparisonDays": 7,
    "basketNormMode": "fallback_by_type",
    "basketNormPeriodDays": 7,
    "basketNormAutoMinOrders": 1,
    "basketNormFallbackByGarment": {
        "tshirt": 20,
        "hoodie": 15,
        "longsleeve": 10,
    },
    "planFactMetric": "orders",
    "planFactFactPeriodDays": 7,
    "planFactIntervalHours": 4,
    "warmupExitBaskets": 40,
    "nightMedianCollectEnabled": True,
    "nightMedianWindowStartHour": 23,
    "nightMedianWindowEndHour": 6,
    "nightMedianApplyDeltaPct": 8,
    "nightMedianTimezone": "МСК (UTC+3)",
    "cogsByGarmentRub": {
        "tshirt": 380,
        "hoodie": 720,
        "longsleeve": 440,
    },
    "promoAlertEnabled": True,
    "telegramDigestEnabled": True,
    "telegramDigestTime": "08:15",
    "telegramDigestTimezone": "МСК (UTC+3)",
    "telegramDigestMetrics": [
        "Выручка вчера",
        "Ср. маржа",
        "Корзины/сут",
        "Топ-5 SKU по выручке",
        "SKU без P_min",
    ],
    "telegramAlertStockLowThreshold": 5,
    "telegramAlertStockLowEnabled": True,
    "telegramAlertLossMarginThresholdPct": -5,
    "telegramAlertLossMarginEnabled": True,
    "telegramAlertCancelRateThresholdPct": 15,
    "telegramAlertCancelRateEnabled": False,
    "telegramAlertPriceGuardEnabled": True,
    "warmupDays": 10,
    "warmupMarginPct": 15,
    "warmupDailyLimitPct": 20,
    "liquidationAutoFlagEnabled": True,
    "liquidationStepPct": 3,
    "liquidationMinCogsPct": 60,
}

DEMO_SKU_META_OVERRIDES: dict[str, dict[str, Any]] = {
    "FBBT_42": {"status": "auto", "basketsLast7d": 34, "basketNorm": 20},
    "HCBT_19": {
        "status": "manual",
        "basketsLast7d": 2,
        "basketNorm": 15,
        "managerId": "manager-irina",
        "managerName": "Ирина",
        "assignmentSource": "xlsx",
    },
    "FBBT_55": {"status": "liquidation", "basketsLast7d": 1, "basketNorm": 20},
    "LBBT_03": {"status": "manual", "basketsLast7d": 4, "basketNorm": 10},
    "HBBT_22": {"status": "warmup", "basketsLast7d": 3, "basketNorm": 15, "warmupDaysLeft": 8},
    "FCBT_18": {"status": "manual", "basketsLast7d": 6, "basketNorm": 20},
}

DEMO_SKU_SETTINGS_OVERRIDES: dict[str, dict[str, Any]] = {
    "HCBT_19": {"automationEnabled": False},
    "LBBT_03": {"automationEnabled": False, "allowNegativeMargin": True},
    "HBBT_22": {"automationEnabled": False},
    "FCBT_18": {"automationEnabled": False},
}

DEMO_SKU_COMMENTS: dict[str, list[dict[str, Any]]] = {
    "FBBT_42": [
        {
            "id": "comment-fbbt-42-1",
            "sku": "FBBT_42",
            "author": {"id": "manager-maria-dudina", "name": "Мария Дудина", "role": "manager"},
            "createdAt": (_utc_now() - timedelta(hours=3)).isoformat(),
            "text": "Проверить цену перед следующим циклом ночной медианы.",
        }
    ],
    "HCBT_19": [
        {
            "id": "comment-hcbt-19-1",
            "sku": "HCBT_19",
            "author": {"id": "manager-irina", "name": "Ирина", "role": "manager"},
            "createdAt": (_utc_now() - timedelta(hours=5)).isoformat(),
            "text": "Низкие корзины, не запускать снижение без проверки рекламы.",
        }
    ],
    "LBBT_03": [
        {
            "id": "comment-lbbt-03-1",
            "sku": "LBBT_03",
            "author": {"id": "finance-maxim", "name": "Максим", "role": "finance"},
            "createdAt": (_utc_now() - timedelta(hours=7)).isoformat(),
            "text": "Перед ликвидацией сверить складские и логистические издержки.",
        }
    ],
}

DEMO_SKU_AUDIT_EVENTS: dict[str, list[dict[str, Any]]] = {
    sku: [
        {
            "id": f"audit-{sku}-sync",
            "sku": sku,
            "createdAt": (_utc_now() - timedelta(hours=2)).isoformat(),
            "actor": {"id": "system", "name": "Система", "role": "system"},
            "source": "system",
            "scope": "sku",
            "action": "Пересчёт репрайсера",
            "oldValue": "предыдущий цикл",
            "newValue": "актуальные корзины и маржа",
        }
    ]
    for sku in DEMO_SKU_META_OVERRIDES
}

SKU_META_OVERRIDES: dict[str, dict[str, Any]] = {}
SKU_SETTINGS_OVERRIDES: dict[str, dict[str, Any]] = {}
SKU_COMMENTS: dict[str, list[dict[str, Any]]] = {}
SKU_AUDIT_EVENTS: dict[str, list[dict[str, Any]]] = {}
FRONTEND_STRATEGY_ASSIGNMENTS: dict[str, dict[str, Any]] = {}
REPRICER_SKU_GROUPS: dict[str, dict[str, Any]] = {}

PROMOTION_UPLOAD_OVERRIDES: dict[str, dict[str, Any]] = {}
NEGATIVE_MARGIN_CONFIRMATIONS: dict[str, str] = {}
ALGORITHM_SETTINGS_STATE: dict[str, Any] = deepcopy(DEFAULT_ALGORITHM_SETTINGS)
CATALOG_GOODS_CACHE_TTL_SECONDS = 120
CATALOG_GOODS_CACHE: dict[tuple[str, str], tuple[datetime, list[dict[str, Any]]]] = {}
COMMISSION_TARIFFS_CACHE: dict[tuple[str, str], tuple[datetime, dict[str, dict[str, Any]]]] = {}
COMMISSION_TARIFFS_TTL = timedelta(hours=6)


def _token_cache_key(wb_token: str | None) -> str:
    if not wb_token:
        return "env-or-fake"
    return hashlib.sha256(wb_token.encode("utf-8")).hexdigest()


def _normalize_subject_name(raw: Any) -> str:
    return str(raw or "").strip().lower().replace("ё", "е")


def _int_or_none(raw: Any) -> int | None:
    value = _number_or_none(raw)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


TEMPLATES_STATE: dict[str, Any] = {
    "globalCommissionPct": 25,
    "types": deepcopy(TYPE_DEFAULTS),
}
FRONTEND_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "stockout_guard",
        "typedStrategyId": None,
        "type": "stock",
        "name": "Защита out of stock",
        "color": "#0284C7",
        "description": "Повышает цену, если товара осталось меньше чем на целевое число дней.",
        "status": "active",
        "rules": [
            {"label": "Сигнал", "value": "дней до OOS"},
            {"label": "Порог", "value": "< 7 дней"},
            {"label": "Действие", "value": "повышаем цену"},
        ],
    },
    {
        "id": "turnover_control",
        "typedStrategyId": None,
        "type": "turnover",
        "name": "Контроль оборачиваемости",
        "color": "#0F766E",
        "description": "Регулирует цену от базовой: повышает при риске дефицита и снижает при излишках.",
        "status": "active",
        "rules": [
            {"label": "0-3 дня", "value": "+25%"},
            {"label": "5-7 дней", "value": "+15%"},
            {"label": ">90 дней", "value": "-25%"},
        ],
    },
    {
        "id": "plan_fact_daily",
        "typedStrategyId": None,
        "type": "plan_fact",
        "name": "План-факт стандартный",
        "color": "#2563EB",
        "description": "Корректирует цену для выполнения дневного плана по заказам на артикул.",
        "status": "active",
        "rules": [
            {"label": "Период анализа", "value": "средние заказы за 3 дня"},
            {"label": "0-10% плана", "value": "мин. цена"},
            {"label": ">140% плана", "value": "+10%"},
        ],
    },
    {
        "id": "plan_fact_period",
        "typedStrategyId": None,
        "type": "plan_fact",
        "name": "План-факт на период",
        "color": "#1D4ED8",
        "description": "Сравнивает план и факт по заказам, продажам, выручке или марже за выбранный период.",
        "status": "active",
        "rules": [
            {"label": "Метрика", "value": "заказы / выручка / маржа"},
            {"label": "100-110%", "value": "баз. цена"},
            {"label": "130-140%", "value": "+5%"},
        ],
    },
    {
        "id": "plan_fact_group",
        "typedStrategyId": None,
        "type": "plan_fact",
        "name": "План-факт на период групповой",
        "color": "#1E40AF",
        "description": "Применяет план-факт к группе товаров по внутреннему ID.",
        "status": "active",
        "rules": [
            {"label": "Уровень", "value": "группа товаров"},
            {"label": "<90% плана", "value": "снижаем всю группу"},
            {"label": "Настройка", "value": "группа задается выбранными SKU"},
        ],
    },
    {
        "id": "plan_fact_interval",
        "typedStrategyId": None,
        "type": "plan_fact",
        "name": "План-факт интервальный",
        "color": "#1E3A8A",
        "description": "Разбивает дневной план по заказам на 4-часовые интервалы с весами по истории продаж.",
        "status": "active",
        "rules": [
            {"label": "Интервал", "value": "4 часа"},
            {"label": "Основа", "value": "WB statistics orders по часам"},
            {"label": "Расчет", "value": "вес интервала = его доля в истории"},
        ],
    },
    {
        "id": "cross_marketplace",
        "typedStrategyId": None,
        "type": "external",
        "name": "Кроссмаркетплейс",
        "color": "#0891B2",
        "description": "Синхронизирует цены одинаковых товаров между Ozon и Wildberries.",
        "status": "disabled",
        "rules": [
            {"label": "Источник", "value": "Ozon + WB"},
            {"label": "Настройка", "value": "дороже / дешевле / одна цена"},
            {"label": "Статус", "value": "временно выключено"},
        ],
    },
    {
        "id": "illiquid",
        "typedStrategyId": None,
        "type": "liquidation",
        "name": "Неликвид",
        "color": "#DC2626",
        "description": "Ищет цену для новинок, несезонных и залежалых товаров с нулевым или слабым спросом.",
        "status": "special",
        "rules": [
            {"label": "0 заказов", "value": "снижаем цену"},
            {"label": "1 заказ", "value": "держим цену"},
            {"label": ">1 заказа", "value": "повышаем цену"},
        ],
    },
    {
        "id": "optimal_price",
        "typedStrategyId": None,
        "type": "demand",
        "name": "Оптимальная цена",
        "color": "#7C3AED",
        "description": "Сравнивает заказы в день за последние 3 и 7 завершённых дней с общей датой окончания.",
        "status": "active",
        "rules": [
            {"label": "Основной период", "value": "3 дня"},
            {"label": "Сравнение", "value": "7 дней"},
            {"label": "Реакция", "value": "рост / удержание / спад"},
        ],
    },
    {
        "id": "baskets_orders",
        "typedStrategyId": "baskets_orders_4599",
        "type": "demand",
        "name": "Динамика корзин и заказов",
        "color": "#4338CA",
        "description": "Меняет цену по матрице роста корзин и заказов: от -3% до +3%.",
        "status": "active",
        "rules": [
            {"label": "Корзины + заказы растут", "value": "+3%"},
            {"label": "Только заказы растут", "value": "+1%"},
            {"label": "Не растут оба", "value": "-3%"},
        ],
    },
    {
        "id": "metric_dynamics",
        "typedStrategyId": "revenue_dynamics_4600",
        "type": "metric",
        "name": "Поддержание динамики показателя",
        "color": "#C026D3",
        "description": "Сравнивает заказы последнего завершённого дня со средним за последние 7 завершённых дней. 100% — без изменения.",
        "status": "active",
        "rules": [
            {"label": "<75%", "value": "-5%, минимум 30 ₽"},
            {"label": "95-105%", "value": "держим цену"},
            {"label": ">125%", "value": "+5%, минимум 30 ₽"},
        ],
    },
    {
        "id": "night_price_mode",
        "typedStrategyId": "night_price_mode",
        "type": "night",
        "name": "Ночная медиана",
        "color": "#7C3AED",
        "description": "Ночью собирает snapshots корзин, после 06:00 МСК применяет утреннюю коррекцию от ночной медианы.",
        "status": "active",
        "rules": [
            {"label": "23:00-06:00 МСК", "value": "сбор корзин"},
            {"label": "Ночью", "value": "цены не меняются"},
            {"label": "Утром", "value": "коррекция по медиане"},
        ],
    },
    {
        "id": "schedule",
        "typedStrategyId": None,
        "type": "schedule",
        "name": "Расписание",
        "color": "#D97706",
        "description": "Устанавливает цену по датам, дням недели или часам.",
        "status": "active",
        "rules": [
            {"label": "Правила", "value": "даты / дни / часы"},
            {"label": "Действие", "value": "заданная цена / +/- % / держать"},
            {"label": "Настройка", "value": "задается при выборе стратегии"},
        ],
    },
    {
        "id": "bundles",
        "typedStrategyId": None,
        "type": "bundle",
        "name": "Комплекты",
        "color": "#475569",
        "description": "Рассчитывает цену комплекта из цен всех входящих товаров.",
        "status": "disabled",
        "rules": [
            {"label": "Источник", "value": "состав комплекта"},
            {"label": "Расчет", "value": "по ценам компонентов"},
            {"label": "Статус", "value": "временно выключено"},
        ],
    },
]
FRONTEND_STRATEGY_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in FRONTEND_STRATEGIES}
FRONTEND_STRATEGY_ALIASES: dict[str, str] = {
    "stockout_guard": "stockout_guard",
    "защита out of stock": "stockout_guard",
    "out of stock": "stockout_guard",
    "oos": "stockout_guard",
    "turnover_control": "turnover_control",
    "контроль оборачиваемости": "turnover_control",
    "cons": "turnover_control",
    "консервативный": "turnover_control",
    "plan_fact_daily": "plan_fact_daily",
    "план-факт стандартный": "plan_fact_daily",
    "план факт стандартный": "plan_fact_daily",
    "план факт стадартный": "plan_fact_daily",
    "plan_fact_period": "plan_fact_period",
    "план-факт на период": "plan_fact_period",
    "план факт на период": "plan_fact_period",
    "plan_fact_group": "plan_fact_group",
    "план-факт на период групповой": "plan_fact_group",
    "план факт на период групповой": "plan_fact_group",
    "план факт на период на группу": "plan_fact_group",
    "plan_fact_interval": "plan_fact_interval",
    "план-факт интервальный": "plan_fact_interval",
    "план факт интервальный": "plan_fact_interval",
    "cross_marketplace": "cross_marketplace",
    "кроссмаркетплейс": "cross_marketplace",
    "следование за ozon": "cross_marketplace",
    "illiquid": "illiquid",
    "неликвид": "illiquid",
    "liq": "illiquid",
    "ликвидация": "illiquid",
    "warm": "illiquid",
    "запуск новинки": "illiquid",
    "launch": "illiquid",
    "optimal_price": "optimal_price",
    "оптимальная цена": "optimal_price",
    "baskets_orders": "baskets_orders",
    "динамика корзин и заказов": "baskets_orders",
    "baskets_orders_4599": "baskets_orders",
    "aggr": "baskets_orders",
    "агрессивный": "baskets_orders",
    "metric_dynamics": "metric_dynamics",
    "поддержание динамики показателя": "metric_dynamics",
    "revenue_dynamics_4600": "metric_dynamics",
    "cust": "metric_dynamics",
    "ночная медиана": "night_price_mode",
    "night_price_mode": "night_price_mode",
    "schedule": "schedule",
    "расписание": "schedule",
    "bundles": "bundles",
    "комплекты": "bundles",
}
LIQUIDATION_ACTIVE: dict[str, dict[str, Any]] = {
    "FBBT_55": {
        "articleId": "FBBT_55",
        "startPriceKopecks": 165000,
        "currentPriceKopecks": 125000,
        "targetPriceKopecks": 70000,
        "startedAt": (_utc_now() - timedelta(days=3)).isoformat(),
        "nextStepAt": (_utc_now() + timedelta(hours=10)).isoformat(),
        "stepPct": 5,
        "requiresNegativeMarginConfirm": False,
    },
    "HCBT_19": {
        "articleId": "HCBT_19",
        "startPriceKopecks": 285000,
        "currentPriceKopecks": 155000,
        "targetPriceKopecks": 145000,
        "startedAt": (_utc_now() - timedelta(days=5)).isoformat(),
        "nextStepAt": (_utc_now() + timedelta(hours=2)).isoformat(),
        "stepPct": 5,
        "requiresNegativeMarginConfirm": True,
    },
}
LIQUIDATION_HISTORY: list[dict[str, Any]] = []

CHANGELOG_ENTRIES: list[dict[str, Any]] = []


def _p_min_kopecks(
    cogs_kopecks: int,
    wb_commission_pct: float,
    logistics_kopecks: int,
    min_margin_pct: float,
) -> int:
    denominator = 1 - wb_commission_pct / 100 - min_margin_pct / 100
    if denominator <= 0:
        return 10**12
    return round((cogs_kopecks + logistics_kopecks) / denominator)


def _pct_fraction(value: Any, default: float = 0.0) -> float:
    parsed = _number_or_none(value)
    if parsed is None:
        parsed = default
    return max(0.0, min(100.0, float(parsed))) / 100


def _algorithm_float_setting(key: str, default: float = 0.0) -> float:
    raw = ALGORITHM_SETTINGS_STATE.get(key)
    if raw is None or raw == "":
        raw = DEFAULT_ALGORITHM_SETTINGS.get(key, default)
    parsed = _number_or_none(raw)
    return float(default if parsed is None else parsed)


def _finance_unit_key(item: dict[str, Any]) -> str | None:
    for field in ("srid", "saleId", "saleID", "rid"):
        value = str(item.get(field) or "").strip()
        if value:
            return value
    return None


def _statistics_order_unit_key(item: dict[str, Any]) -> str | None:
    for field in ("srid", "odid", "rid"):
        value = str(item.get(field) or "").strip()
        if value:
            return value
    group_value = ""
    for field in ("orderUid", "orderUID", "gNumber"):
        value = str(item.get(field) or "").strip()
        if value:
            group_value = value
            break
    nm_id = str(item.get("nmId") or item.get("nmID") or "").strip()
    barcode = str(item.get("barcode") or item.get("sku") or "").strip()
    tech_size = str(item.get("techSize") or item.get("size") or "").strip()
    order_date = str(item.get("date") or "").strip()
    price = str(item.get("finishedPrice") or item.get("priceWithDisc") or "").strip()
    if group_value and nm_id and (barcode or tech_size or order_date or price):
        return "|".join((group_value, nm_id, barcode, tech_size, order_date, price))
    if nm_id and (barcode or order_date or price):
        return "|".join((nm_id, barcode, order_date, price))
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "да"}


def _is_cancelled_order(item: dict[str, Any]) -> bool:
    if _boolish(item.get("isCancel") or item.get("isCanceled") or item.get("cancel")):
        return True
    cancel_date = _date_from_any(item.get("cancelDate") or item.get("cancelDt") or item.get("cancel_date"))
    return cancel_date is not None and cancel_date.year > 2001


def _settings_pmin_override_kopecks(settings: dict[str, Any]) -> int | None:
    try:
        value = int(settings.get("pMinKopecks") or settings.get("pminKopecks") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _effective_pmin(row: dict[str, Any]) -> tuple[int, Literal["explicit", "calculated"]]:
    settings = row.get("settings") or {}
    strategy_id = (row.get("strategy") or {}).get("id") or (row.get("meta") or {}).get("activeStrategyId")
    if strategy_id == "illiquid":
        return _illiquid_floor_kopecks(row), "calculated"
    source = "explicit" if _settings_pmin_override_kopecks(settings) is not None else "calculated"
    return settings_minimum_price_kopecks(settings), source


def _liquidation_pmin_kopecks(row: dict[str, Any], min_margin_pct: float | None = None) -> int:
    settings = row["settings"]
    if min_margin_pct is None:
        override = _settings_pmin_override_kopecks(settings)
        if override is not None:
            return override
    return _p_min_kopecks(
        int(settings["cogsKopecks"]),
        float(settings["wbCommissionPct"]),
        int(settings["logisticsKopecks"]),
        float(settings["minMarginPct"] if min_margin_pct is None else min_margin_pct),
    )


def _liquidation_stock_units(row: dict[str, Any]) -> int | None:
    stock_units = row.get("analytics", {}).get("wbStockUnits")
    return int(stock_units) if stock_units is not None else None


def _liquidation_margin_status(margin_pct: float | None) -> str:
    if margin_pct is None:
        return "unknown"
    if margin_pct < 0:
        return "negative"
    if margin_pct < 3:
        return "thin"
    return "ok"


def _liquidation_reason(row: dict[str, Any], margin_pct: float | None) -> str:
    if margin_pct is not None and margin_pct < 0:
        return "negative_margin"
    orders_units = int((row.get("analytics") or {}).get("ordersUnits") or 0)
    if orders_units <= 0:
        return "zero_orders"
    baskets_last_7d = int(row["meta"]["basketsLast7d"])
    basket_norm = int(row["meta"]["basketNorm"])
    if baskets_last_7d < basket_norm * 0.25:
        return "low_baskets"
    if orders_units <= 1:
        return "weak_orders"
    return "slow_stock"


def _is_liquidation_candidate(row: dict[str, Any], *, current_price_kopecks: int, pmin_kopecks: int, margin_pct: float | None) -> bool:
    stock_units = _liquidation_stock_units(row)
    if stock_units is not None and stock_units <= 0:
        return False
    negative_margin = margin_pct is not None and margin_pct < 0
    below_pmin = current_price_kopecks < pmin_kopecks
    if negative_margin or below_pmin:
        return True

    baskets_last_7d = int(row["meta"].get("basketsLast7d") or 0)
    basket_norm = max(1, int(row["meta"].get("basketNorm") or 1))
    orders_units = int((row.get("analytics") or {}).get("ordersUnits") or 0)
    low_baskets = baskets_last_7d < basket_norm * 0.25
    low_orders = orders_units <= _illiquid_hold_orders_to()
    thin_margin = margin_pct is not None and margin_pct < 3
    return bool(thin_margin and low_baskets and low_orders)


def _illiquid_step_pct() -> float:
    return float(ALGORITHM_SETTINGS_STATE.get("liquidationStepPct") or 3)


def _illiquid_hold_orders_to() -> int:
    return 1


def _illiquid_floor_kopecks(row: dict[str, Any]) -> int:
    settings = row["settings"]
    return _p_min_kopecks(
        int(settings["cogsKopecks"]),
        float(settings["wbCommissionPct"]),
        int(settings["logisticsKopecks"]),
        0,
    )


def _illiquid_next_price(row: dict[str, Any], *, current_price_kopecks: int | None = None) -> tuple[int, str]:
    current = int(current_price_kopecks or row["meta"]["currentPriceKopecks"])
    orders_units = int((row.get("analytics") or {}).get("ordersUnits") or 0)
    step_pct = _illiquid_step_pct()
    hold_to = _illiquid_hold_orders_to()
    floor_price = max(1, min(current, _illiquid_floor_kopecks(row)))
    if orders_units <= 0:
        return max(floor_price, round(current * (1 - step_pct / 100))), "zero_orders"
    if orders_units <= hold_to:
        return current, "hold_orders"
    return round(current * (1 + step_pct / 100)), "orders_recovered"


def _liquidation_history_entry(article_id: str, sku_name: str, action: str, actor: str = "system") -> None:
    LIQUIDATION_HISTORY.insert(
        0,
        {
            "id": f"liq-{article_id}-{len(LIQUIDATION_HISTORY) + 1}",
            "articleId": article_id,
            "skuName": sku_name,
            "action": action,
            "actor": actor,
            "timestamp": _iso_now(),
        },
    )
    del LIQUIDATION_HISTORY[50:]


def _status_after_settings(article_id: str, current_status: str, automation_enabled: bool) -> str:
    if current_status == "warmup":
        return "warmup"
    if article_id in LIQUIDATION_ACTIVE:
        return "liquidation"
    return "auto" if automation_enabled else "manual"


def _comment_summary(comments: list[dict[str, Any]]) -> dict[str, Any]:
    latest = comments[-1] if comments else None
    return {
        "count": len(comments),
        "latestText": latest["text"] if latest else None,
        "latestAuthor": latest["author"]["name"] if latest else None,
        "latestAt": latest["createdAt"] if latest else None,
    }


def _normalize_frontend_strategy_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if not normalized:
        return None
    return FRONTEND_STRATEGY_ALIASES.get(normalized)


def _frontend_strategy_definition(raw: str | None) -> dict[str, Any]:
    strategy_id = _normalize_frontend_strategy_id(raw)
    if strategy_id is None or strategy_id not in FRONTEND_STRATEGY_BY_ID:
        raise HTTPException(status_code=422, detail="UNKNOWN_FRONTEND_STRATEGY")
    return FRONTEND_STRATEGY_BY_ID[strategy_id]


def _derived_frontend_strategy_id(article_id: str, current_status: str, baskets_last_7d: int, basket_norm: int) -> str:
    if article_id in FRONTEND_STRATEGY_ASSIGNMENTS:
        assigned = str(FRONTEND_STRATEGY_ASSIGNMENTS[article_id]["strategyId"])
        return _normalize_frontend_strategy_id(assigned) or assigned
    if current_status == "liquidation":
        return "illiquid"
    if current_status == "warmup":
        return "illiquid"
    if current_status == "manual":
        return "metric_dynamics"
    return "baskets_orders" if baskets_last_7d >= basket_norm else "turnover_control"


def _frontend_strategy_payload(
    article_id: str,
    *,
    current_status: str,
    baskets_last_7d: int,
    basket_norm: int,
) -> dict[str, Any]:
    strategy_id = _derived_frontend_strategy_id(article_id, current_status, baskets_last_7d, basket_norm)
    definition = FRONTEND_STRATEGY_BY_ID[strategy_id]
    assignment = FRONTEND_STRATEGY_ASSIGNMENTS.get(article_id)
    return {
        "id": definition["id"],
        "name": definition["name"],
        "typedStrategyId": definition["typedStrategyId"],
        "type": definition["type"],
        "description": definition["description"],
        "color": definition["color"],
        "assignedAt": assignment.get("assignedAt") if assignment else None,
        "assignmentSource": assignment.get("source") if assignment else "derived",
        "status": definition["status"],
        "config": deepcopy(assignment.get("config") or {}) if assignment else {},
    }


def _demo_repricer_data_enabled(wb_token: str | None) -> bool:
    return wb_token is None and get_settings().wb_api_mode == "fake"


def _stable_int(value: str, minimum: int, maximum: int) -> int:
    if maximum <= minimum:
        return minimum
    seed = sum((index + 1) * ord(char) for index, char in enumerate(value))
    return minimum + seed % (maximum - minimum + 1)


def _optional_wb_goods_price_kopecks(raw: Any) -> int | None:
    kopecks = wb_goods_price_to_kopecks(raw)
    return kopecks if kopecks > 0 else None


def _optional_explicit_kopecks(raw: Any) -> int | None:
    value = _number_or_none(raw)
    if value is None or value <= 0:
        return None
    return int(round(value))


def _seller_spp_pct(seller_discounted_kopecks: int, buyer_price_no_wallet_kopecks: int | None) -> float | None:
    if seller_discounted_kopecks <= 0 or buyer_price_no_wallet_kopecks is None:
        return None
    spp_pct = (
        (Decimal(seller_discounted_kopecks) - Decimal(buyer_price_no_wallet_kopecks))
        / Decimal(seller_discounted_kopecks)
        * Decimal("100")
    )
    if spp_pct < 0 or spp_pct > 100:
        return None
    return float(spp_pct.quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _first_optional_price_kopecks(*raw_values: Any) -> int | None:
    for raw in raw_values:
        value = _optional_wb_goods_price_kopecks(raw)
        if value is not None:
            return value
    return None


def _first_optional_explicit_kopecks(*raw_values: Any) -> int | None:
    for raw in raw_values:
        value = _optional_explicit_kopecks(raw)
        if value is not None:
            return value
    return None


def _derive_wallet_buyer_price_kopecks(
    buyer_price_no_wallet_kopecks: int | None,
    wallet_pct: float | None,
) -> int | None:
    if buyer_price_no_wallet_kopecks is None or wallet_pct is None:
        return None
    price_rub = (Decimal(buyer_price_no_wallet_kopecks) / Decimal("100")) * (
        Decimal("1") - Decimal(str(wallet_pct)) / Decimal("100")
    )
    return int(price_rub.quantize(Decimal("1"), rounding=ROUND_FLOOR) * Decimal("100"))


def _discount_pct(before_kopecks: int | None, after_kopecks: int | None) -> float | None:
    if before_kopecks is None or before_kopecks <= 0 or after_kopecks is None:
        return None
    pct = (Decimal(before_kopecks) - Decimal(after_kopecks)) / Decimal(before_kopecks) * Decimal("100")
    return float(pct.quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _spp_pct_or_none(raw: Any) -> float | None:
    value = _number_or_none(raw)
    if value is None or not isfinite(value) or value < 0 or value > 100:
        return None
    return float(Decimal(str(value)).quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _buyer_price_from_spp_pct(seller_discounted_kopecks: int | None, spp_pct: float | None) -> int | None:
    if seller_discounted_kopecks is None or seller_discounted_kopecks <= 0 or spp_pct is None:
        return None
    multiplier = Decimal("1") - Decimal(str(spp_pct)) / Decimal("100")
    return int((Decimal(seller_discounted_kopecks) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _algorithm_spp_accounting_mode() -> str:
    mode = str(ALGORITHM_SETTINGS_STATE.get("sppAccountingMode") or "spp_only")
    return mode if mode in {"spp_only", "spp_plus_wallet"} else "spp_only"


def _algorithm_wallet_pct() -> float | None:
    value = _number_or_none(ALGORITHM_SETTINGS_STATE.get("wbWalletType"))
    if value is None or value <= 0 or value > 100:
        return None
    return float(Decimal(str(value)).quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _combined_discount_pct(spp_pct: float | None, wallet_pct: float | None) -> float | None:
    if spp_pct is None and wallet_pct is None:
        return None
    spp = Decimal(str(spp_pct or 0))
    wallet = Decimal(str(wallet_pct or 0))
    multiplier = (Decimal("1") - spp / Decimal("100")) * (Decimal("1") - wallet / Decimal("100"))
    return float(((Decimal("1") - multiplier) * Decimal("100")).quantize(Decimal("1.00"), rounding=ROUND_HALF_UP))


def _resolve_spp_analytics(
    good: dict[str, Any],
    price_size: dict[str, Any],
    discounted_price_kopecks: int,
) -> tuple[int | None, int | None, float | None, float | None]:
    """Buyer price without wallet, SPP %, and WB wallet/club %.

    Indeepa-style SPP needs a live buyer price before WB wallet. The official
    Prices API only gives seller prices plus WB Club fields, so clubDiscount
    and clubDiscountedPrice are not treated as SPP.
    """
    wallet_pct = _spp_pct_or_none(good.get("walletPct"))
    if discounted_price_kopecks <= 0:
        return None, None, None, wallet_pct

    buyer_price_no_wallet_kopecks = _first_optional_explicit_kopecks(
        price_size.get("buyerPriceNoWalletKopecks"),
        price_size.get("buyerPriceKopecks"),
        good.get("buyerPriceNoWalletKopecks"),
        good.get("buyerPriceKopecks"),
    )
    if buyer_price_no_wallet_kopecks is None:
        buyer_price_no_wallet_kopecks = _first_optional_price_kopecks(
            price_size.get("buyerPriceNoWallet"),
            price_size.get("buyerPrice"),
            price_size.get("clientPrice"),
            good.get("buyerPriceNoWallet"),
            good.get("buyerPrice"),
            good.get("clientPrice"),
        )
    buyer_price_invalid = (
        buyer_price_no_wallet_kopecks is not None
        and (
            buyer_price_no_wallet_kopecks > discounted_price_kopecks
            or price_size.get("buyerPriceSellerKopecks") not in (None, discounted_price_kopecks)
        )
    )
    if buyer_price_invalid:
        buyer_price_no_wallet_kopecks = None
    buyer_price_with_wallet_kopecks = _first_optional_explicit_kopecks(
        price_size.get("buyerPriceWithWalletKopecks"),
        good.get("buyerPriceWithWalletKopecks"),
    )
    if buyer_price_with_wallet_kopecks is None:
        buyer_price_with_wallet_kopecks = _first_optional_price_kopecks(
            price_size.get("buyerPriceWithWallet"),
            price_size.get("walletPrice"),
            good.get("buyerPriceWithWallet"),
            good.get("walletPrice"),
        )
    if buyer_price_with_wallet_kopecks is None:
        buyer_price_with_wallet_kopecks = _derive_wallet_buyer_price_kopecks(buyer_price_no_wallet_kopecks, wallet_pct)
    if buyer_price_invalid:
        buyer_price_with_wallet_kopecks = None
    if (
        buyer_price_with_wallet_kopecks is not None
        and buyer_price_no_wallet_kopecks is not None
        and buyer_price_with_wallet_kopecks > buyer_price_no_wallet_kopecks
    ):
        buyer_price_with_wallet_kopecks = None
    spp_pct = _seller_spp_pct(discounted_price_kopecks, buyer_price_no_wallet_kopecks)
    return buyer_price_no_wallet_kopecks, buyer_price_with_wallet_kopecks, spp_pct, wallet_pct


def _wb_wallet_pct(raw: Any) -> float | None:
    value = _number_or_none(raw)
    return round(value, 1) if value is not None else None


def _promotion_window_status(item: dict[str, Any], now: datetime) -> str:
    start_at = _parse_iso(str(item.get("startDateTime") or "")) or now
    end_at = _parse_iso(str(item.get("endDateTime") or "")) or now
    if end_at < now:
        return "ended"
    if start_at > now:
        return "upcoming"
    return "active"


def _number_or_none(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _first_number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number_or_none(item.get(key))
        if value is not None:
            return value
    return None


def _kopecks_from_rub(raw: Any) -> int:
    value = _number_or_none(raw)
    return round(value * 100) if value is not None else 0


def _first_kopecks(item: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = _kopecks_from_rub(item.get(key))
        if value:
            return value
    return 0


def _finance_logistics_kopecks(item: dict[str, Any]) -> int:
    return _first_kopecks(item, "delivery_service", "deliveryService", "delivery_rub", "deliveryRub")


def _finance_loyalty_cost_kopecks(item: dict[str, Any]) -> int:
    return _first_kopecks(item, "cashback_amount", "cashbackAmount") + _first_kopecks(
        item, "cashback_commission_change", "cashbackCommissionChange"
    )


def _finance_seller_revenue_kopecks(item: dict[str, Any], quantity: int) -> int:
    # Seller's discounted sale price, not WB's buyer payment or seller payout.
    # The API field is a unit price. Keep decimal rounding at the operation grain.
    raw = _finance_raw_first(item, "retailPriceWithDisc", "retail_price_withdisc_rub")
    value = _number_or_none(raw)
    if value is None or not isfinite(value):
        return 0
    return int((Decimal(str(raw).replace(",", ".")) * abs(quantity) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _finance_operation_date(item: dict[str, Any]) -> date | None:
    doc = str(item.get("docTypeName") or item.get("doc_type_name") or "").strip().lower()
    if doc in {"продажа", "возврат"} and _finance_seller_revenue_kopecks(item, 1):
        return _date_from_any(item.get("saleDt") or item.get("sale_dt"))
    return _date_from_any(item.get("saleDt") or item.get("rrDate") or item.get("date"))


def _finalize_finance_commission(row: dict[str, Any]) -> None:
    reported_commission = int(row.get("commissionKopecks") or 0)
    row["reportedCommissionKopecks"] = reported_commission
    reported_commission_rows = int(row.get("reportedCommissionRows") or 0)
    payable_rows = int(row.pop("_payableRows", 0) or 0)
    settlement_rows = int(row.pop("_settlementRows", 0) or 0)
    if settlement_rows <= 0 or payable_rows < settlement_rows:
        if not reported_commission_rows:
            row["commissionKopecks"] = int(row.get("commissionFormulaKopecks") or 0)
        row["commissionSource"] = "ppvzSalesCommission" if reported_commission_rows else "commissionPercent"
        return
    commission_with_acquiring = int(row.get("buyerRevenueKopecks") or 0) - int(
        row.get("payableKopecks") or 0
    )
    row["commissionKopecks"] = commission_with_acquiring - int(row.get("acquiringKopecks") or 0)
    row["commissionSource"] = "buyerRevenueKopecks-payableKopecks-acquiringKopecks"


def _tariff_base_commission_pct(row: dict[str, Any]) -> float | None:
    # Do not substitute a different fulfilment model when this tariff is absent.
    return _spp_pct_or_none(row.get("kgvpMarketplace"))


def _commission_tariffs_index_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in rows:
        base_pct = _tariff_base_commission_pct(item)
        if base_pct is None:
            continue
        normalized = {
            "subjectID": _int_or_none(item.get("subjectID") or item.get("subjectId")),
            "subjectName": str(item.get("subjectName") or ""),
            "baseCommissionPct": base_pct,
            "sourceField": "tariffs.commission.kgvpMarketplace",
        }
        if normalized["subjectID"] is not None:
            index[f"id:{normalized['subjectID']}"] = normalized
        subject_name = _normalize_subject_name(normalized["subjectName"])
        if subject_name:
            index[f"name:{subject_name}"] = normalized
    return index


def fetch_commission_tariffs(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    force: bool = False,
    organization_id: int | None = None,
) -> dict[str, dict[str, Any]]:
    cache_key = (scenario, f"org:{organization_id}" if organization_id is not None else _token_cache_key(wb_token))
    cached = COMMISSION_TARIFFS_CACHE.get(cache_key)
    if cached is None and organization_id is not None:
        persisted = get_source_cache(organization_id, "commission_tariffs", slim=True) or {}
        fetched_at = _parse_iso(str(persisted.get("fetchedAt") or ""))
        if fetched_at is not None and isinstance(persisted.get("index"), dict):
            cached = (fetched_at, persisted["index"])
    now = _utc_now()
    if not force and cached is not None and now - cached[0] <= COMMISSION_TARIFFS_TTL:
        return cached[1]

    try:
        client = RateLimitedWbApiClient(inner=build_wb_common_client(scenario, token_override=wb_token))
        envelope = client.request(WbApiRequest(method="GET", path="/api/v1/tariffs/commission", query={"locale": "ru"}))
    except Exception:
        return cached[1] if cached is not None else {}
    if not envelope.ok:
        return cached[1] if cached is not None else {}
    payload = envelope.data if isinstance(envelope.data, dict) else {}
    report = payload.get("report") if isinstance(payload, dict) else None
    index = _commission_tariffs_index_from_rows(report if isinstance(report, list) else [])
    if not index:
        return cached[1] if cached is not None else {}
    for tariff in index.values():
        tariff["fetchedAt"] = now.isoformat()
    COMMISSION_TARIFFS_CACHE[cache_key] = (now, index)
    if organization_id is not None:
        save_source_cache(organization_id, "commission_tariffs", {"index": index})
    return index


def cached_commission_tariffs(
    scenario: str = "complete", wb_token: str | None = None, *, organization_id: int | None = None,
) -> dict[str, dict[str, Any]]:
    if organization_id is not None:
        persisted = get_source_cache(organization_id, "commission_tariffs", slim=True) or {}
        return persisted.get("index") if isinstance(persisted.get("index"), dict) else {}
    cached = COMMISSION_TARIFFS_CACHE.get((scenario, _token_cache_key(wb_token)))
    if cached is None:
        return {}
    return cached[1]


def _fallback_subject_for_article(article_id: str) -> str | None:
    return ARTICLE_TYPE_FALLBACK_SUBJECTS.get(_article_type(article_id))


def _resolve_tariff_base_commission_pct(
    tariffs_index: dict[str, dict[str, Any]] | None,
    *,
    article_id: str,
    subject_id: int | None,
    subject: str,
) -> tuple[float | None, str | None, str | None]:
    row = None
    if tariffs_index:
        if subject_id is not None:
            row = tariffs_index.get(f"id:{subject_id}")
        if row is None:
            row = tariffs_index.get(f"name:{_normalize_subject_name(subject)}")
    if row is not None:
        return _number_or_none(row.get("baseCommissionPct")), str(row.get("sourceField") or "tariffs.commission"), row.get("fetchedAt")
    return None, None, None


def _basket_norm_garment_key(article_id: str) -> str:
    type_key = _article_type(article_id)
    if type_key == "H":
        return "hoodie"
    if type_key == "L":
        return "longsleeve"
    return "tshirt"


def _algorithm_int_setting(key: str, default: int = 0) -> int:
    from app.repricer_settings import current_algorithm_settings
    raw = current_algorithm_settings(ALGORITHM_SETTINGS_STATE).get(key)
    if raw is None or raw == "":
        raw = DEFAULT_ALGORITHM_SETTINGS.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _algorithm_dict_setting(key: str) -> dict[str, Any]:
    raw = ALGORITHM_SETTINGS_STATE.get(key)
    if not isinstance(raw, dict):
        raw = DEFAULT_ALGORITHM_SETTINGS.get(key, {})
    return deepcopy(raw) if isinstance(raw, dict) else {}


def _default_basket_norm(article_id: str) -> int:
    fallback_by_garment = _algorithm_dict_setting("basketNormFallbackByGarment")
    garment_key = _basket_norm_garment_key(article_id)
    raw = fallback_by_garment.get(garment_key)
    if raw is None:
        raw = {"hoodie": 15, "longsleeve": 10}.get(garment_key, 20)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 20


def _basket_norm_period_days() -> int:
    return max(1, _algorithm_int_setting("basketNormPeriodDays", 7))


def _resolve_basket_norm_for_row(
    article_id: str,
    meta: dict[str, Any],
    settings: dict[str, Any],
    *,
    period_orders_units: int,
    period_days: int | None,
) -> None:
    sku_mode = str(settings.get("basketNormMode") or "").strip().lower()
    manual_norm = settings.get("basketNormManual")
    if sku_mode == "manual" and manual_norm is not None:
        try:
            meta["basketNorm"] = max(0, int(manual_norm))
            meta["basketNormSource"] = "manual"
            return
        except (TypeError, ValueError):
            pass

    mode = str(ALGORITHM_SETTINGS_STATE.get("basketNormMode") or "fallback_by_type").strip().lower()
    if mode in {"auto", "auto_orders", "auto_orders_avg"} and period_orders_units > 0:
        source_days = max(1, int(period_days or _basket_norm_period_days()))
        norm_days = _basket_norm_period_days()
        min_orders = max(0, _algorithm_int_setting("basketNormAutoMinOrders", 1))
        meta["basketNorm"] = max(min_orders, round(period_orders_units / source_days * norm_days))
        meta["basketNormSource"] = "auto"
        return

    meta["basketNorm"] = _default_basket_norm(article_id)
    meta["basketNormSource"] = "fallback"


def _sku_meta_seed(article_id: str, use_demo_data: bool) -> dict[str, Any]:
    basket_norm = _default_basket_norm(article_id)
    baskets_last_7d = _stable_int(article_id, 2, basket_norm + 8)
    defaults = {
        "status": "auto",
        "basketsLast7d": baskets_last_7d,
        "basketNorm": basket_norm,
        "basketNormSource": "fallback",
        "warmupDaysLeft": None,
        "managerId": None,
        "managerName": "Без ответственного",
        "assignmentSource": "none",
        "assignedAt": None,
    }
    if use_demo_data:
        defaults.update(
            {
                "basketsLast7d": 20,
                "basketNorm": 20,
                "basketNormSource": "auto",
                "managerId": "manager-maria-dudina",
                "managerName": "Мария Дудина",
                "assignmentSource": "manual",
                "assignedAt": (_utc_now() - timedelta(days=2)).isoformat(),
            }
        )
        defaults.update(DEMO_SKU_META_OVERRIDES.get(article_id, {}))
    defaults.update(SKU_META_OVERRIDES.get(article_id, {}))
    if defaults["status"] == "warmup" and not defaults.get("basketNormSource"):
        defaults["basketNormSource"] = "fallback"
    return defaults


def _sku_comments_for_article(article_id: str, use_demo_data: bool) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    if use_demo_data:
        comments.extend(deepcopy(DEMO_SKU_COMMENTS.get(article_id, [])))
    comments.extend(deepcopy(SKU_COMMENTS.get(article_id, [])))
    return comments


def _sku_audit_events_for_article(article_id: str, use_demo_data: bool) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if use_demo_data:
        events.extend(deepcopy(DEMO_SKU_AUDIT_EVENTS.get(article_id, [])))
    events.extend(deepcopy(SKU_AUDIT_EVENTS.get(article_id, [])))
    return events


def _ensure_ok(data: Any, path: str) -> Any:
    if isinstance(data, dict) and data.get("error") is False and "data" in data:
        return data["data"]
    if isinstance(data, dict) and "data" in data and len(data) == 1:
        return data["data"]
    return data


def _rate_limit_details(response: WbApiResponseEnvelope) -> dict[str, Any] | None:
    if response.rateLimit is None:
        return None
    payload = response.rateLimit.model_dump(exclude_none=True)
    return payload if payload else None


def _upstream_error_headers(response: WbApiResponseEnvelope) -> dict[str, str]:
    headers: dict[str, str] = {"X-Upstream-Status": str(response.statusCode)}
    if response.wbRequestId:
        headers["X-WB-Request-Id"] = response.wbRequestId
    rate_limit = _rate_limit_details(response)
    if rate_limit is None:
        return headers
    if "limit" in rate_limit:
        headers["X-Ratelimit-Limit"] = str(rate_limit["limit"])
    if "remaining" in rate_limit:
        headers["X-Ratelimit-Remaining"] = str(rate_limit["remaining"])
    if "retryAfterSeconds" in rate_limit:
        headers["X-Ratelimit-Retry"] = str(rate_limit["retryAfterSeconds"])
        headers["Retry-After"] = str(rate_limit["retryAfterSeconds"])
    if "resetAfterSeconds" in rate_limit:
        headers["X-Ratelimit-Reset"] = str(rate_limit["resetAfterSeconds"])
    return headers


def _upstream_error_status(response: WbApiResponseEnvelope) -> int:
    if response.statusCode == 429 or response.statusCode >= 500:
        return response.statusCode
    return 502


def _wb_upstream_url(path: str) -> str:
    settings = get_settings()
    if path.startswith("/api/finance/"):
        return f"{settings.wb_finance_api_base_url}{path}"
    if path.startswith("/adv/") or path.startswith("/api/advert/"):
        return f"{settings.wb_ads_api_base_url}{path}"
    if path.startswith("/api/v1/calendar/"):
        return f"{settings.wb_promotions_api_base_url}{path}"
    if path.startswith("/api/analytics/") or path.startswith("/api/v1/warehouse_remains") or path.startswith("/api/v1/acceptance_report") or path.startswith("/api/v1/paid_storage"):
        return f"{settings.wb_analytics_api_base_url}{path}"
    if path.startswith("/api/v1/supplier/") or path.startswith("/api/v5/supplier/"):
        return f"{settings.wb_statistics_api_base_url}{path}"
    if path.startswith("/api/v1/feedback") or path.startswith("/api/v1/feedbacks") or path.startswith("/api/common/v1/rating"):
        return f"{settings.wb_feedbacks_api_base_url}{path}"
    if path.startswith("/content/") or path.startswith("/api/v2/cards"):
        return f"{settings.wb_content_api_base_url}{path}"
    if path.startswith("/api/v1/tariffs/"):
        return f"{settings.wb_common_api_base_url}{path}"
    return f"{settings.wb_api_base_url}{path}"


def _raise_upstream_error(response: WbApiResponseEnvelope) -> None:
    message = response.error.message if response.error else f"WB request failed for {response.request.path}"
    details: dict[str, Any] = {
        "source": "wb",
        "upstreamPath": response.request.path,
        "upstreamUrl": _wb_upstream_url(response.request.path),
        "upstreamStatus": response.statusCode,
        "wbRequestId": response.wbRequestId,
    }
    if response.error is not None:
        details["upstreamCode"] = response.error.code
        details["retryable"] = response.error.retryable
    rate_limit = _rate_limit_details(response)
    if rate_limit is not None:
        details["rateLimit"] = rate_limit

    raise HTTPException(
        status_code=_upstream_error_status(response),
        detail={
            "code": response.error.code if response.error is not None else "wb_request_failed",
            "message": message,
            "details": details,
        },
        headers=_upstream_error_headers(response),
    )


def _is_retryable_transport_response(response: WbApiResponseEnvelope) -> bool:
    return bool(
        not response.ok
        and response.error is not None
        and response.error.retryable
        and response.error.code == "wb_transport_error"
        and response.statusCode >= 500
    )


def _http_exception_is_retryable_transport(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    details = detail.get("details") if isinstance(detail, dict) else None
    if not isinstance(details, dict):
        return False
    return bool(
        details.get("retryable") is True
        and str(detail.get("code") or details.get("upstreamCode") or "") == "wb_transport_error"
        and int(details.get("upstreamStatus") or exc.status_code or 0) >= 500
    )


def _request_or_raise(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    from app.wb_api.report_reads import active_report_read
    last_response: WbApiResponseEnvelope | None = None
    attempts = 1 if active_report_read.get() is not None else _WB_TRANSPORT_MAX_ATTEMPTS
    for attempt in range(attempts):
        response = client.request(request)
        last_response = response
        if response.ok:
            if request.path.startswith("/api/finance/v1/sales-reports/"):
                if response.statusCode == 204:
                    return []
                if response.statusCode != 200:
                    raise HTTPException(status_code=502, detail="WB_FINANCE_INVALID_RESPONSE")
            return _ensure_ok(response.data, request.path)
        if not _is_retryable_transport_response(response) or attempt >= attempts - 1:
            _raise_upstream_error(response)
        time.sleep(_WB_TRANSPORT_RETRY_DELAYS_S[min(attempt, len(_WB_TRANSPORT_RETRY_DELAYS_S) - 1)])
    if last_response is not None:
        _raise_upstream_error(last_response)
    raise RuntimeError("WB request failed without response")


def _retry_after_from_http_exception(exc: HTTPException) -> float | None:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    details = detail.get("details") if isinstance(detail, dict) else None
    if not isinstance(details, dict):
        return None
    rate_limit = details.get("rateLimit")
    if not isinstance(rate_limit, dict):
        return None
    retry_after = rate_limit.get("retryAfterSeconds")
    if retry_after is None:
        return None
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


def _ads_fullstats_has_transient_error(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    details = detail.get("details") if isinstance(detail, dict) else None
    if isinstance(details, dict):
        try:
            upstream_status = int(details.get("upstreamStatus") or 0)
        except (TypeError, ValueError):
            upstream_status = 0
        if upstream_status >= 500:
            return True
        if details.get("retryable") is True and exc.status_code >= 500:
            return True

    text_parts = [str(exc.status_code)]
    if isinstance(detail, dict):
        text_parts.append(str(detail.get("message") or ""))
        text_parts.append(str(detail.get("code") or ""))
    elif isinstance(exc.detail, str):
        text_parts.append(exc.detail)
    if isinstance(details, dict):
        text_parts.extend(str(value) for value in details.values() if value is not None)
    text = " ".join(text_parts).lower()
    return any(marker in text for marker in _ADS_FULLSTATS_TRANSIENT_ERROR_MARKERS)


def _request_or_raise_calendar(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    """Calendar API allows ~1 req/s; serialize and retry WB 429 responses."""
    global _promotions_calendar_last_request_at
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return _request_or_raise(client, request)
    last_exc: HTTPException | None = None
    for attempt in range(_PROMOTIONS_CALENDAR_MAX_ATTEMPTS):
        now = time.monotonic()
        wait_s = _PROMOTIONS_CALENDAR_MIN_INTERVAL_S - (now - _promotions_calendar_last_request_at)
        if wait_s > 0:
            time.sleep(wait_s)
        _promotions_calendar_last_request_at = time.monotonic()
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            if exc.status_code != 429 or attempt >= _PROMOTIONS_CALENDAR_MAX_ATTEMPTS - 1:
                raise
            retry_s = _retry_after_from_http_exception(exc) or _PROMOTIONS_CALENDAR_MIN_INTERVAL_S
            time.sleep(max(retry_s, _PROMOTIONS_CALENDAR_MIN_INTERVAL_S))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("calendar request failed without exception")


def _request_or_raise_finance_report(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    """Finance detailed report is seller-limited to roughly one request per minute."""
    global _finance_report_last_request_at
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return _request_or_raise(client, request)
    if isinstance(getattr(client, "inner", None), FakeWbApiClient):
        return _request_or_raise(client, request)
    last_exc: HTTPException | None = None
    for attempt in range(_FINANCE_REPORT_MAX_ATTEMPTS):
        if get_settings().wb_api_mode == "real":
            now = time.monotonic()
            wait_s = _FINANCE_REPORT_MIN_INTERVAL_S - (now - _finance_report_last_request_at)
            if wait_s > 0:
                time.sleep(wait_s)
            _finance_report_last_request_at = time.monotonic()
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            if exc.status_code != 429 or attempt >= _FINANCE_REPORT_MAX_ATTEMPTS - 1:
                raise
            retry_s = _retry_after_from_http_exception(exc) or _FINANCE_REPORT_MIN_INTERVAL_S
            if retry_s > _FINANCE_REPORT_MAX_RETRY_AFTER_S:
                raise
            time.sleep(max(retry_s, _FINANCE_REPORT_MIN_INTERVAL_S) + 1.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("finance report request failed without exception")


def _request_or_raise_stock_report(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    """Stocks report is seller-limited; serialize wb/mp requests and honor WB retry headers."""
    global _stock_report_last_request_at
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return _request_or_raise(client, request)
    last_exc: HTTPException | None = None
    for attempt in range(_STOCK_REPORT_MAX_ATTEMPTS):
        if get_settings().wb_api_mode == "real":
            with _stock_report_lock:
                now = time.monotonic()
                wait_s = _STOCK_REPORT_MIN_INTERVAL_S - (now - _stock_report_last_request_at)
                if wait_s > 0:
                    time.sleep(wait_s)
                _stock_report_last_request_at = time.monotonic()
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            if exc.status_code != 429 or attempt >= _STOCK_REPORT_MAX_ATTEMPTS - 1:
                raise
            retry_s = _retry_after_from_http_exception(exc) or _STOCK_REPORT_RETRY_DELAYS_S[min(attempt, len(_STOCK_REPORT_RETRY_DELAYS_S) - 1)]
            time.sleep(max(retry_s, _STOCK_REPORT_MIN_INTERVAL_S) + 1.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("stocks report request failed without exception")


def _request_or_raise_statistics_report(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    """Statistics supplier reports are seller-limited; serialize orders/sales and honor WB retry headers."""
    global _statistics_report_last_request_at
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return _request_or_raise(client, request)
    last_exc: HTTPException | None = None
    for attempt in range(_STATISTICS_REPORT_MAX_ATTEMPTS):
        with _statistics_report_lock:
            now = time.monotonic()
            wait_s = _STATISTICS_REPORT_MIN_INTERVAL_S - (now - _statistics_report_last_request_at)
            if wait_s > 0:
                time.sleep(wait_s)
            _statistics_report_last_request_at = time.monotonic()
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            if exc.status_code != 429 or attempt >= _STATISTICS_REPORT_MAX_ATTEMPTS - 1:
                raise
            retry_s = _retry_after_from_http_exception(exc) or _STATISTICS_REPORT_MIN_INTERVAL_S
            if retry_s > _STATISTICS_REPORT_MAX_RETRY_AFTER_S:
                raise
            time.sleep(max(retry_s, _STATISTICS_REPORT_MIN_INTERVAL_S) + 1.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("statistics report request failed without exception")


def _request_or_raise_ads_fullstats(
    client: RateLimitedWbApiClient,
    request: WbApiRequest,
    *,
    wait_callback: Callable[[str], None] | None = None,
) -> Any:
    """Ads fullstats is a heavy endpoint: WB allows about one request every 20 seconds."""
    global _ads_fullstats_last_request_at
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        from app.wb_api.ads_runtime import _request_with_retry
        response = _request_with_retry(client, request, min_interval_s=_ADS_FULLSTATS_MIN_INTERVAL_S)
        if response.ok:
            return _ensure_ok(response.data, request.path)
        _raise_upstream_error(response)
    last_exc: HTTPException | None = None
    transient_attempts = 0
    for attempt in range(_ADS_FULLSTATS_MAX_ATTEMPTS):
        with _ads_fullstats_lock:
            now = time.monotonic()
            wait_s = _ADS_FULLSTATS_MIN_INTERVAL_S - (now - _ads_fullstats_last_request_at)
            if wait_s > 0:
                if wait_callback is not None:
                    wait_callback(f"WB Ads fullstats: ждём окно лимита {round(wait_s)} сек")
                time.sleep(wait_s)
            _ads_fullstats_last_request_at = time.monotonic()
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            if exc.status_code == 429:
                if attempt >= _ADS_FULLSTATS_MAX_ATTEMPTS - 1:
                    raise
                retry_s = _retry_after_from_http_exception(exc) or _ADS_FULLSTATS_MIN_INTERVAL_S
                if retry_s > _ADS_FULLSTATS_MAX_RETRY_AFTER_S:
                    raise
                wait_s = max(retry_s, _ADS_FULLSTATS_MIN_INTERVAL_S) + 2.0
                if wait_callback is not None:
                    wait_callback(f"WB Ads fullstats: 429, ждём {round(wait_s)} сек и повторяем {attempt + 2}/{_ADS_FULLSTATS_MAX_ATTEMPTS}")
                time.sleep(wait_s)
                continue
            if not _ads_fullstats_has_transient_error(exc):
                raise
            transient_attempts += 1
            if transient_attempts >= _ADS_FULLSTATS_TRANSIENT_MAX_ATTEMPTS:
                raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("ads fullstats request failed without exception")


def _ads_fullstats_is_invalid_payload(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    details = detail.get("details") if isinstance(detail, dict) else None
    upstream_status = 0
    if isinstance(details, dict):
        try:
            upstream_status = int(details.get("upstreamStatus") or 0)
        except (TypeError, ValueError):
            upstream_status = 0

    text_parts = [str(exc.status_code)]
    if isinstance(detail, dict):
        text_parts.append(str(detail.get("message") or ""))
        text_parts.append(str(detail.get("code") or ""))
    elif isinstance(exc.detail, str):
        text_parts.append(exc.detail)
    if isinstance(details, dict):
        text_parts.extend(str(value) for value in details.values() if value is not None)
    text = " ".join(text_parts).lower()
    return upstream_status in {400, 422} and "invalid payload" in text


def _ads_fullstats_query(campaign_ids: list[int], start: date, end: date) -> dict[str, str]:
    return {
        "ids": ",".join(str(item) for item in campaign_ids),
        "beginDate": start.isoformat(),
        "endDate": end.isoformat(),
    }


def _ads_fullstats_date_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=30), end)
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _request_ads_fullstats_payloads(
    client: RateLimitedWbApiClient,
    campaign_ids: list[int],
    *,
    start: date,
    end: date,
    wait_callback: Callable[[str], None] | None = None,
) -> list[Any]:
    if not campaign_ids:
        return []
    if (end - start).days > 30:
        return [
            payload
            for window_start, window_end in _ads_fullstats_date_windows(start, end)
            for payload in _request_ads_fullstats_payloads(
                client,
                campaign_ids,
                start=window_start,
                end=window_end,
                wait_callback=wait_callback,
            )
        ]
    try:
        fullstats = _request_or_raise_ads_fullstats(
            client,
            WbApiRequest(
                method="GET",
                path="/adv/v3/fullstats",
                query=_ads_fullstats_query(campaign_ids, start, end),
            ),
            wait_callback=wait_callback,
        )
    except HTTPException as exc:
        if not _ads_fullstats_is_invalid_payload(exc):
            raise
        if len(campaign_ids) == 1:
            logger.warning("Skipping WB ads fullstats campaign %s: %s", campaign_ids[0], _http_exception_message(exc))
            return []
        midpoint = len(campaign_ids) // 2
        return [
            *_request_ads_fullstats_payloads(client, campaign_ids[:midpoint], start=start, end=end, wait_callback=wait_callback),
            *_request_ads_fullstats_payloads(client, campaign_ids[midpoint:], start=start, end=end, wait_callback=wait_callback),
        ]
    payload = fullstats.get("data") if isinstance(fullstats, dict) and fullstats.get("data") is not None else fullstats
    return [payload]


def _request_or_raise_sales_funnel_products(
    client: RateLimitedWbApiClient,
    request: WbApiRequest,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Sales funnel products is seller-limited; serialize pages and honor WB Retry-After."""
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return _request_or_raise(client, request)
    last_exc: HTTPException | None = None
    for attempt in range(_SALES_FUNNEL_PRODUCTS_MAX_ATTEMPTS):
        try:
            return _request_or_raise(client, request)
        except HTTPException as exc:
            last_exc = exc
            retryable_transport = _http_exception_is_retryable_transport(exc)
            if (exc.status_code != 429 and not retryable_transport) or attempt >= _SALES_FUNNEL_PRODUCTS_MAX_ATTEMPTS - 1:
                raise
            retry_s = _retry_after_from_http_exception(exc) or _SALES_FUNNEL_PRODUCTS_MIN_INTERVAL_S
            if retry_s > _SALES_FUNNEL_PRODUCTS_MAX_RETRY_AFTER_S:
                raise WbSalesFunnelDeferred(
                    retry_after_seconds=retry_s,
                    message=f"WB Sales Funnel paused by long Retry-After: {round(retry_s)} sec",
                ) from exc
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "rate_limit",
                        "message": f"WB Sales Funnel: 429, ждём {round(max(retry_s, _SALES_FUNNEL_PRODUCTS_MIN_INTERVAL_S))} сек",
                        "retryAfterSeconds": retry_s,
                        "attempt": attempt + 1,
                        "maxAttempts": _SALES_FUNNEL_PRODUCTS_MAX_ATTEMPTS,
                    }
                )
            time.sleep(max(retry_s, _SALES_FUNNEL_PRODUCTS_MIN_INTERVAL_S) + 1.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("sales funnel products request failed without exception")


def _catalog_goods_retry_after_seconds(response: WbApiResponseEnvelope) -> float | None:
    rate_limit = response.rateLimit
    if rate_limit is not None and rate_limit.retryAfterSeconds is not None and rate_limit.retryAfterSeconds > 0:
        return float(rate_limit.retryAfterSeconds)
    message = response.error.message.lower() if response.error is not None else ""
    if "global limiter" in message or "rate limit" in message or "too many" in message:
        return _CATALOG_GOODS_DEFAULT_RETRY_AFTER_S
    return None


def _content_cards_retry_after_seconds(response: WbApiResponseEnvelope) -> float | None:
    rate_limit = response.rateLimit
    if rate_limit is not None and rate_limit.retryAfterSeconds is not None and rate_limit.retryAfterSeconds > 0:
        return float(rate_limit.retryAfterSeconds)
    message = response.error.message.lower() if response.error is not None else ""
    if "global limiter" in message or "rate limit" in message or "too many" in message:
        return _CONTENT_CARDS_DEFAULT_RETRY_AFTER_S
    return None


def _request_or_raise_catalog_goods(client: RateLimitedWbApiClient, request: WbApiRequest) -> Any:
    last_response: WbApiResponseEnvelope | None = None
    for attempt in range(_CATALOG_GOODS_MAX_ATTEMPTS):
        response = client.request(request)
        last_response = response
        if response.ok:
            return _ensure_ok(response.data, request.path)
        if response.statusCode != 429 or attempt >= _CATALOG_GOODS_MAX_ATTEMPTS - 1:
            _raise_upstream_error(response)
        retry_s = _catalog_goods_retry_after_seconds(response)
        if retry_s is None or retry_s > _CATALOG_GOODS_MAX_RETRY_AFTER_S:
            _raise_upstream_error(response)
        sleep_fn = getattr(client, "_sleep", time.sleep)
        sleep_fn(retry_s + 1.0)
    if last_response is not None:
        _raise_upstream_error(last_response)
    raise RuntimeError("catalog goods request failed without response")


def _request_catalog_goods_page(client: RateLimitedWbApiClient, *, limit: int, offset: int) -> Any:
    post_attempts = [
        WbApiRequest(
            method="POST",
            path="/api/v2/list/goods/filter",
            query={"limit": limit, "offset": offset},
            jsonBody={"limit": limit, "offset": offset},
        ),
        WbApiRequest(
            method="POST",
            path="/api/v2/list/goods/filter",
            query={"limit": limit, "offset": offset},
        ),
    ]
    last_exc: HTTPException | None = None
    for post_request in post_attempts:
        try:
            return _request_or_raise_catalog_goods(client, post_request)
        except HTTPException as exc:
            if exc.status_code == 429:
                raise
            last_exc = exc
    get_request = WbApiRequest(
        method="GET",
        path="/api/v2/list/goods/filter",
        query={"limit": limit, "offset": offset},
    )
    try:
        return _request_or_raise_catalog_goods(client, get_request)
    except HTTPException:
        if last_exc is not None:
            raise last_exc
        raise


def _good_missing_spp_fields(good: dict[str, Any]) -> bool:
    size = good.get("sizes") or []
    first_size = size[0] if isinstance(size, list) and size and isinstance(size[0], dict) else {}
    for source in (first_size, good):
        for key in (
            "buyerPriceNoWallet",
            "buyerPriceNoWalletKopecks",
            "buyerPriceKopecks",
            "buyerPrice",
            "clientPrice",
        ):
            if source.get(key) is not None:
                return False
    return True


def _merge_good_spp_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    if source.get("clubDiscount") is not None:
        target["clubDiscount"] = source.get("clubDiscount")
    source_sizes = source.get("sizes") or []
    source_size = source_sizes[0] if isinstance(source_sizes, list) and source_sizes and isinstance(source_sizes[0], dict) else {}
    if not source_size:
        return
    target_sizes = target.get("sizes") or []
    target_size = dict(target_sizes[0]) if target_sizes and isinstance(target_sizes[0], dict) else {}
    for key in (
        "discountedPrice",
        "price",
        "clubDiscountedPrice",
        "buyerPriceNoWallet",
        "buyerPriceNoWalletKopecks",
        "buyerPriceKopecks",
        "buyerPrice",
        "clientPrice",
        "buyerPriceWithWallet",
        "buyerPriceWithWalletKopecks",
    ):
        if source_size.get(key) is not None:
            target_size[key] = source_size.get(key)
    # Preserve source size grain; enrichment of the first legacy row must not
    # discard the remaining offers or copy buyer/Club fields onto them.
    target["sizes"] = [target_size, *target_sizes[1:]]


def _enrich_goods_page_spp_fields(
    scenario: str,
    wb_token: str | None,
    goods: list[dict[str, Any]],
) -> None:
    """One batched WB-06 fetch per catalog page (used only on goods refresh, not on list reads)."""
    if not goods or wb_token is None:
        return
    if not any(_good_missing_spp_fields(good) for good in goods):
        return

    goods_by_nm: dict[int, dict[str, Any]] = {}
    nm_ids: list[int] = []
    for good in goods:
        nm_id = int(good.get("nmID") or 0)
        if nm_id <= 0:
            continue
        goods_by_nm[nm_id] = good
        nm_ids.append(nm_id)
    if not nm_ids:
        return

    client = RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=wb_token))
    batch_size = 100
    for start in range(0, len(nm_ids), batch_size):
        batch = nm_ids[start : start + batch_size]
        try:
            payload = _request_or_raise_catalog_goods(
                client,
                WbApiRequest(
                    method="POST",
                    path="/api/v2/list/goods/filter",
                    query={"limit": len(batch)},
                    jsonBody={"nmList": batch},
                ),
            )
        except HTTPException:
            continue
        page = payload.get("listGoods", []) if isinstance(payload, dict) else []
        for item in page:
            if not isinstance(item, dict):
                continue
            nm_id = int(item.get("nmID") or 0)
            target = goods_by_nm.get(nm_id)
            if target is not None:
                _merge_good_spp_fields(target, item)


def validate_catalog_goods_page(goods: Any, *, limit: int, seen: set[int]) -> None:
    """Reject malformed/repeated pages rather than publishing a truncated catalog."""
    if not isinstance(goods, list) or len(goods) > limit:
        raise HTTPException(status_code=502, detail="WB_CATALOG_INVALID_PAGE")
    for item in goods:
        nm_id = item.get("nmID") if isinstance(item, dict) else None
        if isinstance(nm_id, bool) or not isinstance(nm_id, int) or nm_id <= 0:
            raise HTTPException(status_code=502, detail="WB_CATALOG_INVALID_PRODUCT_ID")
        if nm_id in seen:
            raise HTTPException(status_code=502, detail="WB_CATALOG_REPEATED_PRODUCT")
        seen.add(nm_id)


def _fetch_catalog_goods(scenario: str, wb_token: str | None = None) -> list[dict[str, Any]]:
    cache_key = (scenario, _token_cache_key(wb_token))
    cached = CATALOG_GOODS_CACHE.get(cache_key)
    now = _utc_now()
    if cached is not None:
        cached_at, cached_goods = cached
        if (now - cached_at).total_seconds() <= CATALOG_GOODS_CACHE_TTL_SECONDS:
            return deepcopy(cached_goods)

    client = RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=wb_token))
    limit = 1000
    offset = 0
    seen: set[int] = set()
    goods: list[dict[str, Any]] = []

    while True:
        payload = _request_catalog_goods_page(client, limit=limit, offset=offset)
        page = payload.get("listGoods") if isinstance(payload, dict) else None
        validate_catalog_goods_page(page, limit=limit, seen=seen)
        goods.extend(page)
        if len(page) < limit:
            break
        offset += limit
    if goods:
        CATALOG_GOODS_CACHE[cache_key] = (now, deepcopy(goods))
    return goods


def fetch_catalog_goods_page(
    scenario: str,
    wb_token: str | None = None,
    *,
    limit: int = 1000,
    offset: int = 0,
    enrich_spp: bool = True,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=wb_token))
    payload = _request_catalog_goods_page(client, limit=limit, offset=offset)
    raw_goods = payload.get("listGoods") if isinstance(payload, dict) else None
    validate_catalog_goods_page(raw_goods, limit=limit, seen=set())
    goods = [dict(item) for item in raw_goods]
    if enrich_spp:
        _enrich_goods_page_spp_fields(scenario, wb_token, goods)
    return {
        "goods": goods,
        "limit": limit,
        "offset": offset,
        "wbRequestId": None,
    }


def _request_or_raise_content_cards(
    client: RateLimitedWbApiClient,
    request: WbApiRequest,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    last_response: WbApiResponseEnvelope | None = None
    for attempt in range(_CONTENT_CARDS_MAX_ATTEMPTS):
        response = client.request(request)
        last_response = response
        if response.ok:
            return _ensure_ok(response.data, request.path)
        if response.statusCode != 429 or attempt >= _CONTENT_CARDS_MAX_ATTEMPTS - 1:
            _raise_upstream_error(response)
        retry_s = _content_cards_retry_after_seconds(response)
        if retry_s is None or retry_s > _CONTENT_CARDS_MAX_RETRY_AFTER_S:
            _raise_upstream_error(response)
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "rate_limit",
                    "message": f"WB ограничил карточки, ждём окно лимита {int(round(retry_s))} сек",
                    "retryAfterSeconds": retry_s,
                    "attempt": attempt + 1,
                    "maxAttempts": _CONTENT_CARDS_MAX_ATTEMPTS,
                }
            )
        sleep_fn = getattr(client, "_sleep", time.sleep)
        sleep_fn(retry_s + 1.0)
    if last_response is not None:
        _raise_upstream_error(last_response)
    raise RuntimeError("content cards request failed without response")


def _fetch_content_cards(
    scenario: str,
    wb_token: str | None = None,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    page_callback: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    client = RateLimitedWbApiClient(inner=build_wb_content_client(scenario, token_override=wb_token))
    cursor: dict[str, Any] | None = None
    seen: set[int] = set()
    cards: list[dict[str, Any]] = []

    for page_index in count():
        request_body: dict[str, Any] = {"settings": {"cursor": {"limit": 100}}}
        if cursor:
            request_body["settings"]["cursor"].update(cursor)
        payload = _request_or_raise_content_cards(
            client,
            WbApiRequest(method="POST", path="/content/v2/get/cards/list", jsonBody=request_body),
            progress_callback=progress_callback,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
            raise RuntimeError("WB вернул некорректный список карточек; прежний каталог сохранён")
        page = payload["cards"]
        if any(not isinstance(card, dict) or not str(card.get("nmID", "")).isdigit()
               or int(card["nmID"]) <= 0 for card in page):
            raise RuntimeError("WB вернул карточку без корректного ID; прежний каталог сохранён")
        if page_callback and page:
            page_callback(page)
        fresh = 0
        for card in page:
            nm_id = int(card.get("nmID") or 0)
            if nm_id <= 0 or nm_id in seen:
                continue
            seen.add(nm_id)
            cards.append(card)
            fresh += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "cards",
                    "message": f"Карточки WB: получено {len(cards)}",
                    "progressCurrent": len(cards),
                    "progressTotal": max(len(cards), (page_index + 2) * 100) if page and len(page) >= 100 else len(cards),
                }
            )
        next_cursor = payload.get("cursor")
        if len(page) < 100:
            break
        if (fresh == 0 or not isinstance(next_cursor, dict)
                or not isinstance(next_cursor.get("updatedAt"), str) or not next_cursor["updatedAt"].strip()
                or not str(next_cursor.get("nmID", "")).isdigit() or int(next_cursor["nmID"]) <= 0
                or (cursor and all(next_cursor.get(key) == cursor.get(key) for key in ("updatedAt", "nmID")))):
            raise RuntimeError("WB не подтвердил следующую страницу карточек; прежний каталог сохранён")
        cursor = {"updatedAt": next_cursor.get("updatedAt"), "nmID": next_cursor.get("nmID")}
    return cards


def _safe_fetch_content_cards(scenario: str, wb_token: str | None = None) -> list[dict[str, Any]]:
    try:
        return _fetch_content_cards(scenario, wb_token=wb_token)
    except HTTPException as exc:
        if exc.status_code in {401, 402, 403, 429, 500, 502, 503, 504}:
            return []
        raise


_STOCK_AVAILABILITY_FILTERS = ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"]


def _stock_date_iso(value: date | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def _extract_stock_product_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("products") or payload.get("cards") or payload.get("rows") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _merge_stock_product_totals(
    client: RateLimitedWbApiClient,
    result: dict[str, dict[str, Any]],
    *,
    date_from: date | datetime | None,
    date_to: date | datetime | None,
) -> None:
    date_to_iso = _stock_date_iso(date_to)
    date_from_iso = _stock_date_iso(date_from) if date_from is not None else date_to_iso
    request = WbApiRequest(
        method="POST",
        path="/api/v2/stocks-report/products/products",
        jsonBody={
            "nmIDs": [],
            "currentPeriod": {"start": date_from_iso, "end": date_to_iso},
            "stockType": "",
            "skipDeletedNm": True,
            "orderBy": {"field": "avgOrders", "mode": "desc"},
            "availabilityFilters": _STOCK_AVAILABILITY_FILTERS,
            "limit": 1000,
            "offset": 0,
        },
    )
    try:
        envelope = client.request(request)
    except Exception:
        logger.exception("Failed to fetch WB stock product totals")
        return
    if not envelope.ok:
        logger.info("WB stock product totals unavailable: status=%s", envelope.statusCode)
        return
    for item in _extract_stock_product_items(envelope.data):
        nm_id = int(item.get("nmId") or item.get("nmID") or 0)
        if nm_id <= 0:
            continue
        stock_count = int(item.get("stockCount") or 0)
        if stock_count <= 0:
            continue
        row = result.setdefault(
            str(nm_id),
            {
                "wbStockUnits": 0,
                "marketplaceStockUnits": 0,
                "totalStockUnits": 0,
                "inWayToClient": 0,
                "inWayFromClient": 0,
                "warehouses": 0,
            },
        )
        wb_stock_units = int(row.get("wbStockUnits") or 0)
        row["stockCount"] = max(stock_count, wb_stock_units)
        row["totalStockUnits"] = max(stock_count, wb_stock_units)
        row["marketplaceStockUnits"] = max(int(row["totalStockUnits"]) - wb_stock_units, 0)


def stock_circulation_units(stock: dict[str, Any] | None) -> int | None:
    """Current inventory in circulation, never available-to-sell inventory."""
    values = [(stock or {}).get(key) for key in ("wbStockUnits", "inWayToClient", "inWayFromClient")]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return sum(values)


def fetch_stock_aggregates(
    scenario: str,
    wb_token: str | None = None,
    *,
    date_from: date | datetime | None = None,
    date_to: date | datetime | None = None,
) -> dict[str, dict[str, Any]]:
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client(scenario, token_override=wb_token))
    result: dict[str, dict[str, Any]] = {}
    limit = 250_000
    offset = 0
    while True:
        if offset > 0:
            time.sleep(20.5)
        request = WbApiRequest(
            method="POST",
            path="/api/analytics/v1/stocks-report/wb-warehouses",
            jsonBody={"limit": limit, "offset": offset, "stockType": "wb"},
        )
        payload = _request_or_raise_stock_report(client, request)
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload["data"]
        else:
            data = payload if isinstance(payload, dict) else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break
        for item in items:
            nm_id = int(item.get("nmId") or item.get("nmID") or 0)
            if nm_id <= 0:
                continue
            key = str(nm_id)
            row = result.setdefault(
                key,
                {
                    "wbStockUnits": 0,
                    "marketplaceStockUnits": 0,
                    "totalStockUnits": 0,
                    "inWayToClient": 0,
                    "inWayFromClient": 0,
                    "warehouses": 0,
                },
            )
            quantity = int(item.get("quantity") or 0)
            row["wbStockUnits"] += quantity
            row["totalStockUnits"] = max(int(row.get("totalStockUnits") or 0), int(row.get("wbStockUnits") or 0))
            row["marketplaceStockUnits"] = max(int(row["totalStockUnits"]) - int(row.get("wbStockUnits") or 0), 0)
            for direction in ("inWayToClient", "inWayFromClient"):
                value = item.get(direction)
                row[direction] = (
                    row[direction] + value
                    if row[direction] is not None and type(value) is int and value >= 0
                    else None
                )
            row["warehouses"] += 1
        if len(items) < limit:
            break
        offset += limit
    _merge_stock_product_totals(client, result, date_from=date_from, date_to=date_to)
    return result


def fetch_period_stats_aggregates(
    scenario: str,
    wb_token: str | None = None,
    *,
    date_from: datetime,
    date_to: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    client = RateLimitedWbApiClient(inner=build_wb_statistics_client(scenario, token_override=wb_token))
    start_date = date_from.date()
    end_date = (date_to or _utc_now()).date()
    query = {"dateFrom": f"{date_from.date().isoformat()}T00:00:00"}
    orders_payload = _request_or_raise_statistics_report(client, WbApiRequest(method="GET", path="/api/v1/supplier/orders", query=query))
    sales_payload = _request_or_raise_statistics_report(client, WbApiRequest(method="GET", path="/api/v1/supplier/sales", query=query))
    orders = orders_payload if isinstance(orders_payload, list) else []
    sales = sales_payload if isinstance(sales_payload, list) else []
    result: dict[str, dict[str, Any]] = {}
    daily_result: dict[str, dict[str, dict[str, Any]]] = {}

    for item in orders:
        cancelled = _is_cancelled_order(item)
        cancel_date = _date_from_any(item.get("cancelDate") or item.get("cancelDt") or item.get("cancel_date"))
        item_date = (
            cancel_date
            if cancelled and cancel_date and cancel_date.year > 2001
            else _date_from_any(item.get("date") or item.get("lastChangeDate") or item.get("dt"))
        )
        if item_date is not None and (item_date < start_date or item_date > end_date):
            continue
        nm_id = int(item.get("nmId") or item.get("nmID") or 0)
        if nm_id <= 0:
            continue
        row = result.setdefault(
            str(nm_id),
            {
                "ordersUnits": 0,
                "cancelledOrdersUnits": 0,
                "salesUnits": 0,
                "returnsUnits": 0,
                "revenueKopecks": 0,
                "ordersBuyerPriceKopecksSum": 0,
                "ordersBuyerPriceCount": 0,
                "sppPct": None,
                "sppSource": None,
                "sppObservedAt": None,
                "_orderUnitKeys": set(),
                "_cancelledOrderUnitKeys": set(),
                "_hourlyOrders": {},
                "_todayHourlyOrders": {},
            },
        )
        day_row = None
        if item_date is not None:
            day_row = daily_result.setdefault(item_date.isoformat(), {}).setdefault(
                str(nm_id),
                {
                    "ordersUnits": 0,
                    "cancelledOrdersUnits": 0,
                    "salesUnits": 0,
                    "returnsUnits": 0,
                    "revenueKopecks": 0,
                    "ordersBuyerPriceKopecksSum": 0,
                    "ordersBuyerPriceCount": 0,
                    "sppPct": None,
                    "sppSource": None,
                    "sppObservedAt": None,
                    "_orderUnitKeys": set(),
                    "_cancelledOrderUnitKeys": set(),
                    "_hourlyOrders": {},
                    "_todayHourlyOrders": {},
                },
            )
        order_unit_key = _statistics_order_unit_key(item)
        if order_unit_key:
            order_unit_keys = row["_orderUnitKeys"]
            if order_unit_key in order_unit_keys:
                if cancelled:
                    cancelled_order_unit_keys = row.setdefault("_cancelledOrderUnitKeys", set())
                    if order_unit_key not in cancelled_order_unit_keys:
                        row["cancelledOrdersUnits"] += 1
                        cancelled_order_unit_keys.add(order_unit_key)
                        if day_row is not None:
                            day_cancelled_keys = day_row.setdefault("_cancelledOrderUnitKeys", set())
                            if order_unit_key not in day_cancelled_keys:
                                day_row["cancelledOrdersUnits"] += 1
                                day_cancelled_keys.add(order_unit_key)
                continue
            order_unit_keys.add(order_unit_key)
            if day_row is not None:
                day_row["_orderUnitKeys"].add(order_unit_key)
        if cancelled:
            if order_unit_key:
                cancelled_order_unit_keys = row.setdefault("_cancelledOrderUnitKeys", set())
                if order_unit_key not in cancelled_order_unit_keys:
                    row["cancelledOrdersUnits"] += 1
                    cancelled_order_unit_keys.add(order_unit_key)
                    if day_row is not None:
                        day_cancelled_keys = day_row.setdefault("_cancelledOrderUnitKeys", set())
                        if order_unit_key not in day_cancelled_keys:
                            day_row["cancelledOrdersUnits"] += 1
                            day_cancelled_keys.add(order_unit_key)
            else:
                row["cancelledOrdersUnits"] += 1
                if day_row is not None:
                    day_row["cancelledOrdersUnits"] += 1
            continue
        row["ordersUnits"] += 1
        if day_row is not None:
            day_row["ordersUnits"] += 1
        buyer_price_kopecks = _first_kopecks(item, "finishedPrice")
        if buyer_price_kopecks:
            row["ordersBuyerPriceKopecksSum"] += buyer_price_kopecks
            row["ordersBuyerPriceCount"] += 1
            if day_row is not None:
                day_row["ordersBuyerPriceKopecksSum"] += buyer_price_kopecks
                day_row["ordersBuyerPriceCount"] += 1
        order_hour = _hour_from_any(item.get("date") or item.get("lastChangeDate") or item.get("dt"))
        if order_hour is not None:
            revenue_kopecks = buyer_price_kopecks or _first_kopecks(item, "priceWithDisc", "totalPrice")
            hourly: dict[int, dict[str, int]] = row.setdefault("_hourlyOrders", {})
            bucket = hourly.setdefault(order_hour, {"hour": order_hour, "ordersUnits": 0, "revenueKopecks": 0})
            bucket["ordersUnits"] += 1
            bucket["revenueKopecks"] += int(revenue_kopecks or 0)
            if day_row is not None:
                day_hourly: dict[int, dict[str, int]] = day_row.setdefault("_hourlyOrders", {})
                day_bucket = day_hourly.setdefault(order_hour, {"hour": order_hour, "ordersUnits": 0, "revenueKopecks": 0})
                day_bucket["ordersUnits"] += 1
                day_bucket["revenueKopecks"] += int(revenue_kopecks or 0)
            if item_date == end_date:
                today_hourly: dict[int, dict[str, int]] = row.setdefault("_todayHourlyOrders", {})
                today_bucket = today_hourly.setdefault(order_hour, {"hour": order_hour, "ordersUnits": 0, "revenueKopecks": 0})
                today_bucket["ordersUnits"] += 1
                today_bucket["revenueKopecks"] += int(revenue_kopecks or 0)
        spp_pct = _spp_pct_or_none(item.get("spp"))
        if spp_pct is not None:
            observed_at = str(item.get("lastChangeDate") or item.get("date") or "")
            if row.get("sppObservedAt") is None or observed_at >= str(row.get("sppObservedAt") or ""):
                row["sppPct"] = spp_pct
                row["sppSource"] = "supplier.orders.spp"
                row["sppObservedAt"] = observed_at
                if day_row is not None:
                    day_row["sppPct"] = spp_pct
                    day_row["sppSource"] = "supplier.orders.spp"
                    day_row["sppObservedAt"] = observed_at

    for item in sales:
        item_date = _date_from_any(item.get("date") or item.get("lastChangeDate") or item.get("dt"))
        if item_date is not None and (item_date < start_date or item_date > end_date):
            continue
        nm_id = int(item.get("nmId") or item.get("nmID") or 0)
        if nm_id <= 0:
            continue
        row = result.setdefault(
            str(nm_id),
            {
                "ordersUnits": 0,
                "cancelledOrdersUnits": 0,
                "salesUnits": 0,
                "returnsUnits": 0,
                "revenueKopecks": 0,
                "ordersBuyerPriceKopecksSum": 0,
                "ordersBuyerPriceCount": 0,
                "sppPct": None,
                "sppSource": None,
                "sppObservedAt": None,
                "_orderUnitKeys": set(),
                "_cancelledOrderUnitKeys": set(),
                "_hourlyOrders": {},
                "_todayHourlyOrders": {},
            },
        )
        day_row = None
        if item_date is not None:
            day_row = daily_result.setdefault(item_date.isoformat(), {}).setdefault(
                str(nm_id),
                {
                    "ordersUnits": 0,
                    "cancelledOrdersUnits": 0,
                    "salesUnits": 0,
                    "returnsUnits": 0,
                    "revenueKopecks": 0,
                    "ordersBuyerPriceKopecksSum": 0,
                    "ordersBuyerPriceCount": 0,
                    "sppPct": None,
                    "sppSource": None,
                    "sppObservedAt": None,
                    "_orderUnitKeys": set(),
                    "_cancelledOrderUnitKeys": set(),
                    "_hourlyOrders": {},
                    "_todayHourlyOrders": {},
                },
            )
        is_return = bool(item.get("isReturn"))
        amount_rub = float(item.get("finishedPrice") or item.get("forPay") or item.get("priceWithDisc") or 0)
        if is_return:
            row["returnsUnits"] += 1
            row["revenueKopecks"] -= abs(round(amount_rub * 100))
            if day_row is not None:
                day_row["returnsUnits"] += 1
                day_row["revenueKopecks"] -= abs(round(amount_rub * 100))
        else:
            row["salesUnits"] += 1
            row["revenueKopecks"] += round(amount_rub * 100)
            if day_row is not None:
                day_row["salesUnits"] += 1
                day_row["revenueKopecks"] += round(amount_rub * 100)

    for rows_by_nm in [result, *daily_result.values()]:
        for row in rows_by_nm.values():
            sales_units = int(row.get("salesUnits") or 0)
            orders_units = int(row.get("ordersUnits") or 0)
            revenue = int(row.get("revenueKopecks") or 0)
            orders_buyer_count = int(row.get("ordersBuyerPriceCount") or 0)
            orders_buyer_sum = int(row.get("ordersBuyerPriceKopecksSum") or 0)
            row["avgPriceWithSppKopecks"] = (
                round(revenue / sales_units)
                if sales_units > 0
                else (round(orders_buyer_sum / orders_buyer_count) if orders_buyer_count > 0 else None)
            )
            # Sales and orders are independent event cohorts, not a conversion.
            row["buyoutPct"] = None
            row.pop("ordersBuyerPriceKopecksSum", None)
            row.pop("ordersBuyerPriceCount", None)
            row["unitKeyedOrdersCount"] = len(row.pop("_orderUnitKeys", set()))
            row["cancelledOrderUnitCount"] = len(row.pop("_cancelledOrderUnitKeys", set()))
            hourly_orders = row.pop("_hourlyOrders", {}) or {}
            today_hourly_orders = row.pop("_todayHourlyOrders", {}) or {}
            row["hourlyOrders"] = [
                hourly_orders.get(hour, {"hour": hour, "ordersUnits": 0, "revenueKopecks": 0})
                for hour in range(24)
            ]
            row["todayHourlyOrders"] = [
                today_hourly_orders.get(hour, {"hour": hour, "ordersUnits": 0, "revenueKopecks": 0})
                for hour in range(24)
            ]
    return {"aggregates": result, "dailyAggregates": daily_result}


def _nm_ids_from_goods(goods: list[dict[str, Any]]) -> list[int]:
    nm_ids: list[int] = []
    seen: set[int] = set()
    for good in goods:
        nm_id = int(good.get("nmID") or good.get("nmId") or 0)
        if nm_id <= 0 or nm_id in seen:
            continue
        seen.add(nm_id)
        nm_ids.append(nm_id)
    return nm_ids


def _sales_funnel_metrics(source: dict[str, Any]) -> dict[str, Any]:
    conversions = source.get("conversions") or {}
    order_count = _first_number(source, "orderCount", "ordersCount", "orders")
    buyout_count = int(_first_number(source, "buyoutCount", "buyoutsCount", "buyouts") or 0)
    buyout_pct = _spp_pct_or_none(conversions.get("buyoutPercent"))
    cart_count = _first_number(source, "cartCount", "addToCartCount", "addToCart")
    order_sum_rub = _first_number(source, "orderSum", "ordersSumRub", "ordersSum")
    impressions = _first_number(
        source,
        "viewCount",
        "viewCountTotal",
        "views",
        "viewsCount",
        "impressions",
        "showCount",
        "showCountTotal",
        "impressionCount",
    )
    wishlist_count = _first_number(source, "addToWishlist", "addToWishlistCount")
    return {
        "cartCount": int(cart_count) if cart_count is not None else None,
        "orderCount": int(order_count) if order_count is not None else None,
        "orderSumKopecks": _kopecks_from_rub(order_sum_rub) if order_sum_rub is not None else None,
        "openCount": int(_first_number(source, "openCount", "openCardCount", "openCard") or 0),
        "impressions": int(impressions) if impressions is not None else None,
        "wishlistCount": int(wishlist_count) if wishlist_count is not None else None,
        "buyoutCount": buyout_count,
        "buyoutCountProvided": _first_number(source, "buyoutCount", "buyoutsCount", "buyouts") is not None,
        "cancelCount": _first_number(source, "cancelCount", "cancelledCount", "canceledCount"),
        "buyoutSumKopecks": _first_kopecks(source, "buyoutSum", "buyoutsSumRub", "buyoutsSum"),
        "buyoutPct": buyout_pct,
        "buyoutSource": "sales_funnel.conversions.buyoutPercent" if buyout_pct is not None else None,
        "atcrPct": _first_number(conversions, "addToCartPercent"),
        "cartToOrderPct": _first_number(conversions, "cartToOrderPercent"),
        "localizationPct": _first_number(source, "localizationPercent"),
    }


def _merge_sales_funnel_products(result: dict[str, dict[str, Any]], products: list[Any]) -> None:
    for item in products:
        if not isinstance(item, dict):
            continue
        product = item.get("product") or {}
        nm_id = int(product.get("nmId") or product.get("nmID") or 0)
        if nm_id <= 0:
            continue
        stocks = product.get("stocks") or {}
        wb_stock_units = _first_number(stocks, "wb")
        statistic = item.get("statistic") or {}
        selected = statistic.get("selected") or {}
        previous = statistic.get("past") or statistic.get("previous") or statistic.get("pastPeriod") or {}
        comparison = statistic.get("comparison") or {}
        result[str(nm_id)] = {
            **_sales_funnel_metrics(selected),
            "previous": _sales_funnel_metrics(previous) if isinstance(previous, dict) and previous else {},
            "openCountDeltaPct": _first_number(comparison, "openCountDynamic", "openCardCountDynamic", "openCardDynamic"),
            "cartCountDeltaPct": _first_number(comparison, "cartCountDynamic", "addToCartCountDynamic", "addToCartDynamic"),
            "orderCountDeltaPct": _first_number(comparison, "orderCountDynamic", "ordersCountDynamic", "ordersDynamic"),
            "orderSumDeltaPct": _first_number(comparison, "orderSumDynamic"),
            "buyoutCountDeltaPct": _first_number(comparison, "buyoutCountDynamic", "buyoutsCountDynamic"),
            "buyoutSumDeltaPct": _first_number(comparison, "buyoutSumDynamic", "buyoutsSumDynamic"),
            "productName": product.get("title"),
            "vendorCode": product.get("vendorCode"),
            "brand": product.get("brandName"),
            "category": product.get("subjectName"),
            "wbStockUnits": int(wb_stock_units) if wb_stock_units is not None else None,
            "source": "sales_funnel_products",
        }


def fetch_baskets_aggregates(
    scenario: str,
    wb_token: str | None = None,
    *,
    period_days: int,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    nm_ids: list[int] | None = None,
    include_daily: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client(scenario, token_override=wb_token))
    end = (date_to or _utc_now()).date()
    start = (date_from.date() if date_from is not None else end - timedelta(days=max(1, period_days) - 1))
    resolved_period_days = max(1, (end - start).days + 1)
    past_end = start - timedelta(days=1)
    past_start = past_end - timedelta(days=resolved_period_days - 1)
    result: dict[str, dict[str, Any]] = {}
    limit = 1000
    requested_nm_ids = list(nm_ids or [])
    nm_batches = [requested_nm_ids[index : index + limit] for index in range(0, len(requested_nm_ids), limit)] if requested_nm_ids else [[]]

    requests_total = len(nm_batches)
    requests_completed = 0
    processed_nm_ids = 0
    for batch_index, batch in enumerate(nm_batches, start=1):
        if progress_callback:
            progress_callback({"phase": "requesting", "batch": batch_index, "batchesTotal": requests_total, "requestsCompleted": requests_completed, "requestsTotal": requests_total, "processedNmIds": processed_nm_ids, "totalNmIds": len(requested_nm_ids)})
        offset = 0
        while True:
            payload = _request_or_raise_sales_funnel_products(
                client,
                WbApiRequest(
                    method="POST",
                    path="/api/analytics/v3/sales-funnel/products",
                    jsonBody={
                        "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                        "pastPeriod": {"start": past_start.isoformat(), "end": past_end.isoformat()},
                        "nmIds": batch,
                        "skipDeletedNm": False,
                        "limit": limit,
                        "offset": offset,
                    },
                ),
                progress_callback=(
                    (lambda event, batch_index=batch_index: progress_callback({
                        **event,
                        "batch": batch_index,
                        "batchesTotal": requests_total,
                        "requestsCompleted": requests_completed,
                        "requestsTotal": requests_total,
                        "processedNmIds": processed_nm_ids,
                        "totalNmIds": len(requested_nm_ids),
                    }))
                    if progress_callback
                    else None
                ),
            )
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                data = payload["data"]
            else:
                data = payload if isinstance(payload, dict) else {}
            products = data.get("products", []) if isinstance(data, dict) else []
            if not products:
                break

            _merge_sales_funnel_products(result, products)

            if batch or len(products) < limit:
                break
            offset += limit
        requests_completed += 1
        processed_nm_ids += len(batch)
        if progress_callback:
            progress_callback({"phase": "completed", "batch": batch_index, "batchesTotal": requests_total, "requestsCompleted": requests_completed, "requestsTotal": requests_total, "processedNmIds": processed_nm_ids, "totalNmIds": len(requested_nm_ids)})

    daily_result: dict[str, dict[str, dict[str, Any]]] = {}
    if include_daily and resolved_period_days > 1:
        for day in _date_range(start, end):
            day_rows: dict[str, dict[str, Any]] = {}
            day_past = day - timedelta(days=1)
            for batch in nm_batches:
                offset = 0
                while True:
                    payload = _request_or_raise_sales_funnel_products(
                        client,
                        WbApiRequest(
                            method="POST",
                            path="/api/analytics/v3/sales-funnel/products",
                            jsonBody={
                                "selectedPeriod": {"start": day.isoformat(), "end": day.isoformat()},
                                "pastPeriod": {"start": day_past.isoformat(), "end": day_past.isoformat()},
                                "nmIds": batch,
                                "skipDeletedNm": False,
                                "limit": limit,
                                "offset": offset,
                            },
                        ),
                    )
                    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                        data = payload["data"]
                    else:
                        data = payload if isinstance(payload, dict) else {}
                    products = data.get("products", []) if isinstance(data, dict) else []
                    if not products:
                        break
                    _merge_sales_funnel_products(day_rows, products)
                    if batch or len(products) < limit:
                        break
                    offset += limit
            daily_result[day.isoformat()] = day_rows
    elif include_daily and resolved_period_days == 1:
        daily_result[start.isoformat()] = result

    matched_nm_ids = [int(key) for key in result.keys()]
    return {
        "aggregates": result,
        "dailyAggregates": daily_result,
        "count": len(result),
        "requestedNmIds": len(requested_nm_ids),
        "matchedNmIds": len(matched_nm_ids),
        "comparisonDateFrom": past_start.isoformat(),
        "comparisonDateTo": past_end.isoformat(),
        "periodDays": resolved_period_days,
        "dateFrom": start.isoformat(),
        "dateTo": end.isoformat(),
    }


def fetch_baskets_daily_detail(
    scenario: str,
    wb_token: str | None = None,
    *,
    date_from: datetime,
    date_to: datetime,
    nm_ids: list[int],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    existing_daily_aggregates: dict[str, dict[str, dict[str, Any]]] | None = None,
    existing_chunks: list[dict[str, Any]] | None = None,
    day_completed_callback: Callable[[str, dict[str, dict[str, Any]], dict[str, Any]], None] | None = None,
    chunk_completed_callback: Callable[[str, int, dict[str, dict[str, Any]], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client(scenario, token_override=wb_token))
    start = date_from.date()
    end = date_to.date()
    days = list(_date_range(start, end))
    limit = 1000
    requested_nm_ids = list(nm_ids)
    nm_batches = [requested_nm_ids[index : index + limit] for index in range(0, len(requested_nm_ids), limit)] if requested_nm_ids else [[]]
    daily_result: dict[str, dict[str, dict[str, Any]]] = {
        day_key: rows
        for day_key, rows in (existing_daily_aggregates or {}).items()
        if isinstance(day_key, str) and isinstance(rows, dict)
    }
    chunks: list[dict[str, Any]] = [dict(item) for item in (existing_chunks or []) if isinstance(item, dict)]
    done_chunks = {
        (str(item.get("date")), int(item.get("chunkIndex")))
        for item in chunks
        if item.get("type") == "daily" and item.get("status") == "done" and item.get("date") is not None and item.get("chunkIndex") is not None
    }
    chunk_dates = {
        str(item.get("date"))
        for item in chunks
        if item.get("type") == "daily" and item.get("date") is not None
    }

    def legacy_full_day_ready(day_key: str) -> bool:
        return day_key in daily_result and (existing_chunks is None or day_key not in chunk_dates)

    def chunk_ready(day_key: str, chunk_index: int) -> bool:
        return (day_key, chunk_index) in done_chunks

    requests_total = sum(
        1
        for day in days
        for batch_index in range(1, len(nm_batches) + 1)
        if not legacy_full_day_ready(day.isoformat())
        and not chunk_ready(day.isoformat(), batch_index - 1)
    )
    requests_completed = 0
    failed_chunks: list[dict[str, Any]] = []

    for day_index, day in enumerate(days, start=1):
        day_key = day.isoformat()
        legacy_full_day = legacy_full_day_ready(day_key)
        day_done_by_chunks = bool(done_chunks) and all((day_key, batch_index - 1) in done_chunks for batch_index in range(1, len(nm_batches) + 1))
        if legacy_full_day or day_done_by_chunks:
            if progress_callback:
                progress_callback({
                    "phase": "skipped",
                    "day": day_key,
                    "dayIndex": day_index,
                    "daysTotal": len(days),
                    "batch": len(nm_batches),
                    "batchesTotal": len(nm_batches),
                    "requestsCompleted": requests_completed,
                    "requestsTotal": requests_total,
                })
            continue
        day_rows: dict[str, dict[str, Any]] = dict(daily_result.get(day_key) or {})
        day_past = day - timedelta(days=1)
        for batch_index, batch in enumerate(nm_batches, start=1):
            chunk_index = batch_index - 1
            base_progress = {
                "day": day_key, "dayIndex": day_index, "daysTotal": len(days),
                "batch": batch_index, "batchesTotal": len(nm_batches),
                "requestsCompleted": requests_completed, "requestsTotal": requests_total,
            }
            if (day_key, chunk_index) in done_chunks:
                if progress_callback:
                    progress_callback({**base_progress, "phase": "skipped", "requestsCompleted": requests_completed})
                continue
            if progress_callback:
                progress_callback({**base_progress, "phase": "requesting"})
            try:
                offset = 0
                while True:
                    request = WbApiRequest(method="POST", path="/api/analytics/v3/sales-funnel/products", jsonBody={
                        "selectedPeriod": {"start": day.isoformat(), "end": day.isoformat()},
                        "pastPeriod": {"start": day_past.isoformat(), "end": day_past.isoformat()},
                        "nmIds": batch, "skipDeletedNm": False, "limit": limit, "offset": offset,
                    })
                    payload = _request_or_raise_sales_funnel_products(
                        client,
                        request,
                        progress_callback=(
                            (lambda event, base_progress=base_progress: progress_callback({**base_progress, **event}))
                            if progress_callback
                            else None
                        ),
                    )
                    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else (payload if isinstance(payload, dict) else {})
                    products = data.get("products", []) if isinstance(data, dict) else []
                    _merge_sales_funnel_products(day_rows, products)
                    if batch or len(products) < limit:
                        break
                    offset += limit
            except WbSalesFunnelDeferred:
                raise
            except HTTPException as exc:
                if exc.status_code in {401, 403} or not _http_exception_is_retryable_transport(exc):
                    failed_progress = {**base_progress, "phase": "failed", "status": "failed", "lastError": str(exc), "requestsCompleted": requests_completed}
                    chunks.append({"type": "daily", "date": day_key, "chunkIndex": chunk_index, "status": "failed", "attempts": 1, "lastError": str(exc)})
                    if chunk_completed_callback:
                        chunk_completed_callback(day_key, chunk_index, day_rows, failed_progress)
                    if progress_callback:
                        progress_callback(failed_progress)
                    raise
                requests_completed += 1
                failed_progress = {**base_progress, "phase": "failed", "status": "failed", "lastError": str(exc), "requestsCompleted": requests_completed}
                failed_chunks.append({"date": day_key, "chunkIndex": chunk_index, "error": str(exc)})
                chunks = [
                    item
                    for item in chunks
                    if not (item.get("type") == "daily" and item.get("date") == day_key and item.get("chunkIndex") == chunk_index)
                ]
                chunks.append({"type": "daily", "date": day_key, "chunkIndex": chunk_index, "status": "failed", "attempts": 1, "lastError": str(exc)})
                if chunk_completed_callback:
                    chunk_completed_callback(day_key, chunk_index, day_rows, failed_progress)
                if progress_callback:
                    progress_callback(failed_progress)
                continue
            except Exception as exc:
                failed_progress = {**base_progress, "phase": "failed", "status": "failed", "lastError": str(exc), "requestsCompleted": requests_completed}
                chunks.append({"type": "daily", "date": day_key, "chunkIndex": chunk_index, "status": "failed", "attempts": 1, "lastError": str(exc)})
                if chunk_completed_callback:
                    chunk_completed_callback(day_key, chunk_index, day_rows, failed_progress)
                if progress_callback:
                    progress_callback(failed_progress)
                raise
            requests_completed += 1
            completed_progress = {**base_progress, "phase": "completed", "requestsCompleted": requests_completed}
            chunks = [
                item
                for item in chunks
                if not (item.get("type") == "daily" and item.get("date") == day_key and item.get("chunkIndex") == chunk_index)
            ]
            chunks.append({"type": "daily", "date": day_key, "chunkIndex": chunk_index, "status": "done", "attempts": 1})
            done_chunks.add((day_key, chunk_index))
            if chunk_completed_callback:
                chunk_completed_callback(day_key, chunk_index, day_rows, completed_progress)
            if progress_callback:
                progress_callback(completed_progress)
        daily_result[day_key] = day_rows
        if day_completed_callback:
            day_completed_callback(
                day_key,
                day_rows,
                {
                    "phase": "day-completed",
                    "day": day_key,
                    "dayIndex": day_index,
                    "daysTotal": len(days),
                    "batch": len(nm_batches),
                    "batchesTotal": len(nm_batches),
                    "requestsCompleted": requests_completed,
                    "requestsTotal": requests_total,
                },
            )
    return {
        "dailyAggregates": daily_result,
        "requestedNmIds": len(requested_nm_ids),
        "periodDays": len(days),
        "dateFrom": start.isoformat(),
        "dateTo": end.isoformat(),
        "requestsCompleted": requests_completed,
        "requestsTotal": requests_total,
        "chunks": chunks,
        "failedChunks": failed_chunks,
    }


def fetch_finance_report_aggregates(
    scenario: str,
    wb_token: str | None = None,
    *,
    date_from: datetime,
    date_to: datetime | None = None,
    revenue_only: bool = False,
    repricer_operations_org: int | None = None,
    page_collected_callback=None,
    report_id: int | None = None,
    report_period: str = "daily",
    _client=None,
) -> dict[str, Any]:
    from app.wb_api.financial_reports import DETAIL_FIELDS, DETAIL_PATH, SOURCE_VERSION, integer_id
    if report_period not in {"daily", "weekly"}:
        raise ValueError("Invalid financial report period")
    if report_id is not None:
        report_id = integer_id(report_id)
    client = _client if _client is not None else RateLimitedWbApiClient(inner=build_wb_finance_client(scenario, token_override=wb_token))
    date_to = date_to or _utc_now()
    request_date_to = date_to
    fields = list(DETAIL_FIELDS)
    if revenue_only:
        fields = ["rrdId", "nmId", "docTypeName", "quantity", "retailAmount",
                  "retailPriceWithDisc", "saleDt", "rrDate", "currency", "sellerOperName"]
    limit = 50_000 if revenue_only else 10_000
    rrd_id = 0
    rows: list[dict[str, Any]] = []
    seen_rrd_ids: dict[int, dict[str, Any]] = {}
    duplicate_rows_skipped = 0
    pages_loaded = 0
    checkpoint_key = None
    if repricer_operations_org is not None and wb_token and not revenue_only:
        from app.repricer_page_finance import connection_key
        scope = f"{connection_key(wb_token)}_{date_from.date()}_{date_to.date()}"
        checkpoint_key = (f"repricer_finance_progress_{scope}" if report_period == "daily" and report_id is None
                          else f"wb_realization_progress_{scope}_{report_period}_{report_id or 'period'}")
        checkpoint = get_source_cache(repricer_operations_org, checkpoint_key, slim=False) or {}
        if checkpoint.get("sourceComplete") is False and checkpoint.get("fields") == fields and checkpoint.get("limit") == limit:
            rows = checkpoint.get("operations") or []
            seen_rrd_ids = {int(row["rrdId"]): row for row in rows}
            rrd_id = int(checkpoint.get("cursor") or 0)
            pages_loaded = int(checkpoint.get("pagesLoaded") or 0)
    while True:
        body = {"limit": limit, "rrdId": rrd_id, "fields": fields}
        if report_id is None:
            body.update(dateFrom=date_from.date().isoformat(), dateTo=request_date_to.date().isoformat(), period=report_period)
        payload = _request_or_raise_finance_report(
            client,
            WbApiRequest(
                method="POST",
                path=DETAIL_PATH if report_id is None else f"{DETAIL_PATH}/{report_id}",
                jsonBody=body,
            ),
        )
        page_rows = payload.get("data") if isinstance(payload, dict) else payload
        if (
            not isinstance(page_rows, list)
            or any(not isinstance(item, dict) for item in page_rows)
            or isinstance(payload, dict) and payload.get("error") not in (None, False)
        ):
            raise HTTPException(status_code=502, detail="WB_FINANCE_INVALID_RESPONSE")
        if not page_rows:
            break

        for item in page_rows:
            if str(item.get("currency") or "").upper() not in {"RUB", "RUR", "643"}:
                raise HTTPException(status_code=502, detail="WB_FINANCE_CURRENCY_UNAVAILABLE")
            if report_id is not None and integer_id(item.get("reportId")) != report_id:
                raise HTTPException(status_code=502, detail="WB_FINANCE_REPORT_MISMATCH")
            raw_id = _finance_raw_first(item, "rrdId", "rrd_id")
            valid_id = type(raw_id) is int or (
                isinstance(raw_id, str)
                and raw_id.isascii()
                and raw_id.isdecimal()
                and not raw_id.startswith("0")
            )
            item_rrd_id = _finance_int(raw_id) if valid_id else 0
            if item_rrd_id <= 0:
                raise HTTPException(status_code=502, detail="WB_FINANCE_INVALID_ROW_ID")
            if item_rrd_id in seen_rrd_ids:
                if json.dumps(seen_rrd_ids[item_rrd_id], sort_keys=True) != json.dumps(item, sort_keys=True):
                    raise HTTPException(status_code=502, detail="WB_FINANCE_CONFLICTING_ROWS")
                duplicate_rows_skipped += 1
                continue
            if item_rrd_id <= rrd_id:
                raise HTTPException(status_code=502, detail="WB_FINANCE_CURSOR_STALLED")
            doc_type = str(item.get("docTypeName") or item.get("doc_type_name") or "").strip().lower()
            if doc_type in {"продажа", "возврат"}:
                revenue = _number_or_none(_finance_raw_first(item, "retailAmount", "retail_amount"))
                if revenue is None or not isfinite(revenue * 100):
                    raise HTTPException(status_code=502, detail="WB_FINANCE_INVALID_TRADE_REVENUE")
                seller_price = _number_or_none(_finance_raw_first(item, "retailPriceWithDisc", "retail_price_withdisc_rub"))
                if seller_price is None or not isfinite(seller_price * 100):
                    raise HTTPException(status_code=502, detail="WB_FINANCE_INVALID_SELLER_PRICE")
            seen_rrd_ids[item_rrd_id] = item
            rows.append(item)
        pages_loaded += 1
        if item_rrd_id <= rrd_id:
            raise HTTPException(status_code=502, detail="WB_FINANCE_CURSOR_STALLED")
        rrd_id = item_rrd_id
        if checkpoint_key:
            save_source_cache(repricer_operations_org, checkpoint_key, {
                "operations": rows, "fields": fields, "limit": limit,
                "cursor": rrd_id, "pagesLoaded": pages_loaded,
                "dateFrom": str(date_from.date()), "dateTo": str(date_to.date()),
                "sourceComplete": False,
            }, strict=True)
        if page_collected_callback is not None:
            page_collected_callback(pages_loaded, len(rows))

    if checkpoint_key:
        from app.repricer_page_finance import save_operations
        save_operations(repricer_operations_org, wb_token, date_from.date(), date_to.date(), rows,
                        requested_fields=fields, report_period=report_period, report_id=report_id)
        save_source_cache(repricer_operations_org, checkpoint_key, {"sourceComplete": True}, strict=True)

    result = aggregate_finance_report_rows(rows, date_from=date_from, date_to=date_to)
    result.update(pagesLoaded=pages_loaded, duplicateRowsSkipped=duplicate_rows_skipped,
                  sourceComplete=True, requestedFields=fields, revenueOnly=revenue_only,
                  source="finance_sales_reports_detailed", sourceVersion=SOURCE_VERSION,
                  reportPeriod=report_period, reportId=report_id,
                  sourceScope="report" if report_id is not None else "period")
    return result


def aggregate_finance_report_rows(
    rows: list[dict[str, Any]], *, date_from: datetime, date_to: datetime,
) -> dict[str, Any]:
    """Rebuild arbitrary periods from original operations, never rounded day totals."""
    source_rows = rows
    fields = list(_FINANCE_DIAGNOSTIC_RAW_FIELDS)
    result: dict[str, dict[str, Any]] = {}
    global_rows: list[dict[str, Any]] = []

    for item in rows:
        nm_id = int(item.get("nmId") or item.get("nmID") or item.get("nm_id") or 0)
        if nm_id <= 0:
            global_rows.append(item)
            continue

        row = result.setdefault(
            str(nm_id),
            {
                "vendorCode": str(item.get("vendorCode") or item.get("vendor_code") or "").strip(),
                "rowsCount": 0,
                "salesUnits": 0,
                "returnsUnits": 0,
                "netSalesUnits": 0,
                "revenueGrossKopecks": 0,
                "grossSalesKopecks": 0,
                "returnsKopecks": 0,
                "buyerRevenueKopecks": 0,
                "sellerRevenueKopecks": 0,
                "sellerRevenueRows": 0,
                "sellerRevenueMissingRows": 0,
                "platformDiscountKopecks": 0,
                "discountPctSum": 0.0,
                "discountPctCount": 0,
                "commissionPctSum": 0.0,
                "commissionPctCount": 0,
                "commissionFormulaKopecks": 0,
                "sppPctSum": 0.0,
                "sppPctCount": 0,
                "commissionKopecks": 0,
                "reportedCommissionRows": 0,
                "adSpendKopecks": 0,
                "logisticsKopecks": 0,
                "storageKopecks": 0,
                "acceptanceKopecks": 0,
                "penaltyKopecks": 0,
                "penaltyChargedKopecks": 0,
                "penaltyReturnedKopecks": 0,
                "deductionKopecks": 0,
                "deductionChargedKopecks": 0,
                "deductionCompensationKopecks": 0,
                "additionalPaymentKopecks": 0,
                "rewardAdjustmentKopecks": 0,
                "paymentScheduleKopecks": 0,
                "cashbackAmountKopecks": 0,
                "cashbackDiscountKopecks": 0,
                "cashbackCommissionChangeKopecks": 0,
                "loyaltyCostKopecks": 0,
                "acquiringKopecks": 0,
                "payableKopecks": 0,
                "_payableRows": 0,
                "_settlementRows": 0,
                "source": "finance_sales_reports_detailed",
                "_saleUnitKeys": set(),
                "_returnUnitKeys": set(),
            },
        )
        if not row.get("vendorCode"):
            row["vendorCode"] = str(item.get("vendorCode") or item.get("vendor_code") or "").strip()
        row["rowsCount"] += 1
        quantity = int(_number_or_none(item.get("quantity", item.get("saleQuantity", 1))) or 0)
        buyer_revenue_kopecks = _first_kopecks(item, "retailAmount", "retail_amount")
        seller_revenue_kopecks = _finance_seller_revenue_kopecks(item, quantity)
        sale_day = _finance_operation_date(item)
        if sale_day is None or not date_from.date() <= sale_day <= date_to.date():
            seller_revenue_kopecks = 0
        seller_revenue_available = (
            _finance_raw_first(
                item,
                "retailPriceWithDisc",
                "retail_price_withdisc_rub",
            )
            is not None
        )
        doc_type_name = str(item.get("docTypeName") or item.get("doc_type_name") or "").strip().lower()
        unit_key = _finance_unit_key(item)
        commission_pct = _first_number(
            item,
            "commission_percent",
            "commissionPercent",
        )
        if doc_type_name in {"продажа", "возврат"}:
            row["sellerRevenueRows"] += int(seller_revenue_available)
            row["sellerRevenueMissingRows"] += int(not seller_revenue_available)
        if doc_type_name == "продажа":
            if unit_key:
                sale_unit_keys = row["_saleUnitKeys"]
                if unit_key not in sale_unit_keys:
                    sale_unit_keys.add(unit_key)
                    row["salesUnits"] += max(1, quantity)
            else:
                row["salesUnits"] += quantity
            row["buyerRevenueKopecks"] += buyer_revenue_kopecks
            row["sellerRevenueKopecks"] += seller_revenue_kopecks
            row["revenueGrossKopecks"] += seller_revenue_kopecks
            row["grossSalesKopecks"] += seller_revenue_kopecks
            if commission_pct is not None:
                row["commissionFormulaKopecks"] += round(buyer_revenue_kopecks * commission_pct / 100)
        elif doc_type_name == "возврат":
            if unit_key:
                return_unit_keys = row["_returnUnitKeys"]
                if unit_key not in return_unit_keys:
                    return_unit_keys.add(unit_key)
                    row["returnsUnits"] += max(1, quantity)
            else:
                row["returnsUnits"] += quantity
            row["buyerRevenueKopecks"] -= buyer_revenue_kopecks
            row["sellerRevenueKopecks"] -= seller_revenue_kopecks
            row["revenueGrossKopecks"] -= seller_revenue_kopecks
            row["returnsKopecks"] += seller_revenue_kopecks
            if commission_pct is not None:
                row["commissionFormulaKopecks"] -= round(buyer_revenue_kopecks * commission_pct / 100)
        if doc_type_name in {"продажа", "возврат"} and (buyer_revenue_kopecks or seller_revenue_kopecks):
            row["_settlementRows"] += 1
        document_sign = -1 if doc_type_name == "возврат" else 1
        reported_commission = _finance_raw_first(
            item,
            "ppvz_sales_commission",
            "ppvzSalesCommission",
            "commission",
            "commissionRub",
        )
        if reported_commission is not None:
            row["reportedCommissionRows"] += 1
            row["commissionKopecks"] += document_sign * _kopecks_from_rub(reported_commission)
        payable = _finance_raw_first(item, "ppvz_for_pay", "forPay")
        if payable is not None:
            row["_payableRows"] += 1
            row["payableKopecks"] += document_sign * _kopecks_from_rub(payable)
        costs = _finance_row_costs(item)
        row["adSpendKopecks"] += costs["adSpendKopecks"]
        row["logisticsKopecks"] += costs["logisticsKopecks"]
        row["storageKopecks"] += costs["storageKopecks"]
        row["acceptanceKopecks"] += costs["acceptanceKopecks"]
        penalty_kopecks = costs["penaltyKopecks"]
        deduction_kopecks = costs["deductionKopecks"]
        additional_payment_kopecks = costs["additionalPaymentKopecks"]
        cashback_amount_kopecks = costs["cashbackAmountKopecks"]
        cashback_discount_kopecks = costs["cashbackDiscountKopecks"]
        cashback_commission_change_kopecks = costs["cashbackCommissionChangeKopecks"]
        row["penaltyKopecks"] += penalty_kopecks
        if penalty_kopecks >= 0:
            row["penaltyChargedKopecks"] += penalty_kopecks
        else:
            row["penaltyReturnedKopecks"] += -penalty_kopecks
        row["deductionKopecks"] += deduction_kopecks
        if deduction_kopecks >= 0:
            row["deductionChargedKopecks"] += deduction_kopecks
        else:
            row["deductionCompensationKopecks"] += -deduction_kopecks
        row["additionalPaymentKopecks"] += additional_payment_kopecks
        row["rewardAdjustmentKopecks"] += costs["rewardAdjustmentKopecks"]
        row["paymentScheduleKopecks"] += costs["paymentScheduleKopecks"]
        row["cashbackAmountKopecks"] += cashback_amount_kopecks
        row["cashbackDiscountKopecks"] += cashback_discount_kopecks
        row["cashbackCommissionChangeKopecks"] += cashback_commission_change_kopecks
        row["loyaltyCostKopecks"] += cashback_amount_kopecks + cashback_commission_change_kopecks
        row["acquiringKopecks"] += costs["acquiringKopecks"]
        seller_oper_name = str(item.get("sellerOperName") or item.get("seller_oper_name") or "").strip()
        bonus_type_name = str(item.get("bonusTypeName") or item.get("bonus_type_name") or "").strip()
        adjustment_text = f"{seller_oper_name} {bonus_type_name}".lower()
        if (
            penalty_kopecks < 0
            or deduction_kopecks < 0
            or additional_payment_kopecks != 0
            or any(word in adjustment_text for word in _FINANCE_ADJUSTMENT_WORDS)
        ):
            log_finance_adjustment(
                nm_id=nm_id,
                rrd_id=item.get("rrdId") or item.get("rrd_id"),
                seller_oper_name=seller_oper_name,
                bonus_type_name=bonus_type_name,
                penalty_kopecks=penalty_kopecks,
                deduction_kopecks=deduction_kopecks,
                additional_payment_kopecks=additional_payment_kopecks,
            )

        if commission_pct is not None:
            row["commissionPctSum"] += commission_pct
            row["commissionPctCount"] += 1
        discount_pct = _number_or_none(item.get("sale_percent") or item.get("salePercent"))
        if discount_pct is not None:
            row["discountPctSum"] += discount_pct
            row["discountPctCount"] += 1
        spp_pct = _spp_pct_or_none(item.get("spp") or item.get("ppvz_spp_prc") or item.get("ppvzSppPrc"))
        if spp_pct is not None:
            row["sppPctSum"] += spp_pct
            row["sppPctCount"] += 1

    _allocate_global_finance_costs(result, global_rows)
    finance_ad_spend_authoritative = any(_finance_int(row.get("adSpendKopecks")) != 0 for row in result.values())
    for row in result.values():
        if finance_ad_spend_authoritative:
            row["financeAdSpendAuthoritative"] = True
        _finalize_finance_commission(row)
        row["commissionPct"] = (
            round(row["commissionPctSum"] / row["commissionPctCount"], 2)
            if row["commissionPctCount"]
            else None
        )
        row["discountPct"] = (
            round(row["discountPctSum"] / row["discountPctCount"], 2)
            if row["discountPctCount"]
            else None
        )
        row["sppPct"] = (
            round(row["sppPctSum"] / row["sppPctCount"], 2)
            if row["sppPctCount"]
            else None
        )
        row["platformDiscountKopecks"] = max(
            0,
            int(row.get("sellerRevenueKopecks") or 0) - int(row.get("buyerRevenueKopecks") or 0),
        )
        row["netSalesUnits"] = int(row.get("salesUnits") or 0) - int(row.get("returnsUnits") or 0)
        row["sppSource"] = "finance_sales_reports_detailed.spp" if row["sppPct"] is not None else None
        row.pop("commissionPctSum", None)
        row.pop("commissionPctCount", None)
        row.pop("discountPctSum", None)
        row.pop("discountPctCount", None)
        row.pop("sppPctSum", None)
        row.pop("sppPctCount", None)
        row["unitKeyedSalesCount"] = len(row.pop("_saleUnitKeys", set()))
        row["unitKeyedReturnsCount"] = len(row.pop("_returnUnitKeys", set()))

    daily_result: dict[str, dict[str, dict[str, Any]]] = {}
    daily_global_rows: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        item_date = _finance_operation_date(item)
        if item_date is None:
            continue
        nm_id = int(item.get("nmId") or item.get("nmID") or item.get("nm_id") or 0)
        if nm_id <= 0:
            daily_global_rows.setdefault(item_date.isoformat(), []).append(item)
            continue
        row = daily_result.setdefault(item_date.isoformat(), {}).setdefault(
            str(nm_id),
            {
                "vendorCode": str(item.get("vendorCode") or item.get("vendor_code") or "").strip(),
                "rowsCount": 0,
                "salesUnits": 0,
                "returnsUnits": 0,
                "netSalesUnits": 0,
                "revenueGrossKopecks": 0,
                "grossSalesKopecks": 0,
                "returnsKopecks": 0,
                "buyerRevenueKopecks": 0,
                "sellerRevenueKopecks": 0,
                "sellerRevenueRows": 0,
                "sellerRevenueMissingRows": 0,
                "platformDiscountKopecks": 0,
                "commissionFormulaKopecks": 0,
                "commissionKopecks": 0,
                "reportedCommissionRows": 0,
                "adSpendKopecks": 0,
                "logisticsKopecks": 0,
                "storageKopecks": 0,
                "acceptanceKopecks": 0,
                "penaltyKopecks": 0,
                "penaltyChargedKopecks": 0,
                "penaltyReturnedKopecks": 0,
                "deductionKopecks": 0,
                "deductionChargedKopecks": 0,
                "deductionCompensationKopecks": 0,
                "additionalPaymentKopecks": 0,
                "rewardAdjustmentKopecks": 0,
                "paymentScheduleKopecks": 0,
                "cashbackAmountKopecks": 0,
                "cashbackDiscountKopecks": 0,
                "cashbackCommissionChangeKopecks": 0,
                "loyaltyCostKopecks": 0,
                "acquiringKopecks": 0,
                "payableKopecks": 0,
                "_payableRows": 0,
                "_settlementRows": 0,
                "source": "finance_sales_reports_detailed",
                "_saleUnitKeys": set(),
                "_returnUnitKeys": set(),
            },
        )
        if not row.get("vendorCode"):
            row["vendorCode"] = str(item.get("vendorCode") or item.get("vendor_code") or "").strip()
        row["rowsCount"] += 1
        quantity = int(_number_or_none(item.get("quantity", item.get("saleQuantity", 1))) or 0)
        buyer_revenue_kopecks = _first_kopecks(item, "retailAmount", "retail_amount")
        seller_revenue_kopecks = _finance_seller_revenue_kopecks(item, quantity)
        if not date_from.date() <= item_date <= date_to.date():
            seller_revenue_kopecks = 0
        seller_revenue_available = (
            _finance_raw_first(
                item,
                "retailPriceWithDisc",
                "retail_price_withdisc_rub",
            )
            is not None
        )
        doc_type_name = str(item.get("docTypeName") or item.get("doc_type_name") or "").strip().lower()
        unit_key = _finance_unit_key(item)
        commission_pct = _first_number(item, "commission_percent", "commissionPercent")
        if doc_type_name in {"продажа", "возврат"}:
            row["sellerRevenueRows"] += int(seller_revenue_available)
            row["sellerRevenueMissingRows"] += int(not seller_revenue_available)
        if doc_type_name == "продажа":
            if unit_key:
                sale_unit_keys = row["_saleUnitKeys"]
                if unit_key not in sale_unit_keys:
                    sale_unit_keys.add(unit_key)
                    row["salesUnits"] += max(1, quantity)
            else:
                row["salesUnits"] += quantity
            row["buyerRevenueKopecks"] += buyer_revenue_kopecks
            row["sellerRevenueKopecks"] += seller_revenue_kopecks
            row["revenueGrossKopecks"] += seller_revenue_kopecks
            row["grossSalesKopecks"] += seller_revenue_kopecks
            if commission_pct is not None:
                row["commissionFormulaKopecks"] += round(buyer_revenue_kopecks * commission_pct / 100)
        elif doc_type_name == "возврат":
            if unit_key:
                return_unit_keys = row["_returnUnitKeys"]
                if unit_key not in return_unit_keys:
                    return_unit_keys.add(unit_key)
                    row["returnsUnits"] += max(1, quantity)
            else:
                row["returnsUnits"] += quantity
            row["buyerRevenueKopecks"] -= buyer_revenue_kopecks
            row["sellerRevenueKopecks"] -= seller_revenue_kopecks
            row["revenueGrossKopecks"] -= seller_revenue_kopecks
            row["returnsKopecks"] += seller_revenue_kopecks
            if commission_pct is not None:
                row["commissionFormulaKopecks"] -= round(buyer_revenue_kopecks * commission_pct / 100)
        if doc_type_name in {"продажа", "возврат"} and (buyer_revenue_kopecks or seller_revenue_kopecks):
            row["_settlementRows"] += 1
        document_sign = -1 if doc_type_name == "возврат" else 1
        reported_commission = _finance_raw_first(
            item,
            "ppvz_sales_commission",
            "ppvzSalesCommission",
            "commission",
            "commissionRub",
        )
        if reported_commission is not None:
            row["reportedCommissionRows"] += 1
            row["commissionKopecks"] += document_sign * _kopecks_from_rub(reported_commission)
        payable = _finance_raw_first(item, "ppvz_for_pay", "forPay")
        if payable is not None:
            row["_payableRows"] += 1
            row["payableKopecks"] += document_sign * _kopecks_from_rub(payable)
        costs = _finance_row_costs(item)
        row["adSpendKopecks"] += costs["adSpendKopecks"]
        row["logisticsKopecks"] += costs["logisticsKopecks"]
        row["storageKopecks"] += costs["storageKopecks"]
        row["acceptanceKopecks"] += costs["acceptanceKopecks"]
        penalty_kopecks = costs["penaltyKopecks"]
        deduction_kopecks = costs["deductionKopecks"]
        additional_payment_kopecks = costs["additionalPaymentKopecks"]
        cashback_amount_kopecks = costs["cashbackAmountKopecks"]
        cashback_discount_kopecks = costs["cashbackDiscountKopecks"]
        cashback_commission_change_kopecks = costs["cashbackCommissionChangeKopecks"]
        row["penaltyKopecks"] += penalty_kopecks
        row["penaltyChargedKopecks"] += penalty_kopecks if penalty_kopecks >= 0 else 0
        row["penaltyReturnedKopecks"] += -penalty_kopecks if penalty_kopecks < 0 else 0
        row["deductionKopecks"] += deduction_kopecks
        row["deductionChargedKopecks"] += deduction_kopecks if deduction_kopecks >= 0 else 0
        row["deductionCompensationKopecks"] += -deduction_kopecks if deduction_kopecks < 0 else 0
        row["additionalPaymentKopecks"] += additional_payment_kopecks
        row["rewardAdjustmentKopecks"] += costs["rewardAdjustmentKopecks"]
        row["paymentScheduleKopecks"] += costs["paymentScheduleKopecks"]
        row["cashbackAmountKopecks"] += cashback_amount_kopecks
        row["cashbackDiscountKopecks"] += cashback_discount_kopecks
        row["cashbackCommissionChangeKopecks"] += cashback_commission_change_kopecks
        row["loyaltyCostKopecks"] += cashback_amount_kopecks + cashback_commission_change_kopecks
        row["acquiringKopecks"] += costs["acquiringKopecks"]
    for day_key, global_rows_for_day in daily_global_rows.items():
        _allocate_global_finance_costs(
            daily_result.setdefault(day_key, {}),
            global_rows_for_day,
            fallback_aggregates=result,
        )
    for rows_by_nm in daily_result.values():
        finance_ad_spend_authoritative = any(
            _finance_int(row.get("adSpendKopecks")) != 0 for row in rows_by_nm.values()
        )
        for row in rows_by_nm.values():
            if finance_ad_spend_authoritative:
                row["financeAdSpendAuthoritative"] = True
            _finalize_finance_commission(row)
            row["platformDiscountKopecks"] = max(
                0,
                int(row.get("sellerRevenueKopecks") or 0) - int(row.get("buyerRevenueKopecks") or 0),
            )
            row["netSalesUnits"] = int(row.get("salesUnits") or 0) - int(row.get("returnsUnits") or 0)
            row["unitKeyedSalesCount"] = len(row.pop("_saleUnitKeys", set()))
            row["unitKeyedReturnsCount"] = len(row.pop("_returnUnitKeys", set()))

    return {
        "aggregates": result,
        "dailyAggregates": daily_result,
        "rows": source_rows,
        "diagnostics": build_finance_diagnostics_from_aggregates(
            result,
            raw_rows=rows,
            requested_fields=fields,
            date_from=date_from,
            date_to=date_to,
        ),
        "count": len(result),
        "rowsCount": len(rows),
        "requestedFields": fields,
        "revenueBasis": FINANCE_REVENUE_BASIS,
        "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
        "dateFrom": date_from.date().isoformat(),
        "dateTo": date_to.date().isoformat(),
    }


def _merge_ads_spend_aggregate_from_node(
    node: Any,
    result: dict[str, dict[str, Any]],
    *,
    start: date | None = None,
    end: date | None = None,
    inherited_date: date | None = None,
) -> None:
    if isinstance(node, list):
        for item in node:
            _merge_ads_spend_aggregate_from_node(item, result, start=start, end=end, inherited_date=inherited_date)
        return
    if not isinstance(node, dict):
        return

    local_date = _ads_node_date(node, inherited_date)
    node_in_range = local_date is None or (
        (start is None or local_date >= start)
        and (end is None or local_date <= end)
    )
    nm_id = _int_or_none(node.get("nmId") or node.get("nm"))
    if nm_id is not None and node_in_range and ("sum" in node or "spend" in node):
        row = result.setdefault(
            str(nm_id),
            {
                "adSpendKopecks": 0,
                "adImpressions": 0,
                "adClicks": 0,
                "adCartAdds": 0,
                "adOrders": 0,
                "adRevenueKopecks": 0,
                "source": "wb_ads_fullstats",
            },
        )
        row["adSpendKopecks"] += _kopecks_from_rub(node.get("sum") or node.get("spend"))
        row["adImpressions"] += int(node.get("views") or node.get("impressions") or 0)
        row["adClicks"] += int(node.get("clicks") or 0)
        row["adCartAdds"] += int(node.get("atbs") or node.get("cartAdds") or 0)
        row["adOrders"] += int(node.get("orders") or 0)
        row["adRevenueKopecks"] += _kopecks_from_rub(node.get("sum_price") or node.get("revenue"))

    for value in node.values():
        _merge_ads_spend_aggregate_from_node(value, result, start=start, end=end, inherited_date=local_date)


def _ads_aggregate_totals(result: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals = {
        "adSpendKopecks": 0,
        "adImpressions": 0,
        "adClicks": 0,
        "adCartAdds": 0,
        "adOrders": 0,
        "adRevenueKopecks": 0,
    }
    for row in result.values():
        if not isinstance(row, dict):
            continue
        for key in totals:
            totals[key] += int(row.get(key) or 0)
    return totals


def fetch_ads_spend_aggregates(
    scenario: str,
    wb_token: str | None = None,
    *,
    date_from: datetime,
    date_to: datetime | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario, token_override=wb_token))
    date_to = date_to or _utc_now()
    promotion = _request_or_raise(client, WbApiRequest(method="GET", path="/adv/v1/promotion/count"))
    campaign_ids = _ads_campaign_ids_from_payload(promotion)
    if not campaign_ids:
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "campaigns",
                    "campaignsProcessed": 0,
                    "campaignsTotal": 0,
                    "requestsCompleted": 0,
                    "requestsTotal": 0,
                }
            )
        return {
            "aggregates": {},
            "dailyAggregates": {},
            "count": 0,
            "campaignCount": 0,
            "totals": _ads_aggregate_totals({}),
            "dateFrom": date_from.date().isoformat(),
            "dateTo": date_to.date().isoformat(),
            "source": "wb_ads_fullstats",
        }

    result: dict[str, dict[str, Any]] = {}
    daily_result: dict[str, dict[str, dict[str, Any]]] = {}
    campaign_batches = [
        campaign_ids[batch_start : batch_start + 50]
        for batch_start in range(0, len(campaign_ids), 50)
    ]
    date_windows = _ads_fullstats_date_windows(date_from.date(), date_to.date())
    requests_total = max(1, len(campaign_batches) * len(date_windows))
    requests_completed = 0
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "campaigns",
                "campaignsProcessed": 0,
                "campaignsTotal": len(campaign_ids),
                "requestsCompleted": 0,
                "requestsTotal": requests_total,
            }
        )
    for batch_start in range(0, len(campaign_ids), 50):
        batch = campaign_ids[batch_start : batch_start + 50]
        for window_start, window_end in date_windows:
            def report_ads_wait(message: str) -> None:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "fullstats-wait",
                            "message": message,
                            "campaignsProcessed": min(len(campaign_ids), batch_start),
                            "campaignsTotal": len(campaign_ids),
                            "requestsCompleted": requests_completed,
                            "requestsTotal": requests_total,
                        }
                    )

            for payload in _request_ads_fullstats_payloads(
                client,
                batch,
                start=window_start,
                end=window_end,
                wait_callback=report_ads_wait,
            ):
                _merge_ads_spend_aggregate_from_node(payload, result, start=date_from.date(), end=date_to.date())
                for day in _date_range(date_from.date(), date_to.date()):
                    day_rows = daily_result.setdefault(day.isoformat(), {})
                    _merge_ads_spend_aggregate_from_node(payload, day_rows, start=day, end=day)
            requests_completed += 1
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "fullstats",
                        "campaignsProcessed": min(len(campaign_ids), batch_start + len(batch)),
                        "campaignsTotal": len(campaign_ids),
                        "requestsCompleted": requests_completed,
                        "requestsTotal": requests_total,
                    }
                )

    totals = _ads_aggregate_totals(result)
    return {
        "aggregates": result,
        "dailyAggregates": daily_result,
        "count": len(result),
        "campaignCount": len(campaign_ids),
        "totals": totals,
        "dateFrom": date_from.date().isoformat(),
        "dateTo": date_to.date().isoformat(),
        "source": "wb_ads_fullstats",
    }


def _fetch_promotion_nomenclatures(
    client: RateLimitedWbApiClient,
    promotion_id: int,
    *,
    in_action: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        nomenclatures_payload = _request_or_raise_calendar(
            client,
            WbApiRequest(
                method="GET",
                path="/api/v1/calendar/promotions/nomenclatures",
                query={
                    "promotionID": promotion_id,
                    "inAction": in_action,
                    "limit": _PROMOTION_NOMENCLATURES_PAGE_SIZE,
                    "offset": offset,
                },
            ),
        )
        page = nomenclatures_payload.get("nomenclatures") if isinstance(nomenclatures_payload, dict) else None
        if not isinstance(page, list):
            raise HTTPException(status_code=502, detail="WB вернул неполный состав участников акции")
        if not page:
            break
        for item in page:
            nm_id = product_id(item.get("id")) if isinstance(item, dict) else None
            if nm_id is None or type(item.get("inAction")) is not bool:
                raise HTTPException(status_code=502, detail="WB вернул некорректный состав участников акции")
            if in_action and item["inAction"] is not True:
                continue
            rows.append({**item, "id": nm_id, "promotionID": promotion_id})
        if len(page) < _PROMOTION_NOMENCLATURES_PAGE_SIZE:
            break
        offset += _PROMOTION_NOMENCLATURES_PAGE_SIZE
    return rows


def _fetch_promotions_bundle(
    scenario: str,
    wb_token: str | None = None,
    *,
    summary_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    client = RateLimitedWbApiClient(inner=build_wb_promotions_client(scenario, token_override=wb_token))
    now = _utc_now()
    promotions: list[dict[str, Any]] = []
    offset = 0
    while True:
        listing = _request_or_raise_calendar(
            client,
            WbApiRequest(
                method="GET",
                path="/api/v1/calendar/promotions",
                query={
                    "startDateTime": (now - timedelta(days=60)).isoformat(),
                    "endDateTime": (now + timedelta(days=120)).isoformat(),
                    "allPromo": True,
                    "limit": _PROMOTION_LIST_PAGE_SIZE,
                    "offset": offset,
                },
            ),
        )
        page = listing.get("promotions") if isinstance(listing, dict) else None
        if not isinstance(page, list) or any(not isinstance(item, dict) or product_id(item.get("id")) is None for item in page):
            raise HTTPException(status_code=502, detail="WB вернул неполный список акций")
        if not page:
            break
        promotions.extend(item for item in page if isinstance(item, dict))
        if len(page) < _PROMOTION_LIST_PAGE_SIZE:
            break
        offset += _PROMOTION_LIST_PAGE_SIZE

    refresh_promotion_ids = [
        int(item.get("id"))
        for item in promotions
        if item.get("id") is not None and _promotion_window_status(item, now) in {"active", "upcoming"}
    ]

    details_index: dict[int, dict[str, Any]] = {}
    for batch_start in range(0, len(refresh_promotion_ids), _PROMOTION_DETAILS_BATCH_SIZE):
        batch_ids = refresh_promotion_ids[batch_start : batch_start + _PROMOTION_DETAILS_BATCH_SIZE]
        if not batch_ids:
            continue
        try:
            details_payload = _request_or_raise_calendar(
                client,
                WbApiRequest(
                    method="GET",
                    path="/api/v1/calendar/promotions/details",
                    query={"promotionIDs": batch_ids},
                ),
            )
        except HTTPException as exc:
            if summary_only:
                raise
            if exc.status_code not in {400, 409, 429, 500, 502, 503, 504}:
                raise
            details_payload = {}
        for item in details_payload.get("promotions", []) if isinstance(details_payload, dict) else []:
            details_index[int(item.get("id"))] = item

    if summary_only:
        # The Promotions page needs account totals, not thousands of SKU reads.
        # Keep the full bundle used by other workflows unchanged.
        if any(
            promotion_id not in details_index
            or not isinstance(details_index[promotion_id].get("inPromoActionTotal"), int)
            or not isinstance(details_index[promotion_id].get("notInPromoActionTotal"), int)
            for promotion_id in refresh_promotion_ids
        ):
            raise HTTPException(status_code=502, detail="WB вернул неполные сведения об акциях")
        return promotions, details_index, []

    nomenclatures: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    by_id = {int(item["id"]): item for item in promotions}
    for promotion_id in refresh_promotion_ids:
        detail = details_index.setdefault(promotion_id, {})
        if str(by_id[promotion_id].get("type") or detail.get("type") or "").lower() != "regular":
            detail["_membershipState"] = "unsupported"
            continue
        # An interrupted page must fail the collection, preserving the previous
        # source cache instead of publishing a false empty/complete snapshot.
        nomenclature_rows = _fetch_promotion_nomenclatures(client, promotion_id, in_action=True)
        detail["_membershipState"] = "complete"
        detail["_membershipFetchedAt"] = now.isoformat()
        for item in nomenclature_rows:
            pair = (promotion_id, int(item["id"]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            nomenclatures.append(item)
    return promotions, details_index, nomenclatures


def _safe_fetch_promotions_bundle(
    scenario: str,
    wb_token: str | None = None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    try:
        return _fetch_promotions_bundle(scenario, wb_token=wb_token)
    except HTTPException as exc:
        if exc.status_code in {401, 402, 403, 429, 500, 502, 503, 504}:
            return [], {}, []
        raise


def _sku_cost_settings(article_id: str, use_demo_data: bool = True) -> dict[str, Any]:
    article_id = article_id.strip()
    article_type = _article_type(article_id)
    defaults = deepcopy(TYPE_DEFAULTS.get(article_type, TYPE_DEFAULTS["F"]))
    cogs_key = {"F": "tshirt", "H": "hoodie", "L": "longsleeve"}.get(article_type)
    cogs_by_garment = ALGORITHM_SETTINGS_STATE.get("cogsByGarmentRub")
    cogs_rub = 0.0
    if cogs_key and isinstance(cogs_by_garment, dict):
        try:
            cogs_rub = float(cogs_by_garment.get(cogs_key) or 0)
        except (TypeError, ValueError):
            cogs_rub = 0
        if cogs_rub > 0:
            defaults["cogsKopecks"] = int(round(cogs_rub * 100))
    try:
        target_margin_pct = float(ALGORITHM_SETTINGS_STATE.get("targetMarginPct") or 0)
    except (TypeError, ValueError):
        target_margin_pct = 0
    try:
        other_expense_per_sale_kopecks = round(float(ALGORITHM_SETTINGS_STATE.get("otherExpensePerSaleRub") or 0) * 100)
    except (TypeError, ValueError):
        other_expense_per_sale_kopecks = 0
    try:
        work_return_per_sale_kopecks = round(float(ALGORITHM_SETTINGS_STATE.get("workReturnPerSaleRub") or 0) * 100)
    except (TypeError, ValueError):
        work_return_per_sale_kopecks = 0
    other_expense_price_pct = _algorithm_float_setting(
        "otherExpensePricePct",
        float(DEFAULT_ALGORITHM_SETTINGS.get("otherExpensePricePct") or 0),
    )
    if other_expense_price_pct <= 0:
        other_expense_price_pct = float(DEFAULT_ALGORITHM_SETTINGS.get("otherExpensePricePct") or 0)
    from app.platform.economics.wb_quarterly_tax import planning_tax_pct
    tax_pct = planning_tax_pct(ALGORITHM_SETTINGS_STATE)
    if tax_pct is None:
        tax_pct = _algorithm_float_setting("taxPct", float(DEFAULT_ALGORITHM_SETTINGS.get("taxPct") or 0))
    if target_margin_pct > 0:
        defaults["minMarginPct"] = int(round(target_margin_pct))
    default_step_minutes = max(5, _algorithm_int_setting("syncIntervalMinutes", 60))
    settings = {
        "cogsKopecks": int(defaults["cogsKopecks"]),
        "wbCommissionPct": float(TEMPLATES_STATE["globalCommissionPct"]),
        "logisticsKopecks": int(defaults["logisticsKopecks"]),
        "deliveryToClientKopecks": int(defaults["logisticsKopecks"]),
        "deliveryFromClientKopecks": int(defaults["logisticsKopecks"]),
        "otherExpensePerSaleKopecks": max(0, int(other_expense_per_sale_kopecks)),
        "workReturnPerSaleKopecks": max(0, int(work_return_per_sale_kopecks)),
        "otherExpensePricePct": max(0.0, other_expense_price_pct),
        "taxPct": max(0.0, tax_pct),
        "minMarginKopecks": 0,
        "minMarginPct": int(defaults["minMarginPct"]),
        "pMinKopecks": 0,
        "maxMarginKopecks": 0,
        "maxMarginPct": 0.0,
        "pMaxKopecks": int(defaults["pMaxKopecks"]),
        "priceStepPct": float(ALGORITHM_SETTINGS_STATE.get("priceStepPct") or 6),
        "priceStepMinutes": default_step_minutes,
        "priceStepHours": max(1, int(default_step_minutes / 60)),
        "rrpKopecks": 0,
        "pickPackCostPercent": 0.0,
        "promoCostPercent": max(0.0, other_expense_price_pct),
        "competeDiffType": None,
        "competePriceType": None,
        "competeDiffValue": 0,
        "ordersPlanQty": 0,
        "ordersPlanDays": 0,
        "ordersPlanType": None,
        "normalStockQty": 0,
        "targetDiscountPct": 0.0,
        "minCompeteStock": 0,
        "useSpp": False,
        "beautyPriceEnabled": False,
        "beautyPriceMode": None,
        "beautyPriceTemplate": None,
        "beautyPriceMaxChangeKopecks": 0,
        "beautyPriceMaxChangePct": 0.0,
        "storageCostPerSaleKopecks": 0,
        "discountType": None,
        "beautyPriceLevel": None,
        "promoBoostType": None,
        "useOutOfStock": False,
        "targetTurnover": 0,
        "advertCostPercent": 0.0,
        "competeWalletType": None,
        "stockFbs": 0,
        "stockFbm": 0,
        "allowNegativeMargin": False,
        "automationEnabled": True,
        "basketNormMode": "auto",
        "basketNormManual": None,
        "repricerMode": "baskets",
        "revenueComparisonDays": 7,
        "nightMedianEnabled": True,
        "promoBoostEnabled": False,
        "promoBoostPct": 25,
        "promoBoostHours": 48,
    }
    settings.update(TEMPLATES_STATE["types"].get(_article_type(article_id), {}))
    if cogs_rub > 0:
        settings["cogsKopecks"] = int(round(cogs_rub * 100))
    if use_demo_data:
        settings.update(DEMO_SKU_SETTINGS_OVERRIDES.get(article_id, {}))
    settings.update(SKU_SETTINGS_OVERRIDES.get(article_id, {}))
    settings["taxPct"] = max(0.0, tax_pct)
    settings["workReturnPerSaleKopecks"] = max(0, int(work_return_per_sale_kopecks))
    default_other_expense_price_pct = float(DEFAULT_ALGORITHM_SETTINGS.get("otherExpensePricePct") or 0)
    try:
        current_other_expense_price_pct = float(settings.get("otherExpensePricePct") or 0)
    except (TypeError, ValueError):
        current_other_expense_price_pct = 0
    if current_other_expense_price_pct <= 0 and default_other_expense_price_pct > 0:
        settings["otherExpensePricePct"] = default_other_expense_price_pct
    else:
        settings["otherExpensePricePct"] = max(0.0, current_other_expense_price_pct)
    try:
        settings["otherExpensePerSaleKopecks"] = max(0, int(settings.get("otherExpensePerSaleKopecks") or 0))
    except (TypeError, ValueError):
        settings["otherExpensePerSaleKopecks"] = 0
    settings["deliveryToClientKopecks"] = int(settings.get("deliveryToClientKopecks") or settings.get("logisticsKopecks") or 0)
    settings["deliveryFromClientKopecks"] = int(settings.get("deliveryFromClientKopecks") or settings.get("returnLogisticsKopecks") or settings.get("logisticsKopecks") or 0)
    return settings


def _active_promotion_articles(promotions: list[dict[str, Any]]) -> set[str]:
    active: set[str] = set()
    for promotion in promotions:
        if promotion.get("status") not in {"active", "upcoming"}:
            continue
        article_ids = promotion.get("articleIds") or []
        if isinstance(article_ids, set):
            active.update(str(item) for item in article_ids if item)
        else:
            active.update(str(item) for item in article_ids if item)
    return active


def _active_promotion_nm_ids(promotions: list[dict[str, Any]]) -> set[int]:
    active: set[int] = set()
    for promotion in promotions:
        if promotion.get("status") not in {"active", "upcoming"}:
            continue
        for item in promotion.get("thresholdNmIds") or promotion.get("nmIds") or []:
            try:
                active.add(int(item))
            except (TypeError, ValueError):
                continue
    return active


def _promotion_label(promotion: dict[str, Any]) -> str:
    raw = promotion.get("name") or promotion.get("title") or promotion.get("promotionName")
    if raw:
        return str(raw)
    promotion_id = promotion.get("id") or promotion.get("promotionID") or promotion.get("promotionId")
    return f"Акция {promotion_id}" if promotion_id else "Акция WB"


def _active_promotion_labels_by_article(promotions: list[dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for promotion in promotions:
        if promotion.get("status") not in {"active", "upcoming"}:
            continue
        label = _promotion_label(promotion)
        article_ids = promotion.get("articleIds") or []
        for item in article_ids:
            article_id = str(item).strip()
            if article_id:
                labels.setdefault(article_id, label)
    return labels


def _active_promotion_labels_by_nm_id(promotions: list[dict[str, Any]]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for promotion in promotions:
        if promotion.get("status") not in {"active", "upcoming"}:
            continue
        label = _promotion_label(promotion)
        for key in promotion.get("thresholdNmIds") or promotion.get("nmIds") or []:
            try:
                labels.setdefault(int(key), label)
            except (TypeError, ValueError):
                continue
    return labels


def _promotion_id(promotion: dict[str, Any]) -> str | int | None:
    return promotion.get("id") or promotion.get("promotionID") or promotion.get("promotionId")


def _active_promotion_for_article(
    article_id: str,
    promotions: list[dict[str, Any]],
    *,
    nm_id: int | None = None,
) -> dict[str, Any] | None:
    for promotion in promotions:
        if promotion.get("status") not in {"active", "upcoming"}:
            continue
        if article_id in promotion.get("articleIds", set()):
            return promotion
        if nm_id is None:
            continue
        try:
            nm_ids = {int(item) for item in promotion.get("thresholdNmIds") or promotion.get("nmIds") or []}
        except (TypeError, ValueError):
            nm_ids = set()
        if nm_id in nm_ids:
            return promotion
    return None


def _promotion_status_for_article(
    article_id: str,
    promotions: list[dict[str, Any]],
    *,
    nm_id: int | None = None,
    active_promotion_articles: set[str] | None = None,
    active_promotion_nm_ids: set[int] | None = None,
) -> str:
    if active_promotion_articles is not None:
        if article_id in active_promotion_articles:
            return "yes"
        if nm_id is not None and active_promotion_nm_ids is not None and nm_id in active_promotion_nm_ids:
            return "yes"
        return "no"
    for promotion in promotions:
        if promotion["status"] not in {"active", "upcoming"}:
            continue
        if article_id in promotion.get("articleIds", set()):
            return "yes"
        if nm_id is not None:
            try:
                nm_ids = {int(item) for item in promotion.get("thresholdNmIds") or promotion.get("nmIds") or []}
            except (TypeError, ValueError):
                nm_ids = set()
            if nm_id in nm_ids:
                return "yes"
    return "no"


def _promotion_status_text_for_article(
    article_id: str,
    *,
    nm_id: int | None = None,
    active_promotion_articles: set[str] | None = None,
    active_promotion_nm_ids: set[int] | None = None,
    active_promotion_labels_by_article: dict[str, str] | None = None,
    active_promotion_labels_by_nm_id: dict[int, str] | None = None,
) -> str | None:
    if active_promotion_labels_by_article:
        label = active_promotion_labels_by_article.get(article_id)
        if label:
            return label
    if nm_id is not None and active_promotion_labels_by_nm_id:
        label = active_promotion_labels_by_nm_id.get(nm_id)
        if label:
            return label
    if active_promotion_articles and article_id in active_promotion_articles:
        return "В акции"
    if nm_id is not None and active_promotion_nm_ids and nm_id in active_promotion_nm_ids:
        return "В акции"
    return None


def _good_demand_sort_key(
    good: dict[str, Any],
    *,
    cached_period_stats: dict[str, dict[str, Any]] | None,
    cached_baskets_aggregates: dict[str, dict[str, Any]] | None,
) -> tuple[int, int, int, str]:
    nm_id = int(good.get("nmID") or 0)
    lookup_key = str(nm_id) if nm_id > 0 else ""
    period = (cached_period_stats or {}).get(lookup_key) or {}
    baskets = (cached_baskets_aggregates or {}).get(lookup_key) or {}
    orders = int(period.get("ordersUnits") or 0)
    cart_count = int(baskets.get("cartCount") or 0)
    revenue = int(period.get("revenueKopecks") or 0)
    return (-orders, -cart_count, -revenue, str(good.get("vendorCode") or ""))


def _wb_basket_number(nm_id: int) -> int:
    if nm_id <= 143:
        return 1
    if nm_id <= 287:
        return 2
    if nm_id <= 431:
        return 3
    if nm_id <= 719:
        return 4
    if nm_id <= 1007:
        return 5
    if nm_id <= 1061:
        return 6
    if nm_id <= 1115:
        return 7
    if nm_id <= 1169:
        return 8
    if nm_id <= 1313:
        return 9
    if nm_id <= 1601:
        return 10
    if nm_id <= 1655:
        return 11
    if nm_id <= 1919:
        return 12
    if nm_id <= 2045:
        return 13
    if nm_id <= 2189:
        return 14
    if nm_id <= 2405:
        return 15
    if nm_id <= 2621:
        return 16
    return 17


def _wb_public_photo_url(nm_id: int | None) -> str | None:
    if not nm_id or nm_id <= 0:
        return None
    volume = nm_id // 100000
    part = nm_id // 1000
    return f"https://basket-{_wb_basket_number(volume):02d}.wbbasket.ru/vol{volume}/part{part}/{nm_id}/images/c516x688/1.webp"


def _extract_wb_media_url(payload: dict[str, Any] | None, nm_id: int | None = None) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("photoUrl", "imageUrl", "previewUrl", "bigPhoto", "smallPhoto", "c246x328", "c516x688", "square", "tm", "big"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("photos", "mediaFiles", "images", "pictures"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    nested = _extract_wb_media_url(item, nm_id=None)
                    if nested:
                        return nested
    # CDN sharding cannot be reliably inferred from an nmID. Only return
    # a photo supplied by WB or saved for this exact product.
    return None


def _build_sku_row(
    article_id: str,
    *,
    nm_id: int | None,
    name: str,
    subject: str,
    brand: str | None,
    chrt_ids: list[int],
    current_price_kopecks: int,
    discounted_price_kopecks: int,
    buyer_price_kopecks: int | None,
    buyer_price_with_wallet_kopecks: int | None = None,
    spp_pct: float | None = None,
    wb_wallet_pct: float | None = None,
    promotions: list[dict[str, Any]],
    active_promotion_articles: set[str] | None = None,
    active_promotion_nm_ids: set[int] | None = None,
    use_demo_data: bool,
    stock_aggregate: dict[str, Any] | None = None,
    stocks_cache_loaded: bool = False,
    period_aggregate: dict[str, Any] | None = None,
    previous_period_aggregate: dict[str, Any] | None = None,
    period_days: int | None = None,
    finance_aggregate: dict[str, Any] | None = None,
    finance_tax: dict[str, Any] | None = None,
    ads_aggregate: dict[str, Any] | None = None,
    commission_tariffs_index: dict[str, dict[str, Any]] | None = None,
    baskets_aggregate: dict[str, Any] | None = None,
    baskets_cache_loaded: bool = False,
    list_view: bool = False,
    active_promotion_labels_by_article: dict[str, str] | None = None,
    active_promotion_labels_by_nm_id: dict[int, str] | None = None,
    subject_id: int | None = None,
    image_url: str | None = None,
    buyer_price_source: str | None = None,
    buyer_original_price_kopecks: int | None = None,
    buyer_price_observed_at: str | None = None,
    buyer_price_expires_at: str | None = None,
    canonical_cost_kopecks: int | None = None,
    price_baseline: dict[str, Any] | None = None,
    price_observed_at: str | None = None,
    promotion_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta_overrides = _sku_meta_seed(article_id, use_demo_data)
    if meta_overrides["status"] == "warmup":
        meta_overrides["basketNormSource"] = "fallback"

    settings = _sku_cost_settings(article_id, use_demo_data=use_demo_data)
    if canonical_cost_kopecks is not None:
        settings["cogsKopecks"] = canonical_cost_kopecks
        settings["cogsSource"] = "catalog"
    current_status = _status_after_settings(article_id, str(meta_overrides["status"]), bool(settings["automationEnabled"]))
    price_kopecks = discounted_price_kopecks or current_price_kopecks
    local_price_override_active = (
        meta_overrides.get("currentPriceKopecks") is not None
        and str(meta_overrides.get("localPriceOverrideSource") or "") not in {"wb_api", "wb_api_pending"}
        and get_settings().repricer_preserve_local_price_overrides
    )
    if local_price_override_active:
        price_kopecks = int(meta_overrides["currentPriceKopecks"])
        if price_kopecks != discounted_price_kopecks:
            buyer_price_kopecks = buyer_price_with_wallet_kopecks = spp_pct = None
    meta = {
        "articleId": article_id,
        "nmId": nm_id,
        "name": name,
        "status": current_status,
        "currentPriceKopecks": price_kopecks,
        "basketsLast7d": int(meta_overrides["basketsLast7d"]),
        "basketNorm": int(meta_overrides["basketNorm"]),
        "basketNormSource": meta_overrides["basketNormSource"],
        "warmupDaysLeft": meta_overrides.get("warmupDaysLeft"),
        "lastSavedAt": price_observed_at,
        "priceObservedAt": price_observed_at,
        "wbPriceBaseline": price_baseline,
        "managerId": meta_overrides.get("managerId"),
        "managerName": meta_overrides.get("managerName", "Без ответственного"),
        "assignmentSource": meta_overrides.get("assignmentSource", "none"),
        "assignedAt": meta_overrides.get("assignedAt"),
        "subject": subject,
        "brand": brand,
        "imageUrl": image_url,
        "photoUrl": image_url,
        "chrtIds": chrt_ids,
        "priceSource": "local_override" if local_price_override_active else "wb_cache",
        "localPriceOverrideActive": local_price_override_active,
    }
    from app.repricer_dispatch_store import assignment_revision, pricing_revision
    pricing_state = {"assignments": FRONTEND_STRATEGY_ASSIGNMENTS, "skuMetaOverrides": SKU_META_OVERRIDES,
        "skuSettingsOverrides": SKU_SETTINGS_OVERRIDES, "liquidationActive": LIQUIDATION_ACTIVE}
    meta["assignmentRevision"] = assignment_revision(pricing_state, article_id)
    meta["pricingSettingsRevision"] = pricing_revision(ALGORITHM_SETTINGS_STATE, pricing_state, article_id)

    period_aggregate = period_aggregate or {}
    previous_period_aggregate = previous_period_aggregate or {}
    finance_aggregate = finance_aggregate or None
    ads_aggregate = ads_aggregate or {}
    ad_data_available = bool(ads_aggregate)
    sales_units = int(
        (finance_aggregate.get("salesUnits") or 0)
        if finance_aggregate is not None
        else (period_aggregate.get("salesUnits") or 0)
    )
    returns_units = int(
        (finance_aggregate.get("returnsUnits") or 0)
        if finance_aggregate is not None
        else (period_aggregate.get("returnsUnits") or 0)
    )
    net_sales_units = sales_units - returns_units
    period_orders_units = int(period_aggregate.get("ordersUnits") or 0)
    baskets_order_count_raw = (baskets_aggregate or {}).get("orderCount")
    baskets_order_count = int(baskets_order_count_raw) if baskets_order_count_raw is not None else None
    if baskets_order_count is not None and baskets_order_count < 0:
        baskets_order_count = None
    baskets_order_sum_kopecks = int((baskets_aggregate or {}).get("orderSumKopecks") or 0)
    funnel_avg_price_kopecks = (
        round(baskets_order_sum_kopecks / baskets_order_count)
        if baskets_order_sum_kopecks > 0 and baskets_order_count is not None and baskets_order_count > 0
        else None
    )
    previous_baskets = (baskets_aggregate or {}).get("previous") if isinstance((baskets_aggregate or {}).get("previous"), dict) else {}
    previous_orders_units = int(previous_period_aggregate.get("ordersUnits") or previous_baskets.get("orderCount") or 0)
    finance_units_floor = max(0, sales_units + returns_units)
    if baskets_order_count is not None:
        orders_units = baskets_order_count
        orders_source = "sales_funnel.orderCount"
    elif period_orders_units > 0:
        orders_units = period_orders_units
        orders_source = "supplier.orders"
    elif finance_units_floor > 0 and finance_aggregate is not None:
        orders_units = finance_units_floor
        orders_source = "finance_sales_fallback"
    else:
        orders_units = 0
        orders_source = None
    _resolve_basket_norm_for_row(
        article_id,
        meta,
        settings,
        period_orders_units=orders_units,
        period_days=period_days,
    )
    cogs_total_kopecks = settings["cogsKopecks"] * net_sales_units
    work_return_kopecks = int(settings.get("workReturnPerSaleKopecks") or 0) * net_sales_units
    finance_seller_revenue_kopecks = int((finance_aggregate or {}).get("sellerRevenueKopecks") or 0)
    if finance_aggregate is not None and "sellerRevenueKopecks" not in finance_aggregate:
        finance_seller_revenue_kopecks = price_kopecks * max(0, sales_units)
    finance_buyer_revenue_kopecks = int(
        (finance_aggregate or {}).get("buyerRevenueKopecks")
        or (finance_aggregate or {}).get("revenueGrossKopecks")
        or 0
    )
    other_expenses_kopecks = 0
    fact_tax_state = "configured" if (finance_tax or {}).get("factTaxState") == "configured" and type((finance_tax or {}).get("taxKopecks")) is int else "missing"
    fact_tax_reason = None if fact_tax_state == "configured" else (finance_tax or {}).get("factTaxReason") or "tax_context_missing"
    tax_kopecks = finance_tax["taxKopecks"] if fact_tax_state == "configured" else None
    acquiring_pct = float(ALGORITHM_SETTINGS_STATE["acquiringPct"])
    tariff_base_commission_pct, commission_source, tariff_fetched_at = _resolve_tariff_base_commission_pct(
        commission_tariffs_index,
        article_id=article_id,
        subject_id=subject_id,
        subject=subject,
    )
    report_commission_pct: float | None = (
        _number_or_none((finance_aggregate or {}).get("commissionPct"))
        if finance_aggregate is not None
        else None
    )
    if tariff_base_commission_pct is not None:
        category_commission_pct = tariff_base_commission_pct
        commission_source = commission_source or "tariffs.commission"
        commission_state = "ok"
        commission_reason = None
    elif report_commission_pct is not None:
        category_commission_pct = report_commission_pct
        commission_source = "finance.commissionPct"
        commission_state = "fallback"
        commission_reason = "Using finance report commission percent until WB tariffs sync refreshes category tariff"
    else:
        category_commission_pct = 0.0
        commission_source = "tariffs.commission.missing"
        commission_state = "no_data"
        commission_reason = "WB tariffs did not return commission for this SKU category"
    effective_commission_pct = round(category_commission_pct + acquiring_pct, 2)
    commission_display_pct = round(effective_commission_pct, 1) if commission_state in {"ok", "fallback"} else None
    buyout_source = (baskets_aggregate or {}).get("buyoutSource")
    buyout_pct_value = (
        _spp_pct_or_none((baskets_aggregate or {}).get("buyoutPct"))
        if buyout_source == "sales_funnel.conversions.buyoutPercent"
        else None
    )
    if use_demo_data and buyout_pct_value is None:
        buyout_pct_value = 72.0 if current_status == "auto" else 61.0
        buyout_source = "demo"
    buyout_fraction = buyout_pct_value / 100 if buyout_pct_value is not None else None
    delivery_to_client_kopecks = int(settings.get("deliveryToClientKopecks") or settings.get("logisticsKopecks") or 0)
    delivery_from_client_kopecks = int(settings.get("deliveryFromClientKopecks") or settings.get("returnLogisticsKopecks") or settings.get("logisticsKopecks") or 0)
    live_spp_pct = spp_pct if spp_pct is not None else _seller_spp_pct(price_kopecks, buyer_price_kopecks)
    period_spp_pct = _spp_pct_or_none(period_aggregate.get("sppPct")) if period_aggregate.get("sppPct") is not None else None
    finance_spp_pct = (
        _spp_pct_or_none((finance_aggregate or {}).get("sppPct"))
        if (finance_aggregate or {}).get("sppPct") is not None
        else None
    )
    spp_source = "live_buyer_price" if live_spp_pct is not None else None
    spp_observed_at = buyer_price_observed_at if live_spp_pct is not None else None
    if buyer_price_with_wallet_kopecks is None:
        buyer_price_with_wallet_kopecks = _derive_wallet_buyer_price_kopecks(buyer_price_kopecks, wb_wallet_pct)
    planning_spp_pct = (
        live_spp_pct
        if live_spp_pct is not None
        else period_spp_pct if period_spp_pct is not None else finance_spp_pct
    )
    planning_buyer_price_kopecks = (
        buyer_price_kopecks
        or _buyer_price_from_spp_pct(price_kopecks, planning_spp_pct)
    )
    spp_accounting_mode = _algorithm_spp_accounting_mode()
    configured_wallet_pct = _algorithm_wallet_pct()
    accounted_wb_wallet_pct = configured_wallet_pct if spp_accounting_mode == "spp_plus_wallet" else None
    accounted_buyer_price_kopecks = (
        _derive_wallet_buyer_price_kopecks(buyer_price_kopecks, accounted_wb_wallet_pct)
        if accounted_wb_wallet_pct is not None
        else buyer_price_kopecks
    )
    planning_accounted_buyer_price_kopecks = (
        _derive_wallet_buyer_price_kopecks(planning_buyer_price_kopecks, accounted_wb_wallet_pct)
        if accounted_wb_wallet_pct is not None
        else planning_buyer_price_kopecks
    )
    accounted_platform_discount_pct = (
        _combined_discount_pct(
            live_spp_pct,
            accounted_wb_wallet_pct if spp_accounting_mode == "spp_plus_wallet" else None,
        )
        if live_spp_pct is not None
        else None
    )
    total_wb_discount_pct = _discount_pct(price_kopecks, buyer_price_with_wallet_kopecks)
    unit_margin_kopecks: int | None = None
    margin_pct: float | None = None
    margin_sales_base_kopecks = (
        planning_accounted_buyer_price_kopecks
        or planning_buyer_price_kopecks
        or meta["currentPriceKopecks"]
    )
    planned_inputs_available = (tariff_base_commission_pct is not None and buyout_fraction is not None) or use_demo_data
    if price_kopecks > 0 and planned_inputs_available:
        planned_unit_commission_kopecks = round(price_kopecks * category_commission_pct / 100)
        planned_unit_acquiring_kopecks = round(price_kopecks * acquiring_pct / 100)
        planned_unit_forward_logistics_kopecks = round(delivery_to_client_kopecks * buyout_fraction)
        planned_unit_return_logistics_kopecks = round(delivery_from_client_kopecks * buyout_fraction)
        planned_unit_other_expenses_kopecks = int(settings.get("otherExpensePerSaleKopecks") or 0) + round(
            price_kopecks * float(settings.get("otherExpensePricePct") or 0) / 100
        )
        planned_unit_tax_kopecks = round(price_kopecks * float(settings.get("taxPct") or 0) / 100)
        unit_net = (
            margin_sales_base_kopecks
            + int(settings.get("workReturnPerSaleKopecks") or 0)
            - planned_unit_commission_kopecks
            - planned_unit_acquiring_kopecks
            - planned_unit_forward_logistics_kopecks
            - planned_unit_return_logistics_kopecks
            - planned_unit_other_expenses_kopecks
            - planned_unit_tax_kopecks
            - settings["cogsKopecks"]
        )
        unit_margin_kopecks = round(unit_net)
        margin_pct = round((unit_net / margin_sales_base_kopecks) * 100, 1) if margin_sales_base_kopecks > 0 else None
    else:
        planned_unit_commission_kopecks = 0
        planned_unit_acquiring_kopecks = 0
        planned_unit_forward_logistics_kopecks = 0
        planned_unit_return_logistics_kopecks = 0
        planned_unit_other_expenses_kopecks = 0
        planned_unit_tax_kopecks = 0

    if finance_aggregate is not None:
        from app.repricer_page_finance import report_revenue_kopecks
        revenue_gross_kopecks = report_revenue_kopecks(finance_aggregate)
        revenue_gross_kopecks = int(revenue_gross_kopecks) if revenue_gross_kopecks is not None else 0
        buyer_revenue_kopecks = finance_buyer_revenue_kopecks
        profit_revenue_kopecks = revenue_gross_kopecks if finance_aggregate.get("realizationVersion") else buyer_revenue_kopecks or revenue_gross_kopecks
        platform_discount_kopecks = max(0, finance_seller_revenue_kopecks - buyer_revenue_kopecks)
        finance_commission_kopecks = int(finance_aggregate.get("commissionKopecks") or 0)
        commission_formula_kopecks = int(finance_aggregate.get("commissionFormulaKopecks") or 0)
        commission_kopecks = finance_commission_kopecks
        commission_calc_mode = "finance_actual"
        logistics_kopecks = int(finance_aggregate.get("logisticsKopecks") or 0)
        storage_kopecks = int(finance_aggregate.get("storageKopecks") or 0)
        acceptance_kopecks = int(finance_aggregate.get("acceptanceKopecks") or 0)
        penalty_kopecks = int(finance_aggregate.get("penaltyKopecks") or 0)
        penalty_charged_kopecks = int(finance_aggregate.get("penaltyChargedKopecks") or 0)
        penalty_returned_kopecks = int(finance_aggregate.get("penaltyReturnedKopecks") or 0)
        deduction_kopecks = int(finance_aggregate.get("deductionKopecks") or 0)
        deduction_charged_kopecks = int(finance_aggregate.get("deductionChargedKopecks") or 0)
        deduction_compensation_kopecks = int(finance_aggregate.get("deductionCompensationKopecks") or 0)
        additional_payment_kopecks = int(finance_aggregate.get("additionalPaymentKopecks") or 0)
        reward_adjustment_kopecks = int(finance_aggregate.get("rewardAdjustmentKopecks") or 0)
        payment_schedule_kopecks = int(finance_aggregate.get("paymentScheduleKopecks") or 0)
        cashback_amount_kopecks = int(finance_aggregate.get("cashbackAmountKopecks") or 0)
        cashback_discount_kopecks = int(finance_aggregate.get("cashbackDiscountKopecks") or 0)
        cashback_commission_change_kopecks = int(finance_aggregate.get("cashbackCommissionChangeKopecks") or 0)
        loyalty_cost_kopecks = int(finance_aggregate.get("loyaltyCostKopecks") or 0)
        finance_acquiring_kopecks = int(finance_aggregate.get("acquiringKopecks") or 0)
        acquiring_kopecks = finance_acquiring_kopecks
        payable_kopecks = int(finance_aggregate.get("payableKopecks") or 0)
        ads_row = ads_aggregate or {}
        ad_spend_kopecks = int(
            finance_aggregate.get("adSpendKopecks")
            if finance_aggregate.get("financeAdSpendAuthoritative")
            else ads_row.get("adSpendKopecks") or finance_aggregate.get("adSpendKopecks") or 0
        )
        ad_impressions = int(ads_row.get("adImpressions") or 0)
        ad_clicks = int(ads_row.get("adClicks") or 0)
        ad_cart_adds = int(ads_row.get("adCartAdds") or 0)
        ad_orders = int(ads_row.get("adOrders") or 0)
        ad_revenue_kopecks = int(ads_row.get("adRevenueKopecks") or 0)
        expenses_kopecks = (
            commission_kopecks
            + logistics_kopecks
            + storage_kopecks
            + acceptance_kopecks
            + penalty_kopecks
            + deduction_kopecks
            + loyalty_cost_kopecks
            + acquiring_kopecks
            + ad_spend_kopecks
            + other_expenses_kopecks
            - additional_payment_kopecks
        )
        net_profit_kopecks = (
            profit_revenue_kopecks
            + work_return_kopecks
            - cogs_total_kopecks
            - expenses_kopecks
            - tax_kopecks
        ) if tax_kopecks is not None else None
    else:
        revenue_gross_kopecks = 0
        buyer_revenue_kopecks = 0
        profit_revenue_kopecks = 0
        platform_discount_kopecks = 0
        finance_commission_kopecks = None
        commission_formula_kopecks = None
        commission_calc_mode = "demo_planned" if use_demo_data else "no_finance"
        commission_kopecks = round(margin_sales_base_kopecks * effective_commission_pct / 100) if use_demo_data else None
        logistics_kopecks = settings["logisticsKopecks"] if use_demo_data else None
        storage_kopecks = 0 if use_demo_data else None
        acceptance_kopecks = 0 if use_demo_data else None
        penalty_kopecks = 0 if use_demo_data else None
        penalty_charged_kopecks = 0 if use_demo_data else None
        penalty_returned_kopecks = 0 if use_demo_data else None
        deduction_kopecks = 0 if use_demo_data else None
        deduction_charged_kopecks = 0 if use_demo_data else None
        deduction_compensation_kopecks = 0 if use_demo_data else None
        additional_payment_kopecks = 0 if use_demo_data else None
        reward_adjustment_kopecks = 0 if use_demo_data else None
        payment_schedule_kopecks = 0 if use_demo_data else None
        cashback_amount_kopecks = 0 if use_demo_data else None
        cashback_discount_kopecks = 0 if use_demo_data else None
        cashback_commission_change_kopecks = 0 if use_demo_data else None
        loyalty_cost_kopecks = 0 if use_demo_data else None
        acquiring_kopecks = 0 if use_demo_data else None
        payable_kopecks = 0 if use_demo_data else None
        ad_spend_kopecks = 0 if use_demo_data else None
        tax_kopecks = None
        fact_tax_state, fact_tax_reason = "missing", "finance_missing"
        ad_impressions = 0 if use_demo_data else None
        ad_clicks = 0 if use_demo_data else None
        ad_cart_adds = 0 if use_demo_data else None
        ad_orders = 0 if use_demo_data else None
        ad_revenue_kopecks = 0 if use_demo_data else None
        expenses_kopecks = (
            int(commission_kopecks or 0)
            + int(logistics_kopecks or 0)
            + int(storage_kopecks or 0)
            + int(acceptance_kopecks or 0)
            + int(penalty_kopecks or 0)
            + int(deduction_kopecks or 0)
            + int(loyalty_cost_kopecks or 0)
            + int(acquiring_kopecks or 0)
            + int(ad_spend_kopecks or 0)
            + int(other_expenses_kopecks or 0)
            - int(additional_payment_kopecks or 0)
        ) if use_demo_data else None
        net_profit_kopecks = round((unit_margin_kopecks or 0) * max(1, sales_units)) if use_demo_data else None
        report_commission_pct = None
    planned_sales_units = max(0, sales_units)
    planned_revenue_base_kopecks = (
        finance_buyer_revenue_kopecks
        if finance_buyer_revenue_kopecks > 0
        else margin_sales_base_kopecks * planned_sales_units
    )
    planned_seller_revenue_kopecks = (
        finance_seller_revenue_kopecks
        if finance_seller_revenue_kopecks > 0
        else price_kopecks * planned_sales_units
    )
    planned_commission_kopecks = round(planned_seller_revenue_kopecks * category_commission_pct / 100) if tariff_base_commission_pct is not None or use_demo_data else None
    planned_acquiring_kopecks = round(planned_seller_revenue_kopecks * acquiring_pct / 100)
    planned_forward_logistics_kopecks = round(delivery_to_client_kopecks * buyout_fraction * planned_sales_units) if buyout_fraction is not None else None
    planned_return_logistics_kopecks = round(delivery_from_client_kopecks * buyout_fraction * planned_sales_units) if buyout_fraction is not None else None
    planned_other_expenses_kopecks = (
        int(settings.get("otherExpensePerSaleKopecks") or 0) * planned_sales_units
        + round(planned_seller_revenue_kopecks * float(settings.get("otherExpensePricePct") or 0) / 100)
    )
    planned_tax_kopecks = round(planned_seller_revenue_kopecks * float(settings.get("taxPct") or 0) / 100)
    planned_period_margin_kopecks = (
        planned_revenue_base_kopecks
        + work_return_kopecks
        - planned_commission_kopecks
        - planned_acquiring_kopecks
        - planned_forward_logistics_kopecks
        - planned_return_logistics_kopecks
        - planned_other_expenses_kopecks
        - planned_tax_kopecks
        - cogs_total_kopecks
    ) if planned_inputs_available and (planned_sales_units > 0 or finance_aggregate is not None or use_demo_data) else None
    stock_units = (stock_aggregate or {}).get("wbStockUnits")
    has_stock_data = (stock_aggregate is not None and stock_units is not None) or (stocks_cache_loaded and nm_id is not None)
    if has_stock_data:
        wb_stock_units = int(stock_units or 0)
    elif use_demo_data:
        wb_stock_units = max(1, meta["basketsLast7d"] * 2 + _stable_int(article_id, 0, 9))
    else:
        wb_stock_units = None
    has_period_data = bool(period_aggregate)
    has_baskets_cache = baskets_cache_loaded and nm_id is not None
    # ponytail: legacy schedulers require non-null meta; migrate those callers before making it nullable.
    if has_baskets_cache:
        meta["basketsLast7d"] = int((baskets_aggregate or {}).get("cartCount") or 0)
    has_baskets_data = (
        has_baskets_cache
        and (baskets_aggregate or {}).get("cartCount") is not None
    )
    strategy = _frontend_strategy_payload(
        article_id,
        current_status=current_status,
        baskets_last_7d=int(meta["basketsLast7d"]),
        basket_norm=int(meta["basketNorm"]),
    )
    meta["activeStrategyId"] = strategy["id"]
    meta["activeStrategyName"] = strategy["name"]
    meta["activeTypedStrategyId"] = strategy["typedStrategyId"]
    if list_view:
        comments: list[dict[str, Any]] = []
        comment_summary = {"count": 0, "latestText": None, "latestAuthor": None, "latestAt": None}
        audit_events: list[dict[str, Any]] = []
    else:
        comments = _sku_comments_for_article(article_id, use_demo_data)
        comment_summary = _comment_summary(comments)
        audit_events = _sku_audit_events_for_article(article_id, use_demo_data)
    settings["wbCommissionPct"] = float(effective_commission_pct)
    row = {
        "meta": meta,
        "strategy": strategy,
        "settings": settings,
        "analytics": {
            "abcCode": "AA" if use_demo_data and meta["basketsLast7d"] >= meta["basketNorm"] else ("CC" if use_demo_data else None),
            "productStatus": current_status,
            **(promotion_evidence if promotion_evidence is not None else promotion_membership(promotions, now=_utc_now()).analytics(nm_id)),
            "wbStockUnits": wb_stock_units,
            "circulationStockUnits": stock_circulation_units(stock_aggregate),
            "stockInWayToClient": (stock_aggregate or {}).get("inWayToClient"),
            "stockInWayFromClient": (stock_aggregate or {}).get("inWayFromClient"),
            "stockState": "ok" if has_stock_data else ("fallback" if use_demo_data else "no_data"),
            "buyoutPct": buyout_pct_value,
            "buyoutSource": buyout_source if buyout_pct_value is not None else None,
            "buyoutState": "ok" if buyout_pct_value is not None else "no_data",
            "baskets": int((baskets_aggregate or {}).get("cartCount") or 0) if has_baskets_data else (meta["basketsLast7d"] if use_demo_data else None),
            "totalImpressions": (baskets_aggregate or {}).get("impressions") if has_baskets_cache else None,
            "totalClicks": (baskets_aggregate or {}).get("openCount") if has_baskets_cache else None,
            "basketsState": str((baskets_aggregate or {}).get("coverageState") or "ok") if has_baskets_data else ("fallback" if use_demo_data else "no_data"),
            "basketsReason": (
                f"Данные за {(baskets_aggregate or {}).get('coveredDays')} из {(baskets_aggregate or {}).get('requestedDays')} дней. Обновите выбранный период."
                if (baskets_aggregate or {}).get("coverageState") == "partial"
                else None
            ),
            "ordersUnits": orders_units if orders_source is not None or not use_demo_data else max(1, meta["basketsLast7d"] - _stable_int(article_id, 1, 3)),
            "cancelledOrdersUnits": int(period_aggregate.get("cancelledOrdersUnits") or 0),
            "ordersSource": orders_source or ("fallback" if use_demo_data else None),
            "periodDays": period_days,
            "hourlyOrders": deepcopy(period_aggregate.get("hourlyOrders") or []),
            "todayHourlyOrders": deepcopy(period_aggregate.get("todayHourlyOrders") or []),
            "funnelOrderCount": baskets_order_count,
            # Observed reporting metrics, not legacy strategy fallbacks or current-price estimates.
            "reportOrdersUnits": baskets_order_count if baskets_order_count is not None else period_aggregate.get("ordersUnits"),
            "ordersAmountKopecks": (baskets_aggregate or {}).get("orderSumKopecks"),
            "grossSalesUnits": finance_aggregate.get("salesUnits") if finance_aggregate is not None else None,
            "grossSalesAmountKopecks": finance_aggregate.get("realizationGrossSalesKopecks", finance_aggregate.get("grossSalesKopecks")) if finance_aggregate is not None and not finance_aggregate.get("sellerRevenueMissingRows") else None,
            "previousPeriod": {
                "baskets": int(previous_baskets.get("cartCount") or 0) if previous_baskets else None,
                "ordersUnits": previous_orders_units if (previous_period_aggregate or previous_baskets) else None,
                "funnelOrderCount": int(previous_baskets.get("orderCount") or 0) if previous_baskets else None,
            } if previous_period_aggregate or previous_baskets else None,
            "periodStatsState": "ok" if has_period_data else ("fallback" if use_demo_data else "no_data"),
            "buyoutUnits": finance_aggregate.get("netSalesUnits", net_sales_units) if finance_aggregate else None,
            "buyoutAmountKopecks": report_revenue_kopecks(finance_aggregate) if finance_aggregate else None,
            "salesUnits": sales_units,
            "returnsUnits": returns_units,
            "revenueKopecks": revenue_gross_kopecks,
            "sellerRevenueKopecks": finance_seller_revenue_kopecks,
            "realizationRevenueKopecks": (finance_aggregate or {}).get("realizationRevenueKopecks"),
            "realizationVersion": (finance_aggregate or {}).get("realizationVersion"),
            "buyerRevenueKopecks": buyer_revenue_kopecks,
            "platformDiscountKopecks": platform_discount_kopecks,
            "basePriceKopecks": current_price_kopecks or discounted_price_kopecks,
            "sellerDiscountedPriceKopecks": price_kopecks,
            "buyerPriceNoWalletKopecks": buyer_price_kopecks,
            "browserOriginalPriceKopecks": buyer_original_price_kopecks,
            "buyerPriceWithWalletKopecks": buyer_price_with_wallet_kopecks,
            "buyerPriceSource": buyer_price_source if buyer_price_kopecks is not None else None,
            "buyerPriceObservedAt": buyer_price_observed_at if buyer_price_kopecks is not None else None,
            "buyerPriceExpiresAt": buyer_price_expires_at if buyer_price_kopecks is not None else None,
            "accountedBuyerPriceKopecks": accounted_buyer_price_kopecks,
            "marginBaseKopecks": margin_sales_base_kopecks,
            "avgPriceWithSppKopecks": funnel_avg_price_kopecks,
            "sppPct": live_spp_pct,
            "sppSource": spp_source,
            "sppObservedAt": spp_observed_at,
            "periodSppPct": period_spp_pct,
            "periodSppObservedAt": period_aggregate.get("sppObservedAt"),
            "financeSppPct": finance_spp_pct,
            "liveSppPct": live_spp_pct,
            "sppAccountingMode": spp_accounting_mode,
            "accountedWbWalletPct": accounted_wb_wallet_pct,
            "accountedPlatformDiscountPct": accounted_platform_discount_pct,
            "walletPct": wb_wallet_pct,
            "totalWbDiscountPct": total_wb_discount_pct,
            "wbWalletPct": wb_wallet_pct,
            "sppState": "ok" if live_spp_pct is not None else ("fallback" if use_demo_data else "no_buyer_price"),
            "marginPct": margin_pct,
            "marginKopecks": unit_margin_kopecks,
            "marginMode": "planned_indeepa",
            "plannedMarginState": "ok" if planned_inputs_available else "missing_inputs",
            "plannedMarginKopecks": unit_margin_kopecks,
            "plannedPeriodMarginKopecks": planned_period_margin_kopecks,
            "plannedRevenueBaseKopecks": planned_revenue_base_kopecks,
            "plannedCommissionKopecks": planned_commission_kopecks,
            "plannedAcquiringKopecks": planned_acquiring_kopecks,
            "plannedForwardLogisticsKopecks": planned_forward_logistics_kopecks,
            "plannedReturnLogisticsKopecks": planned_return_logistics_kopecks,
            "plannedOtherExpensesKopecks": planned_other_expenses_kopecks,
            "plannedTaxKopecks": planned_tax_kopecks,
            "deliveryToClientKopecks": delivery_to_client_kopecks,
            "deliveryFromClientKopecks": delivery_from_client_kopecks,
            "netProfitKopecks": net_profit_kopecks,
            "factNetProfitKopecks": net_profit_kopecks,
            "cogsTotalKopecks": cogs_total_kopecks if finance_aggregate is not None or use_demo_data else None,
            "wbCommissionPct": effective_commission_pct,
            "baseWbCommissionPct": category_commission_pct,
            "acquiringPct": acquiring_pct,
            "commissionDisplayPct": commission_display_pct,
            "commissionSource": commission_source,
            "commissionTariffFetchedAt": tariff_fetched_at,
            "commissionState": commission_state,
            "commissionReason": commission_reason,
            "reportCommissionPct": report_commission_pct,
            "commissionKopecks": commission_kopecks,
            "financeCommissionKopecks": finance_commission_kopecks,
            "commissionFormulaKopecks": commission_formula_kopecks,
            "commissionCalcMode": commission_calc_mode,
            "logisticsKopecks": logistics_kopecks,
            "storageKopecks": storage_kopecks,
            "acceptanceKopecks": acceptance_kopecks,
            "penaltyKopecks": penalty_kopecks,
            "penaltyChargedKopecks": penalty_charged_kopecks,
            "penaltyReturnedKopecks": penalty_returned_kopecks,
            "deductionKopecks": deduction_kopecks,
            "deductionChargedKopecks": deduction_charged_kopecks,
            "deductionCompensationKopecks": deduction_compensation_kopecks,
            "additionalPaymentKopecks": additional_payment_kopecks,
            "rewardAdjustmentKopecks": reward_adjustment_kopecks,
            "paymentScheduleKopecks": payment_schedule_kopecks,
            "cashbackAmountKopecks": cashback_amount_kopecks,
            "cashbackDiscountKopecks": cashback_discount_kopecks,
            "cashbackCommissionChangeKopecks": cashback_commission_change_kopecks,
            "loyaltyCostKopecks": loyalty_cost_kopecks,
            "acquiringKopecks": acquiring_kopecks,
            "payableKopecks": payable_kopecks,
            "adSpendKopecks": ad_spend_kopecks,
            "adDataAvailable": ad_data_available,
            "adImpressions": ad_impressions,
            "adClicks": ad_clicks,
            "adCartAdds": ad_cart_adds,
            "adOrders": ad_orders,
            "adRevenueKopecks": ad_revenue_kopecks,
            "otherExpensesKopecks": other_expenses_kopecks if finance_aggregate is not None or use_demo_data else None,
            "taxKopecks": tax_kopecks if finance_aggregate is not None or use_demo_data else None,
            "factTaxState": fact_tax_state,
            "factTaxReason": fact_tax_reason,
            "workReturnKopecks": work_return_kopecks if finance_aggregate is not None or use_demo_data else None,
            "taxPct": settings.get("taxPct"),
            "expensesKopecks": expenses_kopecks if finance_aggregate is not None or use_demo_data else None,
            "discountPct": (finance_aggregate or {}).get("discountPct"),
            "financeState": "ok" if finance_aggregate is not None else ("fallback" if use_demo_data else "no_data"),
        },
        "commentSummary": comment_summary,
        "comments": comments,
        "auditEvents": audit_events,
        "cachedAt": None,
    }
    settings["effectivePMinKopecks"], settings["effectivePMinSource"] = _effective_pmin(row)
    return row


SETTLEMENT_PROFIT_VERSION = "wb-final-payout-cogs-tax-v1"
SETTLEMENT_ABC_VERSION = "wb-units-profit-cumulative-80-95-v1"


def settlement_payable_confirmed(finance: dict[str, Any]) -> bool:
    if type(finance.get("payableKopecks")) is not int:
        return False
    if "settlementPayableConfirmed" in finance:
        return finance["settlementPayableConfirmed"] is True
    if finance.get("commissionSource") == "buyerRevenueKopecks-payableKopecks-acquiringKopecks":
        return True
    # Observed expense-only rows have no sale payout to confirm. Do not infer
    # this for missing rows, netted sale/return pairs, or incomplete fields.
    return (type(finance.get("rowsCount")) is int and finance["rowsCount"] > 0
            and all(type(finance.get(key)) is int and finance[key] == 0 for key in
                    ("salesUnits", "returnsUnits", "buyerRevenueKopecks", "payableKopecks", "commissionKopecks", "acquiringKopecks")))


def settlement_profit_metrics(finance: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """WB settlement only. Commission/acquiring already in payable; no Ads double debit.

    Finance promotion is moved out of deduction by _finance_row_costs, so it is
    subtracted exactly once here. Existing signed corrections remain signed.
    No current-price COGS, workReturn, internal costs or planned tax fallback.
    """
    costs = ("logisticsKopecks", "storageKopecks", "acceptanceKopecks", "penaltyKopecks",
             "deductionKopecks", "loyaltyCostKopecks", "adSpendKopecks")
    fields = (*costs, "payableKopecks", "additionalPaymentKopecks")
    blockers = []
    if not settlement_payable_confirmed(finance):
        blockers.append("settlement_payable_unconfirmed")
    if finance.get("settlementComponentsConfirmed") is False or any(type(finance.get(field)) is not int for field in fields):
        blockers.append("settlement_components_missing")
    payout = None if blockers else (finance["payableKopecks"]
        - sum(finance[field] for field in costs) + finance["additionalPaymentKopecks"])
    cogs = context.get("settlementCogsKopecks")
    tax = context.get("taxKopecks")
    if context.get("settlementAllocationConfirmed") is False:
        blockers.append("settlement_account_expenses_unreconciled")
    if type(cogs) is not int:
        blockers.append("settlement_dated_cogs_missing")
    if context.get("factTaxState") != "configured" or type(tax) is not int:
        blockers.append("settlement_tax_missing")
    # An explicitly labelled product subtotal can be shown even while cabinet
    # fees are unallocated. It must never certify final profit or ABC ranking.
    attributed_profit = (payout - cogs - tax if payout is not None and type(cogs) is int
                         and context.get("factTaxState") == "configured" and type(tax) is int else None)
    return {"settlementFormulaVersion": SETTLEMENT_PROFIT_VERSION,
            "settlementAdvertisingKopecks": None if finance.get("financeAdSpendAuthoritative") is False else finance.get("adSpendKopecks"),
            "finalPayoutKopecks": payout,
            "settlementCogsKopecks": cogs if type(cogs) is int else None,
            "settlementCogsState": context.get("settlementCogsState", "missing"),
            "settlementAttributedProfitKopecks": attributed_profit,
            "settlementProfitKopecks": None if blockers else payout - cogs - tax,
            "settlementBlockers": blockers}


def cumulative_abc(values: dict[str, int | None]) -> dict[str, str | None]:
    """Stable IDs break ties; threshold-crossing item stays in the prior group.

    Missing member blocks the axis (denominator unknown). Nonpositive entries
    are C when there is positive contribution, otherwise the axis is unknown.
    Integer comparisons avoid percent rounding at 80/95 boundaries.
    """
    total = sum(max(value, 0) for value in values.values() if type(value) is int)
    if not total or any(type(value) is not int for value in values.values()):
        return dict.fromkeys(values)
    result = {}
    cumulative = 0
    for key, value in sorted(values.items(), key=lambda item: (-item[1], item[0])):
        result[key] = "C" if value <= 0 else "A" if cumulative * 100 < total * 80 else "B" if cumulative * 100 < total * 95 else "C"
        cumulative += max(value, 0)
    return result


def apply_settlement_abc(rows: list[dict[str, Any]], finance: dict[str, dict[str, Any]],
                         contexts: dict[str, dict[str, Any]]) -> None:
    # The finance universe includes historical SKUs absent from current catalog.
    metrics = {str(key): settlement_profit_metrics(fact, contexts.get(str(key), {})) for key, fact in finance.items()}
    sales = cumulative_abc({str(key): fact.get("salesUnits") for key, fact in finance.items()})
    profits = cumulative_abc({key: value["settlementProfitKopecks"] for key, value in metrics.items()})
    for row in rows:
        key = str((row.get("meta") or {}).get("nmId"))
        analytics = row.setdefault("analytics", {})
        analytics.update(metrics.get(key) or settlement_profit_metrics({}, {}))
        if finance.get(key, {}).get("financeAdSpendAuthoritative") is False:
            analytics["campaignAdSpendKopecks"] = analytics.get("adSpendKopecks")
            analytics.update(adSpendKopecks=None, adDataAvailable=False)
        sales_class, profit_class = sales.get(key), profits.get(key)
        analytics.update(abcPolicyVersion=SETTLEMENT_ABC_VERSION,
                         salesClass=sales_class, profitClass=profit_class,
                         abcCode=f"{sales_class or '—'}{profit_class or '—'}",
                         abcReason=None if sales_class and profit_class else "Недостаточно подтверждённых данных для классификации всего аккаунта")


def _abc_letter(rank_index: int, total: int) -> str:
    if total <= 0:
        return "C"
    ratio = (rank_index + 1) / total
    if ratio <= 0.2:
        return "A"
    if ratio <= 0.5:
        return "B"
    return "C"


def _sku_demand_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    analytics = row.get("analytics") or {}
    meta = row.get("meta") or {}
    orders = int(analytics.get("ordersUnits") or 0)
    baskets_raw = analytics.get("baskets")
    baskets = int(baskets_raw) if baskets_raw is not None else int(meta.get("basketsLast7d") or 0)
    revenue = int(analytics.get("revenueKopecks") or 0)
    article_id = str(meta.get("articleId") or "")
    return (-orders, -baskets, -revenue, article_id)


def _apply_abc_codes(rows: list[dict[str, Any]], *, use_demo_data: bool) -> None:
    if not rows:
        return
    finance_ready = [row for row in rows if row.get("analytics", {}).get("financeState") == "ok"]
    if not finance_ready:
        return

    sales_rank = sorted(
        rows,
        key=lambda row: (
            int(row.get("analytics", {}).get("salesUnits") or 0),
            int(row.get("analytics", {}).get("ordersUnits") or 0),
            int(row.get("analytics", {}).get("revenueKopecks") or 0),
        ),
        reverse=True,
    )
    profit_rank = sorted(
        rows,
        key=lambda row: int(row.get("analytics", {}).get("netProfitKopecks") or 0),
        reverse=True,
    )
    sales_letters = {id(row): _abc_letter(index, len(sales_rank)) for index, row in enumerate(sales_rank)}
    profit_letters = {id(row): _abc_letter(index, len(profit_rank)) for index, row in enumerate(profit_rank)}
    for row in rows:
        analytics = row.get("analytics", {})
        if analytics.get("financeState") == "ok" or use_demo_data:
            analytics["abcCode"] = f"{sales_letters.get(id(row), 'C')}{profit_letters.get(id(row), 'C')}"


def list_repricer_skus(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    include_promotions: bool = True,
    include_content: bool = True,
    tolerate_content_errors: bool = False,
    cached_goods: list[dict[str, Any]] | None = None,
    cached_content_cards: list[dict[str, Any]] | None = None,
    cached_promotions: list[dict[str, Any]] | None = None,
    cached_stock_aggregates: dict[str, dict[str, Any]] | None = None,
    stocks_cache_loaded: bool = False,
    cached_period_stats: dict[str, dict[str, Any]] | None = None,
    cached_finance_aggregates: dict[str, dict[str, Any]] | None = None,
    cached_finance_taxes: dict[str, dict[str, Any]] | None = None,
    cached_ads_aggregates: dict[str, dict[str, Any]] | None = None,
    cached_baskets_aggregates: dict[str, dict[str, Any]] | None = None,
    baskets_cache_loaded: bool = False,
    cached_baskets_observed_at: str | None = None,
    list_view: bool = False,
    period_days: int | None = None,
    max_items: int | None = None,
    sort_by_demand: bool = True,
    allow_commission_tariff_fetch: bool = True,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    use_demo_data = _demo_repricer_data_enabled(wb_token)
    if cached_promotions is not None:
        promotions = cached_promotions
    else:
        promotions = list_promotions(scenario, wb_token=wb_token, fail_open=True) if include_promotions else []
    active_promotion_articles = _active_promotion_articles(promotions) if promotions else set()
    active_promotion_nm_ids = _active_promotion_nm_ids(promotions) if promotions else set()
    active_promotion_labels_by_article = _active_promotion_labels_by_article(promotions) if promotions else {}
    active_promotion_labels_by_nm_id = _active_promotion_labels_by_nm_id(promotions) if promotions else {}
    membership = promotion_membership(promotions, now=_utc_now())
    goods = cached_goods if cached_goods is not None else _fetch_catalog_goods(scenario, wb_token=wb_token)
    if max_items is not None and len(goods) > max_items:
        goods = sorted(
            goods,
            key=lambda good: _good_demand_sort_key(
                good,
                cached_period_stats=cached_period_stats,
                cached_baskets_aggregates=cached_baskets_aggregates,
            ),
        )[:max_items]
    if cached_content_cards is not None:
        cards = cached_content_cards
    elif not include_content:
        cards = []
    elif tolerate_content_errors:
        cards = _safe_fetch_content_cards(scenario, wb_token=wb_token)
    else:
        cards = _fetch_content_cards(scenario, wb_token=wb_token)

    cards_by_vendor: dict[str, dict[str, Any]] = {}
    cards_by_nm: dict[int, dict[str, Any]] = {}
    for card in cards:
        vendor_code = str(card.get("vendorCode") or "")
        nm_id = int(card.get("nmID") or 0)
        if vendor_code:
            cards_by_vendor[vendor_code] = card
        if nm_id > 0:
            cards_by_nm[nm_id] = card

    commission_tariffs_index = (
        fetch_commission_tariffs(scenario, wb_token=wb_token, organization_id=organization_id)
        if allow_commission_tariff_fetch
        else cached_commission_tariffs(scenario, wb_token=wb_token, organization_id=organization_id)
    )
    rows: list[dict[str, Any]] = []
    from app.platform.economics.legacy_catalog import catalog_facts
    facts = catalog_facts(organization_id) if organization_id is not None and not use_demo_data else {}
    # The editable value must be persisted evidence, never TYPE_DEFAULTS used
    # by price-planning fallbacks. Resolve in bulk using the finance cost source.
    saved_costs = {}
    if organization_id is not None and not use_demo_data:
        from app.wb_pnl_costs import saved_current_costs
        saved_costs = saved_current_costs(organization_id, {"aggregates": {
            str(good.get("nmID")): {"vendorCode": good.get("vendorCode")}
            for good in goods if good.get("nmID")
        }})
    for good in goods:
        vendor_code = str(good.get("vendorCode") or "")
        if not vendor_code:
            continue
        nm_id = int(good.get("nmID") or 0) or None
        card = (cards_by_nm.get(nm_id) if nm_id is not None else None) or cards_by_vendor.get(vendor_code) or {}
        sizes = good.get("sizes") or []
        price_size = sizes[0] if sizes else {}
        discounted_price_kopecks = wb_goods_price_to_kopecks(price_size.get("discountedPrice")) or wb_goods_price_to_kopecks(
            price_size.get("price")
        )
        buyer_price_kopecks, buyer_price_with_wallet_kopecks, spp_pct, wb_wallet_pct = _resolve_spp_analytics(
            good,
            price_size,
            discounted_price_kopecks,
        )
        subject_name = str(
            card.get("object")
            or card.get("subjectName")
            or good.get("subjectName")
            or (_fallback_subject_for_article(vendor_code) if use_demo_data else None)
            or "Товары"
        )
        subject_id = _int_or_none(
            card.get("objectID")
            or card.get("objectId")
            or card.get("subjectID")
            or card.get("subjectId")
            or good.get("objectID")
            or good.get("objectId")
            or good.get("subjectID")
            or good.get("subjectId")
        )
        content_sizes = card.get("sizes") or []
        chrt_ids = [int(chrt_id) for size in content_sizes for chrt_id in size.get("skus", []) if int(chrt_id) > 0]
        fact = facts.get(nm_id, {})
        image_url = fact.get("photoUrl") or _extract_wb_media_url(card, nm_id) or _extract_wb_media_url(good, nm_id)
        rows.append(
            _build_sku_row(
                vendor_code,
                nm_id=nm_id,
                name=str(card.get("title") or vendor_code),
                subject=subject_name,
                brand=str(card.get("brand") or good.get("brand") or "") or None,
                chrt_ids=chrt_ids,
                current_price_kopecks=wb_goods_price_to_kopecks(price_size.get("price")),
                discounted_price_kopecks=discounted_price_kopecks,
                buyer_price_kopecks=buyer_price_kopecks,
                buyer_price_with_wallet_kopecks=buyer_price_with_wallet_kopecks,
                spp_pct=spp_pct,
                wb_wallet_pct=wb_wallet_pct,
                promotions=promotions,
                promotion_evidence=membership.analytics(nm_id),
                active_promotion_articles=active_promotion_articles,
                active_promotion_nm_ids=active_promotion_nm_ids,
                active_promotion_labels_by_article=active_promotion_labels_by_article,
                active_promotion_labels_by_nm_id=active_promotion_labels_by_nm_id,
                use_demo_data=use_demo_data,
                stock_aggregate=(cached_stock_aggregates or {}).get(str(nm_id)) if nm_id is not None else None,
                stocks_cache_loaded=stocks_cache_loaded,
                period_aggregate=(cached_period_stats or {}).get(str(nm_id)) if nm_id is not None else None,
                previous_period_aggregate=((cached_baskets_aggregates or {}).get(str(nm_id)) or {}).get("previous") if nm_id is not None else None,
                period_days=period_days,
                finance_aggregate=(cached_finance_aggregates or {}).get(str(nm_id)) if nm_id is not None else None,
                finance_tax=(cached_finance_taxes or {}).get(str(nm_id)) if nm_id is not None else None,
                ads_aggregate=(cached_ads_aggregates or {}).get(str(nm_id)) if nm_id is not None else None,
                commission_tariffs_index=commission_tariffs_index,
                baskets_aggregate=(cached_baskets_aggregates or {}).get(str(nm_id)) if nm_id is not None else None,
                baskets_cache_loaded=baskets_cache_loaded,
                list_view=list_view,
                subject_id=subject_id,
                image_url=image_url,
                canonical_cost_kopecks=fact.get("currentCostKopecks"),
                buyer_price_source=price_size.get("buyerPriceSource") or good.get("buyerPriceSource"),
                buyer_original_price_kopecks=price_size.get("buyerPriceOriginalKopecks"),
                buyer_price_observed_at=price_size.get("buyerPriceObservedAt") or good.get("buyerPriceObservedAt"),
                buyer_price_expires_at=price_size.get("buyerPriceExpiresAt"),
                price_baseline=good.get("priceBaseline"),
                price_observed_at=good.get("priceObservedAt"),
            )
        )
    for row in rows:
        row["analytics"]["basketsObservedAt"] = cached_baskets_observed_at
        row["settings"]["savedCogsKopecks"] = (
            row["settings"]["cogsKopecks"] if use_demo_data
            else saved_costs.get(row["meta"].get("nmId"))
        )
    _apply_abc_codes(rows, use_demo_data=use_demo_data)
    if not use_demo_data:
        apply_settlement_abc(rows, cached_finance_aggregates or {}, cached_finance_taxes or {})
    if sort_by_demand:
        rows.sort(key=_sku_demand_sort_key)
    return rows


def get_repricer_sku_settings(article_id: str, scenario: str = "complete", wb_token: str | None = None) -> dict[str, Any]:
    article_id = article_id.strip()
    for row in list_repricer_skus(scenario, wb_token=wb_token):
        if str(row["meta"]["articleId"]).strip() == article_id:
            return row
    raise HTTPException(status_code=404, detail="SKU_NOT_FOUND")


def put_repricer_sku_settings(
    article_id: str,
    payload: dict[str, Any],
    scenario: str = "complete",
    wb_token: str | None = None,
) -> dict[str, Any]:
    article_id = article_id.strip()
    current = get_repricer_sku_settings(article_id, scenario, wb_token=wb_token)
    next_settings = deepcopy(current["settings"])
    next_settings.update(deepcopy(payload))
    SKU_SETTINGS_OVERRIDES[article_id] = next_settings
    status = _status_after_settings(article_id, current["meta"]["status"], bool(next_settings.get("automationEnabled", True)))
    SKU_META_OVERRIDES.setdefault(article_id, {})
    SKU_META_OVERRIDES[article_id]["status"] = status
    event = {
        "id": f"audit-{article_id}-{len(SKU_AUDIT_EVENTS.get(article_id, [])) + 1}",
        "sku": article_id,
        "createdAt": _iso_now(),
        "actor": {"id": "manager-api", "name": "Backend API", "role": "manager"},
        "source": "manager",
        "scope": "sku",
        "action": "Сохранение настроек SKU",
        "reason": "Изменение параметров repricer UI",
        "oldValue": "предыдущие настройки",
        "newValue": "обновлённые настройки",
    }
    SKU_AUDIT_EVENTS.setdefault(article_id, []).append(event)
    updated = get_repricer_sku_settings(article_id, scenario, wb_token=wb_token)
    updated["meta"]["status"] = status
    updated["meta"]["lastSavedAt"] = event["createdAt"]
    updated["settings"] = deepcopy(next_settings)
    updated["auditEvents"] = deepcopy(SKU_AUDIT_EVENTS.get(article_id, []))
    return updated


def patch_repricer_automation(
    article_id: str,
    automation_enabled: bool,
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    state: dict[str, Any] | None = None,
    current_row: dict[str, Any] | None = None,
    actor_id: str = "manager-api",
    actor_role: str = "manager",
) -> dict[str, Any]:
    if state is not None:
        # Called under the durable product mutation lock. Do not read providers,
        # authenticate again, or materialize inherited settings in this branch.
        article_id = article_id.strip()
        settings = state.setdefault("skuSettingsOverrides", {}).setdefault(article_id, {})
        meta = state.setdefault("skuMetaOverrides", {}).setdefault(article_id, {})
        current_status = meta.get("status") or ((current_row or {}).get("meta") or {}).get("status") or "auto"
        previous_automation = settings.get("automationEnabled", ((current_row or {}).get("settings") or {}).get("automationEnabled", True))
        if current_status == "warmup":
            status = "warmup"
        elif article_id in (state.get("liquidationActive") or {}):
            status = "liquidation"
        else:
            status = "auto" if automation_enabled else "manual"
        changed = settings.get("automationEnabled") is not automation_enabled or meta.get("status") != status
        settings["automationEnabled"] = automation_enabled
        meta["status"] = status
        if changed:
            events = state.setdefault("skuAuditEvents", {}).setdefault(article_id, [])
            events.append({
                "id": f"audit-{article_id}-{len(events) + 1}", "sku": article_id, "createdAt": _iso_now(),
                "actor": {"id": actor_id, "name": "Backend API", "role": actor_role},
                "source": "manager", "scope": "sku", "action": "Изменение режима автоматизации",
                "reason": "Изменение режима из таблицы репрайсера",
                "oldValue": "включена" if previous_automation else "выключена",
                "newValue": "включена" if automation_enabled else "выключена",
            })
        return {"automationEnabled": automation_enabled, "status": status}
    current = get_repricer_sku_settings(article_id, scenario, wb_token=wb_token)
    settings = deepcopy(current["settings"])
    settings["automationEnabled"] = automation_enabled
    put_repricer_sku_settings(article_id, settings, scenario, wb_token=wb_token)
    return {"automationEnabled": automation_enabled}


def get_repricer_dashboard(scenario: str = "complete", wb_token: str | None = None) -> dict[str, Any]:
    rows = list_repricer_skus(scenario, wb_token=wb_token)
    counts = {"auto": 0, "manual": 0, "warmup": 0, "liquidation": 0}
    type_rollups: dict[str, dict[str, float]] = {
        "F": {"total": 0, "auto": 0, "marginSum": 0.0},
        "H": {"total": 0, "auto": 0, "marginSum": 0.0},
        "L": {"total": 0, "auto": 0, "marginSum": 0.0},
    }
    attention: list[dict[str, str]] = []

    for row in rows:
        status = row["meta"]["status"]
        counts[status] = counts.get(status, 0) + 1
        type_key = _article_type(row["meta"]["articleId"])
        type_rollups[type_key]["total"] += 1
        if status == "auto":
            type_rollups[type_key]["auto"] += 1
        type_rollups[type_key]["marginSum"] += float(row["analytics"]["marginPct"])

        if float(row["analytics"]["marginPct"]) < 0:
            attention.append(
                {
                    "articleId": row["meta"]["articleId"],
                    "name": row["meta"]["name"],
                    "reason": "negative_margin",
                    "detail": f"{row['analytics']['marginPct']}%",
                }
            )
        elif row["meta"]["basketsLast7d"] < row["meta"]["basketNorm"] * 0.25:
            attention.append(
                {
                    "articleId": row["meta"]["articleId"],
                    "name": row["meta"]["name"],
                    "reason": "low_baskets",
                    "detail": f"{row['meta']['basketsLast7d']}/{row['meta']['basketNorm']}",
                }
            )

    type_stats = []
    for type_key, data in type_rollups.items():
        total = int(data["total"])
        type_stats.append(
            {
                "type": type_key,
                "total": total,
                "autoPct": round((data["auto"] / total) * 100) if total else 0,
                "avgMarginPct": round(data["marginSum"] / total, 1) if total else 0.0,
            }
        )

    return {
        "counts": counts,
        "total": len(rows),
        "attention": attention[:8],
        "typeStats": type_stats,
        "lastSyncAt": (_utc_now() - timedelta(hours=2)).isoformat(),
    }


def get_repricer_templates(scenario: str = "complete", wb_token: str | None = None) -> dict[str, Any]:
    rows = list_repricer_skus(scenario, wb_token=wb_token)
    sku_count_by_type = {"F": 0, "H": 0, "L": 0}
    for row in rows:
        sku_count_by_type[_article_type(row["meta"]["articleId"])] += 1
    return {
        "globalCommissionPct": TEMPLATES_STATE["globalCommissionPct"],
        "types": deepcopy(TEMPLATES_STATE["types"]),
        "skuCountByType": sku_count_by_type,
    }


def put_repricer_templates(payload: dict[str, Any]) -> dict[str, Any]:
    if "globalCommissionPct" in payload:
        TEMPLATES_STATE["globalCommissionPct"] = payload["globalCommissionPct"]
    if isinstance(payload.get("types"), dict):
        for type_key, template in payload["types"].items():
            if type_key in TEMPLATES_STATE["types"] and isinstance(template, dict):
                TEMPLATES_STATE["types"][type_key].update(template)
    response = deepcopy(get_repricer_templates())
    response["savedAt"] = _iso_now()
    return response


def apply_repricer_template(type_key: str, scenario: str = "complete", wb_token: str | None = None) -> dict[str, Any]:
    template = deepcopy(TEMPLATES_STATE["types"].get(type_key))
    if template is None:
        raise HTTPException(status_code=404, detail="TEMPLATE_TYPE_NOT_FOUND")
    updated = 0
    for row in list_repricer_skus(scenario, wb_token=wb_token):
        article_id = row["meta"]["articleId"]
        if _article_type(article_id) != type_key:
            continue
        next_settings = deepcopy(row["settings"])
        next_settings.update(template)
        SKU_SETTINGS_OVERRIDES[article_id] = next_settings
        updated += 1
    return {"updated": updated}


def get_repricer_algorithm() -> dict[str, Any]:
    return deepcopy(ALGORITHM_SETTINGS_STATE)


def put_repricer_algorithm(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload or {})
    if "syncIntervalMinutes" in payload:
        try:
            sync_interval_minutes = max(5, int(payload.get("syncIntervalMinutes") or 60))
        except (TypeError, ValueError):
            sync_interval_minutes = 60
        payload["syncIntervalMinutes"] = sync_interval_minutes
        payload["syncIntervalHours"] = max(1, round(sync_interval_minutes / 60))
    elif "syncIntervalHours" in payload:
        try:
            sync_interval_hours = max(1, int(payload.get("syncIntervalHours") or 1))
        except (TypeError, ValueError):
            sync_interval_hours = 1
        payload["syncIntervalHours"] = sync_interval_hours
        payload["syncIntervalMinutes"] = max(5, sync_interval_hours * 60)
    if "fullSyncIntervalMinutes" in payload:
        try:
            payload["fullSyncIntervalMinutes"] = max(5, int(payload.get("fullSyncIntervalMinutes") or 60))
        except (TypeError, ValueError):
            payload["fullSyncIntervalMinutes"] = 60
    if "fullSyncPromotionsEnabled" in payload:
        payload["fullSyncPromotionsEnabled"] = bool(payload.get("fullSyncPromotionsEnabled"))
    if "nightMedianAutoEnableAllSkus" in payload and "nightMedianGlobal" not in payload:
        payload = {**payload, "nightMedianGlobal": bool(payload.get("nightMedianAutoEnableAllSkus"))}
    if "nightMedianGlobal" in payload and "nightMedianAutoEnableAllSkus" not in payload:
        payload = {**payload, "nightMedianAutoEnableAllSkus": bool(payload.get("nightMedianGlobal"))}
    if "nightMedianEnabled" in payload:
        payload = {**payload, "nightMedianDefaultAllSkusVersion": 1}
    ALGORITHM_SETTINGS_STATE.update(payload)
    response = deepcopy(ALGORITHM_SETTINGS_STATE)
    response["savedAt"] = _iso_now()
    return response


def _promotion_type(value: str | None) -> str:
    normalized = (value or "").lower()
    if normalized == "auto":
        return "auto"
    if normalized in {"flash", "sale"}:
        return "flash"
    return "special"


def _promotions_for_cache(promotions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for promotion in promotions:
        row = dict(promotion)
        article_ids = row.get("articleIds")
        if isinstance(article_ids, set):
            row["articleIds"] = sorted(str(item) for item in article_ids if item)
        elif isinstance(article_ids, list):
            row["articleIds"] = [str(item) for item in article_ids if item]
        threshold_nm_ids = row.get("thresholdNmIds")
        if isinstance(threshold_nm_ids, set):
            row["thresholdNmIds"] = sorted(int(item) for item in threshold_nm_ids if item)
        elif isinstance(threshold_nm_ids, list):
            normalized_nm_ids: list[int] = []
            for item in threshold_nm_ids:
                try:
                    normalized_nm_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            row["thresholdNmIds"] = normalized_nm_ids
        normalized.append(row)
    return normalized


def _http_exception_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or exc.status_code)
    if isinstance(detail, str):
        return detail
    return str(exc.status_code)


def list_promotions(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    fail_open: bool = False,
    summary_only: bool = False,
) -> list[dict[str, Any]]:
    promotions, details_index, nomenclatures = (
        _safe_fetch_promotions_bundle(scenario, wb_token=wb_token)
        if fail_open
        else _fetch_promotions_bundle(scenario, wb_token=wb_token, summary_only=summary_only)
    )
    now = _utc_now()
    result: list[dict[str, Any]] = []

    by_promotion_id: dict[int, list[dict[str, Any]]] = {}
    for item in nomenclatures:
        by_promotion_id.setdefault(int(item.get("promotionID") or 0), []).append(item)

    for item in promotions:
        promotion_id = int(item.get("id") or 0)
        start_at = _parse_iso(str(item.get("startDateTime") or "")) or now
        end_at = _parse_iso(str(item.get("endDateTime") or "")) or now
        detail = details_index.get(promotion_id, {})
        sku_rows = by_promotion_id.get(promotion_id, [])
        if end_at < now:
            status = "ended"
        elif start_at > now:
            status = "upcoming"
        else:
            status = "active"

        participating = int(detail.get("inPromoActionTotal") or sum(1 for row in sku_rows if row.get("inAction")))
        eligible = int(detail.get("inPromoActionTotal") or 0) + int(detail.get("notInPromoActionTotal") or 0)
        if eligible == 0:
            eligible = len(sku_rows)
        override = PROMOTION_UPLOAD_OVERRIDES.get(str(promotion_id), {})
        excel_loaded = any(row.get("planPrice") is not None for row in sku_rows)
        base = {
            "id": str(promotion_id),
            "name": str(item.get("name") or f"Promotion {promotion_id}"),
            "type": _promotion_type(str(item.get("type") or "")),
            "status": status,
            "startDate": start_at.date().isoformat(),
            "endDate": end_at.date().isoformat(),
            "startDateTime": item.get("startDateTime"),
            "endDateTime": item.get("endDateTime"),
            "wbType": item.get("type") or detail.get("type"),
            "membershipVersion": MEMBERSHIP_VERSION,
            "membershipState": detail.get("_membershipState", "unavailable"),
            "membershipFetchedAt": detail.get("_membershipFetchedAt"),
            "participantNmIds": sorted({int(row["id"]) for row in sku_rows if row.get("inAction") is True}),
            "daysUntilEnd": _days_between(end_at, now),
            "daysUntilStart": max(0, _days_between(start_at, now)),
            "eligibleSkuCount": eligible,
            "participatingSkuCount": participating,
            "participationPct": int(detail.get("participationPercentage") or (round((participating / eligible) * 100) if eligible else 0)),
            "excelLoaded": excel_loaded,
            "excelStatus": "loaded" if excel_loaded else "missing",
            "excelFileName": None,
            "excelLoadedAt": None,
            "thresholdRowsParsed": sum(1 for row in sku_rows if row.get("planPrice") is not None),
            "thresholdNmIds": [
                int(row.get("id"))
                for row in sku_rows
                if row.get("id") is not None and row.get("planPrice") is not None
            ],
            "articleIds": sorted(
                {
                    str(row.get("vendorCode") or "")
                    for row in sku_rows
                    if row.get("vendorCode") and row.get("inAction")
                }
            ),
        }
        base.update(override)
        result.append(base)
    result.sort(key=lambda row: (row["status"], row["startDate"], row["id"]))
    return result


def get_promotion_skus(promo_id: str, scenario: str = "complete", wb_token: str | None = None) -> list[dict[str, Any]]:
    sku_index = {row["meta"]["articleId"]: row for row in list_repricer_skus(scenario, wb_token=wb_token)}
    _promotions, _details, nomenclatures = _fetch_promotions_bundle(scenario, wb_token=wb_token)
    rows: list[dict[str, Any]] = []
    for item in nomenclatures:
        if str(item.get("promotionID") or "") != str(promo_id):
            continue
        article_id = str(item.get("vendorCode") or "")
        sku = sku_index.get(article_id)
        if sku is None:
            continue
        threshold = item.get("planPrice")
        p_min = _settings_pmin_override_kopecks(sku["settings"]) or _p_min_kopecks(
            sku["settings"]["cogsKopecks"],
            sku["settings"]["wbCommissionPct"],
            sku["settings"]["logisticsKopecks"],
            ALGORITHM_SETTINGS_STATE["promoMarginThresholdPct"],
        )
        rows.append(
            {
                "articleId": article_id,
                "name": sku["meta"]["name"],
                "currentPriceKopecks": wb_goods_price_to_kopecks(item.get("price"))
                or int(sku["meta"]["currentPriceKopecks"]),
                "promoThresholdKopecks": wb_goods_price_to_kopecks(threshold) if threshold is not None else None,
                "isProtected": bool(threshold is not None and int(threshold) >= p_min),
                "cogsKopecks": sku["settings"]["cogsKopecks"],
                "wbCommissionPct": sku["settings"]["wbCommissionPct"],
                "logisticsKopecks": sku["settings"]["logisticsKopecks"],
            }
        )
    return rows


def list_frontend_strategy_catalog(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    sku_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    applied_by_strategy = {item["id"]: 0 for item in FRONTEND_STRATEGIES}
    if sku_rows is None:
        # Assignments are already stored per article, so tally them straight
        # from that map.  Hydrating SKU rows here meant a live WB round-trip
        # per page load, and the row cap silently dropped every assignment
        # beyond the first page.
        for assignment in FRONTEND_STRATEGY_ASSIGNMENTS.values():
            if str((assignment or {}).get("source") or "none") not in {"manual", "xlsx"}:
                continue
            strategy_id = str((assignment or {}).get("strategyId") or "")
            if strategy_id in applied_by_strategy:
                applied_by_strategy[strategy_id] += 1
    else:
        for row in sku_rows:
            strategy = row.get("strategy", {})
            assignment_source = str(strategy.get("assignmentSource") or row.get("meta", {}).get("assignmentSource") or "none")
            if assignment_source not in {"manual", "xlsx"}:
                continue
            strategy_id = str(strategy.get("id") or "")
            if strategy_id in applied_by_strategy:
                applied_by_strategy[strategy_id] += 1
    badge_by_status = {
        "active": ("active", "активный"),
        "special": ("special", "особый"),
        "blocked": ("special", "нужны данные"),
        "disabled": ("special", "выключено"),
    }
    return [
        {
            "id": item["id"],
            "type": item["type"],
            "name": item["name"],
            "color": item["color"],
            "description": item["description"],
            "applied": applied_by_strategy.get(item["id"], 0),
            "status": item["status"],
            "badgeClass": badge_by_status.get(item["status"], ("special", "настройка"))[0],
            "badgeText": badge_by_status.get(item["status"], ("special", "настройка"))[1],
            "typedStrategyId": item["typedStrategyId"],
            "rules": deepcopy(item["rules"]),
        }
        for item in FRONTEND_STRATEGIES
    ]


def list_frontend_strategy_assignments(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    sku_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    items: list[dict[str, Any]] = []
    for row in rows:
        strategy = row.get("strategy", {})
        items.append(
            {
                "articleId": row["meta"]["articleId"],
                "strategyId": strategy.get("id"),
                "strategyName": strategy.get("name"),
                "typedStrategyId": strategy.get("typedStrategyId"),
                "assignedAt": strategy.get("assignedAt"),
                "assignmentSource": strategy.get("assignmentSource"),
                "config": deepcopy(strategy.get("config") or {}),
                "status": row["meta"]["status"],
            }
        )
    return items


def list_repricer_sku_groups() -> list[dict[str, Any]]:
    groups = [deepcopy(item) for item in REPRICER_SKU_GROUPS.values()]
    groups.sort(key=lambda item: (str(item.get("groupName") or "").lower(), str(item.get("groupId") or "")))
    return groups


def upsert_repricer_sku_group(payload: dict[str, Any]) -> dict[str, Any]:
    raw_article_ids = payload.get("articleIds") or payload.get("groupArticleIds") or []
    if not isinstance(raw_article_ids, list):
        raw_article_ids = []
    incoming_article_ids = [str(item).strip() for item in raw_article_ids if str(item).strip()]
    if not incoming_article_ids:
        raise HTTPException(status_code=422, detail={"code": "GROUP_SKUS_REQUIRED", "message": "Выберите хотя бы один SKU"})

    group_id = str(payload.get("groupId") or "").strip() or f"group-{uuid4().hex[:10]}"
    group_name = str(payload.get("groupName") or payload.get("name") or "").strip() or f"Группа {len(incoming_article_ids)} SKU"
    mode = str(payload.get("mode") or "add").strip().lower()
    existing = deepcopy(REPRICER_SKU_GROUPS.get(group_id) or {})
    if mode == "replace":
        article_ids = list(dict.fromkeys(incoming_article_ids))
    else:
        article_ids = list(dict.fromkeys(list(existing.get("articleIds") or []) + incoming_article_ids))

    try:
        plan_orders = int(payload.get("planOrders") or existing.get("planOrders") or 0)
    except (TypeError, ValueError):
        plan_orders = 0
    if plan_orders <= 0:
        plan_orders = max(1, len(article_ids) * 5)

    now = _iso_now()
    group = {
        "groupId": group_id,
        "groupName": group_name,
        "articleIds": article_ids,
        "planOrders": plan_orders,
        "createdAt": existing.get("createdAt") or now,
        "updatedAt": now,
    }
    REPRICER_SKU_GROUPS[group_id] = group
    return deepcopy(group)


def _set_frontend_assignment_meta(article_id: str, *, status: str, assigned_at: str, source: str, state=None) -> None:
    overrides = SKU_META_OVERRIDES if state is None else state.setdefault("skuMetaOverrides", {})
    overrides.setdefault(article_id, {}).update(status=status, assignmentSource=source, assignedAt=assigned_at)


def _clear_frontend_assignment_state(article_id: str, *, state=None) -> None:
    liquidation = LIQUIDATION_ACTIVE if state is None else state.setdefault("liquidationActive", {})
    overrides = SKU_META_OVERRIDES if state is None else state.setdefault("skuMetaOverrides", {})
    liquidation.pop(article_id, None)
    overrides.setdefault(article_id, {})["warmupDaysLeft"] = None


def _ensure_liquidation_runtime(article_id: str, current_price_kopecks: int, pmin_kopecks: int, *, state=None) -> None:
    liquidation = LIQUIDATION_ACTIVE if state is None else state.setdefault("liquidationActive", {})
    if article_id in liquidation:
        return
    from app.repricer_settings import current_algorithm_settings
    step_pct = float(current_algorithm_settings(ALGORITHM_SETTINGS_STATE).get("liquidationStepPct") or 3)
    liquidation[article_id] = {
        "articleId": article_id,
        "startPriceKopecks": current_price_kopecks,
        "currentPriceKopecks": current_price_kopecks,
        "targetPriceKopecks": max(1, min(current_price_kopecks, pmin_kopecks)),
        "startedAt": _iso_now(),
        "nextStepAt": _utc_now().isoformat(),
        "stepPct": step_pct,
        "holdOrdersTo": 1,
        "strategyKind": "illiquid",
        "requiresNegativeMarginConfirm": False,
    }


def _normalize_time_text(raw: Any, *, fallback: str) -> str:
    text = str(raw or fallback).strip()
    parts = text.split(":", 1)
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        hour, minute = [int(part) for part in fallback.split(":", 1)]
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return f"{hour:02d}:{minute:02d}"


def _normalize_iso_date_text(raw: Any) -> str | None:
    parsed = _date_from_any(raw)
    return parsed.isoformat() if parsed is not None else None


def _normalize_schedule_strategy_config(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    config = raw_config or {}
    raw_rules = config.get("scheduleRules")
    if raw_rules is None and config:
        raw_rules = [config]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCHEDULE_RULES_REQUIRED",
                "message": "Для стратегии «Расписание» нужны правила дат/часов",
            },
        )

    normalized_rules: list[dict[str, Any]] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or item.get("action") or "set_price").strip()
        if mode not in {"set_price", "delta_pct", "hold"}:
            mode = "set_price"
        days = item.get("daysOfWeek")
        if not isinstance(days, list):
            days = [1, 2, 3, 4, 5, 6, 7]
        days_of_week = sorted(
            {
                max(1, min(7, int(day)))
                for day in days
                if str(day).strip()
            }
        ) or [1, 2, 3, 4, 5, 6, 7]
        rule: dict[str, Any] = {
            "id": str(item.get("id") or f"rule-{index + 1}"),
            "dateFrom": _normalize_iso_date_text(item.get("dateFrom") or item.get("startDate")),
            "dateTo": _normalize_iso_date_text(item.get("dateTo") or item.get("endDate")),
            "timeFrom": _normalize_time_text(item.get("timeFrom") or item.get("startTime"), fallback="00:00"),
            "timeTo": _normalize_time_text(item.get("timeTo") or item.get("endTime"), fallback="23:59"),
            "daysOfWeek": days_of_week,
            "mode": mode,
        }
        if mode == "set_price":
            price_kopecks = int(item.get("priceKopecks") or round(float(item.get("priceRub") or 0) * 100))
            if price_kopecks <= 0:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "SCHEDULE_PRICE_REQUIRED",
                        "message": "Для действия «задать цену» нужна цена больше 0",
                    },
                )
            rule["priceKopecks"] = price_kopecks
        elif mode == "delta_pct":
            try:
                delta_pct = float(item.get("deltaPct") or 0)
            except (TypeError, ValueError):
                delta_pct = 0.0
            rule["deltaPct"] = max(-95.0, min(200.0, delta_pct))
        normalized_rules.append(rule)

    if not normalized_rules:
        raise HTTPException(status_code=422, detail={"code": "SCHEDULE_RULES_REQUIRED"})
    return {"scheduleRules": normalized_rules}


def _normalize_group_strategy_config(
    raw_config: dict[str, Any] | None,
    *,
    article_ids: list[str],
    row_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    config = raw_config or {}
    group_id = str(config.get("groupId") or "").strip()
    existing_group = REPRICER_SKU_GROUPS.get(group_id) if group_id else None
    raw_group_ids = config.get("groupArticleIds")
    group_ids = [str(item).strip() for item in raw_group_ids if str(item).strip()] if isinstance(raw_group_ids, list) else []
    existing_article_ids = list((existing_group or {}).get("articleIds") or [])
    group_article_ids = list(dict.fromkeys(group_ids + existing_article_ids + article_ids))
    if not group_article_ids:
        raise HTTPException(status_code=422, detail={"code": "GROUP_SKUS_REQUIRED"})

    try:
        plan_orders = int(config.get("planOrders") or (existing_group or {}).get("planOrders") or 0)
    except (TypeError, ValueError):
        plan_orders = 0
    if plan_orders <= 0:
        plan_orders = sum(max(1, int((row_index.get(article_id) or {}).get("meta", {}).get("basketNorm") or 1)) for article_id in group_article_ids)

    group_name = str(config.get("groupName") or (existing_group or {}).get("groupName") or "").strip() or f"Группа {len(group_article_ids)} SKU"
    group_id = group_id or f"group-{uuid4().hex[:10]}"
    return {
        "groupId": group_id,
        "groupName": group_name,
        "groupArticleIds": group_article_ids,
        "planOrders": max(1, plan_orders),
    }


def _normalize_strategy_assignment_config(
    definition: dict[str, Any],
    raw_config: dict[str, Any] | None,
    *,
    article_ids: list[str],
    row_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    strategy_id = str(definition["id"])
    if strategy_id == "schedule":
        return _normalize_schedule_strategy_config(raw_config)
    if strategy_id == "plan_fact_group":
        return _normalize_group_strategy_config(raw_config, article_ids=article_ids, row_index=row_index)
    if strategy_id == "plan_fact_interval":
        return {"intervalHours": max(1, _algorithm_int_setting("planFactIntervalHours", 4))}
    return deepcopy(raw_config or {})


def apply_frontend_strategy_assignment(
    article_ids: list[str],
    *,
    strategy_id_or_name: str,
    scenario: str = "complete",
    wb_token: str | None = None,
    source: str = "manual",
    sku_rows: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    definition = _frontend_strategy_definition(strategy_id_or_name)
    if definition.get("status") == "disabled":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STRATEGY_DISABLED",
                "message": f"Стратегия «{definition['name']}» временно выключена",
            },
        )
    assignments = FRONTEND_STRATEGY_ASSIGNMENTS if state is None else state.setdefault("assignments", {})
    settings_overrides = SKU_SETTINGS_OVERRIDES if state is None else state.setdefault("skuSettingsOverrides", {})
    meta_overrides = SKU_META_OVERRIDES if state is None else state.setdefault("skuMetaOverrides", {})
    audit_events = SKU_AUDIT_EVENTS if state is None else state.setdefault("skuAuditEvents", {})
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    row_index = {row["meta"]["articleId"]: row for row in rows}
    unique_article_ids = list(dict.fromkeys(article_ids))
    missing = [article_id for article_id in unique_article_ids if article_id not in row_index]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "articleIds": missing})
    assignment_config = _normalize_strategy_assignment_config(
        definition,
        config,
        article_ids=unique_article_ids,
        row_index=row_index,
    )

    assigned_at = _iso_now()
    for article_id in unique_article_ids:
        row = row_index[article_id]
        settings = deepcopy(row["settings"] if state is not None else _sku_cost_settings(article_id, use_demo_data=_demo_repricer_data_enabled(wb_token)))
        settings_overrides.setdefault(article_id, {})

        _clear_frontend_assignment_state(article_id, state=state)
        if definition["id"] == "illiquid":
            settings_overrides[article_id]["automationEnabled"] = True
            _set_frontend_assignment_meta(article_id, status="liquidation", assigned_at=assigned_at, source=source, state=state)
            _ensure_liquidation_runtime(
                article_id,
                int(row["meta"]["currentPriceKopecks"]),
                _p_min_kopecks(
                    int(settings["cogsKopecks"]),
                    float(settings["wbCommissionPct"]),
                    int(settings["logisticsKopecks"]),
                    0,
                ),
                state=state,
            )
        else:
            settings_overrides[article_id]["automationEnabled"] = True
            _set_frontend_assignment_meta(article_id, status="auto", assigned_at=assigned_at, source=source, state=state)

        assignments[article_id] = {
            "articleId": article_id,
            "strategyId": definition["id"],
            "typedStrategyId": definition["typedStrategyId"],
            "assignedAt": assigned_at,
            "source": source,
            "config": deepcopy(assignment_config),
        }
        audit_events.setdefault(article_id, []).append(
            {
                "id": f"audit-{article_id}-strategy-{len(audit_events.get(article_id, [])) + 1}",
                "sku": article_id,
                "createdAt": assigned_at,
                "actor": {"id": "manager-api", "name": "Backend API", "role": "manager"},
                "source": source,
                "scope": "sku",
                "action": "Назначение стратегии",
                "reason": f"Применена стратегия «{definition['name']}»",
                "oldValue": row.get("strategy", {}).get("name") or "не задана",
                "newValue": definition["name"],
            }
        )

    assignment_status = "liquidation" if definition["id"] == "illiquid" else "auto"
    filtered = [
        {
            "articleId": article_id,
            "strategyId": definition["id"],
            "strategyName": definition["name"],
            "typedStrategyId": definition["typedStrategyId"],
            "assignedAt": assigned_at,
            "assignmentSource": source,
            "config": deepcopy(assignment_config),
            "status": assignment_status,
        }
        for article_id in unique_article_ids
    ]
    return {
        "strategy": {
            "id": definition["id"],
            "name": definition["name"],
            "typedStrategyId": definition["typedStrategyId"],
        },
        "assignedCount": len(filtered),
        "items": filtered,
    }


def unassign_frontend_strategy_assignment(
    article_ids: list[str],
    *,
    scenario: str = "complete",
    wb_token: str | None = None,
    source: str = "manual",
    sku_rows: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assignments = FRONTEND_STRATEGY_ASSIGNMENTS if state is None else state.setdefault("assignments", {})
    settings_overrides = SKU_SETTINGS_OVERRIDES if state is None else state.setdefault("skuSettingsOverrides", {})
    meta_overrides = SKU_META_OVERRIDES if state is None else state.setdefault("skuMetaOverrides", {})
    audit_events = SKU_AUDIT_EVENTS if state is None else state.setdefault("skuAuditEvents", {})
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    row_index = {row["meta"]["articleId"]: row for row in rows}
    unique_article_ids = list(dict.fromkeys(article_ids))
    missing = [article_id for article_id in unique_article_ids if article_id not in row_index]
    if missing:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "articleIds": missing})

    unassigned_at = _iso_now()
    items: list[dict[str, Any]] = []
    for article_id in unique_article_ids:
        row = row_index[article_id]
        old_strategy_name = row.get("strategy", {}).get("name") or "не задана"
        _clear_frontend_assignment_state(article_id, state=state)
        assignments.pop(article_id, None)
        settings_overrides.setdefault(article_id, {})
        settings_overrides[article_id]["automationEnabled"] = False
        meta_overrides.setdefault(article_id, {})
        meta_overrides[article_id]["status"] = "manual"
        meta_overrides[article_id]["assignmentSource"] = "none"
        meta_overrides[article_id]["assignedAt"] = unassigned_at
        audit_events.setdefault(article_id, []).append(
            {
                "id": f"audit-{article_id}-strategy-{len(audit_events.get(article_id, [])) + 1}",
                "sku": article_id,
                "createdAt": unassigned_at,
                "actor": {"id": "manager-api", "name": "Backend API", "role": "manager"},
                "source": source,
                "scope": "sku",
                "action": "Снятие стратегии",
                "reason": "Стратегия снята вручную",
                "oldValue": old_strategy_name,
                "newValue": "не задана",
            }
        )
        items.append(
            {
                "articleId": article_id,
                "strategyId": "none",
                "strategyName": "не задана",
                "typedStrategyId": None,
                "assignedAt": unassigned_at,
                "assignmentSource": "none",
                "config": {},
                "status": "manual",
            }
        )

    return {
        "strategy": {
            "id": "none",
            "name": "не задана",
            "typedStrategyId": None,
        },
        "assignedCount": 0,
        "unassignedCount": len(items),
        "items": items,
    }


def upload_promotion_excel(
    promo_id: str,
    filename: str | None,
    scenario: str = "complete",
    wb_token: str | None = None,
) -> dict[str, Any]:
    today = _utc_now().date().isoformat()
    PROMOTION_UPLOAD_OVERRIDES[str(promo_id)] = {
        "excelLoaded": True,
        "excelFileName": filename or "upload.xlsx",
        "excelLoadedAt": today,
    }
    for promotion in list_promotions(scenario, wb_token=wb_token):
        if promotion["id"] == str(promo_id):
            promotion.update(PROMOTION_UPLOAD_OVERRIDES[str(promo_id)])
            return promotion
    raise HTTPException(status_code=404, detail="PROMOTION_NOT_FOUND")


def get_repricer_liquidation(
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    sku_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    candidates: list[dict[str, Any]] = []
    sku_index = {row["meta"]["articleId"]: row for row in rows}
    for row in rows:
        article_id = row["meta"]["articleId"]
        if article_id in LIQUIDATION_ACTIVE:
            continue
        raw_margin_pct = row["analytics"].get("marginPct")
        margin_pct = float(raw_margin_pct) if raw_margin_pct is not None else None
        current_price_kopecks = int(row["meta"]["currentPriceKopecks"])
        pmin_kopecks = _liquidation_pmin_kopecks(row)
        if not _is_liquidation_candidate(
            row,
            current_price_kopecks=current_price_kopecks,
            pmin_kopecks=pmin_kopecks,
            margin_pct=margin_pct,
        ):
            continue
        recommended, decision = _illiquid_next_price(row)
        stock_units = _liquidation_stock_units(row)
        reason = "below_pmin" if current_price_kopecks < pmin_kopecks else _liquidation_reason(row, margin_pct)
        candidates.append(
            {
                "articleId": article_id,
                "name": row["meta"]["name"],
                "photoUrl": row["meta"].get("photoUrl") or row["meta"].get("imageUrl"),
                "nmId": row["meta"].get("nmId"),
                "currentPriceKopecks": current_price_kopecks,
                "pMinKopecks": pmin_kopecks,
                "stockUnits": stock_units,
                "stockValueKopecks": current_price_kopecks * stock_units if stock_units is not None else None,
                "basketsLast7d": row["meta"]["basketsLast7d"],
                "basketNorm": row["meta"]["basketNorm"],
                "daysSinceLastSale": 7 + len(article_id),
                "marginPct": margin_pct,
                "marginStatus": _liquidation_margin_status(margin_pct),
                "reason": reason,
                "decision": decision,
                "ordersUnits": int((row.get("analytics") or {}).get("ordersUnits") or 0),
                "recommendedPriceKopecks": recommended,
            }
        )

    active: list[dict[str, Any]] = []
    for article_id, item in LIQUIDATION_ACTIVE.items():
        sku = sku_index.get(article_id)
        if sku is None:
            continue
        stock_units = _liquidation_stock_units(sku)
        raw_margin_pct = sku["analytics"].get("marginPct")
        margin_pct = float(raw_margin_pct) if raw_margin_pct is not None else None
        recommended, decision = _illiquid_next_price(sku, current_price_kopecks=int(item["currentPriceKopecks"]))
        active.append(
            {
                "articleId": article_id,
                "name": sku["meta"]["name"],
                "photoUrl": sku["meta"].get("photoUrl") or sku["meta"].get("imageUrl"),
                "nmId": sku["meta"].get("nmId"),
                "pMinKopecks": _liquidation_pmin_kopecks(sku),
                "stockUnits": stock_units,
                "stockValueKopecks": int(item["currentPriceKopecks"]) * stock_units if stock_units is not None else None,
                "marginPct": margin_pct,
                "marginStatus": _liquidation_margin_status(margin_pct),
                "daysSinceLastSale": 7 + len(article_id),
                "negativeMarginConfirmed": article_id in NEGATIVE_MARGIN_CONFIRMATIONS,
                "decision": decision,
                "ordersUnits": int((sku.get("analytics") or {}).get("ordersUnits") or 0),
                "recommendedPriceKopecks": recommended,
                **deepcopy(item),
            }
        )
    return {"candidates": candidates, "active": active, "history": deepcopy(LIQUIDATION_HISTORY)}


def start_repricer_liquidation(
    article_ids: list[str],
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    sku_rows: list[dict[str, Any]] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any]:
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    sku_index = {row["meta"]["articleId"]: row for row in rows}
    unique_article_ids = list(dict.fromkeys(article_ids))
    for article_id in article_ids:
        sku = sku_index.get(article_id)
        if sku is None:
            continue
        old_price = int(sku["meta"]["currentPriceKopecks"])
        target = _illiquid_floor_kopecks(sku)
        next_price, decision = _illiquid_next_price(sku)
        normal_pmin = _p_min_kopecks(
            sku["settings"]["cogsKopecks"],
            sku["settings"]["wbCommissionPct"],
            sku["settings"]["logisticsKopecks"],
            sku["settings"]["minMarginPct"],
        )
        LIQUIDATION_ACTIVE[article_id] = {
            "articleId": article_id,
            "name": sku["meta"]["name"],
            "startPriceKopecks": old_price,
            "currentPriceKopecks": next_price,
            "targetPriceKopecks": target,
            "startedAt": _iso_now(),
            "nextStepAt": (_utc_now() + timedelta(hours=24)).isoformat(),
            "stepPct": _illiquid_step_pct(),
            "holdOrdersTo": _illiquid_hold_orders_to(),
            "strategyKind": "illiquid",
            "lastDecision": decision,
            "requiresNegativeMarginConfirm": bool(NEGATIVE_MARGIN_CONFIRMATIONS.get(article_id) is None and next_price < normal_pmin),
        }
        assigned_at = _iso_now()
        SKU_META_OVERRIDES.setdefault(article_id, {})
        SKU_META_OVERRIDES[article_id]["status"] = "liquidation"
        SKU_META_OVERRIDES[article_id]["assignmentSource"] = "manual"
        SKU_META_OVERRIDES[article_id]["assignedAt"] = assigned_at
        SKU_SETTINGS_OVERRIDES.setdefault(article_id, {})["automationEnabled"] = True
        FRONTEND_STRATEGY_ASSIGNMENTS[article_id] = {
            "articleId": article_id,
            "strategyId": "illiquid",
            "typedStrategyId": None,
            "assignedAt": assigned_at,
            "source": "manual",
        }
        _liquidation_history_entry(article_id, sku["meta"]["name"], f"started:{decision}", "manager")
        if next_price != old_price:
            record_repricer_changelog_event(
                article_id=article_id,
                sku_name=sku["meta"]["name"],
                old_price_kopecks=old_price,
                new_price_kopecks=next_price,
                trigger="liquidation",
                margin_after_pct=sku.get("analytics", {}).get("marginPct") or 0,
                actor={"id": "manager", "name": "Менеджер", "role": "manager"},
                source="bulk" if len(unique_article_ids) > 1 else "manager",
                scope="bulk" if len(unique_article_ids) > 1 else "sku",
                reason=f"Запуск ликвидации / стратегии «Неликвид»: {decision}",
                old_value=f"{old_price}",
                new_value=f"{next_price}",
                organization_id=organization_id,
            )
    return {"started": True}


def stop_repricer_liquidation(article_id: str, *, organization_id: int | None = None) -> dict[str, Any]:
    active = LIQUIDATION_ACTIVE.pop(article_id, None)
    FRONTEND_STRATEGY_ASSIGNMENTS.pop(article_id, None)
    if article_id in SKU_META_OVERRIDES:
        SKU_META_OVERRIDES[article_id]["status"] = "manual"
        SKU_META_OVERRIDES[article_id]["assignmentSource"] = "none"
    if active is not None:
        _liquidation_history_entry(article_id, str(active.get("name") or article_id), "stopped", "manager")
        current_price = int(active.get("currentPriceKopecks") or active.get("startPriceKopecks") or 1)
        record_repricer_changelog_event(
            article_id=article_id,
            sku_name=str(active.get("name") or article_id),
            old_price_kopecks=current_price,
            new_price_kopecks=current_price,
            trigger="liquidation",
            margin_after_pct=0,
            actor={"id": "manager", "name": "Менеджер", "role": "manager"},
            source="manager",
            scope="sku",
            reason="Ликвидация остановлена вручную: дальнейшие плановые шаги цены отключены",
            old_value="liquidation",
            new_value="manual",
            organization_id=organization_id,
        )
    return {"stopped": True}


def confirm_repricer_negative_margin(article_id: str, *, organization_id: int | None = None) -> dict[str, Any]:
    expires_at = (_utc_now() + timedelta(hours=24)).isoformat()
    NEGATIVE_MARGIN_CONFIRMATIONS[article_id] = expires_at
    if article_id in LIQUIDATION_ACTIVE:
        LIQUIDATION_ACTIVE[article_id]["requiresNegativeMarginConfirm"] = False
        active = LIQUIDATION_ACTIVE[article_id]
        current_price = int(active.get("currentPriceKopecks") or active.get("startPriceKopecks") or 1)
        record_repricer_changelog_event(
            article_id=article_id,
            sku_name=str(active.get("name") or article_id),
            old_price_kopecks=current_price,
            new_price_kopecks=current_price,
            trigger="liquidation",
            margin_after_pct=0,
            actor={"id": "manager", "name": "Менеджер", "role": "manager"},
            source="manager",
            scope="sku",
            reason="Подтверждена отрицательная маржа для ликвидации на 24 часа",
            old_value="negative_margin_required",
            new_value="negative_margin_confirmed",
            organization_id=organization_id,
        )
    _liquidation_history_entry(article_id, article_id, "negative_margin_confirmed", "manager")
    return {"confirmed": True, "expiresAt": expires_at}


def _liquidation_total_days(start_price: int, target_price: int, step_pct: float) -> int:
    if start_price <= target_price or step_pct <= 0:
        return 1
    days = 1
    price = start_price
    while price > target_price and days < 365:
        next_price = max(target_price, round(price * (1 - step_pct / 100)))
        if next_price >= price:
            break
        price = next_price
        days += 1
    return days


def _days_since_iso(value: str | None) -> int | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return max(1, floor((_utc_now() - parsed).total_seconds() / 86400) + 1)


def _next_strategy_cycle_at(row: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    last_event = events[0] if events else None
    last_at = _parse_iso(last_event.get("timestamp")) if isinstance(last_event, dict) else None
    base = last_at or _parse_iso(row.get("meta", {}).get("lastSavedAt"))
    if base is None:
        return None
    interval_minutes = _sku_step_interval_minutes(row)
    return (base + timedelta(minutes=interval_minutes)).isoformat()


def _sku_step_interval_minutes(row: dict[str, Any]) -> int:
    settings = row.get("settings") or {}
    try:
        minutes = int(settings.get("priceStepMinutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        return max(5, minutes)
    try:
        hours = int(settings.get("priceStepHours") or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours > 0:
        return max(5, hours * 60)
    return max(5, _algorithm_int_setting("syncIntervalMinutes", 60))


def _pricing_status_stage(row: dict[str, Any], active_liquidation: dict[str, Any] | None) -> str:
    meta = row.get("meta") or {}
    settings = row.get("settings") or {}
    status = str(meta.get("status") or "")
    if active_liquidation is not None:
        return "active_liquidation"
    if status == "liquidation":
        return "liquidation_pending"
    if status == "warmup":
        return "warmup"
    if not bool(settings.get("automationEnabled", True)):
        return "manual_hold"
    return "active_pricing"


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _date_from_any(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=_MOSCOW_TZ)
        return value.astimezone(_MOSCOW_TZ).date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_MOSCOW_TZ)
    return value.astimezone(_MOSCOW_TZ).date()


def _hour_from_any(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if "T" in text and len(text) >= 13:
        try:
            return int(text.split("T", 1)[1][:2])
        except ValueError:
            pass
    parsed = _parse_iso(str(raw)) if raw is not None else None
    if parsed is None:
        return None
    return parsed.hour


def _row_date(item: dict[str, Any]) -> date | None:
    return _date_from_any(
        item.get("date")
        or item.get("dt")
        or item.get("day")
        or item.get("begin")
        or item.get("dateFrom")
        or item.get("lastChangeDate")
    )


def _row_hour(item: dict[str, Any]) -> int | None:
    return _hour_from_any(item.get("date") or item.get("dt") or item.get("lastChangeDate"))


def _empty_timeseries_day(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "ordersUnits": 0,
        "cancelledOrdersUnits": 0,
        "salesUnits": 0,
        "returnsUnits": 0,
        "revenueKopecks": 0,
        "avgPriceWithSppKopecks": None,
        "avgSellerPriceKopecks": None,
        "buyoutPct": None,
        "openCount": None,
        "cartCount": None,
        "funnelOrderCount": None,
        "crPct": None,
        "funnelBuyoutPct": None,
        "adSpendKopecks": None,
        "adImpressions": None,
        "adClicks": None,
        "adCartAdds": None,
        "adOrders": None,
        "adRevenueKopecks": None,
        "drrPct": None,
        "stockUnits": None,
        "priceKopecks": None,
    }


def _source_status(source_id: str, status: str, rows: int = 0, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"source": source_id, "status": status, "rows": rows}
    if message:
        payload["message"] = message
    return payload


def _collect_period_statistics_timeseries(
    *,
    scenario: str,
    wb_token: str | None,
    nm_id: int,
    start: date,
    end: date,
    include_raw: bool,
    include_sales: bool = True,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_statistics_client(scenario, token_override=wb_token))
    query = {"dateFrom": f"{start.isoformat()}T00:00:00"}
    try:
        orders_payload = _request_or_raise_statistics_report(client, WbApiRequest(method="GET", path="/api/v1/supplier/orders", query=query))
        sales_payload = (_request_or_raise_statistics_report(client, WbApiRequest(method="GET", path="/api/v1/supplier/sales", query=query))
                         if include_sales else [])
    except HTTPException as exc:
        return {
            "daily": {},
            "hourlyOrders": [],
            "rawRows": {},
            "sources": [_source_status("wb-statistics-orders-sales", "blocked", message=_http_exception_message(exc))],
        }

    orders = [item for item in orders_payload if isinstance(item, dict)] if isinstance(orders_payload, list) else []
    sales = [item for item in sales_payload if isinstance(item, dict)] if isinstance(sales_payload, list) else []
    daily: dict[str, dict[str, Any]] = {}
    hourly: dict[int, dict[str, Any]] = {}
    raw_orders: list[dict[str, Any]] = []
    raw_sales: list[dict[str, Any]] = []
    seen_order_keys: set[str] = set()

    for item in orders:
        if int(item.get("nmId") or item.get("nmID") or 0) != nm_id:
            continue
        day = _row_date(item)
        if day is None or day < start or day > end:
            continue
        is_cancelled_order = _is_cancelled_order(item)
        order_unit_key = _statistics_order_unit_key(item)
        if order_unit_key and order_unit_key in seen_order_keys:
            continue
        if order_unit_key:
            seen_order_keys.add(order_unit_key)
        raw_orders.append(item)
        key = day.isoformat()
        bucket = daily.setdefault(
            key,
            {
                "ordersUnits": 0,
                "cancelledOrdersUnits": 0,
                "ordersBuyerPriceKopecksSum": 0,
                "ordersBuyerPriceCount": 0,
                "ordersSellerPriceKopecksSum": 0,
                "ordersSellerPriceCount": 0,
            },
        )
        if is_cancelled_order:
            bucket["cancelledOrdersUnits"] += 1
            continue
        bucket["ordersUnits"] += 1
        buyer_price = _first_kopecks(item, "finishedPrice", "buyerPrice", "clientPrice")
        seller_price = _first_kopecks(item, "priceWithDisc", "priceWithDiscount", "discountedPrice")
        if buyer_price:
            bucket["ordersBuyerPriceKopecksSum"] += buyer_price
            bucket["ordersBuyerPriceCount"] += 1
        if seller_price:
            bucket["ordersSellerPriceKopecksSum"] += seller_price
            bucket["ordersSellerPriceCount"] += 1
        hour = _row_hour(item)
        if hour is not None:
            hour_bucket = hourly.setdefault(hour, {"hour": hour, "ordersUnits": 0, "revenueKopecks": 0})
            hour_bucket["ordersUnits"] += 1
            hour_bucket["revenueKopecks"] += buyer_price or 0

    for item in sales:
        if int(item.get("nmId") or item.get("nmID") or 0) != nm_id:
            continue
        day = _row_date(item)
        if day is None or day < start or day > end:
            continue
        raw_sales.append(item)
        key = day.isoformat()
        bucket = daily.setdefault(
            key,
            {
                "ordersUnits": 0,
                "cancelledOrdersUnits": 0,
                "ordersBuyerPriceKopecksSum": 0,
                "ordersBuyerPriceCount": 0,
                "ordersSellerPriceKopecksSum": 0,
                "ordersSellerPriceCount": 0,
            },
        )
        bucket.setdefault("salesUnits", 0)
        bucket.setdefault("returnsUnits", 0)
        bucket.setdefault("revenueKopecks", 0)
        amount = _first_kopecks(item, "finishedPrice", "forPay", "priceWithDisc")
        if bool(item.get("isReturn")):
            bucket["returnsUnits"] += 1
            bucket["revenueKopecks"] -= abs(amount)
        else:
            bucket["salesUnits"] += 1
            bucket["revenueKopecks"] += amount

    normalized_daily: dict[str, dict[str, Any]] = {}
    for key, bucket in daily.items():
        orders_units = int(bucket.get("ordersUnits") or 0)
        sales_units = int(bucket.get("salesUnits") or 0)
        buyer_count = int(bucket.get("ordersBuyerPriceCount") or 0)
        seller_count = int(bucket.get("ordersSellerPriceCount") or 0)
        normalized_daily[key] = {
            "ordersUnits": orders_units,
            "cancelledOrdersUnits": int(bucket.get("cancelledOrdersUnits") or 0),
            "salesUnits": sales_units,
            "returnsUnits": int(bucket.get("returnsUnits") or 0),
            "revenueKopecks": int(bucket.get("revenueKopecks") or 0),
            "avgPriceWithSppKopecks": round(int(bucket.get("ordersBuyerPriceKopecksSum") or 0) / buyer_count) if buyer_count else None,
            "avgSellerPriceKopecks": round(int(bucket.get("ordersSellerPriceKopecksSum") or 0) / seller_count) if seller_count else None,
            "buyoutPct": None,
        }

    return {
        "daily": normalized_daily,
        "hourlyOrders": [hourly.get(hour, {"hour": hour, "ordersUnits": 0, "revenueKopecks": 0}) for hour in range(24)],
        "rawRows": {"orders": raw_orders, "sales": raw_sales} if include_raw else {},
        "sources": [
            _source_status("wb-statistics-orders", "fresh" if raw_orders else "empty", len(raw_orders)),
            _source_status("wb-statistics-sales", "fresh" if raw_sales else "empty", len(raw_sales)),
        ],
    }


def _extract_sales_funnel_selected(products: list[Any], nm_id: int) -> dict[str, Any] | None:
    for item in products:
        if not isinstance(item, dict):
            continue
        product = item.get("product") or {}
        if int(product.get("nmId") or product.get("nmID") or 0) != nm_id:
            continue
        statistic = item.get("statistic") or {}
        selected = statistic.get("selected") or {}
        metrics = _sales_funnel_metrics(selected)
        return {
            "openCount": int(_first_number(selected, "openCount", "openCardCount", "openCard") or 0),
            "cartCount": metrics["cartCount"],
            "funnelOrderCount": metrics["orderCount"],
            "funnelBuyoutPct": metrics["buyoutPct"],
            "buyoutPct": metrics["buyoutPct"],
            "buyoutSource": metrics["buyoutSource"],
            "impressions": int(
                selected.get("viewCount")
                or selected.get("viewCountTotal")
                or selected.get("views")
                or selected.get("impressions")
                or selected.get("showCount")
                or selected.get("impressionCount")
                or 0
            ),
        }
    return None


def _collect_sales_funnel_daily_timeseries(
    *,
    scenario: str,
    wb_token: str | None,
    nm_id: int,
    start: date,
    end: date,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client(scenario, token_override=wb_token))
    daily: dict[str, dict[str, Any]] = {}
    blocked: list[str] = []
    for day in _date_range(start, end):
        past = day - timedelta(days=1)
        try:
            payload = _request_or_raise_sales_funnel_products(
                client,
                WbApiRequest(
                    method="POST",
                    path="/api/analytics/v3/sales-funnel/products",
                    jsonBody={
                        "selectedPeriod": {"start": day.isoformat(), "end": day.isoformat()},
                        "pastPeriod": {"start": past.isoformat(), "end": past.isoformat()},
                        "nmIds": [nm_id],
                        "skipDeletedNm": False,
                        "limit": 1000,
                        "offset": 0,
                    },
                ),
            )
        except HTTPException as exc:
            blocked.append(_http_exception_message(exc))
            continue
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        products = data.get("products", []) if isinstance(data, dict) else []
        selected = _extract_sales_funnel_selected(products, nm_id)
        if selected is None:
            continue
        cart_count = int(selected.get("cartCount") or 0)
        open_count = int(selected.get("openCount") or 0)
        order_count = int(selected.get("funnelOrderCount") or 0)
        daily[day.isoformat()] = {
            **selected,
            "crPct": round((order_count / open_count) * 100, 2) if open_count else None,
            "cartToOrderPct": round((order_count / cart_count) * 100, 2) if cart_count else None,
        }
    if blocked and not daily:
        status = "blocked"
        message = blocked[0]
    elif blocked:
        status = "partial"
        message = blocked[0]
    else:
        status = "fresh" if daily else "empty"
        message = None
    return {
        "daily": daily,
        "sources": [_source_status("wb-analytics-sales-funnel-products-daily", status, len(daily), message)],
    }


_ADS_FULLSTATS_ALLOWED_CAMPAIGN_STATUSES = {7, 9, 11}


def _ads_fullstats_status_allowed(*values: Any) -> bool:
    statuses = [_int_or_none(value) for value in values]
    resolved = [status for status in statuses if status is not None]
    if not resolved:
        return True
    return any(status in _ADS_FULLSTATS_ALLOWED_CAMPAIGN_STATUSES for status in resolved)


def _ads_campaign_ids_from_payload(payload: Any) -> list[int]:
    ids: list[int] = []
    if not isinstance(payload, dict):
        return ids
    for group in payload.get("adverts") or []:
        if not isinstance(group, dict):
            continue
        group_status = group.get("status")
        for item in group.get("advert_list") or []:
            if isinstance(item, dict):
                advert_id = _int_or_none(item.get("advertId"))
                if advert_id is not None and _ads_fullstats_status_allowed(item.get("status"), group_status):
                    ids.append(advert_id)
            else:
                advert_id = _int_or_none(item)
                if advert_id is not None and _ads_fullstats_status_allowed(group_status):
                    ids.append(advert_id)
        if _int_or_none(group.get("advertId")) is not None and _ads_fullstats_status_allowed(group_status):
            ids.append(int(group["advertId"]))
    return sorted(set(ids))


def _ads_node_date(node: dict[str, Any], inherited: date | None) -> date | None:
    return _date_from_any(node.get("date") or node.get("day") or node.get("dt") or node.get("beginDate")) or inherited


def _merge_ads_daily_from_node(
    node: Any,
    nm_id: int,
    daily: dict[str, dict[str, Any]],
    inherited_date: date | None = None,
    *,
    fallback_date: date | None = None,
) -> None:
    if isinstance(node, list):
        for item in node:
            _merge_ads_daily_from_node(item, nm_id, daily, inherited_date, fallback_date=fallback_date)
        return
    if not isinstance(node, dict):
        return
    local_date = _ads_node_date(node, inherited_date)
    node_nm_id = _int_or_none(node.get("nmId") or node.get("nm"))
    if node_nm_id == nm_id and (local_date is not None or fallback_date is not None):
        key_date = local_date or fallback_date
        key = (key_date or _utc_now().date()).isoformat()
        bucket = daily.setdefault(
            key,
            {
                "adSpendKopecks": 0,
                "adImpressions": 0,
                "adClicks": 0,
                "adCartAdds": 0,
                "adOrders": 0,
                "adRevenueKopecks": 0,
            },
        )
        if local_date is None:
            bucket["adsSourceGranularity"] = "period_or_undated"
        bucket["adSpendKopecks"] += _kopecks_from_rub(node.get("sum") or node.get("spend"))
        bucket["adImpressions"] += int(node.get("views") or node.get("impressions") or 0)
        bucket["adClicks"] += int(node.get("clicks") or 0)
        bucket["adCartAdds"] += int(node.get("atbs") or node.get("cartAdds") or 0)
        bucket["adOrders"] += int(node.get("orders") or 0)
        bucket["adRevenueKopecks"] += _kopecks_from_rub(node.get("sum_price") or node.get("revenue"))
    for value in node.values():
        _merge_ads_daily_from_node(value, nm_id, daily, local_date, fallback_date=fallback_date)


def _collect_ads_daily_timeseries(
    *,
    scenario: str,
    wb_token: str | None,
    nm_id: int,
    start: date,
    end: date,
) -> dict[str, Any]:
    client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario, token_override=wb_token))
    try:
        promotion = _request_or_raise(client, WbApiRequest(method="GET", path="/adv/v1/promotion/count"))
        campaign_ids = _ads_campaign_ids_from_payload(promotion)
        if not campaign_ids:
            return {"daily": {}, "sources": [_source_status("wb-ads-fullstats", "empty", 0, "No active advert ids")]}
        payloads = _request_ads_fullstats_payloads(client, campaign_ids[:50], start=start, end=end)
    except HTTPException as exc:
        return {"daily": {}, "sources": [_source_status("wb-ads-fullstats", "blocked", 0, _http_exception_message(exc))]}
    daily: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        _merge_ads_daily_from_node(payload, nm_id, daily, fallback_date=end)
    if not daily:
        return {
            "daily": {},
            "sources": [_source_status("wb-ads-fullstats", "empty", 0, "No exact nmId daily rows in ads fullstats")],
        }
    for row in daily.values():
        spend = int(row.get("adSpendKopecks") or 0)
        revenue = int(row.get("adRevenueKopecks") or 0)
        row["drrPct"] = round((spend / revenue) * 100, 2) if revenue else None
    return {"daily": daily, "sources": [_source_status("wb-ads-fullstats", "fresh", len(daily))]}


def _collect_stock_snapshot_timeseries(
    *,
    scenario: str,
    wb_token: str | None,
    nm_id: int,
    end: date,
) -> dict[str, Any]:
    try:
        aggregates = fetch_stock_aggregates(scenario, wb_token=wb_token, date_to=end)
    except HTTPException as exc:
        return {"daily": {}, "sources": [_source_status("wb-analytics-stocks-report", "blocked", 0, _http_exception_message(exc))]}
    row = aggregates.get(str(nm_id))
    if not row:
        return {"daily": {}, "sources": [_source_status("wb-analytics-stocks-report", "empty", 0)]}
    return {
        "daily": {
            end.isoformat(): {
                "stockUnits": int(row.get("wbStockUnits") or 0),
                "inWayToClient": int(row.get("inWayToClient") or 0),
                "inWayFromClient": int(row.get("inWayFromClient") or 0),
                "warehouses": int(row.get("warehouses") or 0),
                "stockSourceGranularity": "current_snapshot",
            }
        },
        "sources": [_source_status("wb-analytics-stocks-report", "fresh", 1, "Current stock snapshot only")],
    }


def _price_history_timeseries(
    *,
    article_id: str,
    start: date,
    end: date,
    organization_id: int | None,
    current_price_kopecks: int | None,
) -> dict[str, Any]:
    changelog = list_repricer_changelog(
        article_id=article_id,
        page=1,
        limit=500,
        organization_id=organization_id,
    )
    events = [
        item
        for item in changelog["items"]
        if item.get("newPriceKopecks") is not None and _date_from_any(item.get("timestamp")) is not None
    ]
    events.sort(key=lambda item: str(item.get("timestamp") or ""))
    price_events = [
        {
            "timestamp": item.get("timestamp"),
            "date": (_date_from_any(item.get("timestamp")) or end).isoformat(),
            "oldPriceKopecks": item.get("oldPriceKopecks"),
            "newPriceKopecks": item.get("newPriceKopecks"),
            "trigger": item.get("trigger"),
            "reason": item.get("reason"),
        }
        for item in events
    ]
    if not events:
        return {
            "daily": {},
            "events": [],
            "sources": [_source_status("repricer-changelog-price", "empty", 0, "No price changelog events for this SKU")],
            "currentPriceKopecks": current_price_kopecks,
        }

    known_price: int | None = None
    daily: dict[str, dict[str, Any]] = {}
    for day in _date_range(start, end):
        for item in events:
            event_day = _date_from_any(item.get("timestamp"))
            if event_day is not None and event_day <= day:
                known_price = int(item.get("newPriceKopecks") or known_price or 0)
        if known_price is not None:
            daily[day.isoformat()] = {"priceKopecks": known_price}
    return {
        "daily": daily,
        "events": price_events,
        "sources": [_source_status("repricer-changelog-price", "fresh", len(price_events))],
        "currentPriceKopecks": current_price_kopecks,
    }


def get_repricer_sku_timeseries(
    article_id: str,
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    days: int = 30,
    include_raw: bool = False,
    sku_rows: list[dict[str, Any]] | None = None,
    organization_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    finance_allowed: bool = True,
) -> dict[str, Any]:
    bounded_days = max(1, min(days, 90))
    end = _utc_now().date()
    start = end - timedelta(days=bounded_days - 1)
    if date_from is not None or date_to is not None:
        from app.repricer_sync import _normalize_period_range
        range_start, range_end, bounded_days = _normalize_period_range(days, date_from=date_from, date_to=date_to)
        start, end = range_start.date(), range_end.date()
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    row = next((item for item in rows if item.get("meta", {}).get("articleId") == article_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "articleId": article_id})
    meta = row.get("meta") or {}
    nm_id = int(meta.get("nmId") or 0)
    if nm_id <= 0:
        raise HTTPException(status_code=422, detail={"code": "SKU_NM_ID_REQUIRED", "articleId": article_id})

    day_map = {day.isoformat(): _empty_timeseries_day(day) for day in _date_range(start, end)}
    sources: list[dict[str, Any]] = []
    raw_rows: dict[str, Any] = {}

    statistics = _collect_period_statistics_timeseries(
        scenario=scenario,
        wb_token=wb_token,
        nm_id=nm_id,
        start=start,
        end=end,
        include_raw=include_raw,
        include_sales=organization_id is None,
    )
    sources.extend(statistics["sources"])
    raw_rows.update(statistics.get("rawRows") or {})
    for key, values in statistics["daily"].items():
        day_map.setdefault(key, _empty_timeseries_day(_date_from_any(key) or end)).update(values)

    funnel = _collect_sales_funnel_daily_timeseries(
        scenario=scenario,
        wb_token=wb_token,
        nm_id=nm_id,
        start=start,
        end=end,
    )
    sources.extend(funnel["sources"])
    for key, values in funnel["daily"].items():
        day_map.setdefault(key, _empty_timeseries_day(_date_from_any(key) or end)).update(values)

    ads = _collect_ads_daily_timeseries(
        scenario=scenario,
        wb_token=wb_token,
        nm_id=nm_id,
        start=start,
        end=end,
    )
    sources.extend(ads["sources"])
    for key, values in ads["daily"].items():
        day_map.setdefault(key, _empty_timeseries_day(_date_from_any(key) or end)).update(values)

    stock = _collect_stock_snapshot_timeseries(
        scenario=scenario,
        wb_token=wb_token,
        nm_id=nm_id,
        end=end,
    )
    sources.extend(stock["sources"])
    for key, values in stock["daily"].items():
        day_map.setdefault(key, _empty_timeseries_day(_date_from_any(key) or end)).update(values)

    price = _price_history_timeseries(
        article_id=article_id,
        start=start,
        end=end,
        organization_id=organization_id,
        current_price_kopecks=int(meta.get("currentPriceKopecks") or 0),
    )
    sources.extend(price["sources"])
    for key, values in price["daily"].items():
        day_map.setdefault(key, _empty_timeseries_day(_date_from_any(key) or end)).update(values)

    if organization_id is not None:
        from app.wb_finance_timeseries import financial_daily_series
        finance = financial_daily_series(organization_id, nm_id, start, end) if finance_allowed else {
            "coverage": {"complete": False, "state": "forbidden"}, "state": "blocked",
            "daily": {key: {"salesUnits": None, "returnsUnits": None, "revenueKopecks": None} for key in day_map}}
        sources = [item for item in sources if item.get("source") != "wb-statistics-sales"]
        sources.append(_source_status("wb-finance-realization", finance["state"],
            len(finance["daily"]), message="retailAmount / rrDate; продажи за вычетом возвратов"))
        for key, values in finance["daily"].items():
            day_map[key].update(values)

    statuses = {str(item.get("status")) for item in sources}
    if "blocked" in statuses and any(item.get("status") == "fresh" for item in sources):
        source_status = "partial"
    elif "blocked" in statuses:
        source_status = "blocked"
    elif "partial" in statuses:
        source_status = "partial"
    elif any(item.get("status") == "fresh" for item in sources):
        source_status = "fresh"
    else:
        source_status = "empty"

    daily_rows = [day_map[key] for key in sorted(day_map)]
    return {
        "articleId": article_id,
        "nmId": nm_id,
        "skuName": meta.get("name") or article_id,
        "period": {"days": bounded_days, "dateFrom": start.isoformat(), "dateTo": end.isoformat()},
        "sourceStatus": source_status,
        "sources": sources,
        "daily": daily_rows,
        "hourlyOrders": statistics.get("hourlyOrders") or [],
        "priceEvents": price["events"],
        "currentPriceKopecks": price["currentPriceKopecks"],
        "rawRows": raw_rows if include_raw else None,
        **({"financialCoverage": finance["coverage"]} if organization_id is not None else {}),
    }


def get_repricer_pricing_status(
    article_id: str,
    scenario: str = "complete",
    wb_token: str | None = None,
    *,
    sku_rows: list[dict[str, Any]] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any]:
    rows = sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token)
    row = next((item for item in rows if item.get("meta", {}).get("articleId") == article_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "articleId": article_id})

    meta = row.get("meta") or {}
    settings = row.get("settings") or {}
    analytics = row.get("analytics") or {}
    strategy = row.get("strategy") or {}
    changelog = list_repricer_changelog(
        article_id=article_id,
        page=1,
        limit=10,
        organization_id=organization_id,
    )
    events = changelog["items"]
    last_event = events[0] if events else None
    active_liquidation = LIQUIDATION_ACTIVE.get(article_id)
    stage = _pricing_status_stage(row, active_liquidation)

    progress: dict[str, Any]
    if active_liquidation is not None:
        started_at = str(active_liquidation.get("startedAt") or "")
        start_price = int(active_liquidation.get("startPriceKopecks") or meta.get("currentPriceKopecks") or 0)
        target_price = int(active_liquidation.get("targetPriceKopecks") or 0)
        step_pct = float(active_liquidation.get("stepPct") or ALGORITHM_SETTINGS_STATE.get("liquidationStepPct") or 3)
        total_days = _liquidation_total_days(start_price, target_price, step_pct)
        current_day = min(total_days, _days_since_iso(started_at) or 1)
        progress = {
            "kind": "liquidation",
            "currentDay": current_day,
            "totalDays": total_days,
            "startedAt": started_at,
            "nextStepAt": active_liquidation.get("nextStepAt"),
            "stepPct": step_pct,
            "startPriceKopecks": start_price,
            "currentPriceKopecks": int(active_liquidation.get("currentPriceKopecks") or meta.get("currentPriceKopecks") or 0),
            "targetPriceKopecks": target_price,
            "requiresNegativeMarginConfirm": bool(active_liquidation.get("requiresNegativeMarginConfirm")),
        }
    elif str(meta.get("status")) == "warmup":
        total_days = int(ALGORITHM_SETTINGS_STATE.get("warmupDays") or 10)
        days_left = int(meta.get("warmupDaysLeft") or 0)
        progress = {
            "kind": "warmup",
            "currentDay": max(1, total_days - days_left + 1),
            "totalDays": total_days,
            "daysLeft": days_left,
            "nextStepAt": None,
        }
    else:
        progress = {
            "kind": "pricing",
            "currentDay": None,
            "totalDays": None,
            "assignedAt": strategy.get("assignedAt") or meta.get("assignedAt"),
            "nextStepAt": _next_strategy_cycle_at(row, events),
            "syncIntervalHours": max(1, int(_sku_step_interval_minutes(row) / 60)),
            "syncIntervalMinutes": _sku_step_interval_minutes(row),
        }

    rules = deepcopy(FRONTEND_STRATEGY_BY_ID.get(str(strategy.get("id") or ""), {}).get("rules") or [])
    latest_reason = (
        last_event.get("reason")
        if isinstance(last_event, dict) and last_event.get("reason")
        else strategy.get("description")
    )
    return {
        "articleId": article_id,
        "skuName": meta.get("name") or article_id,
        "stage": stage,
        "status": meta.get("status"),
        "strategy": {
            "id": strategy.get("id"),
            "name": strategy.get("name"),
            "type": strategy.get("type"),
            "typedStrategyId": strategy.get("typedStrategyId"),
            "assignmentSource": strategy.get("assignmentSource"),
            "assignedAt": strategy.get("assignedAt") or meta.get("assignedAt"),
            "rules": rules,
        },
        "progress": progress,
        "current": {
            "priceKopecks": int(meta.get("currentPriceKopecks") or 0),
            "pMinKopecks": _liquidation_pmin_kopecks(row),
            "pMaxKopecks": int(settings.get("pMaxKopecks") or 0),
            "marginPct": analytics.get("marginPct"),
            "basketsLast7d": int(meta.get("basketsLast7d") or analytics.get("baskets") or 0),
            "basketNorm": int(meta.get("basketNorm") or 0),
            "ordersUnits": analytics.get("ordersUnits"),
            "stockUnits": analytics.get("wbStockUnits"),
        },
        "lastDecision": {
            "timestamp": last_event.get("timestamp") if isinstance(last_event, dict) else None,
            "trigger": last_event.get("trigger") if isinstance(last_event, dict) else None,
            "reason": latest_reason,
            "oldPriceKopecks": last_event.get("oldPriceKopecks") if isinstance(last_event, dict) else None,
            "newPriceKopecks": last_event.get("newPriceKopecks") if isinstance(last_event, dict) else None,
            "changePct": last_event.get("changePct") if isinstance(last_event, dict) else None,
        },
        "recentChanges": events,
        "changelogTotal": changelog["total"],
    }


def record_repricer_price_change(
    *,
    article_id: str,
    sku_name: str,
    old_price_kopecks: int,
    new_price_kopecks: int,
    trigger: str = "algorithm",
    strategy_name: str | None = None,
    reason: str | None = None,
    source: str = "system",
    organization_id: int | None = None,
) -> None:
    # Real observations, history and night consumption are committed together
    # by the durable confirmed-price ledger, including read-only reconciliation.
    if source in {"wb_api", "wb_api_pending"}:
        return
    if new_price_kopecks == old_price_kopecks:
        return
    now = _iso_now()
    SKU_META_OVERRIDES.setdefault(article_id, {})
    SKU_META_OVERRIDES[article_id]["currentPriceKopecks"] = new_price_kopecks
    SKU_META_OVERRIDES[article_id]["localPriceOverrideActive"] = source != "wb_api"
    SKU_META_OVERRIDES[article_id]["localPriceOverrideSource"] = source
    SKU_META_OVERRIDES[article_id]["localPriceOverrideUpdatedAt"] = now
    record_repricer_changelog_event(
        article_id=article_id,
        sku_name=sku_name,
        old_price_kopecks=old_price_kopecks,
        new_price_kopecks=new_price_kopecks,
        trigger=trigger,
        margin_after_pct=0,
        actor={"id": "repricer-engine", "name": "Repricer Engine", "role": "system"},
        source=source,
        scope="sku",
        reason=reason or (f"Strategy «{strategy_name}»" if strategy_name else "Strategy execution"),
        old_value=str(old_price_kopecks),
        new_value=str(new_price_kopecks),
        organization_id=organization_id,
    )


def record_repricer_changelog_event(
    *,
    article_id: str,
    sku_name: str,
    old_price_kopecks: int,
    new_price_kopecks: int,
    trigger: str,
    margin_after_pct: float | int | None,
    actor: dict[str, Any],
    source: str,
    scope: str,
    reason: str,
    old_value: str | None = None,
    new_value: str | None = None,
    organization_id: int | None = None,
    event_id: str | None = None,
) -> None:
    old_price = max(1, int(old_price_kopecks or 0))
    new_price = max(1, int(new_price_kopecks or 0))
    change_pct = round(((new_price - old_price) / old_price) * 100, 2) if old_price else 0.0
    entry = {
        "id": event_id or f"chg-{article_id}-{len(CHANGELOG_ENTRIES) + 1}",
        "articleId": article_id,
        "skuName": sku_name,
        "timestamp": _iso_now(),
        "oldPriceKopecks": old_price,
        "newPriceKopecks": new_price,
        "changePct": change_pct,
        "trigger": trigger,
        "marginAfterPct": float(margin_after_pct or 0),
        "actor": actor,
        "source": source,
        "applyMode": source,
        "wbMutationSent": source == "wb_api",
        "scope": scope,
        "reason": reason,
        "oldValue": old_value if old_value is not None else str(old_price),
        "newValue": new_value if new_value is not None else str(new_price),
    }
    CHANGELOG_ENTRIES.insert(0, entry)
    if organization_id is not None:
        from app.repricer_persistence.store import append_changelog_entry

        append_changelog_entry(organization_id=organization_id, entry=entry)


def list_repricer_changelog(
    *,
    article_id: str | None = None,
    triggers: list[str] | None = None,
    from_iso: str | None = None,
    page: int = 1,
    limit: int = 50,
    organization_id: int | None = None,
) -> dict[str, Any]:
    if organization_id is not None:
        from app.repricer_persistence.store import list_changelog_page

        return list_changelog_page(
            organization_id=organization_id, article_id=article_id, triggers=triggers,
            from_date=_parse_iso(from_iso), page=page, limit=limit,
        )
    items = list(CHANGELOG_ENTRIES)
    items = sorted(items, key=lambda row: row["timestamp"], reverse=True)
    if article_id:
        items = [row for row in items if row["articleId"] == article_id]
    if triggers:
        allowed = set(triggers)
        items = [row for row in items if row["trigger"] in allowed]
    from_dt = _parse_iso(from_iso)
    if from_dt is not None:
        items = [row for row in items if (_parse_iso(row["timestamp"]) or _utc_now()) >= from_dt]
    total = len(items)
    start = max(0, (max(page, 1) - 1) * max(limit, 1))
    return {"items": deepcopy(items[start : start + max(limit, 1)]), "total": total}
