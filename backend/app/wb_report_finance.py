"""WB product-report monetary definition, separate from repricer pricing math."""
from datetime import date
from collections import OrderedDict
from copy import deepcopy
from threading import RLock

from app.repricer_page_finance import project_realization_operations

VERSION = "wb-product-report-v4"
_projections = OrderedDict()
_projection_lock = RLock()


def load_report_finance(organization_id, start, end):
    """Reuse only an identical account/connection/raw-source revision, bounded to four ranges."""
    from app.cabinet.store import get_organization_wb_token_secret
    from app.repricer_page_finance import load_page_finance, connection_key, page_connection, VERSION as BASE_VERSION
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix
    token = get_organization_wb_token_secret(organization_id)
    connection = page_connection.get() or (connection_key(token) if token else None)
    if not connection:
        return load_page_finance(organization_id, start, end, projector=project_report_operations)
    def revision():
        from app.wb_storage import revision as storage_revision
        metas = [*list_source_cache_ranges_by_prefix(organization_id, f"repricer_finance_{connection}_", limit=100),
                 *list_source_cache_ranges_by_prefix(organization_id, "stock_history_", limit=500)]
        return (storage_revision(organization_id), tuple(sorted((str(m.get("sourceKey")), str(m.get("fetchedAt"))) for m in metas)))
    before = revision()
    key = (organization_id, connection, start, end, VERSION, BASE_VERSION, before)
    with _projection_lock:
        if key in _projections:
            _projections.move_to_end(key)
            return deepcopy(_projections[key])
    marker = page_connection.set(connection)
    try:
        result = load_page_finance(organization_id, start, end, projector=project_report_operations)
    finally:
        page_connection.reset(marker)
    if before == revision():
        with _projection_lock:
            _projections[key] = deepcopy(result)
            while len(_projections) > 4:
                _projections.popitem(last=False)
    return result


def project_report_operations(rows: list[dict], start: date, end: date) -> dict:
    # The older product-export adjustments changed seller-price revenue. The
    # realization report's retailAmount must not be reduced by compensation.
    result = project_realization_operations(rows, start, end)
    from app.wb_report_commission import attach_reported_commission
    attach_reported_commission(result, rows, start, end)
    result.update(reportMetricVersion=VERSION, reportRevenueAdjustmentKopecks=0)
    return result


def apply_storage_allocation(result, org, start, end, history_metas=None):
    """Replace all storage contributions once; raw receipts remain untouched."""
    from app.wb_storage import allocate, daily_allocations, load_stock
    from app.repricer_bff import aggregate_finance_report_rows, build_finance_diagnostics_from_aggregates
    from app.repricer_page_finance import REALIZATION_VERSION
    from datetime import datetime

    aggregates = result.setdefault("aggregates", {})
    account = result.setdefault("unallocatedFinancials", {})
    values = [row.get("storageKopecks") for row in aggregates.values()]
    values.append(account.get("storageKopecks", 0))
    total = sum(values) if all(type(value) is int for value in values) else None
    # A known zero needs neither a catalogue nor a warehouse observation.
    snapshot = load_stock(org) if total else {}
    allocation = allocate(total, snapshot, financial_complete=result.get("sourceComplete") is True)
    result["storageAllocation"] = {key: value for key, value in allocation.items() if key != "amounts"}
    result["storageAllocation"]["financialComplete"] = result.get("sourceComplete") is True
    if type(total) is not int:
        return
    daily = result.setdefault("dailyAggregates", {})
    account_daily = result.setdefault("unallocatedDailyFinancials", {})
    day_totals = {day: sum(row.get("storageKopecks", 0) for row in rows.values())
        + account_daily.get(day, {}).get("storageKopecks", 0) for day, rows in daily.items()}
    for day, row in account_daily.items():
        if day not in day_totals:
            day_totals[day] = row.get("storageKopecks", 0)

    def seed(nm):
        row = aggregate_finance_report_rows([{"nmId": int(nm), "quantity": 0, "docTypeName": "",
            "saleDt": str(end), "forPay": 0}], date_from=datetime.combine(start, datetime.min.time()),
            date_to=datetime.combine(end, datetime.min.time()))["aggregates"][nm]
        row.update(realizationRevenueKopecks=0, realizationGrossSalesKopecks=0,
            realizationReturnsKopecks=0, realizationVersion=REALIZATION_VERSION,
            settlementPayableConfirmed=True, reportedCommissionKopecks=0, reportedCommissionComplete=True,
            financeAdSpendAuthoritative=result.get("financialAdvertisingComplete") is not False, compensationKopecks=0)
        return row

    amounts = allocation["amounts"] if allocation["complete"] else {}
    for row in aggregates.values():
        row["storageKopecks"] = 0
        row["storageAttribution"] = "current_stock_allocation" if allocation["complete"] else "account_unallocated"
    for nm, amount in amounts.items():
        if amount or nm in aggregates:
            aggregate = aggregates.setdefault(nm, seed(nm)) if nm not in aggregates else aggregates[nm]
            aggregate.update(storageKopecks=amount, storageAttribution="current_stock_allocation")
    for rows in daily.values():
        for row in rows.values():
            row["storageKopecks"] = 0
    if allocation["complete"] and sum(day_totals.values()) == total:
        for day, portions in daily_allocations(day_totals, snapshot).items():
            for nm, amount in portions.items():
                if amount:
                    daily.setdefault(day, {}).setdefault(nm, seed(nm))["storageKopecks"] = amount
            if day in account_daily:
                account_daily[day]["storageKopecks"] = 0
    else:
        for day, amount in day_totals.items():
            account_daily.setdefault(day, {})["storageKopecks"] = amount
    account["storageKopecks"] = allocation["unallocatedKopecks"]
    raw = result.get("diagnostics", {}).get("rawExpenseTotals", {})
    result.setdefault("diagnostics", {}).update(build_finance_diagnostics_from_aggregates(aggregates))
    result["diagnostics"]["rawExpenseTotals"] = raw
