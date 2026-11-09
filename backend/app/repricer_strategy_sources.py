"""Required WB strategy windows, using the existing account cache and read jobs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.repricer_cache.store import get_source_cache
from app.repricer_price_safety import utc_timestamp
from app.wb_api.source_freshness import source_is_fresh
from app.repricer_source_provenance import (
    oldest_source_observation, source_binding_matches, source_observations_fresh,
)


def prepare_strategy_rows(rows, *, org, token, algorithm, default_days, now, queue_missing=False, article_ids=None):
    from app.repricer_bff import FRONTEND_STRATEGY_BY_ID
    from app.routers.wb_repricer_bff import _period_source_cache
    from app.repricer_sync import _period_cache_suffix, wb_token_fingerprint
    from app.repricer_page_finance import connection_key
    from app.repricer_cache.store import finance_cache_uses_current_revenue_basis
    fingerprint = wb_token_fingerprint(token)
    connection = connection_key(token) if token else None
    today = now.astimezone(ZoneInfo("Europe/Moscow")).date()
    end = today - timedelta(days=1)
    selected = set(article_ids) if article_ids is not None else {row["meta"]["articleId"] for row in rows}
    eligible = [row for row in rows if row.get("meta", {}).get("articleId") in selected
        and row.get("settings", {}).get("automationEnabled", True)
        and row.get("meta", {}).get("status") not in {"manual", "warmup"}
        and FRONTEND_STRATEGY_BY_ID.get((row.get("strategy") or {}).get("id"), {}).get("status") in {"active", "special"}
        and (row.get("strategy", {}).get("assignmentSource") or row.get("meta", {}).get("assignmentSource")) in {"manual", "xlsx"}]
    if not eligible:
        return rows
    selected = {row["meta"]["articleId"] for row in eligible}
    group_members = {str(article) for row in eligible if row.get("strategy", {}).get("id") == "plan_fact_group"
        for article in (row.get("strategy", {}).get("config") or {}).get("groupArticleIds", [])}
    memo, missing, evidence = {}, set(), {}
    dependencies = set()

    def cached(prefix, days, last=end):
        first = last - timedelta(days=days - 1)
        key = (prefix, first, last)
        if key not in memo:
            suffix = _period_cache_suffix(days, date_from=first, date_to=last)
            payload = _period_source_cache(org, prefix, suffix, days,
                datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.min.time()),
                slim=False, require_full_sync_coverage=False, prefer_freshest_covering=True,
                strategy_fingerprint=fingerprint, strategy_now=now)
            valid = (source_binding_matches(payload, organization_id=org, fingerprint=fingerprint, connection_key=connection)
                and source_observations_fresh(payload, now=now, date_to=last.isoformat())
                and (prefix != "finance" or finance_cache_uses_current_revenue_basis(payload))
                and str(payload.get("dateFrom"))[:10] == first.isoformat()
                and str(payload.get("dateTo"))[:10] == last.isoformat()
                and payload.get("coverageState", "ok") in {"ok", "complete"}
                and payload.get("aggregateComplete", True) is True)
            if not valid:
                missing.add((prefix, first.isoformat(), last.isoformat()))
                payload = {}
            else:
                at = oldest_source_observation(payload)
                evidence[key] = {"source": prefix, "fetchedAt": at.isoformat(),
                    "staleAfter": (at + timedelta(hours=1)).isoformat(),
                    "periodFrom": first.isoformat(), "periodTo": last.isoformat()}
            memo[key] = payload
        return memo[key]

    def facts(prefix, days, nm, last=end):
        payload = cached(prefix, days, last)
        dependencies.add((prefix, last - timedelta(days=days - 1), last))
        value = (payload.get("aggregates") or {}).get(str(nm)) or {}
        count = value.get("orderCount" if prefix == "baskets" else "ordersUnits")
        valid = type(count) is int and count >= 0 and value.get("coverageState", "ok") in {"ok", "complete"}
        return {"ordersUnits": count if valid else None, "periodDays": days,
            "periodFrom": (last - timedelta(days=days - 1)).isoformat(), "periodTo": last.isoformat(),
            "state": "ok" if payload and valid else "no_data", "source": prefix,
            "observedAt": payload.get("fetchedAt")}, value

    stock = get_source_cache(org, "stocks", slim=False) or {}
    stock_valid = (stock.get("strategyTokenFingerprint") == fingerprint
        and source_is_fresh({**stock, "dateTo": today.isoformat()}, now=now))
    if not stock_valid:
        missing.add(("stocks", today.isoformat(), today.isoformat()))
    else:
        at = utc_timestamp(stock.get("fetchedAt"))
        evidence["stocks"] = {"source": "stocks", "fetchedAt": at.isoformat(),
            "staleAfter": (at + timedelta(hours=1)).isoformat()}
    group_days = max(1, int(algorithm.get("planFactFactPeriodDays") or default_days))
    financial_rows = None
    groups_used = any((row.get("strategy") or {}).get("id") == "plan_fact_group" for row in eligible)
    prepared = []
    for original in rows:
        if original.get("meta", {}).get("articleId") not in selected | group_members:
            prepared.append(original)
            continue
        dependencies = {"stocks"}
        row = deepcopy(original)
        analytics, meta = row.setdefault("analytics", {}), row.get("meta") or {}
        meta.setdefault("pricingSettingsRevision", "unverified")
        nm, strategy = meta.get("nmId"), (row.get("strategy") or {}).get("id")
        observation = (stock.get("aggregates") or {}).get(str(nm)) or {}
        units = observation.get("wbStockUnits") if stock_valid else None
        analytics.update(wbStockUnits=units, stockState="ok" if type(units) is int and units >= 0 else "no_data",
            stockObservedAt=stock.get("fetchedAt") if stock_valid else None)
        if groups_used:
            group_fact, _ = facts("baskets", group_days, nm)
            analytics["groupFact"] = group_fact
            if strategy != "plan_fact_group":
                dependencies.discard(("baskets", end - timedelta(days=group_days - 1), end))
        if strategy in {"optimal_price", "metric_dynamics"}:
            if meta.get("articleId") not in selected:
                prepared.append(row)
                continue
            main, _ = facts("baskets", 3 if strategy == "optimal_price" else 1, nm)
            comparison, _ = facts("baskets", 7, nm)
            analytics["strategyPeriods"] = {"main": main, "comparison": comparison}
        elif meta.get("articleId") in selected and strategy not in {"schedule", "night_price_mode", "cross_marketplace", "bundles", None}:
            days = (3 if strategy == "plan_fact_daily" else group_days if strategy in {"plan_fact_period", "plan_fact_group"}
                else max(1, int(algorithm.get("basketNormPeriodDays") or 7)) if strategy == "baskets_orders"
                else max(1, int(default_days)))
            period, value = facts("baskets", days, nm)
            analytics.update(ordersUnits=period["ordersUnits"], ordersSource="sales_funnel.orderCount",
                periodDays=days, periodFrom=period["periodFrom"], periodTo=period["periodTo"],
                baskets=value.get("cartCount"), basketsState=period["state"], basketsObservedAt=period["observedAt"])
            analytics.setdefault("periodCoverageStates", {})["basketsState"] = period["state"]
            if strategy == "plan_fact_interval":
                history, value = facts("period_stats", days, nm)
                current, current_value = facts("period_stats", 1, nm, today)
                analytics.update(hourlyOrders=value.get("hourlyOrders") if history["state"] == "ok" else None,
                    todayHourlyOrders=current_value.get("hourlyOrders") if current["state"] == "ok" else None,
                    todayHourlyObservedAt=current["observedAt"])
            if strategy == "plan_fact_period" and algorithm.get("planFactMetric") in {"revenue", "margin"}:
                finance = cached("finance", days)
                dependencies.add(("finance", end - timedelta(days=days - 1), end))
                if finance and financial_rows is None:
                    from app.routers.wb_repricer_bff import _list_repricer_skus_from_cached_sources
                    first = end - timedelta(days=days - 1)
                    financial_rows = {item["meta"]["articleId"]: item for item in _list_repricer_skus_from_cached_sources(
                        org, "complete", wb_token=token, period_days=days,
                        period_suffix=_period_cache_suffix(days, date_from=first, date_to=end),
                        range_start=datetime.combine(first, datetime.min.time()), range_end=datetime.combine(end, datetime.min.time()),
                        max_items=None, sort_by_demand=False, require_full_sync_coverage=False)}
                current_finance = ((financial_rows or {}).get(meta.get("articleId")) or {}).get("analytics") or {}
                if finance and current_finance:
                    for field in ("revenueKopecks", "marginKopecks", "financeState", "plannedMarginState"):
                        analytics[field] = current_finance.get(field)
                    analytics.setdefault("periodCoverageStates", {})["financeState"] = finance.get("coverageState", "ok")
                else:
                    analytics.update(revenueKopecks=None, marginKopecks=None, financeState="no_data")
        analytics["strategySourceEvidence"] = [deepcopy(evidence[key]) for key in dependencies if key in evidence]
        if meta.get("articleId") in selected and strategy != "illiquid" and algorithm.get("nightMedianEnabled", True) and (
            row.get("settings", {}).get("nightMedianEnabled", False) or algorithm.get("nightMedianAutoEnableAllSkus", False)
        ):
            night_days = max(1, int(algorithm.get("basketNormPeriodDays") or 7))
            period, value = facts("baskets", night_days, nm)
            analytics.update(nightBaskets=value.get("cartCount") if period["state"] == "ok" else None,
                nightBasketsObservedAt=period["observedAt"] if period["state"] == "ok" else None,
                nightBasketsState=period["state"])
            night_key = ("baskets", end - timedelta(days=night_days - 1), end)
            analytics["nightStrategySourceEvidence"] = [deepcopy(evidence[key]) for key in ("stocks", night_key) if key in evidence]
        prepared.append(row)
    if missing and queue_missing:
        queue_strategy_sources(org, fingerprint, sorted(missing))
    return prepared


def queue_strategy_sources(org, fingerprint, requests):
    from app import repricer_dispatch_store as ledger
    from app.infra.celery_app import celery_app
    digest = ledger._key("s", org, fingerprint, requests)
    now = datetime.now(timezone.utc)
    with ledger.transaction(org=org) as session:
        ledger._lock_runtime(session, org)
        old = ledger._row(session, digest, org)
        if old and (at := utc_timestamp(old.report_payload.get("queuedAt"))) and now - at < timedelta(minutes=5):
            return
        ledger._put(session, digest, org, {"state": "queued", "queuedAt": now.isoformat(),
            "requests": requests}, "strategy_sources")
    try:
        # Reuse the configured report reader queue, including the local
        # read-only queue. A general price-writing worker is not required.
        route = celery_app.amqp.router.route({}, "reports.refresh_budget_step")
        queue = route.get("queue")
        celery_app.send_task("repricer.prepare_strategy_sources", args=[org, fingerprint, requests],
            **({"queue": getattr(queue, "name", queue)} if queue is not None else {}))
    except Exception:
        with ledger.transaction(org=org) as session:
            ledger._put(session, digest, org, {"state": "queue_failed", "queuedAt": now.isoformat()}, "strategy_sources")
        # Price calculation remains blocked; a queue failure never supplies facts.
