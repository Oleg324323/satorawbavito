"""Read-only Statistics projection. Never changes repricing economics."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

AD_FIELDS = {
    "adSpendKopecks": "adSpendKopecks", "adImpressions": "impressions",
    "adClicks": "clicks", "adCartAdds": "cartAdds", "adOrders": "ordersCount",
    "adRevenueKopecks": "ordersKopecks",
}


def observed_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if Decimal(str(number)).is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def ratio(numerator, denominator, digits=1):
    num, den = observed_number(numerator), observed_number(denominator)
    if num is None or den is None or den <= 0:
        return None
    return float((Decimal(str(num)) / Decimal(str(den)) * 100).quantize(
        Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def ad_observation(row):
    """Accept the two persisted collector schemas without converting null to zero."""
    return {key: observed_number(row[key] if key in row else row.get(alias))
            for key, alias in AD_FIELDS.items()}


def project_row(row, ads_cache):
    nm = str((row.get("meta") or {}).get("nmId") or "")
    ads = (ads_cache.get("aggregates") or {}).get(nm)
    analytics = {**(row.get("analytics") or {}),
                 **ad_observation(ads or {}), "adDataAvailable": isinstance(ads, dict),
                 "adMetricsAvailable": isinstance(ads, dict),
                 "adsState": ("partial" if ads_cache.get("coverageState") == "partial" or ads_cache.get("missingDates") or ads_cache.get("sourceComplete") is False
                              else "ok") if isinstance(ads, dict) else "no_data"}
    return {**row, "analytics": analytics}


def account_ad_totals(cache):
    raw = cache.get("totals")
    if isinstance(raw, dict):
        return ad_observation(raw)
    raw = cache.get("campaignTotals")
    if isinstance(raw, dict):
        names = dict(zip(AD_FIELDS, ("ad_spend_kopecks", "impressions", "clicks", "cart_adds", "orders_count", "orders_kopecks")))
        return ad_observation({key: raw.get(name) for key, name in names.items()})
    # Aggregates are already grouped once per nmId by the collector. Include
    # historical/unallocated rows, but never add campaign parents to children.
    rows = [ad_observation(row) for row in (cache.get("aggregates") or {}).values() if isinstance(row, dict)]
    return {key: sum(row[key] for row in rows) if rows and all(row[key] is not None for row in rows) else None
            for key in AD_FIELDS}


def apply_account_totals(summary, cache):
    totals = account_ad_totals(cache)
    fields = {"impressions": "adImpressions", "clicks": "adClicks",
              "adCartAdds": "adCartAdds", "adSpendKopecks": "adSpendKopecks"}
    values = {field: totals[source] for field, source in fields.items()}
    values["ctrPct"] = ratio(totals["adClicks"], totals["adImpressions"])
    values["drrPct"] = ratio(totals["adSpendKopecks"], totals["adRevenueKopecks"], 2)
    partial = cache.get("coverageState") == "partial" or bool(cache.get("missingDates")) or cache.get("sourceComplete") is False
    for key, value in values.items():
        summary["metricTotals"][key].update(value=value, scope="account", partialPeriod=partial,
                                           source="wb_ads_fullstats")
    summary.update(impressions=values["impressions"], clicks=values["clicks"],
                   adCtrPct=values["ctrPct"], adSpendKopecks=values["adSpendKopecks"],
                   adRevenueKopecks=totals["adRevenueKopecks"], drrPct=values["drrPct"])


def compact_cache(payload):
    """Keep the exact-period result/provenance, not unused raw per-day copies."""
    return {key: value for key, value in payload.items()
            if key not in {"dailyAggregates", "chunks", "rows", "revenueOperations", "operations"}}
