"""Week-only financial ledger projection (storage allocation is downstream).

Read completed, connection-scoped financial cursor chains already stored by the
existing collector. This module never makes WB requests or recalculates profit.
Source: POST /api/finance/v1/sales-reports/detailed (official WB financial API).
Ruble amounts become integer kopecks. rrDate is the ledger posting day, not
saleDt. Advertising here means settlement deductions explicitly identified as
WB Promotion, not campaign statistics by impression day. Tax is the existing
dated P&L calculation, not a WB-supplied tax liability.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from threading import RLock

VERSION = "week-financial-columns-v6"
FIELDS = {
    "commissionKopecks": ("Комиссия, ₽", "ppvzSalesCommission"),
    "logisticsKopecks": ("Логистика, ₽", "deliveryService"),
    "storageKopecks": ("Хранение, ₽", "paidStorage"),
    "adSpendKopecks": ("Реклама, ₽", "deduction"),
    "acceptanceKopecks": ("Платная приёмка, ₽", "paidAcceptance"),
    "penaltyKopecks": ("Штрафы, ₽", "penalty"),
    "compensationKopecks": ("Компенсация, ₽", "forPay"),
    "taxKopecks": ("Налог, ₽", None),
}
_cache = OrderedDict()
_lock = RLock()


def source_revision(org, start=None, end=None):
    from app.repricer_page_finance import page_finance_revision
    from app.wb_week_sources import source_revision as operational_revision
    return sha256((page_finance_revision(org, start, end) + operational_revision(org, start, end)).encode()).hexdigest()


def _amount(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _buyout_operation(row, start, end, amounts, unknown):
    """Financial-report merchandise, net of returns, on the posting day.

    Only a sale/return payment reason proves merchandise movement. A financial
    document marked Продажа may instead be compensation or a service payment.
    Unspecified correction semantics cannot prove additional sold units.
    """
    from app.repricer_bff import _date_from_any

    operation = str(row.get("sellerOperName") or "").strip().casefold()
    doc = str(row.get("docTypeName") or "").strip().casefold()
    trade = (doc, operation) in {("продажа", "продажа"), ("возврат", "возврат")}
    correction = operation in {"коррекция продаж", "коррекция возвратов", "сторно продаж",
                              "сторно возвратов", "корректная продажа", "корректный возврат"}
    if not trade and not correction:
        return
    day = _date_from_any(row.get("rrDate"))
    if day and not start <= day <= end:
        return
    nm = str(int(row.get("nmId") or 0))
    if int(nm) <= 0:
        nm = "0"
    bucket = amounts.setdefault(nm, {"units": 0, "kopecks": 0})
    missing = unknown.setdefault(nm, set())
    if not day or correction:
        # Keep an ambiguous correction visible as unavailable, never turn
        # its technical quantity or payout into a new buyout or fake zero.
        missing.update(("units", "kopecks"))
        return
    raw = row.get("quantity")
    try:
        quantity = Decimal(str(raw))
        quantity = int(quantity) if not isinstance(raw, bool) and quantity.is_finite() and quantity == quantity.to_integral_value() else None
    except (InvalidOperation, TypeError, ValueError):
        quantity = None
    if quantity is None:
        missing.update(("units", "kopecks"))
        return
    sign = -1 if doc == "возврат" else 1
    bucket["units"] += sign * quantity
    amount = _amount(row.get("retailAmount"))
    if amount is None or str(row.get("currency") or "RUB").upper() not in {"RUB", "RUR", "643"}:
        missing.add("kopecks")
    else:
        # retailAmount is the operation total, not a unit price or payout.
        bucket["kopecks"] += sign * amount


def product_buyouts(cache, nm):
    """A complete raw cursor is required to confirm a period total or zero."""
    nm = str(nm)
    values = cache.get("buyouts", {}).get(nm, {})
    unknown = cache.get("unknownBuyouts", {}).get(nm, [])
    unallocated = cache.get("buyouts", {}).get("0", {}) if nm != "0" else {}
    unallocated_unknown = cache.get("unknownBuyouts", {}).get("0", []) if nm != "0" else []
    return {field: None if (cache.get("sourceComplete") is not True and field not in values) or field in unknown
            or (not values.get(field) and (unallocated.get(field) or field in unallocated_unknown))
            else values.get(field, 0) for field in ("units", "kopecks")}


def project_operations(rows, start, end):
    from app.repricer_bff import _date_from_any

    amounts, unknown, seen, storage_daily, unallocated_storage_daily = {}, {}, {}, {}, {}
    buyouts, unknown_buyouts = {}, {}
    for row in rows:
        identity = str(row.get("rrdId") or "")
        if not identity:
            raise ValueError("WB_FINANCE_INVALID_ROW_ID")
        if identity in seen:
            if seen[identity] != row:
                raise ValueError("WB_FINANCE_CONFLICTING_ROWS")
            continue
        seen[identity] = row
        _buyout_operation(row, start, end, buyouts, unknown_buyouts)
        nm = str(int(row.get("nmId") or 0))
        if int(nm) <= 0:
            nm = "0"
        posting = _date_from_any(row.get("rrDate"))
        if posting and not start <= posting <= end:
            continue
        bucket = amounts.setdefault(nm, {field: 0 for field in FIELDS if field != "taxKopecks"})
        missing = unknown.setdefault(nm, set())
        operation = str(row.get("sellerOperName") or "").strip().casefold()
        doc = str(row.get("docTypeName") or "").strip().casefold()
        compensation = "компенсац" in operation
        promotion = "wb продвижение" in str(row.get("bonusTypeName") or "").casefold()
        currency = str(row.get("currency") or "RUB").upper()
        if currency not in {"RUB", "RUR", "643"}:
            missing.update(bucket)  # never present another currency as rubles
            continue
        if not row.get("bonusTypeName") and _amount(row.get("deduction")) not in (None, 0):
            missing.add("adSpendKopecks")
        for field, (_, source) in FIELDS.items():
            if field == "taxKopecks":
                continue
            if (field == "compensationKopecks" and not compensation) or (field == "adSpendKopecks" and not promotion):
                continue
            value = _amount(row.get(source))
            if field == "commissionKopecks" and (doc, operation) in {("продажа", "продажа"), ("возврат", "возврат")}:
                # Same retail realization settlement basis as P&L:
                # retailAmount - payout - acquiring. The reported
                # ppvzSalesCommission is a different, pre-agent, ex-VAT basis.
                revenue, payout, acquiring = (_amount(row.get(key)) for key in
                    ("retailAmount", "forPay", "acquiringFee"))
                value = revenue - payout - acquiring if None not in (revenue, payout, acquiring) else None
            elif field == "commissionKopecks" and value not in (None, 0):
                # An adjustment carrying only the ex-VAT, pre-agent amount
                # cannot be mixed into the retail-basis settlement commission.
                value = None
            if field == "compensationKopecks":
                # A compensation is a classified credit, not every additional
                # payment/correction or every negative charge in the ledger.
                extra = _amount(row.get("additionalPayment"))
                if value in (None, 0) and extra is not None:
                    value = extra
                elif value not in (None, 0) and extra not in (None, 0, value):
                    value = None  # ambiguous credit; do not count it twice
            if value is None or not posting:
                missing.add(field)
                continue
            if field in {"commissionKopecks", "compensationKopecks"} and doc == "возврат":
                value = -value  # document sign, preserving signed corrections
            bucket[field] += value
            if field == "storageKopecks":
                day = posting.isoformat()
                storage_daily[day] = storage_daily.get(day, 0) + value
                if nm == "0":
                    unallocated_storage_daily[day] = unallocated_storage_daily.get(day, 0) + value
    return {"aggregates": {}, "amounts": amounts,
            "buyouts": buyouts,
            "unknownBuyouts": {nm: sorted(fields) for nm, fields in unknown_buyouts.items() if fields},
            "storageDailyKopecks": storage_daily,
            "unallocatedStorageDailyKopecks": unallocated_storage_daily,
            "unknown": {nm: sorted(fields) for nm, fields in unknown.items() if fields}}


def load_finance(org, start, end):
    from app.cabinet.store import get_organization_wb_token_secret
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix
    from app.repricer_page_finance import connection_key, load_page_finance, page_connection, _overlapping_metadata

    token = get_organization_wb_token_secret(org)
    connection = connection_key(token) if token else None
    if not connection:
        return {}
    def revision():
        return tuple(sorted((str(meta.get("sourceKey")), str(meta.get("fetchedAt")))
            for meta in _overlapping_metadata(list_source_cache_ranges_by_prefix(org, f"repricer_finance_{connection}_", limit=100), start, end)))
    before = revision()
    key = (org, connection, start, end, VERSION, before)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return deepcopy(_cache[key])
    marker = page_connection.set(connection)
    try:
        result = load_page_finance(org, start, end, projector=project_operations, include_undated=True)
    finally:
        page_connection.reset(marker)
    result["weekFinancialRevision"] = sha256(repr((connection, list(before))).encode()).hexdigest()
    if before == revision():
        with _lock:
            _cache[key] = deepcopy(result)
            while len(_cache) > 4:
                _cache.popitem(last=False)
    return result


def product_values(cache, nm, pnl):
    complete = cache.get("sourceComplete") is True
    nm = str(nm)
    values = cache.get("amounts", {}).get(nm, {})
    unknown = cache.get("unknown", {}).get(str(nm), [])
    # A charge without nmId may belong to this product. Absence of a directly
    # attributed amount is therefore not a confirmed zero for that field.
    unallocated = cache.get("amounts", {}).get("0", {}) if nm != "0" else {}
    unallocated_unknown = cache.get("unknown", {}).get("0", []) if nm != "0" else []
    return {field: (pnl.get("taxKopecks") if complete else None) if field == "taxKopecks" else
            (None if field in unknown or (not values.get(field) and
                (not complete or unallocated.get(field) or field in unallocated_unknown)) else values.get(field, 0))
            for field in FIELDS}


def coverage_fresh(coverage):
    if not coverage.get("complete"):
        return False
    now = datetime.now(timezone.utc)
    today = now.astimezone(timezone(timedelta(hours=3))).date().isoformat()
    ttl = timedelta(hours=24 if str(coverage.get("dateTo") or "") < today else 1)
    stamps = coverage.get("observedAt") or [coverage.get("fetchedAt")]
    try:
        parsed = [datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) for stamp in stamps]
        return bool(parsed) and all(timedelta(0) <= now - (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)) <= ttl for stamp in parsed)
    except (TypeError, ValueError):
        return False


def attach(report, org, start, end, previous_start, previous_end, *, finance_allowed, current_pnl=None, previous_pnl=None, catalog=None, include_previous=True):
    revision = source_revision(org, previous_start, end)
    current = load_finance(org, start, end) if finance_allowed else {}
    previous = load_finance(org, previous_start, previous_end) if finance_allowed and include_previous else {}
    current_pnl, previous_pnl, catalog = current_pnl or {}, previous_pnl or {}, catalog or {}
    rows = report.setdefault("rows", [])
    existing = {str(row.get("nmId")) for row in rows}
    # Include a product charged this week even if its sale happened earlier.
    for nm in sorted(set(current.get("amounts", {})) | set(previous.get("amounts", {}))):
        if nm == "0" or nm in existing:
            continue
        meta = catalog.get(int(nm), {})
        rows.append({"nmId": int(nm), "sku": meta.get("sku") or f"NM_{nm}",
                     "productName": meta.get("productName"), "photoUrl": meta.get("photoUrl"),
                     "conclusion": "Есть финансовые операции; показатели продаж проверяются отдельно."})
    for row in rows:
        nm = str(row.get("nmId"))
        now = product_values(current, nm, current_pnl.get(int(nm), {}) if nm.isdigit() else {})
        before = product_values(previous, nm, previous_pnl.get(int(nm), {}) if nm.isdigit() else {})
        for field in FIELDS:
            a, b = now[field], before[field]
            row[field] = {"kopecks": a, "previousKopecks": b,
                          "deltaPct": round((a-b) / abs(b) * 100, 1) if current.get("sourceComplete") is True and previous.get("sourceComplete") is True and a is not None and b not in (None, 0) else None}
    report["columns"] = [column for column in report.get("columns", []) if column.get("key") not in FIELDS] + [
        {"key": field, "label": label} for field, (label, _) in FIELDS.items()]
    report["financialVersion"] = VERSION
    report["financialAccess"] = finance_allowed
    report["financialSourceRevision"] = revision
    report["financialCoverage"] = {label: {"complete": cache.get("sourceComplete") is True,
        "missingDates": cache.get("missingDates", []), "fetchedAt": cache.get("fetchedAt"),
        "observedAt": cache.get("pnlExpenseObservedAt", []),
        "unknownFields": cache.get("unknown", {}), "dateFrom": str(left), "dateTo": str(right)}
        for label, cache, left, right in (("selected", current, start, end), ("previous", previous, previous_start, previous_end))}
    report["buyoutSource"] = {"source": "finance_sales_reports_detailed", "dateField": "rrDate",
        "quantityField": "quantity", "amountBasis": "retailAmount", "returns": "net",
        "coverage": {label: {"complete": cache.get("sourceComplete") is True and not cache.get("unknownBuyouts"),
            "unknownFields": cache.get("unknownBuyouts", {}),
            "unallocated": product_buyouts(cache, "0")}
            for label, cache in (("selected", current), ("previous", previous))}}
    report["unallocatedFinancials"] = {"selected": product_values(current, "0", {}), "previous": product_values(previous, "0", {})}
    warnings = []
    if finance_allowed:
        for label, cache in (("выбранного", current), ("предыдущего", previous)):
            if cache.get("sourceComplete") is not True or cache.get("unknown"):
                warnings.append(f"Финансовые данные {label} периода неполные; прочерк не означает ноль.")
            if cache.get("unknownBuyouts"):
                warnings.append(f"Выкупы {label} периода: есть операции с неизвестными датами, полями или неоднозначными корректировками; прочерк не означает ноль.")
            if any(cache.get("buyouts", {}).get("0", {}).values()) or cache.get("unknownBuyouts", {}).get("0"):
                warnings.append(f"Выкупы {label} периода без nmId указаны отдельно в источнике и не распределены по товарам.")
        for label, coverage in report["financialCoverage"].items():
            if coverage["complete"] and not coverage_fresh(coverage):
                warnings.append(f"Финансовые данные {'выбранного' if label == 'selected' else 'предыдущего'} периода сохранены ранее и проверяются на обновления.")
        # Storage and advertising get their final allocation/activity mapping
        # downstream; only that layer can describe their remaining residual.
        if any(value not in (None, 0) for group in report["unallocatedFinancials"].values()
               for field, value in group.items() if field not in {"storageKopecks", "adSpendKopecks"}):
            warnings.append("Есть операции без nmId: они указаны отдельно и не распределены по товарам.")
    if warnings:
        report["warning"] = " ".join(filter(None, [report.get("warning"), *warnings]))
    return report
