"""The realization report's read contract, shared by existing collectors.

No database, scheduling or profit policy lives here. Daily and weekly reports
are alternative representations; exact reports never prove daily coverage.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import HTTPException

from app.wb_api.client import WbApiRequest

DETAIL_PATH = "/api/finance/v1/sales-reports/detailed"
LIST_PATH = "/api/finance/v1/sales-reports/list"
SOURCE_VERSION = "wb-realization-v1"
DETAIL_FIELDS = (
    "rrdId", "reportId", "reportType", "docTypeName", "sellerOperName",
    "bonusTypeName", "quantity", "retailAmount", "retailPrice",
    "retailPriceWithDisc", "nmId", "vendorCode", "sku", "saleDt", "rrDate",
    "srid", "commissionPercent", "ppvzSalesCommission", "salePercent", "spp",
    "deliveryService", "paidStorage", "paidAcceptance", "penalty", "deduction",
    "additionalPayment", "paymentSchedule", "cashbackAmount", "cashbackDiscount",
    "cashbackCommissionChange", "acquiringFee", "acquiringPercent", "forPay",
    "currency", "dateFrom", "dateTo", "createDate", "vw", "vwNds",
    "rebillLogisticCost", "rebillLogisticOrg", "paymentProcessing",
)
# Audit-only additions must not force a backfill, but an eight-field revenue
# download cannot certify the fees already consumed by financial reports.
CORE_FIELDS = frozenset(DETAIL_FIELDS[:33]) | {"currency"}
# WB documents this description as optional. Its absence in an otherwise
# complete response is not a missing monetary field or a reason to reload.
REQUIRED_RESPONSE_FIELDS = CORE_FIELDS - {"bonusTypeName"}
MONEY_FIELDS = frozenset({
    "retailAmount", "retailPriceWithDisc", "forPay", "ppvzSalesCommission",
    "deliveryService", "paidStorage", "paidAcceptance", "penalty", "deduction",
    "additionalPayment", "paymentSchedule", "cashbackAmount", "cashbackDiscount",
    "cashbackCommissionChange", "acquiringFee",
})
SUMMARY_MONEY_FIELDS = (
    "retailAmountSum", "forPaySum", "deliveryServiceSum", "paidStorageSum",
    "paidAcceptanceSum", "deductionSum", "penaltySum", "additionalPaymentSum",
    "cashbackAmountSum", "cashbackDiscountSum", "cashbackCommissionChangeSum",
    "paymentSchedule", "bankPaymentSum",
)


def integer_id(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and not value.startswith("0"):
        return int(value)
    raise HTTPException(502, "WB_FINANCE_INVALID_ROW_ID")


def money_kopecks(value):
    """None means unavailable, not zero. Provider money is denominated in rubles."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if amount.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def is_merchandise(row):
    doc = str(row.get("docTypeName") or "").strip().casefold()
    operation = str(row.get("sellerOperName") or "").strip().casefold()
    return (doc, operation) in {("продажа", "продажа"), ("возврат", "возврат")}


def raw_source_compatible(payload):
    if payload.get("sourceComplete") is not True or payload.get("reportPeriod", "daily") != "daily" or payload.get("reportId") is not None:
        return False
    rows = payload.get("operations")
    count = payload.get("operationCount")
    if count is not None and (type(count) is not int or count < 0 or isinstance(rows, list) and len(rows) != count):
        return False
    if rows == [] and count in (None, 0) and not isinstance(count, bool):
        return True
    if rows is None and payload.get("sourceVersion") == SOURCE_VERSION and type(count) is int and count == 0:
        return True
    if not isinstance(rows, list) and not (
        payload.get("sourceVersion") == SOURCE_VERSION and type(payload.get("operationCount")) is int
        and payload["operationCount"] > 0
    ):
        return False
    currencies = payload.get("currencies")
    if currencies is None and isinstance(rows, list):
        currencies = {str(row.get("currency") or "").upper() for row in rows if isinstance(row, dict)}
    if not currencies or any(currency not in {"RUB", "RUR", "643"} for currency in currencies):
        return False
    fields = payload.get("requestedFields")
    if fields is not None and (not isinstance(fields, list) or not CORE_FIELDS <= set(fields)):
        return False
    available = payload.get("availableFields")
    if available is not None:
        return isinstance(available, list) and REQUIRED_RESPONSE_FIELDS <= set(available)
    # Older receipts have no field mask. Infer only what the actual rows prove;
    # an explicit completed empty cursor remains a confirmed empty interval.
    return isinstance(rows, list) and bool(rows) and all(
        isinstance(row, dict) and REQUIRED_RESPONSE_FIELDS <= row.keys()
        and all(row[field] is not None and not isinstance(row[field], bool) for field in MONEY_FIELDS)
        for row in rows)


def fetch_report_list(client, start: date, end: date, *, period="weekly", limit=1000):
    """Paginate through the existing request budget/checkpoint boundary."""
    from app.repricer_bff import _request_or_raise_finance_report

    if period not in {"daily", "weekly"} or not 1 <= limit <= 1000 or start > end:
        raise ValueError("Invalid financial report list scope")
    reports, offset = {}, 0
    while True:
        payload = _request_or_raise_finance_report(client, WbApiRequest(
            method="POST", path=LIST_PATH, jsonBody={
                "dateFrom": str(start), "dateTo": str(end), "period": period,
                "limit": limit, "offset": offset,
            }))
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if (not isinstance(rows, list) or len(rows) > limit or any(not isinstance(row, dict) for row in rows)
                or isinstance(payload, dict) and payload.get("error") not in (None, False)):
            raise HTTPException(502, "WB_FINANCE_INVALID_RESPONSE")
        added = 0
        for row in rows:
            identity = integer_id(row.get("reportId"))
            if identity in reports and reports[identity] != row:
                raise HTTPException(502, "WB_FINANCE_CONFLICTING_REPORTS")
            added += identity not in reports
            reports[identity] = row
        if len(rows) < limit:
            break
        if not added:
            raise HTTPException(502, "WB_FINANCE_LIST_STALLED")
        offset += len(rows)
    return list(reports.values())


def summarize_reports(reports):
    """Keep currencies and nullable field coverage; never sum mixed money."""
    groups = {}
    seen = {}
    for row in reports:
        identity = integer_id(row.get("reportId"))
        if identity in seen:
            if seen[identity] != row:
                raise HTTPException(502, "WB_FINANCE_CONFLICTING_REPORTS")
            continue
        seen[identity] = row
        currency = str(row.get("currency") or "").upper()
        currency = "RUB" if currency in {"RUR", "643"} else currency
        group = groups.setdefault(currency, {"reportIds": [], "totals": {key: 0 for key in SUMMARY_MONEY_FIELDS}})
        group["reportIds"].append(identity)
        for key in SUMMARY_MONEY_FIELDS:
            value = money_kopecks(row.get(key))
            previous = group["totals"][key]
            group["totals"][key] = previous + value if previous is not None and value is not None else None
    return groups
