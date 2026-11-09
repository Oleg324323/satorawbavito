"""P&L-only, signed product fees; never allocate account charges to products.

Reuse the collector's completed cursor chains, including their connection and
revision isolation. This projection does not make provider requests or change
the repricer/ABC projection. Both fees use the financial posting date rrDate,
not the underlying sale/order date. Undated charges are not silently assigned.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import RLock

from app.repricer_bff import _date_from_any, _kopecks_from_rub
from app.wb_api.financial_reports import money_kopecks

VERSION = "wb-pnl-product-fees-v5"
_cache = OrderedDict()
_lock = RLock()
FIELDS = {"penaltyKopecks": ("penalty", "penaltyRub"),
          "acceptanceKopecks": ("paidAcceptance", "acceptance")}


def project_pnl_operations(rows, start, end):
    from app.wb_report_finance import project_report_operations
    from app.wb_report_commission import with_commission_products

    result = with_commission_products(project_report_operations(rows, start, end), start, end)
    amounts, unknown, seen, daily_amounts = {}, set(), set(), {}
    subtotals, subtotal_unknown = {}, set()
    for row in rows:
        identity = str(row["rrdId"])
        if identity in seen:
            continue  # conflicting IDs have already been rejected by the base projector
        seen.add(identity)
        nm = str(int(row.get("nmId") or 0))
        posting = _date_from_any(row.get("rrDate"))
        # deduction includes financial-report advertising. Add it once, not
        # again alongside the classified promotion component.
        if posting is None or start <= posting <= end:
            values = [next((row[key] for key in aliases if row.get(key) is not None), None)
                      for aliases in (*FIELDS.values(), ("deduction",))]
            values = [money_kopecks(value) for value in values]
            if any(value is None for value in values) or (posting is None and any(values)):
                subtotal_unknown.add(nm)
            elif posting is not None:
                subtotals[nm] = subtotals.get(nm, 0) + sum(values)
        for field, aliases in FIELDS.items():
            day = posting
            if day is None:
                value = next((row[key] for key in aliases if row.get(key) is not None), None)
                if value not in (0, "0", "0.00"):
                    unknown.add((nm, field))
                continue
            if not start <= day <= end:
                continue
            value = next((row[key] for key in aliases if row.get(key) is not None), None)
            if value is None:
                unknown.add((nm, field))
                daily_amounts.setdefault(str(day), {}).setdefault(nm, {key: 0 for key in FIELDS})
                continue
            try:
                parsed = Decimal(str(value))
                if not parsed.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                unknown.add((nm, field))
                continue
            amount = _kopecks_from_rub(value)
            if amount:
                bucket = amounts.setdefault(nm, {key: 0 for key in FIELDS})
                bucket[field] += amount
                daily_amounts.setdefault(str(day), {}).setdefault(nm, {key: 0 for key in FIELDS})[field] += amount

    raw_totals = result.setdefault("diagnostics", {}).setdefault("rawExpenseTotals", {})
    for field in FIELDS:
        raw_totals[field] = sum(value[field] for value in amounts.values())
    for nm, aggregate in result["aggregates"].items():
        for field in FIELDS:
            # Replace any sellerRevenue-weighted account allocation from the
            # legacy aggregator, before (not after) P&L profit is calculated.
            aggregate[field] = amounts.get(nm, {}).get(field, 0)
    # A penalty can refer to an old sale, but is charged in this report. The
    # shared projection may therefore have no product row for it yet.
    seeds = [{"rrdId": f"pnl-fee-{nm}", "nmId": int(nm), "quantity": 0,
              "docTypeName": "", "saleDt": start.isoformat(), "forPay": 0}
             for nm in amounts.keys() | {nm for nm, _ in unknown}
             if nm != "0" and nm not in result["aggregates"]]
    if seeds:
        from app.repricer_bff import aggregate_finance_report_rows
        seeded = aggregate_finance_report_rows(seeds,
            date_from=datetime.combine(start, datetime.min.time()),
            date_to=datetime.combine(end, datetime.min.time()))["aggregates"]
        for nm, aggregate in seeded.items():
            aggregate.update(amounts.get(nm, {key: 0 for key in FIELDS}))
            aggregate.update(result.get("reportedCommissionByNm", {}).get(nm, {
                "reportedCommissionKopecks": None, "reportedCommissionComplete": False}))
            aggregate["settlementPayableConfirmed"] = True
            aggregate.update(realizationRevenueKopecks=0, realizationGrossSalesKopecks=0,
                             realizationReturnsKopecks=0, financeAdSpendAuthoritative=True)
            result["aggregates"][nm] = aggregate
            # A derived expense-only row has zero merchandise on its posting
            # day. Keep that basis explicit rather than failing tax coverage.
            for day, entries in daily_amounts.items():
                if nm in entries:
                    result["dailyAggregates"].setdefault(day, {})[nm] = {**aggregate, **entries[nm]}
    result["pnlExpenseAmounts"] = amounts
    unknown_by_nm = {}
    for nm, field in sorted(unknown):
        unknown_by_nm.setdefault(nm, []).append(field)
    result["pnlExpenseUnknown"] = unknown_by_nm
    result["pnlExpenseVersion"] = VERSION
    result["pnlFinancialSubtotalAmounts"] = subtotals
    result["pnlFinancialSubtotalUnknown"] = dict.fromkeys(sorted(subtotal_unknown), True)
    # Missing product fees are row-local. The caller must check these values
    # before calculation; a global blocker would erase unrelated valid profit.
    return result


def financial_expense_subtotal(cache, nm_id):
    nm = str(nm_id or 0)
    if (cache.get("sourceComplete") is not True or cache.get("pnlExpenseVersion") != VERSION
            or nm in cache.get("pnlFinancialSubtotalUnknown", [])):
        return None
    return cache.get("pnlFinancialSubtotalAmounts", {}).get(nm, 0)


def load_pnl_finance(organization_id: int, start: date, end: date):
    from app.cabinet.store import get_organization_wb_token_secret
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix
    from app.repricer_page_finance import connection_key, load_page_finance, page_connection

    token = get_organization_wb_token_secret(organization_id)
    connection = page_connection.get() or (connection_key(token) if token else None)
    if not connection:
        return {}
    def revision():
        from app.wb_storage import revision as storage_revision
        metas = [*list_source_cache_ranges_by_prefix(organization_id, f"repricer_finance_{connection}_", limit=100),
                 *list_source_cache_ranges_by_prefix(organization_id, "stock_history_", limit=500)]
        return (storage_revision(organization_id), tuple(sorted((str(meta.get("sourceKey")), str(meta.get("fetchedAt"))) for meta in metas)))
    before = revision()
    if not before[1]:
        return {}
    key = (organization_id, connection, start, end, VERSION, before)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return deepcopy(_cache[key])
    marker = page_connection.set(connection)
    try:
        result = load_page_finance(organization_id, start, end, projector=project_pnl_operations, include_undated=True)
    finally:
        page_connection.reset(marker)
    result["pnlExpenseRevision"] = sha256(repr(key).encode()).hexdigest()
    if revision() == before:
        with _lock:
            _cache[key] = deepcopy(result)
            while len(_cache) > 4:
                _cache.popitem(last=False)
    return result


def product_expenses(cache, nm_id):
    """A partial observed zero is not evidence that the product had no fees."""
    nm = str(nm_id or 0)
    complete = cache.get("sourceComplete") is True
    supported = cache.get("pnlExpenseVersion") == VERSION
    unknown = cache.get("pnlExpenseUnknown", {}).get(nm, [])
    amounts = cache.get("pnlExpenseAmounts", {}).get(nm, {})
    values = {}
    for field in FIELDS:
        value = amounts.get(field, 0)
        values[field] = (value if supported and field not in unknown
                         and (complete or value != 0) else None)
    state = "partial" if supported else "unavailable"
    if complete and supported and all(value is not None for value in values.values()):
        from app.wb_reports_sprint_d import _parse_cache_datetime
        now = datetime.now(timezone.utc)
        today = now.astimezone(timezone(timedelta(hours=3))).date().isoformat()
        ttl = timedelta(hours=24 if str(cache.get("dateTo") or "") < today else 1)
        observations = cache.get("pnlExpenseObservedAt", [cache.get("fetchedAt")])
        parsed = [_parse_cache_datetime(value) for value in observations]
        state = "confirmed" if parsed and all(
            observed and timedelta(0) <= now - observed <= ttl for observed in parsed) else "stale"
    return {**values, "state": state}
