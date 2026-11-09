from __future__ import annotations

import hashlib
import html
import re
import secrets
from threading import Lock
from datetime import date, datetime, timedelta, timezone
from typing import Any
from typing import Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.listings import AvitoListingDetailsFetchRequest, AvitoListingsFetchRequest, build_avito_listings_client
from app.avito.orders import (
    AVITO_ORDER_STATUSES,
    AvitoOrderRow,
    AvitoOrdersBrowserOrder,
    AvitoOrdersBrowserSnapshot,
    AvitoOrdersFetchRequest,
    build_avito_orders_client,
    merge_browser_snapshot_orders,
    _parsed_time,
)
from app.avito.orders_ai import enrich_avito_orders_snapshot_with_ai
from app.avito.browser_cache import merge_snapshot
from app.avito.orders_picking_xlsx import build_avito_orders_picking_xlsx, picking_issues, _load_image
from app.avito.images_store import load_product_image
from app.avito.labels_store import enrich_labels, read_artifact, save_pdf
from app.avito.labels_pdf import MAX_PDF_BYTES
from starlette.concurrency import run_in_threadpool
from app.avito.orders_queue import fetch_full_queue, select_queue
from app.avito.returns import AvitoReturnCandidate, extract_return_candidates, match_return_candidates
from app.avito.returns_store import (
    apply_return_operation, enrich_existing_return_candidates, get_return_inventory_item, list_active_return_candidates,
    list_return_inventory, list_return_inventory_events, release_canceled_reservations, return_pickup_remaining, upsert_return_candidates,
)
from app.avito.returns_tasks import (
    AVITO_RETURNS_SYNC_STATUS_KEY,
    get_returns_sync_settings,
    save_returns_sync_settings,
)
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.infra.redis_client import get_redis_client
from app.repricer_cache.store import get_source_cache, get_source_cache_by_source_key, save_source_cache


router = APIRouter(tags=["avito-orders"])

AVITO_ORDERS_BROWSER_SNAPSHOT_KEY = "avito_orders_browser_snapshot"
AVITO_ORDERS_EXTENSION_TOKEN_KEY = "avito_orders_extension_token"
AVITO_ORDERS_EXTENSION_TOKEN_PREFIX = "avito_orders_extension_token_hash_"
AVITO_ORDERS_QUEUE_KEY = "avito_orders_queue_v1"
_QUEUE_LOCAL_LOCKS: dict[str, Lock] = {}


class ReturnInventoryOperation(BaseModel):
    operationId: str = Field(min_length=1, max_length=128)
    action: str
    quantity: int = Field(ge=1)
    linkedOrderId: str | None = None

_AVITO_PUBLIC_COLOR_CACHE: dict[str, str | None] = {}


def _date_from(value: date | None, period_days: int) -> tuple[date, int]:
    today = date.today()
    start = value or (today - timedelta(days=period_days - 1))
    if start > today:
        start = today - timedelta(days=period_days - 1)
    days = (today - start).days + 1
    if days <= 0:
        raise HTTPException(status_code=422, detail="AVITO_ORDERS_INVALID_PERIOD")
    if days > 183:
        raise HTTPException(status_code=422, detail="AVITO_ORDERS_PERIOD_TOO_DEEP")
    return start, days


def _statuses(values: list[str]) -> list[str]:
    normalized: list[str] = []
    allowed = set(AVITO_ORDER_STATUSES)
    for value in values:
        for part in str(value).split(","):
            status = part.strip()
            if not status:
                continue
            if status not in allowed:
                raise HTTPException(status_code=422, detail=f"AVITO_ORDERS_UNKNOWN_STATUS:{status}")
            normalized.append(status)
    return list(dict.fromkeys(normalized))


def _cache_key(start: date, statuses: list[str], page: int, limit: int) -> str:
    status_part = ",".join(statuses) if statuses else "all"
    return f"avito_orders:{start.isoformat()}:{status_part}:p{page}:l{limit}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extension_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    raw = token.strip()
    return raw if raw.startswith("sat_avito_") else None


def _label_organization(request: Request) -> int:
    token = _extension_bearer_token(request)
    if token:
        organization_id = _resolve_extension_token_organization(token)
        if organization_id is None:
            raise HTTPException(401, "AVITO_EXTENSION_TOKEN_INVALID")
        return organization_id
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(403, "NO_ACCESS:cabinet:read")
    return actor.organization_id


class LabelCollectionStatus(BaseModel):
    stage: Literal["opening", "selecting", "waiting_pdf", "downloading", "uploading", "error"]


@router.get("/api/v1/avito/orders/labels/collection-context")
def get_label_collection_context(request: Request):
    organization_id = _label_organization(request)
    snapshot = _browser_snapshot_from_cache(organization_id)
    # Only outbound orders awaiting shipment need a newly generated label.
    # Returns keep their saved historical label, but never require reprinting.
    rows = _browser_snapshot_rows(snapshot, statuses=["ready_to_ship"]) if snapshot else []
    enrich_labels(rows, organization_id)
    missing = [row for row in rows if not row.stickerLabelId]
    return {"accountIds": sorted({row.accountId or "" for row in rows}),
            "total": len(rows), "saved": len(rows) - len(missing),
            "missing": [{"orderId": row.marketplaceId or row.orderId, "shipmentNumber": row.shipmentNumber, "accountId": row.accountId or ""} for row in missing],
            "status": get_source_cache(organization_id, "avito_label_collection", slim=False)}


@router.get("/api/v1/avito/orders/browser-collection-context")
def get_browser_collection_context(request: Request):
    snapshot = _browser_snapshot_from_cache(_label_organization(request))
    # Only collection fields, never buyer contacts/chat text or provider credentials.
    fields = {"orderId", "marketplaceId", "accountId", "accountName", "jobNumber", "shipmentNumber", "shipmentNumberState", "pageUrl"}
    item_fields = {"itemId", "lineIndex", "itemUrl", "imageUrl", "imageUrls", "size", "descriptionSize", "color", "sources", "sellerArticle"}
    rows = []
    for row in [*snapshot.orders, *snapshot.returns] if snapshot else []:
        value = row.model_dump(include=fields)
        value["items"] = [item.model_dump(include=item_fields) for item in row.items]
        rows.append(value)
    return {"orders": rows}


@router.post("/api/v1/avito/orders/labels/collection-status")
def save_label_collection_status(request: Request, status: LabelCollectionStatus):
    organization_id = _label_organization(request)
    # Never accept URLs, exception dumps, cookies or arbitrary diagnostics.
    previous = get_source_cache(organization_id, "avito_label_collection", slim=False) or {}
    save_source_cache(organization_id, "avito_label_collection", {"stage": status.stage, "lastStage": previous.get("stage") if status.stage == "error" else status.stage, "updatedAt": _now_iso()})
    return {"ok": True}


@router.post("/api/v1/avito/orders/labels/import")
async def import_avito_labels(request: Request, account_id: str = Query(default="", alias="accountId", max_length=128)):
    organization_id = _label_organization(request)
    if request.headers.get("content-type", "").split(";")[0] != "application/pdf":
        raise HTTPException(415, "AVITO_LABEL_PDF_REQUIRED")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_PDF_BYTES:
            raise HTTPException(413, "AVITO_LABEL_PDF_TOO_LARGE")
    try:
        result = await run_in_threadpool(save_pdf, organization_id, account_id, bytes(content))
        save_source_cache(organization_id, "avito_label_collection", {"stage": "complete" if result["labels"] else "review", "labels": result["labels"], "updatedAt": _now_iso()})
        return result
    except (ValueError, RuntimeError):
        raise HTTPException(422, "AVITO_LABEL_PDF_UNREADABLE") from None


@router.get("/api/v1/avito/orders/labels/{label_id}/barcode.png")
def get_avito_label_barcode(request: Request, label_id: int):
    data = read_artifact(_label_organization(request), label_id)
    if data is None:
        raise HTTPException(404, "AVITO_LABEL_NOT_FOUND")
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/v1/avito/orders/label-documents/{document_id}.pdf")
def get_avito_label_original(request: Request, document_id: int):
    data = read_artifact(_label_organization(request), document_id, original=True)
    if data is None:
        raise HTTPException(404, "AVITO_LABEL_NOT_FOUND")
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": 'attachment; filename="avito-labels.pdf"'})


def _hash_extension_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extension_token_source_key(token_hash: str) -> str:
    return f"{AVITO_ORDERS_EXTENSION_TOKEN_PREFIX}{token_hash}"


def _extension_token_status_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload.get("activeHash"):
        return {"configured": False}
    return {
        "configured": True,
        "tokenPrefix": payload.get("tokenPrefix"),
        "createdAt": payload.get("createdAt"),
        "rotatedAt": payload.get("rotatedAt"),
        "lastUsedAt": payload.get("lastUsedAt"),
    }


def _returns_sync_settings_payload(organization_id: int) -> dict[str, Any]:
    sync_settings = get_returns_sync_settings(organization_id)
    sync_status = get_source_cache(organization_id, AVITO_RETURNS_SYNC_STATUS_KEY, slim=False) or {}
    candidates = _enriched_return_candidates(organization_id)
    last_auto = sync_settings.get("lastAutoDispatchedAt")
    next_run_at = None
    if last_auto:
        try:
            last_dt = datetime.fromisoformat(str(last_auto).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_run_at = (last_dt + timedelta(minutes=int(sync_settings["intervalMinutes"]))).isoformat()
        except (TypeError, ValueError):
            next_run_at = None
    return {
        **sync_settings,
        "nextRunAt": next_run_at,
        "status": sync_status if isinstance(sync_status, dict) else {},
        "inventory": _return_inventory_meta(candidates, 0),
        "items": [candidate.model_dump(mode="json") for candidate in candidates[:500]],
    }


def _resolve_extension_token_organization(token: str) -> int | None:
    token_hash = _hash_extension_token(token)
    token_row = get_source_cache_by_source_key(_extension_token_source_key(token_hash), slim=False) or None
    if not isinstance(token_row, dict):
        return None
    organization_id = token_row.get("organizationId")
    try:
        resolved_organization_id = int(organization_id)
    except (TypeError, ValueError):
        return None
    status = get_source_cache(resolved_organization_id, AVITO_ORDERS_EXTENSION_TOKEN_KEY, slim=False) or {}
    if status.get("activeHash") != token_hash:
        return None
    status["lastUsedAt"] = _now_iso()
    save_source_cache(resolved_organization_id, AVITO_ORDERS_EXTENSION_TOKEN_KEY, status)
    return resolved_organization_id


def _cache_hit_payload(cached: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    source["error"] = None
    payload["source"] = source
    return payload


def _browser_snapshot_from_cache(organization_id: int) -> AvitoOrdersBrowserSnapshot | None:
    cached = get_source_cache(organization_id, AVITO_ORDERS_BROWSER_SNAPSHOT_KEY, slim=False) or None
    if not isinstance(cached, dict):
        return None
    try:
        return AvitoOrdersBrowserSnapshot.model_validate(cached)
    except Exception:
        return None


def _browser_snapshot_meta(snapshot: AvitoOrdersBrowserSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    collector = snapshot.collector or {}
    ai_extraction = snapshot.aiExtraction or {}
    return {
        "capturedAt": snapshot.capturedAt,
        "pageUrl": snapshot.pageUrl,
        "orders": len(snapshot.orders),
        "returns": len(snapshot.returns),
        "items": sum(len(order.items) for order in [*snapshot.orders, *snapshot.returns]),
        "collector": collector,
        "aiExtraction": ai_extraction,
    }


def _int_or_zero(value: int | None) -> int:
    return value if value is not None else 0


def _summary(rows: list[AvitoOrderRow], total: int) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    action_required = 0
    returns = 0
    disputes = 0
    totals = [row.totalKopecks for row in rows if row.totalKopecks is not None]
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
        if any(action.required for action in row.availableActions):
            action_required += 1
        if row.status == "on_return" or row.returnStatus:
            returns += 1
        if row.status == "in_dispute":
            disputes += 1
    return {
        "total": total or len(rows),
        "shown": len(rows),
        "confirmation": status_counts.get("on_confirmation", 0),
        "readyToShip": status_counts.get("ready_to_ship", 0),
        "inTransit": status_counts.get("in_transit", 0),
        "delivered": status_counts.get("delivered", 0),
        "closed": status_counts.get("closed", 0),
        "canceled": status_counts.get("canceled", 0),
        "returns": returns,
        "disputes": disputes,
        "requiredActions": action_required,
        "items": sum(sum(item.quantity for item in row.items) for row in rows),
        "totalKopecks": sum(_int_or_zero(value) for value in totals) if totals else None,
        "statusCounts": status_counts,
    }


def _response_payload(
    *,
    status: str,
    start: date,
    days: int,
    statuses: list[str],
    page: int,
    limit: int,
    rows: list[AvitoOrderRow],
    total: int,
    diagnostics: dict[str, Any] | None = None,
    error: Any | None = None,
    browser_snapshot: AvitoOrdersBrowserSnapshot | None = None,
    return_inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": status,
        "period": {"dateFrom": start.isoformat(), "days": days},
        "filters": {"statuses": statuses, "page": page, "limit": limit},
        "summary": _summary(rows, total),
        "rows": [row.model_dump(mode="json") for row in rows],
        "source": {
            "mode": "live",
            "cache": {"status": "fresh", "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "ordersEndpoint": "GET /order-management/1/orders",
                "transitionEndpoint": "POST /order-management/1/order/applyTransition",
                "maxDepthDays": 183,
                "readOnly": True,
                "baseUrl": settings.avito_api_base_url,
            },
            "diagnostics": diagnostics,
            "error": error,
            "browserSnapshot": _browser_snapshot_meta(browser_snapshot),
            "returnInventory": return_inventory,
        },
    }


def _merge_browser_snapshot_payload(payload: dict[str, Any], *, organization_id: int) -> dict[str, Any]:
    snapshot = _browser_snapshot_from_cache(organization_id)
    if snapshot is None:
        return _attach_return_inventory_payload(payload, organization_id=organization_id)
    rows = [AvitoOrderRow.model_validate(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
    merge_browser_snapshot_orders(rows, snapshot)
    _enrich_missing_colors_from_public_avito(rows)
    payload["rows"] = [row.model_dump(mode="json") for row in rows]
    summary = dict(payload.get("summary") or {})
    summary.update(_summary(rows, int(summary.get("total") or len(rows))))
    payload["summary"] = summary
    source = dict(payload.get("source") or {})
    source["browserSnapshot"] = _browser_snapshot_meta(snapshot)
    payload["source"] = source
    _attach_return_inventory_payload(payload, organization_id=organization_id)
    return payload


def _return_inventory_meta(candidates: list[Any], matched_items: int) -> dict[str, Any]:
    last_synced_at = next((str(candidate.lastSeenAt) for candidate in candidates if candidate.lastSeenAt), None)
    return {
        "status": "ready",
        "candidates": len(candidates),
        "matchedItems": matched_items,
        "lastSyncedAt": last_synced_at,
    }


def _return_candidate_order_row(candidate: AvitoReturnCandidate) -> AvitoOrderRow:
    return AvitoOrderRow(
        orderId=candidate.returnOrderId,
        marketplaceId=candidate.marketplaceId,
        accountId=candidate.accountId,
        accountName=candidate.accountName,
        status=candidate.status or "on_return",
        returnStatus=candidate.returnStatus,
        updatedAt=candidate.sourceUpdatedAt,
        items=[
            {
                "itemId": candidate.itemId,
                "title": candidate.title,
                "quantity": candidate.quantity,
                "sellerArticle": candidate.sellerArticle,
                "size": candidate.size,
                "color": candidate.color,
                "imageUrl": candidate.imageUrl,
            }
        ],
    )


def _enriched_return_candidates(organization_id: int) -> list[AvitoReturnCandidate]:
    return list_active_return_candidates(organization_id)


def _return_match_key(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]+", " ", (value or "").casefold().replace("ё", "е")).split())


def _enrich_orders_with_return_matches(rows: list[AvitoOrderRow], *, organization_id: int) -> dict[str, Any]:
    try:
        candidates = _enriched_return_candidates(organization_id)
    except Exception:
        return {"status": "unavailable", "candidates": 0, "matchedItems": 0, "error": "avito_return_inventory_unavailable"}
    by_article: dict[str, list[AvitoReturnCandidate]] = {}
    by_title: dict[str, list[AvitoReturnCandidate]] = {}
    for candidate in candidates:
        if candidate.sellerArticle:
            by_article.setdefault(_return_match_key(candidate.sellerArticle), []).append(candidate)
        by_title.setdefault(_return_match_key(candidate.title), []).append(candidate)
    matched_items = 0
    for order in rows:
        for item in order.items:
            subset = {id(candidate): candidate for candidate in [
                *by_article.get(_return_match_key(item.sellerArticle), []),
                *by_title.get(_return_match_key(item.title), []),
            ]}
            matches = match_return_candidates(item, order=order, candidates=list(subset.values()))
            item.returnMatches = matches
            item.reuseSuggestion = matches[0] if matches else None
            if matches:
                matched_items += 1
    return _return_inventory_meta(candidates, matched_items)


def _attach_return_inventory_payload(payload: dict[str, Any], *, organization_id: int) -> dict[str, Any]:
    rows = [AvitoOrderRow.model_validate(row) for row in payload.get("rows") or [] if isinstance(row, dict)]
    return_inventory = _enrich_orders_with_return_matches(rows, organization_id=organization_id)
    payload["rows"] = [row.model_dump(mode="json") for row in rows]
    source = dict(payload.get("source") or {})
    source["returnInventory"] = return_inventory
    payload["source"] = source
    return payload


def _slugify_avito_title(title: str) -> str:
    mapping = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    normalized = "".join(mapping.get(char, char) for char in str(title or "").lower())
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", normalized)).strip("_")


def _public_avito_item_url(title: str, item_id: str) -> str | None:
    slug = _slugify_avito_title(title)
    if not slug or not item_id:
        return None
    return f"https://www.avito.ru/voronezh/odezhda_obuv_aksessuary/{slug}_{item_id}"


def _plain_text_from_html(value: str) -> str:
    without_scripts = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", "\n", value, flags=re.I | re.S)
    with_breaks = re.sub(r"<[^>]+>", "\n", without_scripts)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(with_breaks)).strip()


def _color_from_public_item_html(value: str) -> str | None:
    text = _plain_text_from_html(value)
    match = re.search(r"(?:^|\n|\s)Цвет\s*[:—–-]?\s*([А-Яа-яЁёA-Za-z -]{3,30})(?:\n|$)", text, flags=re.I)
    if not match:
        return None
    color = re.split(r"[,.;|]", match.group(1).strip())[0].strip().lower()
    if len(color) > 24 or re.search(r"\d", color):
        return None
    return color or None


def _fetch_public_avito_color(title: str, item_id: str) -> str | None:
    cached = _AVITO_PUBLIC_COLOR_CACHE.get(item_id)
    if item_id in _AVITO_PUBLIC_COLOR_CACHE:
        return cached
    url = _public_avito_item_url(title, item_id)
    if not url:
        _AVITO_PUBLIC_COLOR_CACHE[item_id] = None
        return None
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
        if response.status_code >= 400:
            _AVITO_PUBLIC_COLOR_CACHE[item_id] = None
            return None
        color = _color_from_public_item_html(response.text)
        _AVITO_PUBLIC_COLOR_CACHE[item_id] = color
        return color
    except Exception:
        _AVITO_PUBLIC_COLOR_CACHE[item_id] = None
        return None


def _enrich_missing_colors_from_public_avito(rows: list[AvitoOrderRow]) -> None:
    for order in rows:
        for item in order.items:
            if item.color or not item.itemId:
                continue
            color = _fetch_public_avito_color(item.title, str(item.itemId))
            if color:
                item.color = color


def _browser_snapshot_rows(snapshot: AvitoOrdersBrowserSnapshot, *, statuses: list[str]) -> list[AvitoOrderRow]:
    rows: list[AvitoOrderRow] = []
    browser_orders = [(order, order.status or "unknown") for order in snapshot.orders]
    browser_orders.extend((order, order.status or "on_return") for order in snapshot.returns)
    for index, (order, fallback_status) in enumerate(browser_orders):
        row_status = fallback_status
        # Native terminal returns may retain the broad on_return status. This
        # classification does not confirm inventory receipt or change Avito.
        if row_status == "on_return" and order.returnStatus in {"received", "completed", "closed"}:
            row_status = "closed"
        if statuses and row_status not in statuses:
            continue
        items = [
            {
                "itemId": item.itemId,
                "title": item.title or "Товар Авито",
                "quantity": item.quantity or 1,
                "priceKopecks": item.priceKopecks,
                "sellerArticle": item.sellerArticle,
                "size": item.size,
                "color": item.color,
                "imageUrl": item.imageUrl,
                "lineIndex": item.lineIndex if item.lineIndex is not None else item_index,
                "sources": item.sources,
                "descriptionSize": item.descriptionSize,
                "sizeMode": item.sizeMode,
                "sizeState": item.sizeState,
                "sizeReason": item.sizeReason,
                "sizeEvidence": item.sizeEvidence,
            }
            for item_index, item in enumerate(order.items)
        ]
        total = sum((item.priceKopecks or 0) * (item.quantity or 1) for item in order.items)
        rows.append(
            AvitoOrderRow.model_validate(
                {
                    "orderId": order.orderId or order.marketplaceId or f"browser-order-{index + 1}",
                    "marketplaceId": order.marketplaceId,
                    "accountId": order.accountId,
                    "accountName": order.accountName or "Авито",
                    "status": row_status,
                    "statusSource": "avito_browser",
                    "statusObservedAt": order.statusObservedAt or snapshot.capturedAt,
                    "deliveryService": order.deliveryService,
                    "dropoffProvider": order.dropoffProvider,
                    "trackNumber": order.trackNumber,
                    "jobNumber": order.jobNumber,
                    "shipmentNumber": order.shipmentNumber,
                    "shipmentNumberState": order.shipmentNumberState,
                    "returnStatus": order.returnStatus,
                    "returnPickupCode": order.returnPickupCode,
                    "returnPickupPlace": order.returnPickupPlace,
                    "returnPickupDeadline": order.returnPickupDeadline,
                    "returnFieldStates": order.returnFieldStates,
                    "buyerName": order.buyerName,
                    "buyerId": order.buyerId,
                    "recipientName": order.recipientName,
                    "totalKopecks": total or None,
                    "items": items or [{"title": "Товар Авито", "quantity": 1}],
                    "availableActions": [],
                    "sourceStatus": "fresh",
                }
            )
        )
    return rows


def _snapshot_identity_values(snapshot: AvitoOrdersBrowserSnapshot) -> set[str]:
    values: set[str] = set()
    for order in [*snapshot.orders, *snapshot.returns]:
        for value in (order.orderId, order.marketplaceId):
            text = str(value or "").strip()
            if text:
                values.add(text)
    return values


def _row_identity_values(row: AvitoOrderRow) -> set[str]:
    return {text for text in (str(row.orderId or "").strip(), str(row.marketplaceId or "").strip()) if text}


def _snapshot_filtered_rows(
    rows: list[AvitoOrderRow],
    snapshot: AvitoOrdersBrowserSnapshot,
) -> list[AvitoOrderRow]:
    identities = _snapshot_identity_values(snapshot)
    if not identities:
        return rows
    return [row for row in rows if _row_identity_values(row) & identities]


def _fetch_orders_filtered_by_snapshot(
    client: Any,
    *,
    snapshot: AvitoOrdersBrowserSnapshot,
    start: date,
    statuses: list[str],
) -> tuple[list[AvitoOrderRow], int, str, dict[str, Any] | None, Any | None]:
    identities = _snapshot_identity_values(snapshot)
    rows: list[AvitoOrderRow] = []
    total = 0
    last_diagnostics: dict[str, Any] | None = None
    last_error: Any | None = None
    if not identities:
        return rows, 0, "synced", {"source": "browser_snapshot_without_order_ids"}, None
    for api_page in range(1, 101):
        result = client.fetch_orders(AvitoOrdersFetchRequest(dateFrom=start, statuses=statuses, limit=20, page=api_page))
        last_diagnostics = result.diagnostics
        last_error = result.error.model_dump(mode="json") if hasattr(result.error, "model_dump") else result.error
        if result.status == "blocked":
            return rows, len(rows), result.status, last_diagnostics, last_error
        matched = _snapshot_filtered_rows(result.orders, snapshot)
        rows.extend(matched)
        total = result.total or total
        found = set().union(*(_row_identity_values(row) for row in rows)) if rows else set()
        if identities.issubset(found) or not result.orders or (total and api_page * 20 >= total):
            break
    rows = _snapshot_filtered_rows(list({row.orderId: row for row in rows}.values()), snapshot)
    merge_browser_snapshot_orders(rows, snapshot)
    diagnostics = {
        **(last_diagnostics or {}),
        "source": "api_filtered_by_browser_snapshot",
        "snapshotOrders": len(snapshot.orders),
        "snapshotIdentities": len(identities),
        "matchedOrders": len(rows),
    }
    return rows, len(rows), "synced", diagnostics, last_error


def _browser_snapshot_payload(
    snapshot: AvitoOrdersBrowserSnapshot,
    *,
    start: date,
    days: int,
    statuses: list[str],
    page: int,
    limit: int,
) -> dict[str, Any]:
    all_rows = _browser_snapshot_rows(snapshot, statuses=statuses)
    page_start = (page - 1) * limit
    rows = all_rows[page_start : page_start + limit]
    payload = _response_payload(
        status="synced",
        start=start,
        days=days,
        statuses=statuses,
        page=page,
        limit=limit,
        rows=rows,
        total=len(all_rows),
        diagnostics={"source": "browser_snapshot_only"},
        browser_snapshot=snapshot,
    )
    source = dict(payload.get("source") or {})
    source["mode"] = "browser-snapshot"
    source["api"] = {**dict(source.get("api") or {}), "readOnly": True, "usedForRows": False}
    payload["source"] = source
    return payload


def _orders_credentials_for_request(request: Request):
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")
    return actor, credentials


def _saved_queue_for_export(request: Request):
    actor, credentials = _orders_credentials_for_request(request)
    key = scoped_avito_cache_key(AVITO_ORDERS_QUEUE_KEY, f"{credentials.client_id}\0{credentials.client_secret}")
    return actor, get_source_cache(actor.organization_id, key, slim=False) or {}


def _orders_client_for_request(request: Request):
    actor, credentials = _orders_credentials_for_request(request)

    settings = get_settings()
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from exc

    client = build_avito_orders_client(
        access_token=access_token,
        base_url=settings.avito_api_base_url,
        timeout_seconds=settings.avito_api_timeout_seconds,
    )
    # Same credential-generation boundary as notification read marks. OAuth
    # refresh must not orphan durable queue data; changing credentials must.
    client.queue_cache_key = scoped_avito_cache_key(
        AVITO_ORDERS_QUEUE_KEY, f"{credentials.client_id}\0{credentials.client_secret}",
    )
    return actor, client, access_token


def _text_or_none(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _listing_cache_key(start: date, end: date) -> str:
    return f"avito_listings_v2:{start.isoformat()}:{end.isoformat()}:all"


def _listing_dicts_from_cache(organization_id: int, start: date, end: date, access_token: str) -> list[dict[str, Any]]:
    key = scoped_avito_cache_key(_listing_cache_key(start, end), access_token)
    cached = get_source_cache(organization_id, key, slim=False) or None
    rows = cached.get("rows") if isinstance(cached, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _enrich_orders_from_listing_rows(rows: list[AvitoOrderRow], listing_rows: list[dict[str, Any]]) -> None:
    by_item_id: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in listing_rows:
        identity = str(row.get("itemId") or row.get("id") or row.get("avitoId") or "")
        if identity:
            by_item_id.setdefault((str(row.get("accountId") or ""), identity), []).append(row)
    for order in rows:
        for item in order.items:
            matches = by_item_id.get((str(order.accountId or ""), str(item.itemId or "")), [])
            if len(matches) != 1:
                continue
            listing = matches[0]
            if not item.sellerArticle:
                item.sellerArticle = _text_or_none(listing.get("sellerArticle"), listing.get("seller_article"), listing.get("vendorCode"), listing.get("sku"))
            if not item.size and item.sizeMode != "chat_ai":
                item.size = _text_or_none(listing.get("size"), listing.get("productSize"), listing.get("product_size"))
            if not item.color:
                item.color = _text_or_none(listing.get("color"), listing.get("colour"))
            if not item.imageUrl:
                item.imageUrl = _text_or_none(listing.get("imageUrl"), listing.get("image_url"))
            if not order.accountName or order.accountName == "Авито":
                order.accountName = _text_or_none(listing.get("accountName"), order.accountName)


def _missing_detail_item_ids(rows: list[AvitoOrderRow]) -> list[str]:
    result: list[str] = []
    for order in rows:
        for item in order.items:
            if not item.itemId:
                continue
            if item.size and item.color and item.sellerArticle and item.imageUrl and order.accountName and order.accountName != "Авито":
                continue
            result.append(str(item.itemId))
    return list(dict.fromkeys(result))


def _enrich_orders_for_picking(rows: list[AvitoOrderRow], *, organization_id: int, access_token: str, start: date) -> None:
    end = date.today()
    listing_rows = _listing_dicts_from_cache(organization_id, start, end, access_token)
    settings = get_settings()
    client = None
    if not listing_rows:
        try:
            client = build_avito_listings_client(
                access_token=access_token,
                base_url=settings.avito_api_base_url,
                timeout_seconds=settings.avito_api_timeout_seconds,
            )
            result = client.fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=end))
            if result.status != "blocked":
                listing_rows = [row.model_dump(mode="json") for row in result.rows]
        except Exception:
            listing_rows = []
    if listing_rows:
        _enrich_orders_from_listing_rows(rows, listing_rows)
    missing_item_ids = _missing_detail_item_ids(rows)
    if missing_item_ids:
        try:
            client = client or build_avito_listings_client(
                access_token=access_token,
                base_url=settings.avito_api_base_url,
                timeout_seconds=settings.avito_api_timeout_seconds,
            )
            details = client.fetch_listing_details(AvitoListingDetailsFetchRequest(itemIds=missing_item_ids))
            if details.status != "blocked":
                _enrich_orders_from_listing_rows(rows, [row.model_dump(mode="json") for row in details.rows])
        except Exception:
            return


@router.get("/api/v1/avito/orders/extension-token")
def get_avito_orders_extension_token_status(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    return _extension_token_status_payload(get_source_cache(actor.organization_id, AVITO_ORDERS_EXTENSION_TOKEN_KEY, slim=False))


@router.post("/api/v1/avito/orders/extension-token/regenerate")
def regenerate_avito_orders_extension_token(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "integrations:write"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:integrations:write")
    token = f"sat_avito_{secrets.token_urlsafe(32)}"
    token_hash = _hash_extension_token(token)
    now = _now_iso()
    previous = get_source_cache(actor.organization_id, AVITO_ORDERS_EXTENSION_TOKEN_KEY, slim=False) or {}
    payload = {
        "activeHash": token_hash,
        "tokenPrefix": token[:18],
        "createdAt": previous.get("createdAt") or now,
        "rotatedAt": now,
        "lastUsedAt": None,
    }
    save_source_cache(actor.organization_id, AVITO_ORDERS_EXTENSION_TOKEN_KEY, payload)
    save_source_cache(
        actor.organization_id,
        _extension_token_source_key(token_hash),
        {"tokenHash": token_hash, "createdAt": now, "tokenPrefix": token[:18]},
    )
    return {**_extension_token_status_payload(payload), "token": token}


@router.post("/api/v1/avito/orders/browser-snapshot")
def save_avito_orders_browser_snapshot(request: Request, snapshot: AvitoOrdersBrowserSnapshot) -> dict[str, Any]:
    extension_token = _extension_bearer_token(request)
    if extension_token:
        organization_id = _resolve_extension_token_organization(extension_token)
        if organization_id is None:
            raise HTTPException(status_code=401, detail="AVITO_EXTENSION_TOKEN_INVALID")
    else:
        actor = actor_from_request(request)
        if not has_permission(actor, "cabinet:read"):
            raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
        organization_id = actor.organization_id
    previous = _browser_snapshot_from_cache(organization_id)
    # Confirmation is server-owned even when a client supplies another mode.
    # Sanitize BEFORE merging durable checkpoints; saved proofs stay trusted.
    for row in [*snapshot.orders, *snapshot.returns]:
        for item in row.items:
            incoming_chat = item.sizeMode == "chat_ai" or item.sources.get("size") == "chat_ai"
            item.sizeState = None
            item.sizeReason = None
            item.sizeEvidence = {}
            if incoming_chat:
                item.sizeMode = "chat_ai"
                item.size = None
                item.sizeState = "needs_review"
                item.sizeReason = "ai_confirmation_missing"
                item.sources.pop("size", None)
    if previous and previous.capturedAt and snapshot.capturedAt:
        try:
            old_time = datetime.fromisoformat(previous.capturedAt.replace("Z", "+00:00"))
            new_time = datetime.fromisoformat(snapshot.capturedAt.replace("Z", "+00:00"))
            if new_time < old_time:
                return {"ok": True, "browserSnapshot": _browser_snapshot_meta(previous), "ignored": "older_snapshot"}
        except ValueError:
            pass
    # Collect details only for operational rows. Terminal rows carry status
    # reconciliation only: retain saved details, never import arbitrary history.
    allowed = {"ready_to_ship", "in_transit", "on_return"}
    credentials = get_organization_avito_credentials_secret(organization_id)
    queue: dict[str, Any] = {}
    if credentials:
        key = scoped_avito_cache_key(AVITO_ORDERS_QUEUE_KEY, f"{credentials.client_id}\0{credentials.client_secret}")
        queue = get_source_cache(organization_id, key, slim=False) or {}
    known_api_rows = [AvitoOrdersBrowserOrder.model_validate(row) for row in queue.get("rows", [])]
    def operational_rows(section: str) -> list:
        result = []
        saved = getattr(previous, section) if previous else []
        for row in getattr(snapshot, section):
            if row.status in allowed and row.returnStatus not in {"received", "completed", "closed"}:
                result.append(row)
                continue
            if row.status not in {"closed", "canceled", "delivered"} and row.returnStatus not in {"received", "completed", "closed"}:
                continue
            number = row.orderId or row.marketplaceId
            # Browser and API can use different aliases for the same order.
            # Deduplicate the same account/order, but reject account collisions.
            candidates = {}
            for old in [*known_api_rows, *saved]:
                if number and number in {old.orderId, old.marketplaceId} and (not row.accountId or row.accountId == old.accountId):
                    candidates[(old.accountId, old.marketplaceId or old.orderId)] = old
            matches = list(candidates.values())
            if len(matches) != 1 or not matches[0].accountId:
                continue
            old = matches[0]
            result.append(old.model_copy(update={
                "status": "closed" if row.returnStatus in {"received", "completed", "closed"} else row.status,
                "returnStatus": row.returnStatus or old.returnStatus,
                "statusObservedAt": snapshot.capturedAt,
            }))
        return result
    snapshot = snapshot.model_copy(update={"orders": operational_rows("orders"), "returns": operational_rows("returns")})
    size_mode = ((snapshot.collector or {}).get("options") or {}).get("sizeMode")
    if size_mode in {"chat_ai", "description", "none"}:
        for row in [*snapshot.orders, *snapshot.returns]:
            if row.status in allowed:
                for item in row.items:
                    item.sizeMode = size_mode
    snapshot = AvitoOrdersBrowserSnapshot.model_validate(merge_snapshot(snapshot.model_dump(mode="json"), previous.model_dump(mode="json") if previous else None))
    # Resolve blank browser account IDs only against the CURRENT credential's
    # saved queue and an unambiguous exact order ID; never enumerate old accounts.
    if credentials:
        for row in [*snapshot.orders, *snapshot.returns]:
            if row.accountId:
                continue
            numbers = {row.orderId, row.marketplaceId} - {None, ""}
            accounts = {old.get("accountId") for old in queue.get("rows", [])
                        if numbers.intersection({old.get("orderId"), old.get("marketplaceId")}) and old.get("accountId")}
            if len(accounts) == 1:
                row.accountId = accounts.pop()
    snapshot, ai_extraction = enrich_avito_orders_snapshot_with_ai(snapshot, trusted_prior=previous, organization_id=organization_id)
    snapshot = snapshot.model_copy(update={"aiExtraction": ai_extraction})
    payload = snapshot.model_dump(mode="json")
    payload["savedAt"] = _now_iso()
    save_source_cache(organization_id, AVITO_ORDERS_BROWSER_SNAPSHOT_KEY, payload)
    persisted = get_source_cache(organization_id, AVITO_ORDERS_BROWSER_SNAPSHOT_KEY, slim=False, strict=True)
    if not persisted or persisted.get("savedAt") != payload["savedAt"]:
        raise HTTPException(503, "AVITO_SNAPSHOT_NOT_SAVED")
    if snapshot.returns:
        return_rows = _browser_snapshot_rows(snapshot, statuses=["on_return"])
        enrich_existing_return_candidates(organization_id, extract_return_candidates(return_rows))
    return {"ok": True, "browserSnapshot": _browser_snapshot_meta(snapshot)}


def _queue_cache_key(access_token: str) -> str:
    return scoped_avito_cache_key(AVITO_ORDERS_QUEUE_KEY, access_token)


def _read_queue_cache(organization_id: int, access_token: str, client: Any) -> tuple[str, dict[str, Any]]:
    legacy_key = _queue_cache_key(access_token)
    key = getattr(client, "queue_cache_key", legacy_key)
    cached = get_source_cache(organization_id, key, slim=False)
    if cached is None and key != legacy_key:
        # Only migrate the exact currently authenticated token's cache. Never
        # enumerate another credential/account's historical cache entries.
        cached = get_source_cache(organization_id, legacy_key, slim=False)
        if cached is not None:
            save_source_cache(organization_id, key, cached)
    return key, cached or {}


def _queue_cached_rows(payload: dict[str, Any]) -> list[AvitoOrderRow]:
    return [AvitoOrderRow.model_validate(value) for value in payload.get("rows") or [] if isinstance(value, dict)]


def _pickup_unreceived(rows: list[AvitoOrderRow], organization_id: int, account_id: str | None) -> list[AvitoOrderRow]:
    try:
        remaining = return_pickup_remaining(organization_id, account_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="AVITO_RETURN_STORE_UNAVAILABLE") from exc
    result: list[AvitoOrderRow] = []
    for row in rows:
        copy = row.model_copy(deep=True)
        available_items = []
        for item in copy.items:
            quantity = remaining.get((copy.accountId or "", copy.orderId, item.lineIndex, item.itemId or ""), 0)
            if quantity > 0:
                item.quantity = min(item.quantity, quantity)
                available_items.append(item)
        copy.items = available_items
        if copy.items:
            result.append(copy)
    return result


def _queue_age_seconds(payload: dict[str, Any], now: datetime) -> float:
    try:
        value = datetime.fromisoformat(str(payload["lastSuccessfulRefresh"]).replace("Z", "+00:00"))
        return (now - value).total_seconds()
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _load_orders_queue(organization_id: int, access_token: str, client: Any, *, force: bool = False) -> dict[str, Any]:
    key, cached = _read_queue_cache(organization_id, access_token, client)
    now = datetime.now(timezone.utc)
    try:
        retry_after = datetime.fromisoformat(str(cached.get("retryAfter") or "").replace("Z", "+00:00"))
    except ValueError:
        retry_after = None
    if retry_after and retry_after > now:
        return cached
    if not force and cached.get("complete") and _queue_age_seconds(cached, now) < 300:
        return cached
    try:
        lock = get_redis_client().lock(f"satorna:avito-orders:{organization_id}", timeout=1800, blocking=False)
        acquired = lock.acquire(blocking=False)
    except Exception:
        # ponytail: process lock is a local-dev fallback; production needs Redis for cross-worker exclusion.
        lock = _QUEUE_LOCAL_LOCKS.setdefault(str(organization_id), Lock())
        acquired = lock.acquire(blocking=False)
    if not acquired:
        return {**cached, "complete": False, "error": {"code": "refresh_in_progress", "retryable": True}}
    try:
        cached = get_source_cache(organization_id, key, slim=False) or {}
        try:
            latest_retry = datetime.fromisoformat(str(cached.get("retryAfter") or "").replace("Z", "+00:00"))
        except ValueError:
            latest_retry = None
        if latest_retry and latest_retry > datetime.now(timezone.utc):
            return cached
        if not force and cached.get("complete") and _queue_age_seconds(cached, datetime.now(timezone.utc)) < 300:
            return cached
        previous = _queue_cached_rows(cached)
        rows, complete, error = fetch_full_queue(
            client, previous=previous, snapshot=_browser_snapshot_from_cache(organization_id),
        )
        if complete:
            try:
                upsert_return_candidates(organization_id, extract_return_candidates(rows))
            except RuntimeError:
                complete, error = False, {"code": "return_inventory_unavailable", "retryable": True}
        if complete:
            release_canceled_reservations(organization_id, {row.orderId for row in rows if row.status == "canceled" and row.statusSource == "avito_api"})
            payload = {
                "rows": [row.model_dump(mode="json") for row in rows],
                "complete": True,
                "lastSuccessfulRefresh": now.isoformat(),
                "error": None,
                "retryAfter": None,
            }
        else:
            payload = {
                **cached,
                "rows": cached.get("rows") or [],
                "complete": False,
                "error": error,
                "retryAfter": (now + timedelta(seconds=60 if (error or {}).get("code") == "rate_limited" else 20)).isoformat(),
            }
        save_source_cache(organization_id, key, payload)
        return payload
    finally:
        try:
            lock.release()
        except Exception:
            pass


@router.get("/api/v1/avito/orders/queue")
def get_avito_orders_queue(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Query(default="active", pattern="^(active|ready_to_ship|in_transit|returns|return_inbound|return_pickup|history|review)$"),
    account_id: str | None = Query(default=None, alias="accountId"),
    search: str = Query(default="", max_length=200),
    history_from: date | None = Query(default=None, alias="historyFrom"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor, client, access_token = _orders_client_for_request(request)
    _, cached = _read_queue_cache(actor.organization_id, access_token, client)
    now = datetime.now(timezone.utc)
    if force_refresh or not cached.get("complete") or _queue_age_seconds(cached, now) >= 300:
        try:
            retry_after = datetime.fromisoformat(str(cached.get("retryAfter") or "").replace("Z", "+00:00"))
        except ValueError:
            retry_after = None
        if retry_after and retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=timezone.utc)
        if not retry_after or retry_after <= now:
            background_tasks.add_task(_load_orders_queue, actor.organization_id, access_token, client, force=force_refresh)
            cached = {**cached, "complete": False, "error": {"code": "refresh_in_progress", "retryable": True}}
    all_rows = _queue_cached_rows(cached)
    # Uploaded details must be visible without re-fetching the marketplace queue.
    snapshot = _browser_snapshot_from_cache(actor.organization_id)
    _reconcile_export_statuses(all_rows, _browser_snapshot_rows(snapshot, statuses=[]) if snapshot else [], cached.get("lastSuccessfulRefresh"))
    merge_browser_snapshot_orders(all_rows, snapshot)
    enrich_labels(all_rows, actor.organization_id)
    selected = select_queue(all_rows, mode, account_id)
    if mode == "return_pickup":
        selected = _pickup_unreceived(selected, actor.organization_id, account_id)
    if mode == "history" and history_from:
        selected = [row for row in selected if row.createdAt and row.createdAt[:10] >= history_from.isoformat()]
    if search.strip():
        needle = search.strip().casefold()
        selected = [row for row in selected if needle in " ".join(
            [row.orderId, row.marketplaceId or "", row.jobNumber or "", row.shipmentNumber or "", row.trackNumber or "", row.buyerName or ""]
            + [item.title + " " + (item.sellerArticle or "") + " " + (item.itemId or "") for item in row.items]
        ).casefold()]
    selected.sort(key=lambda row: row.createdAt or "", reverse=True)
    start = (page - 1) * limit
    page_rows = selected[start:start + limit]
    from app.avito.listing_photos import photo_index
    saved_photos = photo_index(actor.organization_id)
    for row in page_rows:
        for item in row.items:
            item.photoId = saved_photos.get((row.accountId or "", item.itemId))
    _enrich_orders_with_return_matches(page_rows, organization_id=actor.organization_id)
    account_rows = [row for row in all_rows if not account_id or row.accountId == account_id]
    counts = {name: len(select_queue(account_rows, name)) for name in (
        "active", "ready_to_ship", "in_transit", "returns", "return_inbound", "return_pickup", "history", "review",
    )}
    counts["return_pickup"] = len(_pickup_unreceived(select_queue(account_rows, "return_pickup"), actor.organization_id, account_id))
    accounts = sorted({(row.accountId, row.accountName or row.accountId) for row in all_rows if row.accountId})
    return {
        "status": "synced" if cached.get("complete") else "blocked",
        "filters": {"mode": mode, "accountId": account_id, "page": page, "limit": limit, "search": search, "historyFrom": history_from.isoformat() if mode == "history" and history_from else None},
        "accounts": [{"id": key, "name": name} for key, name in accounts],
        "summary": {**_summary(selected, len(selected)), "total": len(selected), "shown": len(page_rows), "readyToShip": counts["ready_to_ship"], "modeCounts": counts},
        "rows": [row.model_dump(mode="json") for row in page_rows],
        "source": {
            "mode": "avito_api_queue",
            "complete": bool(cached.get("complete")),
            "lastSuccessfulRefresh": cached.get("lastSuccessfulRefresh"),
            "error": cached.get("error"),
            "retryAfter": cached.get("retryAfter"),
            "browserSnapshot": _browser_snapshot_meta(_browser_snapshot_from_cache(actor.organization_id)),
            "labelCollection": get_source_cache(actor.organization_id, "avito_label_collection", slim=False),
        },
    }


@router.get("/api/v1/avito/orders/returns-inventory")
def get_avito_returns_inventory(
    request: Request,
    account_id: str | None = Query(default=None, alias="accountId"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    try:
        items, total = list_return_inventory(actor.organization_id, account_id=account_id, page=page, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="AVITO_RETURN_STORE_UNAVAILABLE") from exc
    return {"items": [item.model_dump(mode="json") for item in items], "total": total, "page": page, "limit": limit}


@router.post("/api/v1/avito/orders/returns-inventory/{return_item_id}/operations")
def post_avito_return_operation(request: Request, return_item_id: int, payload: ReturnInventoryOperation) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "integrations:write"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:integrations:write")
    candidate = get_return_inventory_item(actor.organization_id, return_item_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="RETURN_ITEM_NOT_FOUND")
    if payload.action == "reserve":
        actor, client, access_token = _orders_client_for_request(request)
        cached = _load_orders_queue(actor.organization_id, access_token, client)
        if not cached.get("complete") or _queue_age_seconds(cached, datetime.now(timezone.utc)) > 600:
            raise HTTPException(status_code=503, detail="AVITO_ORDERS_QUEUE_NOT_FRESH")
        targets = [row for row in select_queue(_queue_cached_rows(cached), "ready_to_ship") if row.orderId == payload.linkedOrderId and row.statusSource == "avito_api"]
        target = targets[0] if len(targets) == 1 else None
        if target is None or not any(
            match.returnItemId == return_item_id and match.score == 100
            for item in target.items for match in match_return_candidates(item, order=target, candidates=[candidate])
        ):
            raise HTTPException(status_code=409, detail="RETURN_VARIANT_NOT_CONFIRMED")
    try:
        result = apply_return_operation(
            actor.organization_id, return_item_id, operation_id=payload.operationId,
            action=payload.action, quantity=payload.quantity, actor_id=actor.actor_id,
            linked_order_id=payload.linkedOrderId,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "item": result.model_dump(mode="json")}


@router.get("/api/v1/avito/orders/returns-inventory/{return_item_id}/events")
def get_avito_return_inventory_events(
    request: Request, return_item_id: int,
    page: int = Query(default=1, ge=1), limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    try:
        return list_return_inventory_events(actor.organization_id, return_item_id, page=page, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/avito/orders/returns-sync")
def get_avito_returns_sync_settings(request: Request) -> dict[str, Any]:
    extension_token = _extension_bearer_token(request)
    if extension_token:
        organization_id = _resolve_extension_token_organization(extension_token)
        if organization_id is None:
            raise HTTPException(status_code=401, detail="AVITO_EXTENSION_TOKEN_INVALID")
        return _returns_sync_settings_payload(organization_id)
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    return _returns_sync_settings_payload(actor.organization_id)


@router.put("/api/v1/avito/orders/returns-sync")
def update_avito_returns_sync_settings(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "integrations:write"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:integrations:write")
    save_returns_sync_settings(actor.organization_id, payload)
    return _returns_sync_settings_payload(actor.organization_id)


@router.post("/api/v1/avito/orders/returns-sync/run")
def run_avito_returns_sync(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "sync:run"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:sync:run")
    from app.avito.returns_tasks import sync_returns_for_org

    async_result = sync_returns_for_org.delay(actor.organization_id, "manual", True)
    status_payload = {
        "state": "queued",
        "organizationId": actor.organization_id,
        "taskId": async_result.id,
        "queuedAt": _now_iso(),
        "trigger": "manual",
    }
    save_source_cache(actor.organization_id, AVITO_RETURNS_SYNC_STATUS_KEY, status_payload)
    return {**_returns_sync_settings_payload(actor.organization_id), "taskId": async_result.id}


@router.get("/api/v1/avito/orders")
def get_avito_orders(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=183),
    status: list[str] = Query(default_factory=list, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=20),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor, client, access_token = _orders_client_for_request(request)
    start, days = _date_from(date_from, period_days)
    statuses = _statuses(status)
    browser_snapshot = _browser_snapshot_from_cache(actor.organization_id)
    if browser_snapshot is not None:
        try:
            all_rows, total, result_status, diagnostics, error = _fetch_orders_filtered_by_snapshot(
                client,
                snapshot=browser_snapshot,
                start=start,
                statuses=statuses,
            )
            if result_status != "blocked" and all_rows:
                _enrich_missing_colors_from_public_avito(all_rows)
                page_start = (page - 1) * limit
                rows = all_rows[page_start : page_start + limit]
                return_inventory = _enrich_orders_with_return_matches(rows, organization_id=actor.organization_id)
                payload = _response_payload(
                    status=result_status,
                    start=start,
                    days=days,
                    statuses=statuses,
                    page=page,
                    limit=limit,
                    rows=rows,
                    total=total,
                    diagnostics=diagnostics,
                    error=error,
                    browser_snapshot=browser_snapshot,
                    return_inventory=return_inventory,
                )
                source = dict(payload.get("source") or {})
                source["mode"] = "api-filtered-by-browser-snapshot"
                source["api"] = {**dict(source.get("api") or {}), "usedForRows": True}
                payload["source"] = source
                return payload
            fallback = _browser_snapshot_payload(browser_snapshot, start=start, days=days, statuses=statuses, page=page, limit=limit)
            source = dict(fallback.get("source") or {})
            source["diagnostics"] = {
                **dict(source.get("diagnostics") or {}),
                "apiFilterStatus": result_status,
                "apiFilterDiagnostics": diagnostics,
            }
            source["error"] = error
            fallback["source"] = source
            return _attach_return_inventory_payload(fallback, organization_id=actor.organization_id)
        except HTTPException:
            raise
        except Exception:
            fallback = _browser_snapshot_payload(browser_snapshot, start=start, days=days, statuses=statuses, page=page, limit=limit)
            source = dict(fallback.get("source") or {})
            source["diagnostics"] = {
                **dict(source.get("diagnostics") or {}),
                "apiFilterStatus": "failed",
                "apiFilterError": "avito_orders_filter_unavailable",
            }
            fallback["source"] = source
            return _attach_return_inventory_payload(fallback, organization_id=actor.organization_id)
    source_key = scoped_avito_cache_key(_cache_key(start, statuses, page, limit), access_token)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if not force_refresh and isinstance(cached, dict) and cached.get("status") != "blocked" and cached.get("rows") is not None:
        return _merge_browser_snapshot_payload(_cache_hit_payload(cached), organization_id=actor.organization_id)

    result = client.fetch_orders(AvitoOrdersFetchRequest(dateFrom=start, statuses=statuses, limit=limit, page=page))
    browser_snapshot = _browser_snapshot_from_cache(actor.organization_id)
    merge_browser_snapshot_orders(result.orders, browser_snapshot)
    _enrich_missing_colors_from_public_avito(result.orders)
    return_inventory = _enrich_orders_with_return_matches(result.orders, organization_id=actor.organization_id)
    payload = _response_payload(
        status=result.status,
        start=start,
        days=days,
        statuses=statuses,
        page=page,
        limit=limit,
        rows=result.orders,
        total=result.total,
        diagnostics=result.diagnostics,
        error=result.error.model_dump(mode="json") if hasattr(result.error, "model_dump") else result.error,
        browser_snapshot=browser_snapshot,
        return_inventory=return_inventory,
    )
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload


@router.get("/api/v1/avito/orders/picking-list.xlsx")
def get_avito_orders_picking_list_xlsx(
    request: Request,
    account_id: str | None = Query(default=None, alias="accountId"),
    search: str = Query(default="", max_length=200),
    ready_only: bool = Query(default=False, alias="readyOnly"),
    require_fresh: bool = Query(default=False, alias="requireFresh"),
) -> Response:
    return _queue_xlsx_response(request, mode="ready_to_ship", account_id=account_id, search=search, ready_only=ready_only, require_fresh=require_fresh is True)


def _picking_freshness(cached: dict[str, Any], snapshot: AvitoOrdersBrowserSnapshot | None, account_id: str | None, now: datetime) -> dict[str, Any]:
    # Observation time, not DB save time: enrichment must not rejuvenate old statuses.
    def recent(value: Any) -> bool:
        observed = _parsed_time(value)
        return bool(observed and 0 <= (now - observed).total_seconds() <= 600)

    if snapshot is not None:
        collector = snapshot.collector or {}
        captured_at = snapshot.capturedAt
        rows = [*snapshot.orders, *snapshot.returns]
        covered = set(collector.get("coveredAccountIds") or [row.accountId for row in rows if row.accountId])
        cached_rows = _queue_cached_rows(cached)
        # Check the same resolved identities and statuses used for export, not
        # obsolete API rows that omitted their account identity.
        _reconcile_export_statuses(cached_rows, _browser_snapshot_rows(snapshot, statuses=[]), cached.get("lastSuccessfulRefresh"))
        export_rows = [row for row in [*cached_rows, *rows] if row.status == "ready_to_ship" and (not account_id or row.accountId == account_id)]
        required = {account_id} if account_id else {row.accountId for row in export_rows}
        account_matches = bool(covered) and None not in required and required.issubset(covered)
        # A complete scan of account A cannot certify an old cached order of A
        # which was not seen at all. That old order could already be terminal.
        api_recent = cached.get("complete") is True and recent(cached.get("lastSuccessfulRefresh"))
        cached_covered = all(api_recent or any(
            row.accountId == saved.accountId
            and bool({row.orderId, row.marketplaceId} - {None, ""})
            and bool(({row.orderId, row.marketplaceId} - {None, ""}) & ({saved.orderId, saved.marketplaceId} - {None, ""}))
            and recent(row.statusObservedAt or captured_at)
            for row in rows
        ) for saved in cached_rows if saved.status == "ready_to_ship" and (not account_id or saved.accountId == account_id))
        observations_recent = all(recent(row.statusObservedAt or captured_at) for row in rows
                                  if row.status == "ready_to_ship" and (not account_id or row.accountId == account_id))
        complete = collector.get("status") == "completed" and collector.get("checkpoint") is False and account_matches and cached_covered and observations_recent
    else:
        captured_at = cached.get("lastSuccessfulRefresh")
        rows = _queue_cached_rows(cached)
        complete = cached.get("complete") is True and (not account_id or any(row.accountId == account_id for row in rows))
    try:
        observed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        age = (now - observed).total_seconds() if observed.tzinfo else None
    except (ValueError, TypeError):
        age = None
    fresh = bool(complete and age is not None and 0 <= age <= 600)
    return {"fresh": fresh, "observedAt": captured_at, "maxAgeSeconds": 600, "reason": None if fresh else "collection_incomplete_or_stale"}


@router.get("/api/v1/avito/orders/picking-list/freshness")
def get_avito_picking_freshness(request: Request, account_id: str | None = Query(default=None, alias="accountId")) -> dict[str, Any]:
    actor, cached = _saved_queue_for_export(request)
    return _picking_freshness(cached, _browser_snapshot_from_cache(actor.organization_id), account_id, datetime.now(timezone.utc))


@router.get("/api/v1/avito/orders/picking-list/preview")
def get_avito_picking_preview(request: Request, account_id: str | None = Query(default=None, alias="accountId"), search: str = Query(default="", max_length=200)):
    actor, client, access_token = _orders_client_for_request(request)
    _, cached = _read_queue_cache(actor.organization_id, access_token, client)
    if not cached.get("complete") or _queue_age_seconds(cached, datetime.now(timezone.utc)) > 600:
        raise HTTPException(status_code=503, detail="AVITO_ORDERS_QUEUE_NOT_FRESH")
    rows = _queue_cached_rows(cached)
    snapshot = _browser_snapshot_from_cache(actor.organization_id)
    _reconcile_export_statuses(rows, _browser_snapshot_rows(snapshot, statuses=[]) if snapshot else [], cached.get("lastSuccessfulRefresh"))
    merge_browser_snapshot_orders(rows, snapshot)
    enrich_labels(rows, actor.organization_id)
    rows = _filter_picking_search(select_queue(rows, "ready_to_ship", account_id), search)
    excluded = [{"orderId": row.orderId, "number": row.marketplaceId, "issues": picking_issues(row)} for row in rows if picking_issues(row)]
    return {"orders": len(rows), "units": sum(item.quantity for row in rows for item in row.items),
            "ready": len(rows) - len(excluded), "excluded": excluded, "asOf": cached["lastSuccessfulRefresh"]}


def _filter_picking_search(rows: list[AvitoOrderRow], search: str) -> list[AvitoOrderRow]:
    needle = search.strip().casefold()
    if not needle:
        return rows
    return [row for row in rows if needle in " ".join(
        [row.orderId, row.marketplaceId or "", row.jobNumber or "", row.shipmentNumber or "", row.trackNumber or "", row.buyerName or ""]
        + [item.title + " " + (item.sellerArticle or "") + " " + (item.itemId or "") for item in row.items]
    ).casefold()]


@router.get("/api/v1/avito/orders/returns-list.xlsx")
def get_avito_orders_returns_list_xlsx(
    request: Request,
    account_id: str | None = Query(default=None, alias="accountId"),
) -> Response:
    return _queue_xlsx_response(request, mode="returns", account_id=account_id)


def _reconcile_export_statuses(rows: list[AvitoOrderRow], browser_rows: list[AvitoOrderRow], refreshed_at: str | None) -> None:
    """Prefer newer observed states, never promote unknown/ambiguous orders to shipping."""
    for row in rows:
        if row.accountId:
            continue
        ids = {row.orderId, row.marketplaceId} - {None, ""}
        accounts = {other.accountId for other in [*rows, *browser_rows]
                    if other.accountId and ids.intersection({other.orderId, other.marketplaceId})}
        if len(accounts) == 1:
            row.accountId = accounts.pop()
    for browser in browser_rows:
        ids = {browser.orderId, browser.marketplaceId} - {None, ""}
        matches = [row for row in rows if ids.intersection({row.orderId, row.marketplaceId})
                   and (not browser.accountId or not row.accountId or browser.accountId == row.accountId)]
        if not matches:
            rows.append(browser)
            continue
        if len(matches) != 1:
            continue
        row = matches[0]
        observed = _parsed_time(browser.statusObservedAt)
        previous = _parsed_time(row.statusObservedAt) or _parsed_time(row.statusFetchedAt) or _parsed_time(refreshed_at)
        if row.status == browser.status == "delivered" and observed:
            # Same enum, different granularity: the browser observed actual
            # buyer handover. A later broad API update must not reopen it.
            row.statusSource = "avito_browser"
            row.statusObservedAt = browser.statusObservedAt
            continue
        if observed and (not previous or observed > previous):
            row.status = browser.status
            row.statusSource = "avito_browser"
            row.statusObservedAt = browser.statusObservedAt
        elif row.status != browser.status and (not observed or not previous or observed == previous):
            # Conflicting states without an ordering are not proof it is safe to ship.
            row.status = "unknown"


def _queue_xlsx_response(request: Request, *, mode: str, account_id: str | None, search: str = "", ready_only: bool = False, require_fresh: bool = False) -> Response:
    # Export is a database read, never an OAuth request/full provider sync.
    # The normal UI requests a freshness guard; returns retain saved-data export.
    actor, cached = _saved_queue_for_export(request)
    snapshot = _browser_snapshot_from_cache(actor.organization_id)
    if require_fresh and not _picking_freshness(cached, snapshot, account_id, datetime.now(timezone.utc))["fresh"]:
        raise HTTPException(status_code=409, detail="AVITO_ORDERS_QUEUE_NOT_FRESH")
    if "rows" not in cached and not (snapshot and (snapshot.orders or snapshot.returns)):
        raise HTTPException(status_code=404, detail="AVITO_ORDERS_NO_SAVED_SNAPSHOT")
    all_rows = _queue_cached_rows(cached)
    # Newly collected orders need not wait for a separate API refresh to be
    # downloadable. Keep cached API records and add only unmatched exact IDs.
    _reconcile_export_statuses(all_rows, _browser_snapshot_rows(snapshot, statuses=[]) if snapshot else [], cached.get("lastSuccessfulRefresh"))
    merge_browser_snapshot_orders(all_rows, snapshot)
    enrich_labels(all_rows, actor.organization_id)
    rows = select_queue(all_rows, mode, account_id)
    if mode == "ready_to_ship":
        selected_keys = {(row.accountId, row.orderId) for row in rows}
        rows.extend(row for row in all_rows if row.statusSource == "avito_browser"
                    and row.status == "ready_to_ship" and (not account_id or row.accountId == account_id)
                    and (row.accountId, row.orderId) not in selected_keys)
    if mode == "returns":
        # A picking document is not an inventory receipt. Include returns with
        # incomplete pickup details too; missing fields remain explicit.
        rows = [row for row in all_rows if row.status == "on_return" and row.returnStatus not in {"received", "completed", "closed"} and (not account_id or row.accountId == account_id)]
    rows = _filter_picking_search(rows, search)
    if ready_only:
        rows = [row for row in rows if not picking_issues(row)]
        if not rows:
            raise HTTPException(status_code=409, detail="AVITO_PICKING_NO_READY_ORDERS")
    if mode == "return_pickup":
        rows = _pickup_unreceived(rows, actor.organization_id, account_id)
    # Export from our saved bytes first. CDN links can expire or time out,
    # which previously turned a stored photo into "Фото не получено".
    from app.avito.listing_photos import photo_index, read_photo
    saved_photo_ids = photo_index(actor.organization_id)
    item_accounts: dict[str, set[str]] = {}
    for row in rows:
        for item in row.items:
            if item.itemId:
                item_accounts.setdefault(item.itemId, set()).add(row.accountId or "")
    generic_ambiguous = {item_id for item_id, accounts in item_accounts.items() if len(accounts) > 1}
    saved_bytes: dict[str, tuple[bytes, str]] = {}
    for row in rows:
        for item in row.items:
            if not item.itemId:
                continue
            photo_id = saved_photo_ids.get((row.accountId or "", item.itemId))
            if photo_id is None and item.itemId not in generic_ambiguous:
                photo_id = saved_photo_ids.get(("", item.itemId))
            if photo_id is None:
                continue
            key = f"saved-photo:{photo_id}"
            if key not in saved_bytes:
                data = read_photo(actor.organization_id, photo_id)
                if data:
                    saved_bytes[key] = (data, "jpeg")
            if key in saved_bytes:
                item.imageUrl = key
    as_of = str(cached.get("lastSuccessfulRefresh") or "Время обновления неизвестно")
    content = build_avito_orders_picking_xlsx(rows, date_from=date.today(), as_of=as_of, returns=mode in {"return_pickup", "returns"},
        label_loader=lambda label_id: read_artifact(actor.organization_id, label_id),
        image_loader=lambda url: saved_bytes.get(url) or load_product_image(actor.organization_id, url, lambda _url: None))
    name = "returns" if mode in {"return_pickup", "returns"} else "picking"
    filename = f"avito-{name}-list-{date.today().isoformat()}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
