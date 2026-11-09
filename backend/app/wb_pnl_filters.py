"""P&L filter attributes from saved sources only; no sync or financial math."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.control_plane.auth import actor_from_request
from app.control_plane.store import assert_permission_or_audit
from app.infra.db import get_session_factory
from app.platform.economics.legacy_tax import _prepare_read
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_cache.store import get_source_cache, list_source_cache_ranges_by_prefix
from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerChangelogRow, WbRepricerAlgorithmSettingsRow
from app.wb_pnl_advertising import _instant, _source_context
from app.wb_api.price_units import wb_goods_price_to_kopecks


class PnlFilterRequest(BaseModel):
    dateFrom: date
    dateTo: date
    nmIds: list[int] = Field(max_length=10_000)

    @model_validator(mode="after")
    def valid_selection(self):
        if not 1 <= (self.dateTo - self.dateFrom).days + 1 <= 90 or any(nm <= 0 for nm in self.nmIds):
            raise ValueError("Invalid P&L product selection")
        return self


def _number(row, *keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            value = Decimal(str(value))
            if value.is_finite() and value >= 0:
                return float(value)
        except (InvalidOperation, ValueError):
            pass
    return None


def _garment(subject):
    # Category comes from WB content, never the title or an article prefix.
    raw = str(subject or "").strip().casefold()
    for key, words in (("shirt", ("футбол", "t-shirt", "tshirt")), ("hoodie", ("худи", "hoodie")),
                       ("longsleeve", ("лонгслив", "longsleeve")), ("shorts", ("шорты", "shorts")),
                       ("balaclava", ("балаклав", "balaclava"))):
        if any(word in raw for word in words):
            return key
    return None


def _attrs(card):
    result = set()
    for attribute in card.get("characteristics") or []:
        name = str(attribute.get("name") or "").casefold()
        values = attribute.get("value") or []
        raw = " ".join(map(str, values if isinstance(values, list) else [values])).casefold().replace("ё", "е")
        if name in {"пол", "gender"}:
            if any(word in raw for word in ("жен", "female", "woman", "унисекс", "unisex")):
                result.add("female")
            if any(word in raw for word in ("муж", "male", "men", "унисекс", "unisex")) and "female" not in raw:
                result.add("male")
        if name in {"цвет", "color", "colour"}:
            if "бел" in raw or "white" in raw:
                result.add("white")
            if "черн" in raw or "black" in raw:
                result.add("black")
    return sorted(result)


def _saved_cache(org, key, context):
    value = get_source_cache(org, key, slim=False) or {}
    fetched = _instant(value.get("fetchedAt"))
    if (not fetched or fetched < _instant(context["connectedAt"])
            or value.get("organizationId", org) != org
            or value.get("marketplaceAccountId", context["marketplaceAccountId"]) != context["marketplaceAccountId"]):
        return {}
    return value


def _period_cache(org, prefix, payload, context):
    value = _saved_cache(org, f"{prefix}_{payload.dateFrom}_{payload.dateTo}", context)
    def complete(cache):
        return (cache and cache.get("sourceComplete") is not False and cache.get("aggregateComplete") is not False
                and cache.get("coverageState") in (None, "ok") and not cache.get("missingDates"))
    if (complete(value) and str(value.get("dateFrom", ""))[:10] == str(payload.dateFrom)
            and str(value.get("dateTo", ""))[:10] == str(payload.dateTo)):
        return value.get("aggregates") or {}
    # Reuse a complete saved daily series for a custom subrange. Never apply
    # another range's aggregate totals, nor sum its stock snapshot across days.
    days = [str(payload.dateFrom + timedelta(days=i)) for i in range((payload.dateTo - payload.dateFrom).days + 1)]
    fields = {"ordersUnits": ("ordersUnits",)} if prefix == "period_stats" else {
        "cartCount": ("cartCount", "cartAdds", "addToCartCount", "basketCount", "baskets"),
        "orderCount": ("orderCount", "ordersCount", "orders")}
    for meta in list_source_cache_ranges_by_prefix(org, prefix + "_", limit=100):
        if not str(meta.get("dateFrom") or "")[:10] <= str(payload.dateFrom) <= str(payload.dateTo) <= str(meta.get("dateTo") or "")[:10]:
            continue
        cache = _saved_cache(org, meta["sourceKey"], context)
        daily = cache.get("dailyAggregates") or {}
        if not complete(cache) or not all(isinstance(daily.get(day), dict) for day in days):
            continue
        result = {}
        for nm in map(str, payload.nmIds):
            entries = [daily[day].get(nm) for day in days]
            if not any(isinstance(entry, dict) for entry in entries):
                continue
            row = {}
            for field, aliases in fields.items():
                amounts = [_number(entry or {}, *aliases) for entry in entries]
                row[field] = sum(amounts) if all(amount is not None for amount in amounts) else None
            row["wbStockUnits"] = _number((cache.get("aggregates") or {}).get(nm) or {}, "wbStockUnits")
            result[nm] = row
        return result
    return {}


def _saved_floors(org, payload, context, inputs_updated):
    """Reuse the Repricer's effective floor, not merely its explicit override.

    A missing explicit P_min is NOT proof that a calculated floor is absent.
    Cold/stale snapshots stay unknown; this reader must never build a repricer.
    """
    from app.routers.wb_repricer_bff import _repricer_sku_snapshot_chunk_key, SKU_LIST_SNAPSHOT_VERSION
    from app.repricer_bff import _effective_pmin
    ids = set(payload.nmIds)
    for meta in list_source_cache_ranges_by_prefix(org, "sku_list_snapshot_", limit=100):
        if (str(meta.get("dateFrom") or "")[:10] != str(payload.dateFrom)
                or str(meta.get("dateTo") or "")[:10] != str(payload.dateTo)):
            continue
        snapshot = _saved_cache(org, meta["sourceKey"], context)
        observed = _instant(snapshot.get("builtAt") or snapshot.get("fetchedAt"))
        expiry = _instant(snapshot.get("browserPricesExpireAt"))
        if (snapshot.get("version") != SKU_LIST_SNAPSHOT_VERSION or not observed
                or inputs_updated and observed < inputs_updated or expiry and expiry <= datetime.now(timezone.utc)):
            continue
        items = snapshot.get("items") or []
        if snapshot.get("storage") == "chunked":
            items = []
            valid = True
            for index in range(min(int(snapshot.get("chunksCount") or 0), 100)):
                chunk = _saved_cache(org, _repricer_sku_snapshot_chunk_key(meta["sourceKey"], index), context)
                if not chunk or chunk.get("generation") != snapshot.get("generation"):
                    valid = False
                    break
                items.extend(chunk.get("items") or [])
            if not valid:
                continue
        result = {}
        for item in items:
            nm = (item.get("meta") or {}).get("nmId")
            if nm not in ids:
                continue
            try:
                result[nm] = _effective_pmin(item)[0]
            except (TypeError, ValueError, KeyError, OverflowError):
                pass
        if result:
            return result
    return {}


def load_filter_metadata(org, payload):
    context = _source_context(org)
    if context is None:
        raise HTTPException(409, "Нет однозначного подключённого аккаунта WB для фильтров P&L")
    ids = set(payload.nmIds)
    with get_session_factory()() as session:
        _prepare_read(session, org)
        runtime_row = session.get(WbRepricerRuntimeStateRow, org)
        algorithm_row = session.get(WbRepricerAlgorithmSettingsRow, org)
        inputs_updated = max((_instant(row.updated_at) for row in (runtime_row, algorithm_row) if row and row.updated_at), default=None)
        runtime = (runtime_row.state_payload if runtime_row else {}) or {}
        goods = {}
        for page in session.scalars(select(WbRepricerGoodsCacheRow).where(
                WbRepricerGoodsCacheRow.organization_id == org,
                WbRepricerGoodsCacheRow.fetched_at >= _instant(context["connectedAt"]))):
            for good in page.goods_payload or []:
                nm = good.get("nmID") or good.get("nmId")
                if nm in ids:
                    goods[nm] = good
        changed = set()
        now = datetime.now(timezone.utc)
        for row in session.scalars(select(WbRepricerChangelogRow).where(
                WbRepricerChangelogRow.organization_id == org,
                WbRepricerChangelogRow.created_at >= now - timedelta(days=1))):
            entry = row.entry_payload or {}
            action_time = _instant(entry.get("timestamp"))
            if (action_time and now - timedelta(days=1) <= action_time <= now
                    and entry.get("oldPriceKopecks") is not None and entry.get("newPriceKopecks") is not None
                    and entry["oldPriceKopecks"] != entry["newPriceKopecks"]):
                changed.add(row.article_id)
        session.rollback()
    cards = _saved_cache(org, "content_cards", context)
    for card in cards.get("cards") or []:
        nm = card.get("nmID") or card.get("nmId")
        if nm in ids:
            goods[nm] = {**card, **goods.get(nm, {}), "subjectName": card.get("subjectName"),
                         "characteristics": card.get("characteristics") or []}
    baskets = _period_cache(org, "baskets", payload, context)
    orders = _period_cache(org, "period_stats", payload, context)
    stock_cache = _saved_cache(org, "stocks", context)
    stocks = stock_cache.get("aggregates") or {}
    promotions = _saved_cache(org, "promotions", context)
    from app.repricer_bff import _active_promotion_nm_ids
    promoted = _active_promotion_nm_ids(promotions.get("promotions") or [])
    floors = _saved_floors(org, payload, context, inputs_updated)
    products = {}
    for nm in ids:
        good = goods.get(nm, {})
        article = str(good.get("vendorCode") or "")
        meta = (runtime.get("skuMetaOverrides") or {}).get(article) or {}
        settings = (runtime.get("skuSettingsOverrides") or {}).get(article) or {}
        assignment = (runtime.get("assignments") or {}).get(article) or {}
        funnel = baskets.get(str(nm)) or {}
        stock = stocks.get(str(nm)) or {}
        status = meta.get("status")
        if article in (runtime.get("liquidationActive") or {}):
            status = "liquidation"
        elif good and status != "warmup":
            status = "manual" if settings.get("automationEnabled") is False else status or "auto"
        sizes = good.get("sizes") or []
        price_row = sizes[0] if sizes else good
        price_raw = price_row.get("discountedPrice")
        if price_raw is None:
            price_raw = price_row.get("price")
        price = wb_goods_price_to_kopecks(price_raw) if _number({"price": price_raw}, "price") is not None else None
        local_price = _number(meta, "currentPriceKopecks")
        if local_price is not None and str(meta.get("localPriceOverrideSource") or "") not in {"wb_api", "wb_api_pending"}:
            price = round(local_price)
        products[str(nm)] = {
            "managerId": meta.get("managerId"), "managerName": meta.get("managerName"), "managerKnown": bool(good),
            "brand": good.get("brand") or None, "status": status,
            "promotion": nm in promoted if isinstance(promotions.get("promotions"), list) else None,
            "garment": _garment(good.get("subjectName") or good.get("subject")), "attrs": _attrs(good),
            "strategyId": assignment.get("strategyId"), "strategyName": assignment.get("strategyName") or assignment.get("strategyId"),
            "stockUnits": _number(stock, "wbStockUnits", "stockUnits"),
            "currentPriceKopecks": price,
            "pminKopecks": floors.get(nm, _number(settings, "pMinKopecks") if (_number(settings, "pMinKopecks") or 0) > 0 else None),
            "changed24h": article in changed,
            "baskets": _number(funnel, "cartCount", "cartAdds", "addToCartCount", "basketCount", "baskets"),
            "orders": _number(funnel, "orderCount", "ordersCount", "orders"),
        }
        if products[str(nm)]["orders"] is None:
            products[str(nm)]["orders"] = _number(orders.get(str(nm)) or {}, "ordersUnits")
        if products[str(nm)]["stockUnits"] is None:
            # Same saved funnel stock snapshot used by the Repricer, not a sum of daily stock.
            products[str(nm)]["stockUnits"] = _number(funnel, "wbStockUnits")
    return {"marketplaceAccountId": context["marketplaceAccountId"], "dateFrom": str(payload.dateFrom),
            "dateTo": str(payload.dateTo), "products": products}


def pnl_filter_metadata(request: Request, payload: PnlFilterRequest):
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.pnl.filters",
        object_type="wb_report", object_id="pnl", reason="actor cannot read P&L filter attributes")
    return load_filter_metadata(actor.organization_id, payload)
