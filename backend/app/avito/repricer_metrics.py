"""Read-only, account-bound enrichment. No price or strategy mutations."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.avito.auth import scoped_avito_cache_key
from app.avito.stats import AvitoStatsFetchRequest
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.avito.listing_photos import photo_index


def blocked_count(organization_id, scope, client):
    key = scoped_avito_cache_key("avito_repricer_blocked_inventory", scope)
    saved = get_source_cache(organization_id, key, slim=False)
    if saved and datetime.now().timestamp() - saved.get("savedAt", 0) < 300:
        return saved.get("count")
    try:
        count, page = 0, 1
        with client._http_client() as http:
            while True:
                response = http.get(f"{client.base_url}/core/v1/items", params={"status": "blocked", "per_page": 99, "page": page}, headers=client._headers())
                response.raise_for_status()
                items = response.json().get("resources")
                if not isinstance(items, list):
                    raise ValueError("Missing inventory")
                count += len(items)
                if len(items) < 99:
                    break
                page += 1
        save_source_cache(organization_id, key, {"count": count, "savedAt": datetime.now().timestamp()})
        return count
    except Exception:
        return saved.get("count") if saved else None


def ratio(numerator, denominator):
    return round(numerator / denominator * 100, 2) if numerator is not None and denominator is not None and denominator > 0 else None


def row_metrics(row):
    contacts, spend = row.get("contacts"), row.get("spendKopecks")
    return {"orderConversionPct": ratio(row.get("orders"), row.get("views")),
            "averageContactCostKopecks": round(spend / contacts, 2) if spend is not None and contacts is not None and contacts > 0 else None}


def trend(current, previous):
    if current is None or previous is None:
        return {"direction": "missing", "percent": None, "delta": None}
    delta = current - previous
    return {"direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "percent": round(delta / previous * 100, 1) if previous else (0 if not delta else None),
            "delta": delta,
            "current": current, "previous": previous}


def enrich_comparisons(payload, organization_id, scope, client, *, allow_fetch=True):
    end = min(date.fromisoformat(payload["period"]["dateTo"]), datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1))
    windows = [(end - timedelta(days=2), end), (end - timedelta(days=5), end - timedelta(days=3))]
    rows = [dict(row, **row_metrics(row)) for row in payload.get("rows", [])]
    # Item IDs are Avito identifiers, never match photographs by product title.
    browser = get_source_cache(organization_id, "avito_orders_browser_snapshot", slim=False) or {}
    photos = {}
    for order in browser.get("orders", []) + browser.get("returns", []):
        for item in order.get("items", []):
            if item.get("itemId") and item.get("imageUrl"):
                photos[(str(order.get("accountId") or ""), str(item["itemId"]))] = item["imageUrl"]
    account_ids = {row["accountId"] for row in rows}
    stored_photos = photo_index(organization_id)
    for row in rows:
        row['photoId'] = stored_photos.get((row['accountId'], row['itemId']))
        if not row.get("imageUrl"):
            row["imageUrl"] = photos.get((row["accountId"], row["itemId"]))
            if not row["imageUrl"] and len(account_ids) == 1:
                row["imageUrl"] = photos.get(("", row["itemId"]))
    by_account = {}
    for row in rows:
        by_account.setdefault(row["accountId"], []).append(row)
    for account, account_rows in by_account.items():
        snapshots = []
        failure_key = scoped_avito_cache_key(f"avito_repricer_comparison_cooldown:{account}", scope)
        cooldown = get_source_cache(organization_id, failure_key, slim=False) or {}
        error = payload.get('source', {}).get('error') or {}
        try:
            retry_at = datetime.fromisoformat(str(error.get('retryAfterUntil', '')).replace('Z', '+00:00'))
            rate_limited = retry_at.replace(tzinfo=retry_at.tzinfo or timezone.utc) > datetime.now(timezone.utc)
        except ValueError:
            rate_limited = False
        blocked = not allow_fetch or float(cooldown.get("until", 0)) > datetime.now().timestamp() or rate_limited
        for start, finish in windows:
            key = scoped_avito_cache_key(f"avito_repricer_comparison:{account}:{start}:{finish}", scope)
            saved = get_source_cache(organization_id, key, slim=False)
            if saved is None and not blocked:
                try:
                    with client._http_client() as http:
                        items = client._v2_item_analytics_for_account(http, AvitoStatsFetchRequest(dateFrom=start, dateTo=finish), account)
                    saved = {"items": items}
                    save_source_cache(organization_id, key, saved)
                except Exception as error:
                    # Keep period totals usable; never invent item-level trends.
                    blocked = True
                    response = getattr(error, 'response', None)
                    save_source_cache(organization_id, failure_key, {
                        "until": datetime.now().timestamp() + 70,
                        "statusCode": getattr(response, 'status_code', None),
                        "errorType": type(error).__name__,
                    })
            snapshots.append((saved or {}).get("items", {}))
        for row in account_rows:
            current, previous = [snapshot.get(row["itemId"], {}) for snapshot in snapshots]
            row["viewsTrend"] = trend(current.get("views"), previous.get("views"))
            row["contactsTrend"] = trend(current.get("contactsMessenger"), previous.get("contactsMessenger"))
            row["comparisonPeriod"] = {"currentFrom": str(windows[0][0]), "currentTo": str(windows[0][1]),
                                       "previousFrom": str(windows[1][0]), "previousTo": str(windows[1][1])}
    if allow_fetch:
        count = blocked_count(organization_id, scope, client)
    else:
        saved = get_source_cache(organization_id, scoped_avito_cache_key("avito_repricer_blocked_inventory", scope), slim=False) or {}
        count = saved.get("count", payload.get("summary", {}).get("blockedListings"))
    summary = {**payload.get("summary", {}), "blockedListings": count}
    return {**payload, "rows": rows, "summary": summary}
