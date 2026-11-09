from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from typing import Any

import httpx

from app.avito.orders import (
    AVITO_ORDER_STATUSES,
    LiveAvitoOrdersClient,
    AvitoOrderRow,
    AvitoOrdersBrowserSnapshot,
    AvitoOrdersFetchRequest,
    merge_browser_snapshot_orders,
)


ACTIVE_STATUSES = {"ready_to_ship", "in_transit", "on_return"}
HISTORY_STATUSES = {"delivered", "closed", "canceled"}
PICKUP_RETURN_STATUSES = {"ready_for_pickup", "ready_to_pickup", "pickup_ready", "can_pickup"}
INBOUND_RETURN_STATUSES = {"started", "in_transit", "return_in_transit", "on_the_way"}


def is_active_order(row: AvitoOrderRow) -> bool:
    # API `delivered` is not proof that the seller-cabinet order is closed:
    # parcels waiting for buyer pickup can already have this broad API status.
    # Browser `delivered` means the explicit "Выдан покупателю" observation
    # and remains history. Never reclassify the provider's raw status itself.
    return row.status in ACTIVE_STATUSES or (
        row.status == "delivered" and row.statusSource == "avito_api"
        and row.returnStatus not in {"received", "completed", "closed"}
    )


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def order_key(row: AvitoOrderRow) -> tuple[str, str]:
    return row.accountId or "", row.orderId


def merge_queue_rows(
    previous: list[AvitoOrderRow], incoming: list[AvitoOrderRow], snapshot: AvitoOrdersBrowserSnapshot | None,
) -> list[AvitoOrderRow]:
    old = {order_key(row): row for row in previous}
    rows: dict[tuple[str, str], AvitoOrderRow] = {}
    for row in incoming:
        if row.status not in AVITO_ORDER_STATUSES:
            row.status = "unknown"
        prior = old.get(order_key(row))
        if prior:
            old_status_time = _timestamp(prior.statusObservedAt)
            new_status_time = _timestamp(row.statusObservedAt)
            if prior.status != "unknown" and old_status_time and (not new_status_time or new_status_time < old_status_time):
                if row.status != prior.status:
                    row.sourceStatus = "partial"
                row.status = prior.status
                row.rawStatus = prior.rawStatus
                row.canonicalStatus = prior.canonicalStatus
                row.statusSource = prior.statusSource
                row.statusObservedAt = prior.statusObservedAt
                row.statusFetchedAt = prior.statusFetchedAt
            if not (row.returnStatusSource == "avito_api" and prior.returnStatusSource != "avito_api") and _timestamp(prior.returnStatusObservedAt) and (
                not _timestamp(row.returnStatusObservedAt)
                or _timestamp(prior.returnStatusObservedAt) > _timestamp(row.returnStatusObservedAt)
            ):
                if row.returnStatus and row.returnStatus != prior.returnStatus:
                    row.sourceStatus = "partial"
                row.returnStatus = prior.returnStatus
                row.returnStatusSource = prior.returnStatusSource
                row.returnStatusObservedAt = prior.returnStatusObservedAt
            row.shipmentNumberHistory = list(prior.shipmentNumberHistory)
            if not row.dropoffProvider:
                row.dropoffProvider = prior.dropoffProvider
            if row.shipmentNumberState == "ambiguous":
                if prior.shipmentNumber:
                    row.shipmentNumberHistory.append({"number": prior.shipmentNumber, "source": prior.shipmentNumberSource, "observedAt": prior.shipmentNumberObservedAt})
            else:
                for field in ("shipmentNumber", "shipmentNumberSource", "shipmentNumberObservedAt", "shipmentNumberFetchedAt", "shipmentNumberState"):
                    setattr(row, field, getattr(prior, field))
            prior_items = {(item.lineIndex, item.itemId): item for item in prior.items if item.itemId}
            for item in row.items:
                old_item = prior_items.get((item.lineIndex, item.itemId)) if item.itemId else None
                if old_item is None and item.itemId:
                    matches = [known for known in prior.items if known.itemId == item.itemId]
                    old_item = matches[0] if len(matches) == 1 else None
                if old_item:
                    for field in ("brand", "sellerArticle", "size", "color", "imageUrl"):
                        if field == "size" and item.sizeMode == "chat_ai":
                            continue
                        if not getattr(item, field):
                            setattr(item, field, getattr(old_item, field))
                            if field in old_item.sources:
                                item.sources[field] = old_item.sources[field]
                            if field == "size":
                                item.sizeMode = old_item.sizeMode
                                item.sizeState = old_item.sizeState
                                item.sizeReason = old_item.sizeReason
                                item.sizeEvidence = dict(old_item.sizeEvidence)
        rows[order_key(row)] = row
    # Missing rows are retained as uncertain. An incomplete provider response
    # must never silently turn a previously active order into an export row.
    for key, row in old.items():
        if key not in rows:
            uncertain = row.model_copy(deep=True)
            uncertain.status = "unknown"
            uncertain.sourceStatus = "partial"
            rows[key] = uncertain
    result = list(rows.values())
    merge_browser_snapshot_orders(result, snapshot)
    return result


def fetch_full_queue(client: Any, *, previous: list[AvitoOrderRow], snapshot: AvitoOrdersBrowserSnapshot | None) -> tuple[list[AvitoOrderRow], bool, dict[str, Any] | None]:
    if isinstance(client, LiveAvitoOrdersClient):
        with httpx.Client(timeout=client.timeout_seconds) as http:
            return _fetch_full_queue(partial(client.fetch_orders, http_client=http), previous=previous, snapshot=snapshot)
    return _fetch_full_queue(client.fetch_orders, previous=previous, snapshot=snapshot)


def _fetch_full_queue(fetch_orders: Any, *, previous: list[AvitoOrderRow], snapshot: AvitoOrdersBrowserSnapshot | None) -> tuple[list[AvitoOrderRow], bool, dict[str, Any] | None]:
    incoming: dict[tuple[str, str], AvitoOrderRow] = {}
    prior_by_id: dict[str, list[AvitoOrderRow]] = {}
    for old_row in previous:
        prior_by_id.setdefault(old_row.orderId, []).append(old_row)

    def bind_known_account(row: AvitoOrderRow) -> AvitoOrderRow:
        known = prior_by_id.get(row.orderId, [])
        if not row.accountId and len(known) == 1:
            row.accountId = known[0].accountId
            row.accountName = row.accountName or known[0].accountName
        return row

    reported_total = 0
    for page in range(1, 101):
        response = fetch_orders(AvitoOrdersFetchRequest(dateFrom=None, statuses=[], limit=20, page=page))
        if response.status == "blocked":
            error = response.error.model_dump(mode="json") if response.error else {"code": "avito_request_failed"}
            return previous, False, error
        reported_total = max(reported_total, response.total)
        for row in response.orders:
            if row.orderId:
                bind_known_account(row)
                incoming[order_key(row)] = row
        if len(response.orders) < 20 or (reported_total and len(incoming) >= reported_total):
            break
    else:
        return previous, False, {"code": "avito_orders_page_limit", "retryable": True}

    missing = [row for row in previous if is_active_order(row) and order_key(row) not in incoming]
    unresolved = {order_key(row) for row in missing}
    for offset in range(0, len(missing), 20):
        ids = [row.orderId for row in missing[offset:offset + 20]]
        exact = fetch_orders(AvitoOrdersFetchRequest(ids=ids, limit=20, page=1))
        if exact.status == "blocked":
            return previous, False, exact.error.model_dump(mode="json") if exact.error else {"code": "avito_request_failed"}
        for row in exact.orders:
            if row.orderId in ids:
                bind_known_account(row)
                incoming[order_key(row)] = row
                unresolved.discard(order_key(row))
    if unresolved:
        return previous, False, {"code": "active_orders_unverified", "retryable": True}
    return merge_queue_rows(previous, list(incoming.values()), snapshot), True, None


def return_phase(row: AvitoOrderRow) -> str:
    value = (row.returnStatus or "").strip().lower()
    if value in PICKUP_RETURN_STATUSES:
        return "pickup"
    if value in INBOUND_RETURN_STATUSES:
        return "inbound"
    return "unknown"


def queue_mode(row: AvitoOrderRow) -> str:
    if is_active_order(row) and row.status != "on_return":
        return "active"
    if row.status in HISTORY_STATUSES:
        return "history"
    if row.status == "on_return":
        return "returns"
    if row.status in ACTIVE_STATUSES:
        return "active"
    return "review"


def select_queue(rows: list[AvitoOrderRow], mode: str, account_id: str | None = None) -> list[AvitoOrderRow]:
    account_rows = [row for row in rows if not account_id or row.accountId == account_id]
    if mode == "ready_to_ship":
        return [row for row in account_rows if row.status == "ready_to_ship" and row.sourceStatus == "fresh"]
    if mode == "in_transit":
        return [row for row in account_rows if row.status == "in_transit"]
    if mode == "returns":
        return [row for row in account_rows if row.status == "on_return" and row.returnStatus not in {"received", "completed", "closed"}]
    if mode == "return_inbound":
        return [row for row in account_rows if row.status == "on_return" and return_phase(row) == "inbound"]
    if mode == "return_pickup":
        return [row for row in account_rows if row.status == "on_return" and row.sourceStatus == "fresh" and return_phase(row) == "pickup"]
    if mode == "history":
        return [row for row in account_rows if queue_mode(row) == "history"]
    if mode == "review":
        return [row for row in account_rows if queue_mode(row) == "review" or (row.status == "on_return" and return_phase(row) == "unknown")]
    return [row for row in account_rows if is_active_order(row) and row.returnStatus not in {"received", "completed", "closed"}]
