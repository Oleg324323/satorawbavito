"""WB storage allocation from the complete current warehouse observation."""
VERSION = "wb-current-stock-storage-v1"


def revision(org):
    from app.repricer_cache.store import get_source_cache_fetched_at
    return VERSION, str(get_source_cache_fetched_at(org, "stocks"))


def load_stock(org):
    from app.repricer_cache.store import get_source_cache
    from app.wb_pnl_advertising import _source_context, _instant
    snapshot = get_source_cache(org, "stocks", strict=True) or {}
    if not snapshot:
        return {}
    context = _source_context(org)
    if (context is None or snapshot.get("organizationId", org) != org
            or snapshot.get("marketplaceAccountId", context["marketplaceAccountId"]) != context["marketplaceAccountId"]
            or _instant(snapshot.get("fetchedAt")) is None
            or _instant(snapshot["fetchedAt"]) < _instant(context["connectedAt"])):
        return {}
    return snapshot


def allocate(total, snapshot, *, financial_complete=True):
    from app.wb_week_sources import allocate_kopecks
    rows = snapshot.get("aggregates")
    valid = (isinstance(rows, dict) and snapshot.get("sourceComplete") is not False
        and snapshot.get("aggregateComplete") is not False
        and snapshot.get("coverageState") not in {"partial", "missing", "blocked"})
    weights = {}
    if valid:
        for nm, row in rows.items():
            if (not str(nm).isdigit() or int(nm) <= 0 or not isinstance(row, dict)
                    or str(int(nm)) in weights
                    or type(row.get("wbStockUnits")) is not int or row["wbStockUnits"] < 0):
                valid = False
                break
            weights[str(int(nm))] = row["wbStockUnits"]
    denominator = sum(weights.values()) if valid else None
    result = dict(amounts={}, unallocatedKopecks=total, complete=False,
        basis="current_wb_stock_units", stockObservedAt=snapshot.get("fetchedAt"),
        totalStockUnits=denominator, observedDates=[], missingDates=[])
    if type(total) is not int or not financial_complete:
        result.update(reason="financial_storage_incomplete")
        return result
    if total == 0:
        result.update(unallocatedKopecks=0, complete=True, reason=None)
        return result
    if not valid or not denominator:
        result["reason"] = "stock_snapshot_incomplete" if not valid else "stock_total_zero"
        return result
    amounts, residual = allocate_kopecks(total, weights)
    result.update(amounts=amounts, unallocatedKopecks=residual, complete=True, reason=None)
    return result


def daily_allocations(day_totals, snapshot):
    """Cumulative rounding preserves both period shares and posting-day totals."""
    previous, cumulative, result = {}, 0, {}
    for day, total in sorted(day_totals.items()):
        cumulative += total
        current = allocate(cumulative, snapshot)["amounts"]
        result[day] = {nm: current.get(nm, 0) - previous.get(nm, 0) for nm in current.keys() | previous.keys()}
        previous = current
    return result
