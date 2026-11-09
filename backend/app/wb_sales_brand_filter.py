"""Sales Dynamics brand projection from saved, account-scoped sources only."""
from types import SimpleNamespace
import math

from fastapi import HTTPException
from sqlalchemy import select

from app.infra.db import get_session_factory
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.wb_pnl_filters import _saved_cache
from app.wb_pnl_advertising import _source_context, _instant


def saved_catalog(org):
    context = _source_context(org)
    if context is None:
        raise HTTPException(409, "Нет однозначного подключённого аккаунта WB")
    catalog = {}
    with get_session_factory()() as session:
        for page in session.scalars(select(WbRepricerGoodsCacheRow).where(
                WbRepricerGoodsCacheRow.organization_id == org,
                WbRepricerGoodsCacheRow.fetched_at >= _instant(context["connectedAt"]))):
            for good in page.goods_payload or []:
                nm = good.get("nmID") or good.get("nmId")
                if nm:
                    catalog[str(nm)] = good
    for card in _saved_cache(org, "content_cards", context).get("cards") or []:
        nm = card.get("nmID") or card.get("nmId")
        if nm:
            catalog[str(nm)] = {**catalog.get(str(nm), {}), **card}
    return context, catalog


def available_brands(org):
    _, catalog = saved_catalog(org)
    return sorted({str(row["brand"]).strip() for row in catalog.values() if row.get("brand")}, key=str.casefold)


def project_sales_brands(actor, start, end, brands, *, finance_allowed):
    from app.routers import wb_reports_bff as reports
    org = actor.organization_id
    context, catalog = saved_catalog(org)
    selected = {str(brand).strip().casefold() for brand in brands if str(brand).strip()}
    if not selected or len(selected) > 50 or any(len(brand) > 200 for brand in selected):
        raise HTTPException(422, "Invalid sales brand selection")
    ids = {nm for nm, row in catalog.items() if str(row.get("brand") or "").strip().casefold() in selected}
    cache = _saved_cache(org, f"baskets_{start}_{end}", context)
    if str(cache.get("dateFrom"))[:10] != str(start) or str(cache.get("dateTo"))[:10] != str(end):
        cache = {}
    aggregates = cache.get("aggregates") or {}
    complete = (cache.get("aggregateComplete") is True and cache.get("sourceComplete") is not False
                and cache.get("coverageState") not in {"partial", "no_data"}
                and not cache.get("missingDates") and all(catalog.get(str(nm), {}).get("brand") for nm in aggregates))
    scoped = {**cache, "aggregates": {str(nm): row for nm, row in aggregates.items() if str(nm) in ids}}
    rows = reports._cached_funnel_rows_from_baskets_cache(scoped)
    totals = reports._funnel_rows_totals(rows)
    aliases = {
        "openCount": ("openCount", "openCard", "openCardCount", "views", "viewCount"),
        "cartCount": ("cartCount", "cartAdds", "addToCart", "addToCartCount", "basketCount", "baskets"),
        "orderCount": ("orderCount", "ordersCount", "orders"), "orderSumKopecks": ("orderSumKopecks", "ordersKopecks"),
        "buyoutCount": ("buyoutCount", "salesUnits"), "buyoutSumKopecks": ("buyoutSumKopecks", "salesKopecks", "revenueKopecks"),
        "cancelCount": ("cancelCount", "returnsUnits"),
    }
    def provided(row, keys):
        return any(isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
                   and math.isfinite(row[key]) for key in keys)

    for metric, keys in aliases.items():
        if any(not provided(row, keys) for row in scoped["aggregates"].values()):
            totals[metric] = None
    if not complete:
        totals = {key: None for key in totals}
    elif any(row.get("buyoutCountProvided") is not True for row in scoped["aggregates"].values()):
        totals.update(buyoutCount=None, buyoutSumKopecks=None, buyoutPct=None)
    if totals.get("buyoutCount") is None or totals.get("cancelCount") is None:
        totals["buyoutPct"] = None
    daily = {day: [row for row in values if str(row.get("nmId") or row.get("nmID")) in ids]
             for day, values in reports._cached_funnel_daily_rows(cache).items() if str(start) <= day <= str(end)}
    # Prevent the generic digest fallback from fabricating daily zeroes when
    # only the period aggregate is saved. Missing daily observations stay unknown.
    if not daily:
        daily = {str(start): None}
    finance = _saved_cache(org, f"finance_{start}_{end}", context) if finance_allowed else {}
    finance_daily = {day: {str(nm): row for nm, row in values.items() if str(nm) in ids}
                     for day, values in (finance.get("dailyAggregates") or {}).items() if isinstance(values, dict)}
    summary = {"marginKopecks": None, "returnsUnits": None}
    # Reuse already calculated product facts; never reallocate cabinet costs.
    if finance_allowed:
        from app.wb_reports_sprint_d import _get_cached_pnl_response, _pnl_report_cache_key, _complete_finance_ledger
        if _complete_finance_ledger(finance, start, end):
            key = _pnl_report_cache_key(date_from=start, date_to=end, group_by="sku", requested_state="preliminary", finance_allowed=True)
            pnl = _get_cached_pnl_response(organization_id=org, cache_key=key)
            if pnl is not None and pnl.sourceStatus != "stale":
                selected_rows = [row for row in pnl.rows if str(row.nmId) in ids]
                profits = [row.netProfitKopecks for row in selected_rows]
                required_ids = {str(nm) for nm in (finance.get("aggregates") or {}) if str(nm) in ids}
                if required_ids.issubset({str(row.nmId) for row in selected_rows}) and all(value is not None for value in profits):
                    summary["marginKopecks"] = sum(profits)
            source_rows = [row for nm, row in (finance.get("aggregates") or {}).items() if str(nm) in ids]
            if all(row.get("returnsUnits") is not None for row in source_rows):
                summary["returnsUnits"] = sum(row["returnsUnits"] for row in source_rows)
    snapshot = SimpleNamespace(orders=[], sales=[], stocks=[], source_status="cached" if complete else "partial", blocker_ids=[], revenue_by_nm_kopecks={})
    ads = SimpleNamespace(source_status="missing", totals={}, rows=[], blocker_ids=[])
    funnel = {"rows": rows, "totals": totals, "selectionComplete": complete, "dailyRows": daily,
              "financeDailyRows": finance_daily, "topMovers": reports._top_movers_from_cache(scoped, start, end),
              "status": "fresh" if complete else "partial"}
    payload = reports._build_digest_payload({"preset": "custom", "from": str(start), "to": str(end)},
        snapshot, ads, SimpleNamespace(rows=[], blockerIds=["WB-14A"]), funnel, summary)
    payload["availableBrands"] = sorted({str(row["brand"]).strip() for row in catalog.values() if row.get("brand")}, key=str.casefold)
    payload["brandScope"] = sorted(selected)
    payload["cache"] = {"status": "saved-brand-projection", "fresh": complete,
                        "requestedRange": {"from": str(start), "to": str(end)},
                        "marketplaceAccountId": context["marketplaceAccountId"]}
    from app.wb_api.source_freshness import source_is_fresh
    payload["cache"]["fresh"] = complete and source_is_fresh(cache)
    for kpi in payload["kpis"]:
        if kpi.get("id") == "oos_risk":
            kpi["value"] = "—"  # No saved brand-scoped warehouse evidence in this projection.
    if not complete or any(totals.get(key) is None for key in aliases):
        payload["warning"] = "Данные выбранных брендов неполны; общие итоги не подставляются."
    return payload
