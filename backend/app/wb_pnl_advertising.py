"""P&L-only campaign evidence; never relabel a wider total as a selected total."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone


def _instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def _source_context(org):
    """Legacy org caches are reusable only after the current single binding."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.cabinet.store import get_organization_wb_token_secret
    token = get_organization_wb_token_secret(org)
    if not token:
        return None
    try:
        return _read_source_context(org, token)
    except SQLAlchemyError:
        return None  # No fallback to an unverifiable account's financial data.


def _read_source_context(org, token):
    from sqlalchemy import select
    from app.infra.db import get_session_factory, set_tenant_context
    from app.cabinet.orm import LkUserRow, LkUserWbTokenRow
    from app.platform.integrations.orm import MarketplaceAccountRow
    from app.platform.integrations.wb_credentials import resolve_bound_wb_credential, WbCredentialBindingError
    from app.repricer_page_finance import connection_key
    with get_session_factory()() as session:
        set_tenant_context(session, org)
        accounts = session.scalars(select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == org,
            MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all()
        updated = session.scalar(select(LkUserWbTokenRow.updated_at).join(LkUserRow,
            LkUserRow.user_id == LkUserWbTokenRow.user_id).where(
            LkUserWbTokenRow.organization_id == org, LkUserRow.organization_id == org, LkUserRow.is_active.is_(True))
            .order_by(LkUserWbTokenRow.updated_at.desc(), LkUserWbTokenRow.created_at.desc()).limit(1))
        if len(accounts) != 1 or updated is None:
            return None
        account = accounts[0]
        if account.credential_ref:
            try:
                resolve_bound_wb_credential(session, org,
                    marketplace_account_id=account.marketplace_account_id, wb_token=token)
            except WbCredentialBindingError:
                return None
        return dict(connection=connection_key(token), marketplaceAccountId=account.marketplace_account_id,
            bindingVersion=account.ingestion_binding_version,
            # Generic account metadata edits are not credential changes. A
            # verified binding above ties pre-verification reads to this token.
            connectedAt=max(_instant(updated), _instant(account.created_at)).isoformat())


def _prefix(context):
    return f"pnl_ads_v2_{context['connection']}_{context['marketplaceAccountId']}_{context['bindingVersion']}_"


def _candidate_caches(org, start, end, context):
    from app.repricer_cache.store import get_source_cache, list_source_cache_ranges_by_prefix
    keys = [_prefix(context) + f"{start}_{end}", f"ads_{start}_{end}"]
    seen = set(keys)
    for key in keys:
        yield get_source_cache(org, key, slim=False) or {}
    for prefix in (_prefix(context), "ads_"):
        for meta in list_source_cache_ranges_by_prefix(org, prefix, limit=200):
            left, right = str(meta.get("dateFrom") or "")[:10], str(meta.get("dateTo") or "")[:10]
            if left and right and left <= str(end) and right >= str(start):
                key = meta["sourceKey"]
                if key not in seen:
                    seen.add(key)
                    yield get_source_cache(org, key, slim=False) or {}


def _valid(cache, context):
    if (cache.get("source") != "wb_ads_fullstats" or not _instant(cache.get("fetchedAt"))
            or not isinstance(cache.get("aggregates"), dict)):
        return False
    try:
        if date.fromisoformat(cache["dateFrom"]) > date.fromisoformat(cache["dateTo"]):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    scope = cache.get("pnlAdvertisingContext")
    if scope is not None:
        return scope == context and cache.get("pnlCampaignReceiptVersion") == 2
    # Older collectors are single-account/org scoped. Reject observations from
    # before a credential/binding change or an explicitly foreign account.
    return (cache.get("marketplaceAccountId", context["marketplaceAccountId"]) == context["marketplaceAccountId"]
            and _instant(cache["fetchedAt"]) >= _instant(context["connectedAt"]))


def _complete(cache):
    return (cache.get("sourceComplete") is not False and cache.get("coverageState") in (None, "ok")
            and not cache.get("missingDates")
            and not (type(cache.get("coveredDays")) is int and type(cache.get("requestedDays")) is int
                     and cache["coveredDays"] < cache["requestedDays"]))


def _cabinet_total(cache):
    value = (cache.get("campaignTotals") or {}).get("ad_spend_kopecks")
    if type(value) is int:
        return value
    # Legacy `totals` sums SKU rows, not authoritative account/campaign totals.
    return 0 if cache.get("campaignCount") == 0 and _complete(cache) else None


def _daily_total(cache, day):
    totals = cache.get("dailyCampaignTotals") or {}
    value = (totals.get(day) or {}).get("ad_spend_kopecks")
    if type(value) is int:
        return value
    # Infer omitted zero days only if the entire completed daily series already
    # reconciles to the authoritative period total, including residual spend.
    observed = [row.get("ad_spend_kopecks") for row in totals.values() if isinstance(row, dict)]
    if (_complete(cache) and _cabinet_total(cache) is not None
            and all(type(v) is int for v in observed) and sum(observed) == _cabinet_total(cache)):
        return 0
    return None


def _spend(cache):
    aggregates = cache.get("aggregates") or {}
    values = [row.get("adSpendKopecks") if isinstance(row, dict) else None for row in aggregates.values()]
    total = (cache.get("totals") or {}).get("adSpendKopecks")
    valid = all(type(value) is int for value in values)
    product_total = sum(row["adSpendKopecks"] for nm, row in aggregates.items()
        if str(nm).isdigit() and int(nm) > 0) if valid else None
    complete = _complete(cache) and bool(cache.get("fetchedAt"))
    residual = total - product_total if type(total) is int and product_total is not None else None
    return dict(state="ok" if complete and valid and type(total) is int else "partial" if cache else "no_data",
        aggregates=aggregates, adSpendKopecks=total, productCoverageComplete=complete,
        collectionComplete=complete and type(total) is int,
        missingProductsZero=complete and valid and type(total) is int and
            (residual == 0 or cache.get("pnlRoundingAttributionComplete") is True),
        unallocatedSpendKopecks=residual)


def _rounding_attribution(report):
    """Prove rounding from individually reconciled, explicitly attributed campaigns.

    Each source money observation is rounded to kopecks. The sum of N such
    observations and one rounded campaign total can differ by at most
    ceil((N + 1) / 2) kopecks. Never apply a cabinet-wide arbitrary tolerance.
    Amounts and the residual remain unchanged; this only establishes that an
    absent product has zero spend, rather than unknown unallocated spend.
    """
    roots = report.get("rows") or []
    observations = report.get("dailyRows") or []
    by_campaign, by_day = {}, {}
    for row in observations:
        value, sku = row.get("adSpendKopecks"), str(row.get("skuId") or "")
        if type(value) is not int or value < 0 or (value != 0 and (not sku.isdigit() or int(sku) <= 0)):
            return {}
        campaign, day = str(row.get("campaignId") or ""), str(row.get("date") or "")[:10]
        by_campaign.setdefault(campaign, []).append(value)
        by_day.setdefault(day, []).append((campaign, value))
    if not roots:
        return {}
    seen, root_total = set(), 0
    for row in roots:
        campaign, value = str(row.get("campaignId") or ""), row.get("adSpendKopecks")
        values = by_campaign.get(campaign, [])
        if (campaign in seen or type(value) is not int or value < 0
                or (value != 0 and (row.get("attributionLevel") != "exact_sku" or not values))
                or abs(value - sum(values)) > (len(values) + 2) // 2):
            return {}
        seen.add(campaign)
        root_total += value
    if (set(by_campaign) - seen or root_total != (report.get("performanceTotals") or {}).get("ad_spend_kopecks")):
        return {}
    days = {}
    for day, metrics in (report.get("dailyPerformanceTotals") or {}).items():
        values = by_day.get(day, [])
        total = metrics.get("ad_spend_kopecks")
        # One independently rounded total per observed campaign, plus each SKU.
        bound = (len(values) + len({campaign for campaign, _ in values}) + 1) // 2
        days[day] = type(total) is int and abs(total - sum(v for _, v in values)) <= bound
    return dict(pnlRoundingAttributionComplete=True, pnlRoundingAttributionDays=days)


def load_campaign_spend(organization_id, start, end):
    from app.wb_api.source_freshness import source_is_fresh
    context = _source_context(organization_id)
    if context is None:
        return {}, _spend({})
    caches, candidates = [], []
    for cache in _candidate_caches(organization_id, start, end, context):
        if not _valid(cache, context):
            continue
        caches.append(cache)
        if cache.get("dateFrom") == str(start) and cache.get("dateTo") == str(end):
            candidate = deepcopy(cache)
            candidate["totals"] = {"adSpendKopecks": _cabinet_total(cache)}
            spend = _spend(candidate)
            if spend["collectionComplete"] and source_is_fresh(candidate):
                return candidate, spend  # No neighbouring heavy payload reads.
            candidates.append(candidate)
    days = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    daily, totals, observed, rounding_days = {}, [], [], {}
    ordered = sorted(caches, key=lambda c: (_complete(c), _instant(c["fetchedAt"])), reverse=True)
    for day in days:
        for cache in ordered:
            rows = (cache.get("dailyAggregates") or {}).get(day)
            if rows is None and cache.get("campaignCount") == 0 and _complete(cache):
                rows = {}  # A confirmed no-campaign period also proves its subperiods.
            if (not _complete(cache) or not isinstance(rows, dict)
                    or not str(cache.get("dateFrom", "z")) <= day <= str(cache.get("dateTo", ""))):
                continue
            daily[day] = rows
            rounding_days[day] = (cache.get("pnlRoundingAttributionDays") or {}).get(day) is True
            totals.append(_daily_total(cache, day))
            observed.append(_instant(cache["fetchedAt"]))
            break
    if daily:
        aggregates = {}
        for rows in daily.values():
            for nm, row in rows.items():
                if not isinstance(row, dict):
                    continue
                bucket = aggregates.setdefault(str(nm), {"adSpendKopecks": 0})
                value = row.get("adSpendKopecks")
                bucket["adSpendKopecks"] = bucket["adSpendKopecks"] + value if type(value) is int and bucket["adSpendKopecks"] is not None else None
        complete = len(daily) == len(days)
        candidates.append(dict(source="wb_ads_fullstats", dateFrom=str(start), dateTo=str(end),
            aggregates=aggregates, dailyAggregates=daily, fetchedAt=min(observed).isoformat(),
            sourceComplete=complete, coverageState="ok" if complete else "partial",
            missingDates=[day for day in days if day not in daily],
            pnlRoundingAttributionComplete=complete and all(rounding_days.values()),
            pnlRoundingAttributionDays=rounding_days,
            totals={"adSpendKopecks": sum(totals) if complete and all(type(v) is int for v in totals) else None}))
    if not candidates:
        return {}, _spend({})
    cache = max(candidates, key=lambda c: (_spend(c)["state"] == "ok", _complete(c), _instant(c["fetchedAt"])))
    return cache, _spend(cache)


def missing_campaign_ranges(organization_id, start, end, *, fresh_only=False):
    from app.wb_api.source_freshness import source_is_fresh
    cache, spend = load_campaign_spend(organization_id, start, end)
    return ([(start, end)] if not spend["collectionComplete"] or
            (fresh_only and not source_is_fresh(cache)) else [])


def product_spend(spend, nm_id):
    if nm_id is None:
        return spend["unallocatedSpendKopecks"]
    row = spend["aggregates"].get(str(nm_id), {})
    absent = spend["state"] == "ok" and spend.get("missingProductsZero", spend["unallocatedSpendKopecks"] == 0)
    value = row.get("adSpendKopecks", 0 if absent else None)
    return value if type(value) is int else None


def collect_campaign_spend(org, token, start, end, progress):
    """Reuse the Advertising loader/checkpoints, without budgets or Ads-page writes."""
    from app.routers import wb_reports_bff as reports
    from app.wb_report_ads import campaign_performance_cache
    from app.repricer_cache.store import save_source_cache
    from app.repricer_page_finance import connection_key
    from app.repricer_tasks import ReportSourceRateLimited
    context = _source_context(org)
    if context is None or context["connection"] != connection_key(token):
        raise RuntimeError("WB connection changed; reopen P&L")
    if not missing_campaign_ranges(org, start, end, fresh_only=True):
        return  # Another queued report may already have completed this range.
    snapshot = reports.build_ads_attribution_snapshot(date_from=start, date_to=end,
        group_by="campaign", wb_token=token, include_budgets=False, performance_only=True, progress_callback=progress)
    report = reports._map_ads_to_report_response({"from": str(start), "to": str(end), "preset": "custom"}, snapshot)
    payload = campaign_performance_cache(start, end, report)
    if payload is None:
        failures = [item for item in (snapshot.diagnostics or {}).get("sources", []) if item.get("status") == "blocked"]
        limited = [item for item in failures if item.get("statusCode") == 429]
        if limited:
            delay = max(20, *[int((item.get("rateLimit") or {}).get("retryAfterSeconds") or 20) for item in limited]) + 1
            raise ReportSourceRateLimited("WB ограничил рекламу P&L; продолжим с сохранённых пакетов", delay)
        raise RuntimeError("WB не вернул полную статистику кампаний для P&L")
    if _source_context(org) != context:
        raise RuntimeError("WB connection changed during P&L advertising collection")
    # The Ads UI mapper expands campaign parents into SKU rows and residuals.
    # Reconciliation must use the original, unexpanded provider campaign roots.
    attribution = {**report, "rows": [dict(campaignId=row.campaign_id,
        adSpendKopecks=row.ad_spend_kopecks, attributionLevel=row.attribution_level)
        for row in getattr(snapshot, "rows", [])]}
    save_source_cache(org, _prefix(context) + f"{start}_{end}",
        {**payload, **_rounding_attribution(attribution), "pnlAdvertisingContext": context,
         "pnlCampaignReceiptVersion": 2}, strict=True)
