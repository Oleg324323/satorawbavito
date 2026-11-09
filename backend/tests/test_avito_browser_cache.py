from app.avito.browser_cache import merge_snapshot


def row(account="a", size="M", source="order_detail"):
    return {"orderId": "order-1", "accountId": account, "status": "ready_to_ship",
            "shipmentNumber": "000123", "items": [{"itemId": "item-1", "lineIndex": 0,
            "size": size, "imageUrl": "https://b00.img.avito.st/photo.jpg", "sources": {"size": source}}]}


def test_missing_fields_reused_but_new_status_preserved():
    current = row(size=None)
    current["status"] = "on_return"
    current["shipmentNumber"] = None
    merged = merge_snapshot({"orders": [current]}, {"orders": [row()]})["orders"][0]
    assert merged["status"] == "on_return"
    assert merged["items"][0]["size"] == "M"
    assert merged["shipmentNumber"] == "000123"


def test_checkpoint_preserves_unvisited_returns_but_final_snapshot_removes_old():
    old = {"returns": [row()]}
    assert merge_snapshot({"collector": {"checkpoint": True}}, old)["returns"] == old["returns"]
    assert merge_snapshot({}, old)["returns"] == []


def test_no_cross_account_or_product_variant_reuse():
    assert merge_snapshot({"orders": [row("b", None)]}, {"orders": [row()]})["orders"][0]["items"][0]["size"] is None
    current = row(size=None)
    current["items"][0]["itemId"] = "other"
    assert merge_snapshot({"orders": [current]}, {"orders": [row()]})["orders"][0]["items"][0]["size"] is None
    assert merge_snapshot({"orders": [row(None, None)]}, {"orders": [row("a"), row("b")]})["orders"][0]["items"][0]["size"] is None


def test_confirmed_variant_upgrades_listing_guess_not_the_reverse():
    merged = merge_snapshot({"orders": [row(size="XL", source="description")]}, {"orders": [row()]})
    assert merged["orders"][0]["items"][0]["size"] == "M"


def test_api_queue_variant_upgraded_by_confirmed_browser_evidence():
    from app.avito.orders import AvitoOrderItem, AvitoOrdersBrowserItem, _merge_browser_item
    item = AvitoOrderItem(itemId='1', title='Item', size='XL', color='black', sources={'size': 'description', 'color': 'listing'})
    browser = AvitoOrdersBrowserItem(itemId='1', size='M', color='white', sources={'size': 'order_detail', 'color': 'order_detail'})
    _merge_browser_item(item, browser)
    assert (item.size, item.color) == ('M', 'white')
    assert item.sources['size'] == item.sources['color'] == 'order_detail'
    merged = merge_snapshot({"orders": [row()]}, {"orders": [row(size="XL", source="description")]})
    assert merged["orders"][0]["items"][0]["size"] == "M"


def test_checkpoint_does_not_refresh_unvisited_status_timestamp():
    previous = {"capturedAt": "2026-10-04T10:00:00Z", "returns": [row()]}
    result = merge_snapshot({"capturedAt": "2026-10-05T10:00:00Z", "orders": [row()],
                             "collector": {"checkpoint": True}}, previous)
    assert result["orders"][0]["statusObservedAt"] == "2026-10-05T10:00:00Z"
    assert result["returns"][0]["statusObservedAt"] == "2026-10-04T10:00:00Z"
