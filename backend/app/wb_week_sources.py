"""Read-only Week metrics; reuse durable evidence, never infer another period.

Orders/carts: order-cohort WB funnel. Buyouts: financial sale-day ledger.
Advertising: settlement deductions and independently labelled campaign activity.
Storage: the full period expense is allocated over the current complete WB
warehouse stock snapshot, independently of filters and the selected period.
No profit calculations or marketplace mutations live here.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256

from app.repricer_cache import store

FUNNEL_FIELDS = {"cartCount": "carts", "orderCount": "orders", "orderSumKopecks": "orderAmount",
                 "buyoutCount": "buyouts", "buyoutSumKopecks": "buyoutAmount"}
REQUIRED_FUNNEL_FIELDS = {"carts", "orders", "orderAmount"}


def _complete_funnel_cover(period, facts, field):
    """Select a contiguous interval cover; never split/derive an observation.

    Only Week opts into this fallback. The shared funnel aggregator and its
    other consumers keep their existing selection contract.
    """
    from app.wb_api.source_freshness import source_is_fresh
    by_start = defaultdict(list)
    for index, fact in enumerate(facts):
        left, right = date.fromisoformat(fact['dateFrom']), date.fromisoformat(fact['dateTo'])
        if period.date_from <= left <= right <= period.date_to and fact.get(field) is not None:
            by_start[left].append((index, right, source_is_fresh(fact)))
    plans = {period.date_from: ((0, 0, ()), ())}
    for left in sorted(by_start):
        if left not in plans:
            continue
        (fresh_days, count, stamps), chosen = plans[left]
        for index, right, fresh in by_start[left]:
            boundary = right + timedelta(days=1)
            score = (fresh_days + ((right-left).days+1 if fresh else 0), count-1,
                     tuple(sorted((*stamps, facts[index]['fetchedAt']), reverse=True)))
            if boundary not in plans or score > plans[boundary][0]:
                plans[boundary] = (score, (*chosen, index))
    return plans.get(period.date_to+timedelta(days=1), (None, ()))[1]


def _aggregate_funnel(period, facts):
    from app.wb_sales_funnel import aggregate
    values, _ = aggregate(period, facts)
    # Keep the established result if complete. A fresher partial response must
    # not destroy an available full cover, even if that cover is saved/stale.
    replacements = {}
    for field in FUNNEL_FIELDS.values():
        if values['fieldCoverage'][field]:
            continue
        cover = _complete_funnel_cover(period, facts, field)
        if not cover:
            continue
        if cover not in replacements:
            replacements[cover], _ = aggregate(period, [facts[i] for i in cover])
        values[field] = replacements[cover][field]
        values['fieldCoverage'][field] = replacements[cover]['fieldCoverage'][field]
    return values


def _connection(org):
    from app.cabinet.store import get_organization_wb_token_secret
    from app.repricer_page_finance import connection_key
    token = get_organization_wb_token_secret(org)
    return connection_key(token) if token else None


def _facts(org, connection, start, end):
    from sqlalchemy import select, or_
    from app.repricer_cache.orm import WbRepricerSourceCacheRow as Cache
    def read(session):
        # The credential digest and organization scope prevent cross-cabinet
        # reuse. Only facts entirely within the requested interval may be summed.
        query = select(Cache.payload).where(Cache.organization_id == org,
            or_(Cache.source_key.like(f"funnel_v1_%_{connection}_fact_%"),
                Cache.source_key.like(f"funnel_v1_%_{connection}_enumerated_%")),
            Cache.range_date_from >= start, Cache.range_date_to <= end)
        return list(session.scalars(query.execution_options(yield_per=250)))
    return store._run_db(read) or []


def source_revision(org, start=None, end=None):
    from sqlalchemy import select, or_
    from app.repricer_cache.orm import WbRepricerSourceCacheRow as Cache
    from app.repricer_page_finance import _overlapping_metadata
    connection = _connection(org)
    def read(session):
        query = select(Cache.source_key, Cache.fetched_at).where(
            Cache.organization_id == org,
            or_(Cache.source_key.like(f"funnel_v1_%_{connection}_fact_%"),
                Cache.source_key.like(f"funnel_v1_%_{connection}_enumerated_%")))
        if start:
            query = query.where(or_(Cache.range_date_to >= start, Cache.range_date_to.is_(None)))
        if end:
            query = query.where(or_(Cache.range_date_from <= end, Cache.range_date_from.is_(None)))
        # Per-receipt timestamps detect corrections to an existing older fact,
        # even if another fact already has the latest timestamp / same count.
        return sorted((key, str(stamp)) for key, stamp in session.execute(query))
    from app.wb_storage import revision as storage_revision
    revision = ['week-current-storage-v1', storage_revision(org), store._run_db(read), store.cached_goods_meta(org).get("latestFetchedAt")]
    for prefix in (f"week_funnel_{connection}_", "ads_", "stock_history_"):
        # The OOS context reads the seven days ending at the selected end;
        # storage reads both full periods. Undated receipts stay conservative.
        left = min(start, end - timedelta(days=6)) if prefix == "stock_history_" and start and end else start
        revision.extend((m.get("sourceKey"), str(m.get("fetchedAt")))
            for m in _overlapping_metadata(store.list_source_cache_ranges_by_prefix(org, prefix, limit=500), left, end))
    return sha256(repr(revision).encode()).hexdigest()


def load_funnel(org, start, end):
    connection = _connection(org)
    # Report assembly and readiness checks request the same two intervals.
    # Reuse bounded projections, but invalidate immediately on corrected facts,
    # changed credentials/catalog or source receipts; freshness advances too.
    revision = source_revision(org, start, end)
    minute = int(datetime.now(timezone.utc).timestamp()) // 60
    return deepcopy(_load_funnel(org, connection, start, end, revision, minute))


@lru_cache(maxsize=4)
def _load_funnel(org, connection, start, end, revision, minute):
    from app.platform.period import Period
    from app.wb_sales_funnel import days, intervals
    from app.wb_api.source_freshness import source_is_fresh
    if not connection:
        return {"metrics": {}, "complete": False, "dataComplete": False, "populationComplete": False,
                "unresolvedReasons": {"connection_missing": 1}, "needsCollection": True,
                "missingDates": sorted(map(str, days(start, end)))}
    observations, enumerations = defaultdict(list), []
    for fact in _facts(org, connection, start, end):
        if 'nmId' in fact:
            observations[str(fact['nmId'])].append(fact)
        elif fact.get('complete') is True and isinstance(fact.get('returnedNmIds'), list):
            # Existing account/credential-scoped, fully paginated listings are
            # population evidence too. Do not download them again under a new key.
            enumerations.append({**fact, 'sourceComplete': True})
            for nm in fact['returnedNmIds']:
                observations[str(nm)]  # A missing fact remains explicitly unknown.
    exact_key = f"week_funnel_{connection}_{start}_{end}"
    receipts = {exact_key: store.get_source_cache(org, exact_key, slim=False) or {}}
    for meta in store.list_source_cache_ranges_by_prefix(org, f"week_funnel_{connection}_", limit=100):
        left, right = str(meta.get("dateFrom") or ""), str(meta.get("dateTo") or "")
        if str(start) <= left <= right <= str(end) and meta["sourceKey"] not in receipts:
            receipts[meta["sourceKey"]] = store.get_source_cache(org, meta["sourceKey"], slim=False) or {}
    collected_days, population_days = set(), set()
    for cached in [*receipts.values(), *enumerations]:
        left, right = cached.get("dateFrom"), cached.get("dateTo")
        if not left or not right or not str(start) <= left <= right <= str(end):
            continue
        if cached.get("sourceComplete"):
            population_days |= days(date.fromisoformat(left), date.fromisoformat(right))
            if source_is_fresh(cached):
                collected_days |= days(date.fromisoformat(left), date.fromisoformat(right))
        for nm, fact in cached.get("aggregates", {}).items():
            observations[str(nm)].append({**fact, "dateFrom": left, "dateTo": right,
                "fetchedAt": cached.get("fetchedAt")})
    # Do not reuse unbound legacy baskets caches: old normalization defaulted
    # a missing buyoutCount to zero, and did not retain credential identity.
    metrics, missing, field_complete, unresolved = {}, set(), True, defaultdict(int)
    for nm, facts in observations.items():
        values = _aggregate_funnel(Period(start, end), facts)
        row = {}
        for key, field in FUNNEL_FIELDS.items():
            complete = values["fieldCoverage"][field]
            row[key] = values[field] if complete else None
            if field in REQUIRED_FUNNEL_FIELDS:
                field_complete &= complete
        gaps = [field for field in REQUIRED_FUNNEL_FIELDS if not values['fieldCoverage'][field]]
        if gaps:
            reason = 'source_fields_missing' if any(all(fact.get(field) is None for fact in facts) for field in gaps) else 'incomplete_intervals'
            unresolved[reason] += 1
        # Stale facts remain visible; the queue refreshes them independently.
        fresh_facts = [fact for fact in facts if source_is_fresh(fact)]
        fresh_values = _aggregate_funnel(Period(start, end), fresh_facts)
        for field in REQUIRED_FUNNEL_FIELDS:
            if fresh_values["fieldCoverage"][field]:
                continue
            covered = set()
            for observation in fresh_facts:
                left, right = date.fromisoformat(observation["dateFrom"]), date.fromisoformat(observation["dateTo"])
                if start <= left <= right <= end and observation.get(field) is not None:
                    covered |= days(left, right)
            # Overlapping totals that cannot be added still require an exact
            # interval observation, not a fabricated/prorated sum.
            missing |= (days(start, end) - covered or days(start, end))
        metrics[nm] = row
    catalog_ids = {str(good.get("nmID") or good.get("nmId")) for good in store.list_cached_goods(org)}
    if catalog_ids - set(metrics):
        missing.update(days(start, end))
        unresolved['source_product_omitted'] = len(catalog_ids - set(metrics))
    complete = bool(metrics) and field_complete and not missing
    missing = missing or (days(start, end) if not metrics else set())
    return {"metrics": metrics, "complete": complete,
            "populationComplete": population_days == days(start, end),
            "dataComplete": (bool(metrics) or population_days == days(start, end)) and field_complete,
            "unresolvedReasons": dict(unresolved), "needsCollection": bool(missing - collected_days),
            "missingRanges": intervals(missing - collected_days), "missingDates": sorted(map(str, missing))}


def collect_funnel(org, token, start, end):
    """One seller-wide paginated period request, using existing HTTP checkpoints.

    Persist only after all pages succeed. No default-zero normalization and no
    writes into another tab's projections. Retries reuse report-read checkpoints.
    """
    from app.repricer_bff import _request_or_raise_sales_funnel_products
    from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest, build_wb_analytics_client
    from app.wb_sales_funnel import normalized_metrics, SOURCE
    from app.repricer_page_finance import connection_key
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client("complete", token_override=token))
    aggregates, offset = {}, 0
    while True:
        response = _request_or_raise_sales_funnel_products(client, WbApiRequest(method="POST", path=SOURCE, jsonBody={
            "selectedPeriod": {"start": str(start), "end": str(end)},
            "nmIds": [], "skipDeletedNm": False, "limit": 1000, "offset": offset}))
        body = response.get("data") or response
        products = body.get("products")
        if not isinstance(products, list):
            raise ValueError("WB_FUNNEL_INVALID_RESPONSE")
        for product in products:
            nm = (product.get("product") or {}).get("nmId")
            if type(nm) is not int or nm <= 0 or str(nm) in aggregates:
                raise ValueError("WB_FUNNEL_PAGINATION_INVALID")
            selected = (product.get("statistic") or {}).get("selected") or {}
            if selected.get("period") and selected["period"] != {"start": str(start), "end": str(end)}:
                raise ValueError("WB_FUNNEL_PERIOD_MISMATCH")
            values = normalized_metrics(product)
            if body.get("currency") != "RUB":
                values.update(orderAmount=None, buyoutAmount=None)
            aggregates[str(nm)] = values
        if len(products) < 1000:
            break
        offset += 1000
        if offset > 1_000_000:
            raise ValueError("WB_FUNNEL_PAGINATION_INVALID")
    return store.save_source_cache(org, f"week_funnel_{connection_key(token)}_{start}_{end}", dict(
        aggregates=aggregates, sourceComplete=True, dateFrom=str(start), dateTo=str(end)), strict=True)


def allocate_kopecks(total, weights):
    """Signed largest-remainder allocation, deterministic even on equal weights."""
    weights = {str(nm): int(units) for nm, units in weights.items() if type(units) is int and units > 0}
    denominator = sum(weights.values())
    if not denominator:
        return {}, total
    sign, absolute = (-1 if total < 0 else 1), abs(total)
    result = {nm: absolute * weight // denominator for nm, weight in weights.items()}
    ranked = sorted(weights, key=lambda nm: (-(absolute * weights[nm] % denominator), int(nm)))
    for nm in ranked[:absolute - sum(result.values())]:
        result[nm] += 1
    return {nm: sign * amount for nm, amount in result.items()}, 0


def storage_allocation(finance, histories, start, end):
    from app.wb_sales_funnel import days
    known, unallocated, allocated_days, missing_days, daily_amounts = {}, 0, [], [], {}
    daily = finance.get("storageDailyKopecks")
    total = sum(bucket.get("storageKopecks", 0) for bucket in finance.get("amounts", {}).values())
    unknown = any("storageKopecks" in fields for fields in finance.get("unknown", {}).values())
    if unknown or finance.get("sourceComplete") is not True:
        return dict(amounts={}, unallocatedKopecks=total if not unknown else None,
                    complete=False, observedDates=[], missingDates=sorted(map(str, days(start, end))))
    def weights(day):
        snapshot = histories.get(str(day)) or {}
        rows = snapshot.get("aggregates")
        # Snapshots from the all-product collector are complete unless marked
        # partial. Invalid/missing physical quantities cannot prove a denominator.
        if not isinstance(rows, dict) or snapshot.get("sourceComplete") is False:
            return None
        if any(type(row.get("wbStockUnits")) is not int or row["wbStockUnits"] < 0 for row in rows.values()):
            return None
        return {nm: row["wbStockUnits"] for nm, row in rows.items() if str(nm).isdigit() and int(nm) > 0}
    if daily is None:
        # Legacy period total: full historical coverage is required for unit-days.
        per_day = [weights(day) for day in sorted(days(start, end))]
        if any(row is None for row in per_day):
            return dict(amounts={}, unallocatedKopecks=total, complete=False,
                        observedDates=[str(day) for day in sorted(days(start, end)) if weights(day) is not None],
                        missingDates=[str(day) for day in sorted(days(start, end)) if weights(day) is None])
        unit_days = defaultdict(int)
        for row in per_day:
            for nm, units in row.items():
                unit_days[nm] += units
        known, unallocated = allocate_kopecks(total, unit_days)
        return dict(amounts=known, unallocatedKopecks=unallocated, complete=not unallocated,
                    observedDates=sorted(map(str, days(start, end))), missingDates=[])
    for day in sorted(days(start, end)):
        weight = weights(day)
        if weight is not None:
            allocated_days.append(str(day))
        elif daily.get(str(day), 0):
            missing_days.append(str(day))
        amounts, residual = allocate_kopecks(daily.get(str(day), 0), weight or {})
        if amounts:
            daily_amounts[str(day)] = amounts
        unallocated += residual
        for nm, value in amounts.items():
            known[nm] = known.get(nm, 0) + value
    return dict(amounts=known, unallocatedKopecks=unallocated, complete=not missing_days and not unallocated,
                observedDates=allocated_days, missingDates=missing_days, dailyAmounts=daily_amounts)


def load_ads(org, start, end):
    from app.wb_reports_sprint_d import _period_cache
    cache = _period_cache(org, "ads", start, end)
    if cache.get("dateFrom") != str(start) or cache.get("dateTo") != str(end) or cache.get("source") != "wb_ads_fullstats":
        return {}, False, None
    daily = cache.get("dailyAggregates") or {}
    wanted = {(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)}
    complete = cache.get("sourceComplete") is not False and wanted <= set(daily)
    amounts, unallocated = {}, 0
    for nm, row in cache.get("aggregates", {}).items():
        value = row.get("adSpendKopecks")
        if str(nm).isdigit() and int(nm) > 0:
            amounts[str(nm)] = value
        elif value is not None:
            unallocated += value
    return amounts, complete, unallocated if complete else None


def attach(report, org, start, end, previous_start, previous_end, *, finance_allowed, catalog, include_previous=True):
    from app.wb_week_finance import load_finance, product_buyouts
    from app.wb_sales_funnel import days
    revision = source_revision(org, previous_start, end)
    windows = (("selected", start, end), ("previous", previous_start, previous_end))
    funnels = [load_funnel(org, left, right) if label == "selected" or include_previous else
        {"metrics": {}, "complete": False, "needsCollection": True, "missingDates": sorted(map(str, days(left, right)))}
        for label, left, right in windows]
    ads = [load_ads(org, left, right) if finance_allowed and (label == "selected" or include_previous) else ({}, False, None) for label, left, right in windows]
    finances = [load_finance(org, left, right) if finance_allowed and (label == "selected" or include_previous) else {} for label, left, right in windows]
    from app.wb_storage import allocate, load_stock
    snapshot = load_stock(org) if finance_allowed else {}
    storage = []
    for finance in finances:
        fields = [bucket.get("storageKopecks", 0) for bucket in finance.get("amounts", {}).values()]
        unknown = any("storageKopecks" in values for values in finance.get("unknown", {}).values())
        total = sum(fields) if (fields or finance.get("sourceComplete") is True) \
            and all(type(value) is int for value in fields) and not unknown else None
        storage.append(allocate(total, snapshot, financial_complete=finance.get("sourceComplete") is True))
    rows = report.setdefault("rows", [])
    existing = {str(row.get("nmId")) for row in rows}
    ids = set().union(*(set(f["metrics"]) for f in funnels), *(set(a[0]) for a in ads),
        *(set(s["amounts"]) for s in storage), *(set(f.get("buyouts", {})) - {"0"} for f in finances))
    for nm in sorted(ids - existing, key=int):
        if not nm.isdigit() or int(nm) <= 0:
            continue
        meta = catalog.get(int(nm), {})
        rows.append(dict(nmId=int(nm), sku=meta.get("sku") or f"NM_{nm}", productName=meta.get("productName"), photoUrl=meta.get("photoUrl")))
    def pair(a, b, key):
        return {key: a, "previous" + key[0].upper() + key[1:]: b,
                "deltaPct": round((a-b) / abs(b) * 100, 1) if a is not None and b not in (None, 0) else None}
    for row in rows:
        nm = str(row.get("nmId"))
        now, before = (f["metrics"].get(nm, {}) for f in funnels)
        row['funnelPopulation'] = {label: nm in funnel['metrics'] or not funnel.get('populationComplete', False)
            for (label, _, _), funnel in zip(windows, funnels)}
        row["orders"] = pair(now.get("orderCount"), before.get("orderCount"), "units") | {
            "kopecks": now.get("orderSumKopecks"), "previousKopecks": before.get("orderSumKopecks")}
        current_buyouts, previous_buyouts = (product_buyouts(finance, nm) for finance in finances)
        row["sales"] = pair(current_buyouts["units"], previous_buyouts["units"], "units") | {
            "kopecks": current_buyouts["kopecks"], "previousKopecks": previous_buyouts["kopecks"]}
        if not all(finance.get("sourceComplete") is True for finance in finances):
            row["sales"]["deltaPct"] = None
        row["baskets"] = pair(now.get("cartCount"), before.get("cartCount"), "units")
        amounts = [a[0].get(nm, 0 if a[1] and not a[2] else None) for a in ads]
        row["campaignAdSpendKopecks"] = pair(*amounts, "kopecks")
        # adSpendKopecks was attached from Finance; never overwrite it with
        # Promotion fullstats, whose dates and attribution are different.
        allocations, storage_coverage = [], []
        for s, finance in zip(storage, finances):
            complete = s['complete'] and finance.get('sourceComplete') is True
            value = s['amounts'].get(nm, 0) if complete else None
            allocations.append(value)
            storage_coverage.append('complete' if complete else 'missing')
        row["storageKopecks"] = pair(*allocations, "kopecks")
        row['storageCoverage'] = dict(zip(('selected', 'previous'), storage_coverage))
        if any(status != 'complete' for status in storage_coverage):
            row["storageKopecks"]["deltaPct"] = None
        row["storageAttribution"] = "current_wb_stock"
    columns = report.setdefault('columns', [])
    if not any(column.get('key') == 'campaignAdSpendKopecks' for column in columns):
        columns.append({'key': 'campaignAdSpendKopecks', 'label': 'Расход кампаний, ₽'})
    for column in report.get("columns", []):
        if column.get("key") in {"sales", "orders"}:
            column["label"] = "Выкупили" if column["key"] == "sales" else "Заказы"
    # The shell is built before source attachment; do not leave its chart with
    # the old funnel buyouts when the table has the financial values.
    for point, row in zip(report.get("chart", {}).get("points", []), rows):
        point["sales"] = deepcopy(row["sales"])
    report["operationalSourceRevision"] = revision
    report["metricCoverage"] = {label: dict(funnelComplete=funnel["complete"], funnelNeedsCollection=funnel.get("needsCollection", not funnel["complete"]), funnelMissingDates=funnel["missingDates"], advertisingComplete=advertising[1],
        funnelPopulationComplete=funnel.get('populationComplete'), funnelDataComplete=funnel.get('dataComplete'),
        funnelUnresolvedReasons=funnel.get('unresolvedReasons', {}))
        for (label, _, _), funnel, advertising in zip(windows, funnels, ads)}
    report["storageAllocation"] = {label: {key: value for key, value in allocation.items() if key not in {"amounts", "dailyAmounts"}}
        for (label, _, _), allocation in zip(windows, storage)}
    for (label, _, _), advertising, allocation in zip(windows, ads, storage):
        report["unallocatedFinancials"][label].update(storageKopecks=allocation["unallocatedKopecks"])
    report["unallocatedCampaignSpend"] = {label: advertising[2] for (label, _, _), advertising in zip(windows, ads)}
    account_values = report.get("unallocatedFinancials", {})
    account_buyouts = [product_buyouts(finance, "0") for finance in finances]
    if finance_allowed and (any(type(value) is int and value for group in account_values.values() for value in group.values())
            or any(type(value) is int and value for group in account_buyouts for value in group.values())):
        from app.wb_week_finance import FIELDS
        account_row = dict(nmId=None, sku=None, isUnallocated=True,
            productName="Нераспределённые начисления WB", conclusion="Операции кабинета без nmId; не распределены по выручке.")
        comparison_known = all(finance.get("sourceComplete") is True for finance in finances)
        for field in FIELDS:
            account_row[field] = pair(account_values.get("selected", {}).get(field),
                account_values.get("previous", {}).get(field), "kopecks")
            if not comparison_known:
                account_row[field]["deltaPct"] = None
        account_row["sales"] = pair(account_buyouts[0]["units"], account_buyouts[1]["units"], "units") | {
            "kopecks": account_buyouts[0]["kopecks"], "previousKopecks": account_buyouts[1]["kopecks"]}
        if not comparison_known:
            account_row["sales"]["deltaPct"] = None
        rows.append(account_row)
    warnings = []
    for (label, _, _), funnel, advertising, allocation in zip(windows, funnels, ads, storage):
        title = "Выбранный период" if label == "selected" else "Предыдущий период"
        if not funnel["complete"]:
            warnings.append(f"{title}: показатели воронки неполные или сохранены ранее; прочерк не означает ноль.")
        reason_labels = {'source_fields_missing': 'WB не передал обязательные поля',
                         'source_product_omitted': 'товары отсутствуют в ответе воронки; ноль не подтверждён',
                         'incomplete_intervals': 'нет совместимого полного покрытия периода',
                         'connection_missing': 'нет подключения WB'}
        for reason, count in funnel.get('unresolvedReasons', {}).items():
            warnings.append(f"{title}: {reason_labels[reason]} ({count}).")
        if finance_allowed and not advertising[1]:
            warnings.append(f"{title}: рекламная статистика неполная.")
        if finance_allowed:
            observed = allocation.get("stockObservedAt") or "нет полного снимка"
            warnings.append(f"{title}: хранение распределяется по количеству товаров на WB; снимок остатков: {observed}."
                            + (" Известная сумма сохранена на уровне кабинета до полного распределения." if not allocation["complete"] else ""))
    report["warning"] = " ".join(filter(None, [report.get("warning"), *warnings]))
    return report
