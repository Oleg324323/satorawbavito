"""Advertising-only projection. No collection or allocation of campaign totals.

fullstats nmId observations are attribution, not proof of direct membership.
Current membership is independently supplied by adverts.nm_settings.
"""
from copy import deepcopy
from collections import defaultdict


def external_id(value):
    text = str(value or "").strip()
    return str(int(text)) if text.isascii() and text.isdigit() and int(text) > 0 else None


def enrich_advertising_products(report, catalog):
    """Join the saved WB catalog once by nmId, independently of the visited tab."""
    result = advertising_view(report)
    rows = []
    for source in result.get("rows") or []:
        row = dict(source)
        nm = external_id(row.get("nmId"))
        meta = catalog.get(int(nm), {}) if nm else {}
        for field in ("productName", "sku", "photoUrl", "brand", "category"):
            if meta.get(field):
                row[field] = meta[field]
        rows.append(row)
    return {**result, "rows": rows}


def advertising_view(report):
    if report.get("advertisingViewVersion") == 1:
        return report
    result = deepcopy(report)
    start = (report.get("filters", {}).get("dateRange", {}).get("from") or "")[:10]
    end = (report.get("filters", {}).get("dateRange", {}).get("to") or "")[:10]
    membership = report.get("campaignMembership") or {}
    by_campaign = defaultdict(dict)
    product_names = {}
    fields = {"impressions": "impressions", "clicks": "clicks", "baskets": "cartAdds",
              "adSpendKopecks": "adSpendKopecks", "ordersUnits": "ordersCount", "ordersKopecks": "ordersKopecks"}
    # Deduplicate identical cached observations; conflicting observations stay
    # visible as missing metrics instead of selecting an arbitrary amount.
    observations = {}
    conflicts = set()
    for item in report.get("dailyRows") or []:
        campaign = external_id(item.get("campaignId"))
        nm = external_id(item.get("skuId"))
        day = str(item.get("date") or "")[:10]
        if not campaign or not nm or not start <= day <= end:
            continue
        if item.get("productName"):
            product_names[nm] = item["productName"]
        key = campaign, nm, day, str(item.get("appType"))
        if key in observations and any(observations[key].get(field) != item.get(field) for field in fields.values()):
            conflicts.add(key)
        observations[key] = item
    for key, item in observations.items():
        campaign, nm, _, _ = key
        bucket = by_campaign[campaign].setdefault(nm, {field: 0 for field in fields})
        for target, source in fields.items():
            value = item.get(source)
            bucket[target] = None if key in conflicts or value is None or bucket[target] is None else bucket[target] + value

    rows = []
    for source in report.get("rows") or []:
        campaign = external_id(source.get("campaignId"))
        nm = external_id(source.get("nmId"))
        details = by_campaign.get(campaign, {}) if not nm else {}
        if not details:
            rows.append(deepcopy(source))
            continue
        for product, metrics in sorted(details.items(), key=lambda item: int(item[0])):
            row = {**source, "nmId": int(product), "sku": product, "rowKind": "product-campaign",
                   "attributionLevel": "exact_sku", "unallocatedSpend": False, **metrics}
            row["productName"] = product_names.get(product)
            row["adClicks"] = metrics["clicks"]
            row["orders"] = {"units": metrics["ordersUnits"], "kopecks": metrics["ordersKopecks"]}
            # Orders attributed by fullstats are not confirmed buyouts.
            row["sales"] = {"units": None, "kopecks": None}
            row["ctrPct"] = metrics["clicks"] / metrics["impressions"] * 100 if metrics["clicks"] is not None and metrics["impressions"] else None
            row["drrPct"] = metrics["adSpendKopecks"] / metrics["ordersKopecks"] * 100 if metrics["adSpendKopecks"] is not None and metrics["ordersKopecks"] else None
            rows.append(row)
        # Keep the unattributed residual once, never the parent total plus its
        # detail. Negative reconciliation differences are preserved as well.
        residual = {}
        for field in fields:
            total = (source.get("orders") or {}).get("units" if field == "ordersUnits" else "kopecks") if field.startswith("orders") else source.get(field)
            values = [item[field] for item in details.values()]
            residual[field] = total - sum(values) if total is not None and all(value is not None for value in values) else None
        if any(value != 0 for value in residual.values()):
            row = {**source, **residual, "nmId": 0, "sku": "", "rowKind": "campaign-residual",
                   "productName": "Нераспределённая часть РК", "unallocatedSpend": True,
                   "attributionLevel": "campaign_only", "ctrPct": None, "drrPct": None}
            row["adClicks"] = residual["clicks"]
            row["orders"] = {"units": residual["ordersUnits"], "kopecks": residual["ordersKopecks"]}
            row["sales"] = {"units": None, "kopecks": None}
            rows.append(row)

    direct = defaultdict(set)
    attributed = defaultdict(set)
    for campaign, products in membership.items():
        campaign = external_id(campaign)
        if campaign:
            for nm in products:
                if product := external_id(nm):
                    direct[product].add(campaign)
    for row in rows:
        campaign, nm = external_id(row.get("campaignId")), external_id(row.get("nmId"))
        if campaign and nm:
            attributed[nm].add(campaign)
    for row in rows:
        campaign, nm = external_id(row.get("campaignId")), external_id(row.get("nmId"))
        row["campaignId"] = campaign
        row["directCampaignIds"] = sorted(direct[nm], key=int) if nm else []
        row["attributedCampaignIds"] = sorted(attributed[nm], key=int) if nm else []
        row["campaignIds"] = sorted(direct[nm] | attributed[nm], key=int) if nm else ([campaign] if campaign else [])
        row["campaignMappingState"] = "current-membership-and-period-attribution" if campaign in direct[nm] else "period-attribution" if campaign and nm else "campaign-only" if campaign else "current-membership-only" if direct[nm] else "unavailable"
        row["campaignMappingNote"] = ("Текущий состав РК подтверждён; историческое участие не подтверждается составом на сегодня."
            if campaign in direct[nm] else "Атрибуция WB за выбранный период; прямое участие и конверсии из других РК не разделены источником."
            if campaign and nm else "Известен только текущий состав кампаний; распределение показателей за период по РК недоступно."
            if direct[nm] else "WB не передал привязку кампании к товару.")
    result.update(rows=rows, advertisingViewVersion=1)
    return result
