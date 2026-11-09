"""Identity-only bootstrap for the funnel; no offers, costs, tax or prices.

Only an absent catalog is recovered. Existing catalog populations are left
alone. Reuse authenticated, complete WB listings, never organization-only
legacy caches or the products active in a particular reporting period.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from sqlalchemy import select

from app.infra.db import set_tenant_context
from app.platform.catalog.orm import MarketplaceProductRow as Product
from app.platform.integrations.wb_credentials import resolve_bound_wb_credential
from app.repricer_cache.orm import WbRepricerSourceCacheRow as Cache
from app.wb_api.client import WbApiRequest, build_wb_client, build_wb_content_client
from app.wb_api.report_reads import READ_TTL_SECONDS, active_report_read


SOURCE = "wb_sales_funnel_catalog_v1"


def catalog_state(session, org, account, scope):
    set_tenant_context(session, org)
    if session.scalar(select(Product.marketplace_product_id).where(
        Product.organization_id == org, Product.marketplace_account_id == account).limit(1)):
        return "ready"
    receipt = session.scalar(select(Cache.payload).where(
        Cache.organization_id == org, Cache.source_key == f"{scope}_catalog_v1")) or {}
    if (receipt.get("source") == SOURCE and receipt.get("account") == account
            and receipt.get("completeListing") is True
            and type(receipt.get("products")) is int and receipt["products"] == 0):
        try:
            observed = datetime.fromisoformat(receipt["fetchedAt"])
            if timedelta(0) <= datetime.now(timezone.utc) - observed <= timedelta(seconds=READ_TTL_SECONDS):
                return "empty"
        except (KeyError, ValueError, TypeError):
            pass
    return "missing"


def _checkpoint(session, org, token, host, request, after=None):
    identity = json.dumps([host, sha256(token.encode()).hexdigest(), request.model_dump(mode="json")],
                          sort_keys=True, separators=(",", ":"))
    row = session.execute(select(Cache.payload, Cache.fetched_at).where(
        Cache.organization_id == org,
        Cache.source_key == "wb_report_http_" + sha256(identity.encode()).hexdigest())).first()
    if not row:
        return None
    response, fetched = row.payload.get("response") or {}, row.fetched_at
    fetched = fetched.replace(tzinfo=timezone.utc) if fetched.tzinfo is None else fetched
    if (response.get("request") != request.model_dump(mode="json")
            or response.get("ok") is not True or response.get("statusCode") != 200
            or fetched > datetime.now(timezone.utc) or (after and fetched < after)):
        return None
    return response.get("data"), fetched


def _saved_goods(session, org, token):
    from app.repricer_bff import validate_catalog_goods_page
    host = build_wb_client("complete", token_override=token).base_url
    goods, seen, first, last = [], set(), None, None
    for offset in range(0, 1000001, 1000):
        page = None
        for method, body in (("POST", {"limit": 1000, "offset": offset}), ("POST", None), ("GET", None)):
            request = WbApiRequest(method=method, path="/api/v2/list/goods/filter",
                                   query={"limit": 1000, "offset": offset}, jsonBody=body)
            page = _checkpoint(session, org, token, host, request, last)
            if page:
                break
        if not page:
            return None
        payload, last = page
        first = first or last
        payload = payload.get("data", payload) if isinstance(payload, dict) else {}
        items = payload.get("listGoods")
        validate_catalog_goods_page(items, limit=1000, seen=seen)
        goods.extend(items)
        if len(items) < 1000:
            # Identities are durable, not prices. Only a negative catalog needs
            # the existing read TTL, otherwise a once-empty account stays empty.
            if not goods and datetime.now(timezone.utc) - first > timedelta(seconds=READ_TTL_SECONDS):
                return None
            return goods, first
    raise ValueError("WB_FUNNEL_CATALOG_PAGINATION_INVALID")


def _saved_cards(session, org, token):
    from app.repricer_bff import validate_catalog_goods_page
    host = build_wb_content_client("complete", token_override=token).base_url
    cards, seen, cursor, first, last = [], set(), {}, None, None
    while len(cards) <= 1000000:
        request = WbApiRequest(method="POST", path="/content/v2/get/cards/list",
                               jsonBody={"settings": {"cursor": {"limit": 100, **cursor}}})
        cached = _checkpoint(session, org, token, host, request, last)
        if not cached:
            return None
        payload, last = cached
        first = first or last
        items = payload.get("cards") if isinstance(payload, dict) else None
        validate_catalog_goods_page(items, limit=100, seen=seen)
        cards.extend(items)
        if len(items) < 100:
            if not cards and datetime.now(timezone.utc) - first > timedelta(seconds=READ_TTL_SECONDS):
                return None
            return cards, first
        next_cursor = payload.get("cursor") or {}
        next_cursor = {key: next_cursor.get(key) for key in ("updatedAt", "nmID")}
        if (not isinstance(next_cursor["updatedAt"], str) or not next_cursor["updatedAt"].strip()
                or type(next_cursor["nmID"]) is not int or next_cursor["nmID"] <= 0
                or next_cursor == cursor):
            raise ValueError("WB_FUNNEL_CATALOG_PAGINATION_INVALID")
        cursor = next_cursor
    raise ValueError("WB_FUNNEL_CATALOG_PAGINATION_INVALID")


def ensure_catalog(session, org, account, scope, token, heartbeat=lambda: None):
    """Called inside the existing serialized, cancellable funnel job only."""
    if catalog_state(session, org, account, scope) != "missing":
        return
    from app.repricer_bff import fetch_catalog_goods_page, _fetch_content_cards, _extract_wb_media_url, validate_catalog_goods_page
    from app.wb_sales_funnel import scope_key, upsert

    saved = _saved_goods(session, org, token)
    if saved is None:
        goods, seen = [], set()
        fetched_goods = datetime.now(timezone.utc)
        for offset in range(0, 1000001, 1000):
            heartbeat()
            page = fetch_catalog_goods_page("complete", wb_token=token, offset=offset, enrich_spp=False)["goods"]
            validate_catalog_goods_page(page, limit=1000, seen=seen)
            goods.extend(page)
            if len(page) < 1000:
                break
        else:
            raise ValueError("WB_FUNNEL_CATALOG_PAGINATION_INVALID")
    else:
        goods, fetched_goods = saved
    saved = _saved_cards(session, org, token)
    if saved is None:
        fetched_cards = datetime.now(timezone.utc)
        cards = _fetch_content_cards("complete", wb_token=token, progress_callback=lambda _: heartbeat())
        validate_catalog_goods_page(cards, limit=len(cards), seen=set())
    else:
        cards, fetched_cards = saved

    # Both complete catalog sources, not just cards with sales in this period.
    # nmID is the identity; a repeated seller article must not merge products.
    rows = {}
    def text(value, limit):
        return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else None
    for item in [*goods, *cards]:
        row = rows.setdefault(str(item["nmID"]), {})
        for field, value, limit in (
            ("seller_article", item.get("vendorCode"), 128),
            ("title", item.get("title") or item.get("name"), 512),
            ("brand", item.get("brand"), 255),
            ("image_url", _extract_wb_media_url(item), 2048),
        ):
            if (value := text(value, limit)) is not None:
                row[field] = value

    heartbeat()
    if context := active_report_read.get():
        context.check()
    # Lock/revalidate the real credential owner and account before publication.
    # No offers, SKU mappings, economic histories, existing product edits, or
    # writes to the Repricer's goods/content caches are part of this bootstrap.
    bound, _, current_token = resolve_bound_wb_credential(session, org,
        marketplace_account_id=account, wb_token=token, lock=True)
    if scope_key(bound, current_token) != scope:
        raise RuntimeError("WB_ACCOUNT_CHANGED")
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    set_tenant_context(session, org)
    values = [{"organization_id": org, "marketplace_account_id": account, "external_product_id": nm,
               **{field: row.get(field) for field in ("seller_article", "title", "brand", "image_url")}}
              for nm, row in rows.items()]
    for offset in range(0, len(values), 100):
        session.execute(insert(Product).values(values[offset:offset+100]).on_conflict_do_nothing(
            index_elements=["organization_id", "marketplace_account_id", "external_product_id"]))
    fetched = min(fetched_goods, fetched_cards)
    upsert(session, org, [{"source_key": f"{scope}_catalog_v1", "fetched_at": fetched,
        "payload": {"source": SOURCE, "account": account, "completeListing": True,
                    "products": len(rows), "fetchedAt": fetched.isoformat()}}])
    return True
