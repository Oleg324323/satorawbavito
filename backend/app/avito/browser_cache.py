"""Reuse immutable order details without resurrecting obsolete statuses or variants."""
from copy import deepcopy

STRONG = {"order_row", "order_detail", "chat_ai"}


def reuse_order(current: dict, cached: dict) -> dict:
    result = deepcopy(current)
    for field in ("accountId", "accountName", "jobNumber", "shipmentNumber", "shipmentNumberState", "dropoffProvider", "pageUrl"):
        if not result.get(field) and cached.get(field):
            result[field] = cached[field]
    for item in result.get("items", []):
        # Never transfer a variant between different products or ambiguous lines.
        matches = [old for old in cached.get("items", []) if item.get("itemId")
                   and old.get("itemId") == item["itemId"] and old.get("lineIndex") == item.get("lineIndex")]
        if len(matches) != 1:
            continue
        old = matches[0]
        sources = item.setdefault("sources", {})
        for field in ("imageUrl", "imageUrls", "itemUrl", "size", "color", "descriptionSize", "sellerArticle"):
            if field == "size" and item.get("sizeMode") == "chat_ai":
                continue
            if field == "size" and old.get("sizeMode") == "chat_ai" and item.get("sizeMode") in {"description", "none"}:
                continue
            upgrade = field in {"size", "color"} and sources.get(field) not in STRONG and old.get("sources", {}).get(field) in STRONG
            if (not item.get(field) or upgrade) and old.get(field):
                item[field] = old[field]
                if old.get("sources", {}).get(field):
                    sources[field] = old["sources"][field]
    return result


def merge_snapshot(current: dict, previous: dict | None) -> dict:
    result = deepcopy(current)
    previous = previous or {}
    checkpoint = bool((current.get("collector") or {}).get("checkpoint"))
    for section in ("orders", "returns"):
        old_rows = deepcopy(previous.get(section) or [])
        for old in old_rows:
            if not old.get("statusObservedAt") and previous.get("capturedAt"):
                old["statusObservedAt"] = previous["capturedAt"]
        rows = []
        used = set()
        for row in current.get(section) or []:
            row = deepcopy(row)
            if not row.get("statusObservedAt") and current.get("capturedAt"):
                row["statusObservedAt"] = current["capturedAt"]
            number = row.get("orderId") or row.get("marketplaceId")
            matches = [(i, old) for i, old in enumerate(old_rows) if number
                       and number == (old.get("orderId") or old.get("marketplaceId"))
                       and (not row.get("accountId") or not old.get("accountId") or row["accountId"] == old["accountId"])]
            if len(matches) == 1:
                index, cached = matches[0]
                used.add(index)
                row = reuse_order(row, cached)
            rows.append(row)
        if checkpoint:
            rows.extend(deepcopy(old) for i, old in enumerate(old_rows) if i not in used)
        result[section] = rows
    return result
