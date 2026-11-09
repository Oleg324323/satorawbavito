from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Text, and_, cast, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis_client
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.wb_browser_prices import CONNECTION_KEY, PRICES_KEY, apply_browser_prices

_REDIS_CACHE_TTL_SECONDS = 45
_REDIS_DISABLED_UNTIL = 0.0
FINANCE_REVENUE_BASIS = "retailPriceWithDisc*quantity"
FINANCE_SCHEMA_VERSION = "v6"


def _db_timestamp(value: Any) -> str | None:
    """Raw SQL returns datetime in PostgreSQL and ISO text in SQLite."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def finance_cache_uses_current_revenue_basis(payload: dict[str, Any]) -> bool:
    return (
        payload.get("revenueBasis") == FINANCE_REVENUE_BASIS
        and payload.get("financeSchemaVersion") == FINANCE_SCHEMA_VERSION
    )


def _optional_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _source_cache_range_from_key(source_key: str) -> tuple[date | None, date | None]:
    dates = [_optional_date(value) for value in re.findall(r"\d{4}-\d{2}-\d{2}", source_key)]
    valid_dates = [value for value in dates if value is not None]
    return (valid_dates[-2], valid_dates[-1]) if len(valid_dates) >= 2 else (None, None)


def _source_cache_metadata(source_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    daily_aggregates = payload.get("dailyAggregates")
    daily_dates = sorted(
        date_key
        for key in daily_aggregates.keys()
        if isinstance(daily_aggregates, dict)
        and (parsed := _optional_date(key)) is not None
        and (date_key := parsed.isoformat())
    ) if isinstance(daily_aggregates, dict) else []
    key_date_from, key_date_to = _source_cache_range_from_key(source_key)
    return {
        "range_date_from": _optional_date(payload.get("dateFrom")) or key_date_from,
        "range_date_to": _optional_date(payload.get("dateTo")) or key_date_to,
        "revenue_basis": str(payload["revenueBasis"]) if payload.get("revenueBasis") is not None else None,
        "finance_schema_version": str(payload["financeSchemaVersion"]) if payload.get("financeSchemaVersion") is not None else None,
        "daily_detail_status": str(payload["dailyDetailStatus"]) if payload.get("dailyDetailStatus") is not None else None,
        "daily_detail_error": str(payload["dailyDetailError"]) if payload.get("dailyDetailError") is not None else None,
        "daily_detail_deferred_at": payload.get("dailyDetailDeferredAt"),
        "daily_detail_paused_at": payload.get("dailyDetailPausedAt"),
        "daily_detail_failed_at": payload.get("dailyDetailFailedAt"),
        "daily_detail_fetched_at": payload.get("dailyDetailFetchedAt"),
        "daily_detail_partial_at": payload.get("dailyDetailPartialAt"),
        "daily_detail_preserved_at": payload.get("dailyDetailPreservedAt"),
        "daily_detail_preserved_by": payload.get("dailyDetailPreservedBy"),
        "daily_detail_requests_completed": _optional_int(payload.get("dailyDetailRequestsCompleted")),
        "daily_detail_requests_total": _optional_int(payload.get("dailyDetailRequestsTotal")),
        "daily_aggregate_dates": daily_dates,
    }


def _run_db(db_fn):
    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            return db_fn(session)
    except SQLAlchemyError:
        return None


def _redis_available() -> bool:
    return time.monotonic() >= _REDIS_DISABLED_UNTIL


def _disable_redis_briefly() -> None:
    global _REDIS_DISABLED_UNTIL
    _REDIS_DISABLED_UNTIL = time.monotonic() + 5


def _redis_get_json(key: str) -> dict[str, Any] | None:
    if not _redis_available():
        return None
    try:
        raw = get_redis_client().get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        _disable_redis_briefly()
        return None


def _redis_get_json_list(key: str) -> list[dict[str, Any]] | None:
    if not _redis_available():
        return None
    try:
        raw = get_redis_client().get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            return None
        return [item for item in payload if isinstance(item, dict)]
    except Exception:
        _disable_redis_briefly()
        return None


def _redis_set_json_list(key: str, payload: list[dict[str, Any]], *, ttl: int = _REDIS_CACHE_TTL_SECONDS) -> None:
    if not _redis_available():
        return
    try:
        get_redis_client().setex(key, ttl, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        _disable_redis_briefly()


def _redis_set_json(key: str, payload: dict[str, Any], *, ttl: int = _REDIS_CACHE_TTL_SECONDS) -> None:
    if not _redis_available():
        return
    try:
        get_redis_client().setex(key, ttl, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        _disable_redis_briefly()


def _redis_delete(*keys: str) -> None:
    if not keys or not _redis_available():
        return
    try:
        get_redis_client().delete(*keys)
    except Exception:
        _disable_redis_briefly()


def _redis_delete_pattern(pattern: str) -> None:
    if not _redis_available():
        return
    try:
        client = get_redis_client()
        keys = list(client.scan_iter(match=pattern, count=100))
        if keys:
            client.delete(*keys)
    except Exception:
        _disable_redis_briefly()


def _goods_meta_cache_key(organization_id: int) -> str:
    return f"vella:repricer:goods-meta:{organization_id}"


def _goods_list_cache_key(organization_id: int) -> str:
    return f"vella:repricer:goods-list:{organization_id}"


def _source_cache_key(organization_id: int, source_key: str, *, slim: bool) -> str:
    return f"vella:repricer:source-cache:{organization_id}:{source_key}:slim{1 if slim else 0}"


def _source_cache_redis_allowed(source_key: str, *, slim: bool) -> bool:
    if source_key.startswith("sku_list_snapshot_"):
        return not slim
    if source_key.startswith("baskets_detail_status_"):
        return True
    if source_key in {
        "wb_sync_status",
        "reports_payload_materialization_status",
        "baskets_detail_active",
        "excel_imports",
        "promotion_thresholds",
    }:
        return True
    return False


def list_cached_goods_pages(organization_id: int) -> list[WbRepricerGoodsCacheRow]:
    def _db(session: Session) -> list[WbRepricerGoodsCacheRow]:
        return list(
            session.scalars(
                select(WbRepricerGoodsCacheRow)
                .where(WbRepricerGoodsCacheRow.organization_id == organization_id)
                .order_by(WbRepricerGoodsCacheRow.page_offset.asc())
            ).all()
        )

    return _run_db(_db) or []


def _slim_good_payload(item: dict[str, Any]) -> dict[str, Any]:
    sizes = item.get("sizes") or []
    first_size = sizes[0] if isinstance(sizes, list) and sizes and isinstance(sizes[0], dict) else {}
    return {
        "vendorCode": item.get("vendorCode"),
        "nmID": item.get("nmID"),
        "brand": item.get("brand"),
        "subjectName": item.get("subjectName"),
        "subjectID": item.get("subjectID"),
        "subjectId": item.get("subjectId"),
        "objectID": item.get("objectID"),
        "objectId": item.get("objectId"),
        "clubDiscount": item.get("clubDiscount"),
        "discount": item.get("discount"),
        "editableSizePrice": item.get("editableSizePrice"),
        "priceObservedAt": item.get("priceObservedAt"),
        "priceBaseline": item.get("priceBaseline"),
        "photos": item.get("photos"),
        "mediaFiles": item.get("mediaFiles"),
        "photoUrl": item.get("photoUrl") or item.get("imageUrl"),
        "sizes": [first_size] if first_size else [],
    }


def _slim_content_card(card: dict[str, Any]) -> dict[str, Any]:
    slim_sizes: list[dict[str, Any]] = []
    for size in card.get("sizes") or []:
        if not isinstance(size, dict):
            continue
        slim_sizes.append({"skus": size.get("skus") or []})
    return {
        "vendorCode": card.get("vendorCode"),
        "nmID": card.get("nmID"),
        "title": card.get("title"),
        "object": card.get("object"),
        "objectID": card.get("objectID"),
        "objectId": card.get("objectId"),
        "subjectName": card.get("subjectName"),
        "subjectID": card.get("subjectID"),
        "subjectId": card.get("subjectId"),
        "brand": card.get("brand"),
        "photos": card.get("photos"),
        "mediaFiles": card.get("mediaFiles"),
        "photoUrl": card.get("photoUrl") or card.get("imageUrl"),
        "sizes": slim_sizes,
    }


def _slim_promotion(promotion: dict[str, Any]) -> dict[str, Any]:
    article_ids = promotion.get("articleIds") or []
    if isinstance(article_ids, set):
        article_ids = sorted(article_ids)
    threshold_nm_ids = []
    for item in promotion.get("thresholdNmIds") or []:
        try:
            threshold_nm_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    threshold_statuses_by_nm_id = {}
    if isinstance(promotion.get("thresholdStatusesByNmId"), dict):
        threshold_statuses_by_nm_id = {
            str(key): str(value)
            for key, value in promotion["thresholdStatusesByNmId"].items()
            if key is not None and value
        }
    return {
        "id": promotion.get("id"),
        "name": promotion.get("name"),
        "type": promotion.get("type"),
        "status": promotion.get("status"),
        "startDate": promotion.get("startDate"),
        "endDate": promotion.get("endDate"),
        "daysUntilEnd": promotion.get("daysUntilEnd"),
        "daysUntilStart": promotion.get("daysUntilStart"),
        "eligibleSkuCount": promotion.get("eligibleSkuCount"),
        "participatingSkuCount": promotion.get("participatingSkuCount"),
        "participationPct": promotion.get("participationPct"),
        "excelLoaded": promotion.get("excelLoaded"),
        "excelStatus": promotion.get("excelStatus"),
        "excelFileName": promotion.get("excelFileName"),
        "excelLoadedAt": promotion.get("excelLoadedAt"),
        "thresholdRowsParsed": promotion.get("thresholdRowsParsed"),
        "thresholdNmIds": threshold_nm_ids,
        "thresholdStatusesByNmId": threshold_statuses_by_nm_id,
        "articleIds": [str(item) for item in article_ids if item],
        **{key: promotion.get(key) for key in (
            "startDateTime", "endDateTime", "wbType", "membershipVersion",
            "membershipState", "membershipFetchedAt", "participantNmIds",
        )},
    }


def slim_source_cache_payload(source_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    slim = dict(payload)
    if source_key.startswith("repricer_finance_"):
        slim.pop("operations", None)
        return slim
    if source_key.startswith("finance_"):
        slim.pop("rows", None)
        slim.pop("revenueOperations", None)
        return slim
    if source_key == "content_cards":
        cards = payload.get("cards")
        if isinstance(cards, list):
            slim["cards"] = [_slim_content_card(card) for card in cards if isinstance(card, dict)]
        return slim
    if source_key == "promotions":
        promotions = payload.get("promotions")
        if isinstance(promotions, list):
            slim["promotions"] = [_slim_promotion(item) for item in promotions if isinstance(item, dict)]
        return slim
    return slim


def get_goods_price_revision(organization_id: int) -> str | None:
    """DB metadata only: price confirmation must survive a missed Redis eviction.

    updated_at changes for the patched page; fetched_at remains the observation
    time of the other products. Never use this revision as catalogue freshness.
    """
    with get_session_factory()() as session:
        return _db_timestamp(session.scalar(select(func.max(WbRepricerGoodsCacheRow.updated_at)).where(
            WbRepricerGoodsCacheRow.organization_id == organization_id)))


def list_cached_goods(organization_id: int) -> list[dict[str, Any]]:
    revision = get_goods_price_revision(organization_id)
    redis_key = f"{_goods_list_cache_key(organization_id)}:{revision}"
    cached = _redis_get_json_list(redis_key)
    if cached is not None:
        return _goods_with_browser_prices(organization_id, cached)

    goods: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in list_cached_goods_pages(organization_id):
        for item in page.goods_payload or []:
            if not isinstance(item, dict):
                continue
            vendor_code = str(item.get("vendorCode") or "")
            dedupe_key = str(item.get("nmID") or "") or vendor_code
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            enriched = _slim_good_payload(item)
            from app.repricer_price_safety import price_baseline
            observed_at = item.get("priceObservedAt") or _db_timestamp(page.fetched_at)
            enriched["priceObservedAt"] = observed_at
            enriched["priceBaseline"] = price_baseline(item, observed_at)
            goods.append(enriched)
    _redis_set_json_list(redis_key, goods)
    return _goods_with_browser_prices(organization_id, goods)


def browser_prices_selected(organization_id: int) -> bool:
    return bool((get_source_cache(organization_id, CONNECTION_KEY, strict=True) or {}).get("marketplaceAccountId"))


def _goods_with_browser_prices(organization_id: int, goods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not goods:
        return goods
    connection = get_source_cache(organization_id, CONNECTION_KEY, strict=True)
    return apply_browser_prices(goods, connection, get_source_cache(organization_id, PRICES_KEY) if connection else None)


def cached_goods_meta(organization_id: int) -> dict[str, Any]:
    redis_key = _goods_meta_cache_key(organization_id)
    cached = _redis_get_json(redis_key)
    if cached is not None:
        return cached

    def _db(session: Session) -> dict[str, Any]:
        row = session.execute(
            text(
                """
                SELECT
                    count(*) AS pages_cached,
                    coalesce(sum(json_array_length(goods_payload)), 0) AS total_cached,
                    coalesce(max(page_offset + page_limit), 0) AS next_offset,
                    coalesce(max(page_limit), 1000) AS page_limit,
                    max(fetched_at) AS latest_fetched_at
                FROM wb_repricer_goods_cache
                WHERE organization_id = :organization_id
                """
            ),
            {"organization_id": organization_id},
        ).mappings().first()
        latest_fetched_at = _db_timestamp(row["latest_fetched_at"]) if row else None
        return {
            "pagesCached": int(row["pages_cached"] or 0) if row else 0,
            "totalCached": int(row["total_cached"] or 0) if row else 0,
            "nextOffset": int(row["next_offset"] or 0) if row else 0,
            "pageLimit": int(row["page_limit"] or 1000) if row else 1000,
            "latestFetchedAt": latest_fetched_at,
        }

    meta = _run_db(_db) or {
        "pagesCached": 0,
        "totalCached": 0,
        "nextOffset": 0,
        "pageLimit": 1000,
        "latestFetchedAt": None,
    }
    _redis_set_json(redis_key, meta)
    return meta


def replace_goods_pages(*, organization_id: int, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Publish only a complete WB catalog, atomically; failed refreshes keep old rows."""
    from app.repricer_bff import validate_catalog_goods_page

    if not pages:
        raise ValueError("WB catalog refresh has no pages")
    limit = pages[0]["limit"]
    seen: set[int] = set()
    for index, page in enumerate(pages):
        if page["offset"] != index * limit or page["limit"] != limit:
            raise ValueError("WB catalog pages are not contiguous")
        validate_catalog_goods_page(page["goods"], limit=limit, seen=seen)
        if index < len(pages) - 1 and len(page["goods"]) != limit:
            raise ValueError("WB catalog ended before its final page")
    if len(pages[-1]["goods"]) >= limit:
        raise ValueError("WB catalog refresh did not reach its final page")

    now = datetime.now(timezone.utc)
    # Do not use _run_db here: a failed write must not be reported as success.
    with get_session_factory()() as session:
        session.execute(delete(WbRepricerGoodsCacheRow).where(
            WbRepricerGoodsCacheRow.organization_id == organization_id,
        ))
        session.add_all([
            WbRepricerGoodsCacheRow(
                organization_id=organization_id, page_offset=page["offset"],
                page_limit=limit, goods_payload=page["goods"],
                wb_request_id=page.get("wbRequestId"), fetched_at=now, updated_at=now,
            )
            for page in pages
        ])
        session.commit()
    _redis_delete(_goods_meta_cache_key(organization_id), _goods_list_cache_key(organization_id))
    return {
        "pagesCached": len(pages), "totalCached": len(seen),
        "nextOffset": len(pages) * limit, "pageLimit": limit,
        "latestFetchedAt": now.isoformat(),
    }


def save_goods_page(
    *,
    organization_id: int,
    page_offset: int,
    page_limit: int,
    goods: list[dict[str, Any]],
    wb_request_id: str | None,
    replace_all: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    def _db(session: Session) -> dict[str, Any]:
        if replace_all:
            session.execute(delete(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id == organization_id))
        row = session.scalar(
            select(WbRepricerGoodsCacheRow).where(
                WbRepricerGoodsCacheRow.organization_id == organization_id,
                WbRepricerGoodsCacheRow.page_offset == page_offset,
                WbRepricerGoodsCacheRow.page_limit == page_limit,
            )
        )
        if row is None:
            row = WbRepricerGoodsCacheRow(
                organization_id=organization_id,
                page_offset=page_offset,
                page_limit=page_limit,
                goods_payload=goods,
                wb_request_id=wb_request_id,
                fetched_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.goods_payload = goods
            row.wb_request_id = wb_request_id
            row.fetched_at = now
            row.updated_at = now
        session.commit()
        _redis_delete(_goods_meta_cache_key(organization_id), _goods_list_cache_key(organization_id))
        return cached_goods_meta(organization_id)

    return _run_db(_db) or {
        "pagesCached": 0,
        "totalCached": 0,
        "nextOffset": 0,
        "pageLimit": page_limit,
        "latestFetchedAt": None,
    }


def update_confirmed_goods_in_session(session: Session, organization_id: int, goods: list[dict[str, Any]], observed_at: datetime) -> None:
    """Patch price fields only, within the caller's receipt transaction.

    Do not mark the other products in a page fresh or replace/flatten the catalog.
    The caller must supply only goods verified against its immutable intent.
    """
    from copy import deepcopy
    from app.repricer_price_safety import price_baseline
    by_nm = {item["nmID"]: item for item in goods}
    pages = session.scalars(select(WbRepricerGoodsCacheRow).where(
        WbRepricerGoodsCacheRow.organization_id == organization_id).with_for_update()).all()
    for page in pages:
        payload = deepcopy(page.goods_payload or [])
        changed = False
        for index, old in enumerate(payload):
            actual = by_nm.get(old.get("nmID"))
            if actual is None:
                continue
            if old.get("vendorCode") != actual.get("vendorCode"):
                raise ValueError("CONFIRMED_PRICE_CATALOG_IDENTITY_MISMATCH")
            item = {**old, **{key: actual[key] for key in ("discount", "clubDiscount", "editableSizePrice", "sizes") if key in actual}}
            item["priceObservedAt"] = observed_at.isoformat()
            item["priceBaseline"] = price_baseline(actual, observed_at.isoformat())
            payload[index] = item
            changed = True
        if changed:
            page.goods_payload = payload
            page.updated_at = observed_at
    # Only price-dependent repricer views. Finance/funnel/report facts stay intact.
    session.execute(delete(WbRepricerSourceCacheRow).where(
        WbRepricerSourceCacheRow.organization_id == organization_id,
        or_(WbRepricerSourceCacheRow.source_key.startswith("sku_list_snapshot_"),
            WbRepricerSourceCacheRow.source_key.startswith("stats_list_snapshot_"))))


def invalidate_confirmed_price_views(organization_id: int) -> None:
    _redis_delete(_goods_list_cache_key(organization_id))
    _redis_delete_pattern(f"vella:repricer:source-cache:{organization_id}:sku_list_snapshot_*")
    _redis_delete_pattern(f"vella:repricer:source-cache:{organization_id}:stats_list_snapshot_*")


def compact_heavy_source_cache_rows(organization_id: int) -> None:
    """Drop finance raw rows in Postgres without loading the full JSON into app memory."""
    _redis_delete_pattern(f"vella:repricer:source-cache:{organization_id}:finance_*")

    def _db(session: Session) -> None:
        now = datetime.now(timezone.utc)
        session.execute(
            text(
                """
                UPDATE wb_repricer_source_cache
                SET payload = (payload::jsonb - 'rows')::json,
                    updated_at = :updated_at
                WHERE organization_id = :organization_id
                  AND source_key LIKE 'finance_%'
                  AND (payload::jsonb ? 'rows')
                """
            ),
            {"organization_id": organization_id, "updated_at": now},
        )
        session.commit()

    _run_db(_db)


def get_source_cache(
    organization_id: int,
    source_key: str,
    *,
    slim: bool = False,
    strict: bool = False,
) -> dict[str, Any] | None:
    if source_key.startswith("baskets_"):
        start, end = _source_cache_range_from_key(source_key)
        if start and end:
            exported = get_source_cache(organization_id, f"funnel_seller_{start}_{end}", strict=strict)
            if exported and exported.get("source") == "seller_export":
                return exported
    redis_key = _source_cache_key(organization_id, source_key, slim=slim)
    redis_allowed = _source_cache_redis_allowed(source_key, slim=slim)
    if redis_allowed:
        cached = _redis_get_json(redis_key)
        if cached is not None:
            return cached

    def _db(session: Session) -> dict[str, Any] | None:
        row = session.scalar(
            select(WbRepricerSourceCacheRow).where(
                WbRepricerSourceCacheRow.organization_id == organization_id,
                WbRepricerSourceCacheRow.source_key == source_key,
            )
        )
        if row is None:
            return None
        payload = dict(row.payload or {})
        payload.setdefault("fetchedAt", row.fetched_at.isoformat())
        if slim:
            return slim_source_cache_payload(source_key, payload)
        return payload

    if strict:
        # Source selection must distinguish a missing row from unavailable storage.
        with get_session_factory()() as session:
            payload = _db(session)
    else:
        payload = _run_db(_db)
    if payload is not None and redis_allowed:
        _redis_set_json(redis_key, payload)
    return payload


def get_source_cache_by_source_key(
    source_key: str,
    *,
    slim: bool = False,
) -> dict[str, Any] | None:
    def _db(session: Session) -> dict[str, Any] | None:
        row = session.scalar(
            select(WbRepricerSourceCacheRow)
            .where(WbRepricerSourceCacheRow.source_key == source_key)
            .order_by(WbRepricerSourceCacheRow.fetched_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        payload = dict(row.payload or {})
        payload.setdefault("fetchedAt", row.fetched_at.isoformat())
        payload["organizationId"] = row.organization_id
        payload["sourceKey"] = row.source_key
        return slim_source_cache_payload(row.source_key, payload) if slim else payload

    return _run_db(_db)


def list_source_cache_by_prefix(
    organization_id: int,
    source_key_prefix: str,
    *,
    limit: int = 25,
    slim: bool = False,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(100, int(limit)))

    def _db(session: Session) -> list[dict[str, Any]]:
        rows = list(
            session.scalars(
                select(WbRepricerSourceCacheRow)
                .where(
                    WbRepricerSourceCacheRow.organization_id == organization_id,
                    WbRepricerSourceCacheRow.source_key.like(f"{source_key_prefix}%"),
                )
                .order_by(WbRepricerSourceCacheRow.fetched_at.desc())
                .limit(safe_limit)
            ).all()
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row.payload or {})
            payload.setdefault("fetchedAt", row.fetched_at.isoformat())
            payload["sourceKey"] = row.source_key
            result.append(slim_source_cache_payload(row.source_key, payload) if slim else payload)
        return result

    return _run_db(_db) or []


def list_source_cache_ranges_by_prefix(
    organization_id: int,
    source_key_prefix: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(500, int(limit)))

    def _db(session: Session) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT
                    source_key,
                    range_date_from,
                    range_date_to,
                    daily_detail_status,
                    daily_detail_error,
                    daily_detail_deferred_at,
                    daily_detail_paused_at,
                    daily_detail_failed_at,
                    daily_detail_fetched_at,
                    daily_detail_partial_at,
                    daily_detail_preserved_at,
                    daily_detail_preserved_by,
                    daily_detail_requests_completed,
                    daily_detail_requests_total,
                    daily_aggregate_dates,
                    revenue_basis,
                    finance_schema_version,
                    fetched_at
                FROM wb_repricer_source_cache
                WHERE organization_id = :organization_id
                  AND source_key LIKE :source_key_pattern
                ORDER BY fetched_at DESC
                LIMIT :limit
                """
            ),
            {
                "organization_id": organization_id,
                "source_key_pattern": f"{source_key_prefix}%",
                "limit": safe_limit,
            },
        ).mappings()
        result: list[dict[str, Any]] = []
        for row in rows:
            key_date_from, key_date_to = _source_cache_range_from_key(row["source_key"])
            range_date_from = row["range_date_from"] or key_date_from
            range_date_to = row["range_date_to"] or key_date_to
            raw_dates = row["daily_aggregate_dates"]
            # Raw SQL returns decoded JSON on PostgreSQL, but JSON text on
            # SQLite. Iterating that text would turn dates into characters and
            # make every saved period look missing to the refresh planner.
            if isinstance(raw_dates, str):
                try:
                    raw_dates = json.loads(raw_dates)
                except (ValueError, TypeError):
                    raw_dates = None
            daily_dates = sorted({parsed.isoformat() for value in raw_dates
                if (parsed := _optional_date(value)) is not None}) if isinstance(raw_dates, list) else []
            result.append(
                {
                    "sourceKey": row["source_key"],
                    "dateFrom": range_date_from.isoformat() if isinstance(range_date_from, date) else range_date_from,
                    "dateTo": range_date_to.isoformat() if isinstance(range_date_to, date) else range_date_to,
                    "dailyDetailStatus": row["daily_detail_status"],
                    "dailyDetailError": row["daily_detail_error"],
                    "dailyDetailDeferredAt": row["daily_detail_deferred_at"],
                    "dailyDetailPausedAt": row["daily_detail_paused_at"],
                    "dailyDetailFailedAt": row["daily_detail_failed_at"],
                    "dailyDetailFetchedAt": row["daily_detail_fetched_at"],
                    "dailyDetailPartialAt": row["daily_detail_partial_at"],
                    "dailyDetailPreservedAt": row["daily_detail_preserved_at"],
                    "dailyDetailPreservedBy": row["daily_detail_preserved_by"],
                    "dailyDetailRequestsCompleted": row["daily_detail_requests_completed"],
                    "dailyDetailRequestsTotal": row["daily_detail_requests_total"],
                    "dailyAggregatesDays": len(daily_dates) if isinstance(raw_dates, list) else None,
                    "dailyAggregateDates": daily_dates,
                    "revenueBasis": row["revenue_basis"],
                    "financeSchemaVersion": row["finance_schema_version"],
                    "fetchedAt": _db_timestamp(row["fetched_at"]),
                }
            )
        return result

    return _run_db(_db) or []


def get_covering_source_cache(
    organization_id: int,
    source_key_prefix: str,
    *,
    date_from: date,
    date_to: date,
    slim: bool = False,
) -> dict[str, Any] | None:
    def _db(session: Session) -> dict[str, Any] | None:
        conditions = [
            WbRepricerSourceCacheRow.organization_id == organization_id,
            WbRepricerSourceCacheRow.source_key.like(f"{source_key_prefix}%"),
            text("""json_typeof(payload) = 'object'
                AND json_typeof(payload->'dailyAggregates') = 'object'
                AND (payload->'dailyAggregates')::jsonb <> '{}'::jsonb
                AND (payload->>'dateFrom') <= :date_from
                AND (payload->>'dateTo') >= :date_to"""),
        ]
        if source_key_prefix == "finance_":
            conditions.append(~WbRepricerSourceCacheRow.source_key.like("finance_revenue_%"))
        if source_key_prefix in {"finance_", "baskets_", "period_stats_", "ads_"}:
            # Match the partial index to avoid scanning large saved report bodies.
            conditions.append(text("""(source_key LIKE 'finance_%' OR source_key LIKE 'baskets_%'
                OR source_key LIKE 'period_stats_%' OR source_key LIKE 'ads_%')"""))
        row = session.scalar(
            select(WbRepricerSourceCacheRow)
            .where(*conditions)
            .params(date_from=date_from.isoformat(), date_to=date_to.isoformat())
            .order_by(WbRepricerSourceCacheRow.fetched_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        payload = dict(row.payload or {})
        payload.setdefault("fetchedAt", row.fetched_at.isoformat())
        payload["sourceKey"] = row.source_key
        return slim_source_cache_payload(row.source_key, payload) if slim else payload

    return _run_db(_db)


def get_source_cache_fetched_at(organization_id: int, source_key: str) -> str | None:
    if _source_cache_redis_allowed(source_key, slim=True):
        cached = _redis_get_json(_source_cache_key(organization_id, source_key, slim=True))
        if cached is not None:
            fetched_at = cached.get("fetchedAt")
            return str(fetched_at) if fetched_at else None

    def _db(session: Session) -> str | None:
        row = session.scalar(
            select(WbRepricerSourceCacheRow.fetched_at).where(
                WbRepricerSourceCacheRow.organization_id == organization_id,
                WbRepricerSourceCacheRow.source_key == source_key,
            )
        )
        return row.isoformat() if row is not None else None

    return _run_db(_db)


def get_repricer_sources_revision(organization_id: int, *, date_from: date | None = None, date_to: date | None = None) -> str | None:
    """Small metadata read; never deserialize period payloads to check freshness."""
    def _db(session: Session) -> str | None:
        row = session.scalar(
            select(func.max(WbRepricerSourceCacheRow.fetched_at)).where(
                WbRepricerSourceCacheRow.organization_id == organization_id,
                or_(
                    WbRepricerSourceCacheRow.source_key.in_(("content_cards", "commission_tariffs", "stocks", "promotions", "promotion_thresholds", CONNECTION_KEY)),
                    and_(
                        or_(*(WbRepricerSourceCacheRow.source_key.like(f"{prefix}_%") for prefix in ("finance", "repricer_finance", "baskets", "period_stats", "ads", "funnel_seller"))),
                        or_(WbRepricerSourceCacheRow.range_date_from.is_(None), WbRepricerSourceCacheRow.range_date_from <= date_to) if date_to else True,
                        or_(WbRepricerSourceCacheRow.range_date_to.is_(None), WbRepricerSourceCacheRow.range_date_to >= date_from) if date_from else True,
                    ),
                ),
                ~WbRepricerSourceCacheRow.source_key.like("baskets_detail_%"),
                ~WbRepricerSourceCacheRow.source_key.like("repricer_finance_progress_%"),
            )
        )
        return row.isoformat() if row is not None else None

    return _run_db(_db)


def get_source_cache_range_revision(
    organization_id: int, source_key_prefix: str, *, date_from: date, date_to: date,
) -> str | None:
    """Detect source updates from metadata, including overlapping daily windows."""
    def _db(session: Session) -> str | None:
        fetched_at = session.scalar(select(func.max(WbRepricerSourceCacheRow.fetched_at)).where(
            WbRepricerSourceCacheRow.organization_id == organization_id,
            WbRepricerSourceCacheRow.source_key.like(f"{source_key_prefix}%"),
            or_(WbRepricerSourceCacheRow.range_date_from.is_(None), WbRepricerSourceCacheRow.range_date_from <= date_to),
            or_(WbRepricerSourceCacheRow.range_date_to.is_(None), WbRepricerSourceCacheRow.range_date_to >= date_from),
        ))
        return fetched_at.isoformat() if fetched_at is not None else None
    return _run_db(_db)


def get_source_cache_completion_flags(organization_id: int, source_key: str) -> dict[str, Any]:
    """Project only receipt flags, not a multi-megabyte operation ledger.

    Keep native JSON types: a missing flag or the string "true" is not a
    confirmed completed cursor. No Redis payload hydration is required.
    """
    def _db(session: Session) -> dict[str, Any]:
        def confirmed(key):
            if session.bind.dialect.name == "sqlite":
                # json_extract maps JSON true to integer 1 on SQLite; inspect
                # its type so strings/numbers cannot masquerade as receipts.
                return func.json_type(WbRepricerSourceCacheRow.payload, f"$.{key}") == "true"
            return cast(WbRepricerSourceCacheRow.payload[key], Text) == "true"
        row = session.execute(select(
            confirmed("sourceComplete"), confirmed("aggregateComplete"),
            WbRepricerSourceCacheRow.payload["requestedFields"],
            WbRepricerSourceCacheRow.payload["reportPeriod"],
            WbRepricerSourceCacheRow.payload["reportId"],
            WbRepricerSourceCacheRow.payload["sourceVersion"],
            WbRepricerSourceCacheRow.payload["operationCount"],
            WbRepricerSourceCacheRow.payload["currencies"],
            WbRepricerSourceCacheRow.payload["availableFields"],
        ).where(
            WbRepricerSourceCacheRow.organization_id == organization_id,
            WbRepricerSourceCacheRow.source_key == source_key,
        )).first()
        if not row:
            return {}
        result = {"sourceComplete": row[0] is True, "aggregateComplete": row[1] is True}
        result.update({key: value for key, value in zip(
            ("requestedFields", "reportPeriod", "reportId", "sourceVersion", "operationCount", "currencies", "availableFields"), row[2:]) if value is not None})
        return result
    return _run_db(_db) or {}


def get_source_cache_meta_fields(organization_id: int, source_key: str) -> dict[str, Any]:
    if _source_cache_redis_allowed(source_key, slim=True):
        cached = _redis_get_json(_source_cache_key(organization_id, source_key, slim=True))
        if cached is not None:
            fetched_at = cached.get("fetchedAt")
            if source_key.startswith("finance_"):
                return {
                    "fetchedAt": fetched_at,
                    "cachedGoodsNmIds": cached.get("cachedGoodsNmIds"),
                    "matchedCachedGoodsNmIds": cached.get("matchedCachedGoodsNmIds"),
                }
            if source_key.startswith("baskets_"):
                return {
                    "fetchedAt": fetched_at,
                    "requestedNmIds": cached.get("requestedNmIds"),
                    "matchedNmIds": cached.get("matchedNmIds"),
                }
            return {"fetchedAt": fetched_at} if fetched_at else {}

    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _db(session: Session) -> dict[str, Any]:
        row = session.execute(
            text(
                """
                SELECT
                    fetched_at,
                    payload->>'cachedGoodsNmIds' AS cached_goods_nm_ids,
                    payload->>'matchedCachedGoodsNmIds' AS matched_cached_goods_nm_ids,
                    payload->>'requestedNmIds' AS requested_nm_ids,
                    payload->>'matchedNmIds' AS matched_nm_ids
                FROM wb_repricer_source_cache
                WHERE organization_id = :organization_id
                  AND source_key = :source_key
                LIMIT 1
                """
            ),
            {"organization_id": organization_id, "source_key": source_key},
        ).mappings().first()
        if row is None:
            return {}
        fetched_at = _db_timestamp(row["fetched_at"])
        if source_key.startswith("finance_"):
            return {
                "fetchedAt": fetched_at,
                "cachedGoodsNmIds": _optional_int(row["cached_goods_nm_ids"]),
                "matchedCachedGoodsNmIds": _optional_int(row["matched_cached_goods_nm_ids"]),
            }
        if source_key.startswith("baskets_"):
            return {
                "fetchedAt": fetched_at,
                "requestedNmIds": _optional_int(row["requested_nm_ids"]),
                "matchedNmIds": _optional_int(row["matched_nm_ids"]),
            }
        return {"fetchedAt": fetched_at} if fetched_at else {}

    return _run_db(_db) or {}


def save_source_cache(organization_id: int, source_key: str, payload: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    if source_key == "stocks":
        from zoneinfo import ZoneInfo
        observed_day = datetime.now(ZoneInfo("Europe/Moscow")).date()
        save_source_cache(organization_id, f"stock_history_{observed_day.isoformat()}", payload, strict=strict)
    stored_payload = dict(payload)
    if source_key.startswith("finance_"):
        if isinstance(stored_payload.get("rows"), list):
            # Retain the small trade ledger needed for arbitrary date selections.
            # Full fee/diagnostic rows remain in the HTTP checkpoints.
            fields = ("rrdId", "nmId", "saleDt", "rrDate", "docTypeName", "quantity",
                      "retailPriceWithDisc", "retailAmount")
            stored_payload["revenueOperations"] = [
                {key: row.get(key) for key in fields}
                for row in stored_payload["rows"]
                if str(row.get("docTypeName") or "").strip().lower() in {"продажа", "возврат"}
            ]
        stored_payload.pop("rows", None)
    now = datetime.now(timezone.utc)
    metadata = _source_cache_metadata(source_key, stored_payload)
    _redis_delete(
        _source_cache_key(organization_id, source_key, slim=False),
        _source_cache_key(organization_id, source_key, slim=True),
    )
    redis_full_allowed = _source_cache_redis_allowed(source_key, slim=False)
    redis_slim_allowed = _source_cache_redis_allowed(source_key, slim=True)

    def _db(session: Session) -> dict[str, Any]:
        update_statement = (
            update(WbRepricerSourceCacheRow)
            .where(
                WbRepricerSourceCacheRow.organization_id == organization_id,
                WbRepricerSourceCacheRow.source_key == source_key,
            )
            .values(payload=stored_payload, fetched_at=now, updated_at=now, **metadata)
        )
        result = session.execute(update_statement)
        if result.rowcount == 0:
            row = WbRepricerSourceCacheRow(
                organization_id=organization_id,
                source_key=source_key,
                payload=stored_payload,
                fetched_at=now,
                updated_at=now,
                **metadata,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                session.execute(update_statement)
                session.commit()
        else:
            session.commit()
        result = dict(stored_payload)
        result["fetchedAt"] = now.isoformat()
        if redis_full_allowed:
            _redis_set_json(_source_cache_key(organization_id, source_key, slim=False), result)
        if redis_slim_allowed:
            _redis_set_json(_source_cache_key(organization_id, source_key, slim=True), slim_source_cache_payload(source_key, result))
        return result

    result = _run_db(_db)
    if result is not None:
        return result
    if strict:
        raise RuntimeError("Не удалось сохранить данные в БД. Повторите после восстановления соединения.")
    fallback = dict(stored_payload)
    fallback["fetchedAt"] = now.isoformat()
    return fallback
