"""Share completed campaign performance observations with other WB reports."""
from datetime import timedelta
from app.repricer_cache.store import save_source_cache


def campaign_performance_cache(start, end, report):
    """Normalize completed fullstats without publishing it to another consumer."""
    if not report.get("performanceComplete"):
        return None
    daily = {(start + timedelta(days=i)).isoformat(): {} for i in range((end-start).days+1)}
    aggregates = {}
    observed_fields = {}
    fields = ("adSpendKopecks", "impressions", "clicks", "cartAdds", "ordersCount", "ordersKopecks")
    for observation in report.get("dailyRows", []):
        day, nm = str(observation.get("date", ""))[:10], str(observation.get("skuId") or "")
        if day not in daily or not nm.isdigit() or int(nm) <= 0:
            continue
        for target in (daily[day], aggregates):
            bucket = target.setdefault(nm, dict(nmId=int(nm), **{key: None for key in fields}))
            seen = observed_fields.setdefault(id(bucket), set())
            for field in fields:
                if observation.get(field) is None or (field in seen and bucket[field] is None):
                    bucket[field] = None
                else:
                    bucket[field] = (bucket[field] or 0) + int(observation[field])
                seen.add(field)
    names = dict(zip(fields, ("ad_spend_kopecks", "impressions", "clicks", "cart_adds", "orders_count", "orders_kopecks")))
    totals = report.get("performanceTotals", {})
    daily_totals = report.get("dailyPerformanceTotals", {})
    for day, day_totals in daily_totals.items():
        if day not in daily:
            continue
        day_residual = {field: int(day_totals[name]) - sum(row.get(field) or 0 for row in daily[day].values())
                        for field, name in names.items() if day_totals.get(name) is not None}
        if any(day_residual.values()):
            daily[day]["campaign-unallocated"] = dict(nmId=0, campaignId="unallocated", **day_residual)
    residual = {field: int(totals[name]) - sum(row.get(field) or 0 for row in aggregates.values())
                for field, name in names.items() if totals.get(name) is not None}
    # Retain account/campaign amounts and rounding differences without inventing SKU attribution.
    if any(residual.values()):
        aggregates["campaign-unallocated"] = dict(nmId=0, campaignId="unallocated", **residual)
    return dict(
        dateFrom=str(start), dateTo=str(end), periodDays=(end-start).days+1,
        aggregates=aggregates, dailyAggregates=daily, sourceComplete=True,
        source="wb_ads_fullstats", campaignTotals=totals, dailyCampaignTotals=daily_totals,
        unallocatedPerformance=residual)


def save_campaign_performance(organization_id, start, end, report):
    payload = campaign_performance_cache(start, end, report)
    if payload is None:
        return False
    save_source_cache(organization_id, f"ads_{start}_{end}", payload)
    return True
