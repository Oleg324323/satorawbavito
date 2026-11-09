"""Bounded WB product reads and purpose-bound, revision-fenced keyset cursors."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from sqlalchemy import text

from app.wb_live.contracts import ERROR_CODES
from app.wb_live.products_http import Product, ProductsPage, ProductsQuery, SourceState

_PURPOSE = b"satorna.wb-live.products.cursor.v1\x00"
_SORT_COLUMNS = {
    "nmId": "nm_id",
    "vendorCode": "vendor_code",
    "title": "title",
    "brand": "brand",
}
SIZE_LIMIT = 5


class ProductsReadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _encode(decoded) != value:
        raise ValueError
    return decoded


@dataclass(frozen=True)
class ProductPosition:
    revision: str
    nm_id: int
    value: str | None


class ProductsCursorCodec:
    def __init__(self, key: bytes):
        if type(key) is not bytes or len(key) < 32:
            raise ProductsReadError("WB_PRODUCTS_UNAVAILABLE")
        self._key = key

    def __repr__(self):
        return "<ProductsCursorCodec redacted>"

    def __reduce_ex__(self, protocol):
        raise TypeError("Cursor key is private")

    def issue(self, *, context, account_id, query, position):
        value = {
            "v": 1,
            "context": list(context),
            "account": account_id,
            "query": query.model_dump(),
            "revision": position.revision,
            "nm": position.nm_id,
            "value": position.value,
        }
        payload = _encode(_json(value))
        token = (
            payload
            + "."
            + _encode(
                hmac.digest(self._key, _PURPOSE + payload.encode("ascii"), "sha256")
            )
        )
        if len(token) > 16384:
            raise ProductsReadError("WB_PRODUCTS_UNAVAILABLE")
        return token

    def parse(self, token, *, context, account_id, query):
        try:
            if type(token) is not str or len(token) > 16384:
                raise ValueError
            payload, signature = token.split(".")
            expected = hmac.digest(
                self._key, _PURPOSE + payload.encode("ascii"), "sha256"
            )
            if not hmac.compare_digest(_decode(signature), expected):
                raise ValueError
            raw = _decode(payload)
            value = json.loads(raw)
            if type(value) is not dict or set(value) != {
                "v",
                "context",
                "account",
                "query",
                "revision",
                "nm",
                "value",
            }:
                raise ValueError
            if _json(value) != raw or type(value["v"]) is not int or value["v"] != 1:
                raise ValueError
            if (
                _json(value["context"]) != _json(list(context))
                or type(value["account"]) is not int
                or value["account"] != account_id
                or _json(value["query"]) != _json(query.model_dump())
            ):
                raise ValueError
            if type(value["nm"]) is not int or not 0 < value["nm"] < 2**63:
                raise ValueError
            if not isinstance(value["revision"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", value["revision"]
            ):
                raise ValueError
            if value["value"] is not None and type(value["value"]) is not str:
                raise ValueError
            return ProductPosition(value["revision"], value["nm"], value["value"])
        except (ValueError, TypeError, UnicodeError, binascii.Error, KeyError):
            raise ProductsReadError("WB_PRODUCTS_CURSOR_INVALID") from None


def product_selection(query: ProductsQuery, position: ProductPosition | None):
    """Only allowlisted identifiers enter SQL; search text is literal, bound data."""
    column = _SORT_COLUMNS[query.sort]
    ascending = query.direction == "asc"
    direction = "ASC" if ascending else "DESC"
    comparison = ">" if ascending else "<"
    nulls = "LAST" if ascending else "FIRST"
    order = f"p.{column} {direction} NULLS {nulls}"
    if column != "nm_id":
        order += f", p.nm_id {direction}"
    predicates = ["p.organization_id=:org", "p.marketplace_account_id=:account"]
    parameters = {"limit": query.limit + 1}
    if query.brand is not None:
        predicates.append("p.brand=:brand")
        parameters["brand"] = query.brand
    if query.q:
        escaped = query.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters["search"] = "%" + escaped + "%"
        predicates.append(
            "(p.title ILIKE :search ESCAPE '\\' OR p.vendor_code ILIKE :search ESCAPE '\\' OR CAST(p.nm_id AS TEXT) ILIKE :search ESCAPE '\\')"
        )
    if position is not None:
        parameters["after_nm"] = position.nm_id
        if column == "nm_id":
            predicates.append(f"p.nm_id {comparison} :after_nm")
        else:
            # Resolve the original key in the same snapshot. Display truncation
            # and arbitrarily large source text never become cursor identity.
            key = f"(SELECT b.{column} FROM wb_live_products b WHERE b.organization_id=:org AND b.marketplace_account_id=:account AND b.nm_id=:after_nm)"
            null_transition = (
                f"({key} IS NOT NULL AND p.{column} IS NULL)"
                if ascending
                else f"({key} IS NULL AND p.{column} IS NOT NULL)"
            )
            predicates.append(
                f"(p.{column} {comparison} {key} OR {null_transition} OR (p.{column} IS NOT DISTINCT FROM {key} AND p.nm_id {comparison} :after_nm))"
            )
    return " AND ".join(predicates), order, parameters


def source_readiness(sources, *, has_rows):
    complete = len(sources) == 2 and all(
        source.state == "completed" for source in sources
    )
    failed = any(source.state == "failed" for source in sources)
    if complete:
        return "ready" if has_rows else "empty"
    if failed and not has_rows:
        return "error"
    return "partial"


def page_statement(query, position):
    where, order, parameters = product_selection(query, position)
    # One statement gives source revisions and all nested rows one MVCC snapshot.
    # LATERAL index probes are bounded per selected product, not ORM N+1 queries.
    sql = f"""
    WITH latest_job AS (
      SELECT job_id FROM wb_live_sync_jobs
      WHERE organization_id=:org AND marketplace_account_id=:account
      ORDER BY created_at DESC,job_id DESC LIMIT 1
    ), sources AS (
      SELECT s.source,s.state,s.processed,s.updated_at,s.error_code,
             s.job_id,s.run_id,s.revision
      FROM wb_live_sync_sources s JOIN latest_job j ON j.job_id=s.job_id
      WHERE s.organization_id=:org AND s.marketplace_account_id=:account
    ), page AS (
      SELECT p.*
      FROM wb_live_products p WHERE {where}
      ORDER BY {order} LIMIT :limit
    )
    SELECT
      COALESCE((SELECT jsonb_agg(to_jsonb(s) ORDER BY source) FROM sources s),'[]'::jsonb) AS sources,
      COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'nmId',p.nm_id::text,'vendorCode',left(p.vendor_code,128),'title',left(p.title,128),'brand',left(p.brand,64),
        'subjectId',p.subject_id::text,'subjectName',left(p.subject_name,128),
        'photoUrl',CASE WHEN length(p.photo_url)<=512 THEN p.photo_url ELSE NULL END,
        'truncatedFields',array_remove(ARRAY[
          CASE WHEN length(p.vendor_code)>128 THEN 'vendorCode' END,
          CASE WHEN length(p.title)>128 THEN 'title' END,
          CASE WHEN length(p.brand)>64 THEN 'brand' END,
          CASE WHEN length(p.subject_name)>128 THEN 'subjectName' END,
          CASE WHEN length(p.photo_url)>512 THEN 'photoUrl' END],NULL),
        'contentUpdatedAt',p.content_updated_at,'pricesUpdatedAt',p.prices_updated_at,
        'sizes',sz.items,'sizesTruncated',sz.truncated
      ) ORDER BY {order}) FROM page p
      CROSS JOIN LATERAL (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
          'chrtId',s.chrt_id::text,'techSize',left(s.tech_size,32),
          'truncatedFields',CASE WHEN length(s.tech_size)>32 THEN ARRAY['techSize'] ELSE ARRAY[]::text[] END,
          'skus',CASE WHEN s.skus IS NULL THEN NULL ELSE '[]'::jsonb END,
          'skusTruncated',COALESCE(jsonb_array_length(s.skus),0)>0,
          'priceKopecks',s.price_kopecks::text,
          'discountedPriceKopecks',s.discounted_price_kopecks::text
        ) ORDER BY s.chrt_id) FILTER (WHERE s.position <= {SIZE_LIMIT}),'[]'::jsonb) AS items,
        count(*)>{SIZE_LIMIT} AS truncated
        FROM (
          SELECT z.*,row_number() OVER (ORDER BY chrt_id) AS position
          FROM wb_live_product_sizes z
          WHERE z.organization_id=:org AND z.marketplace_account_id=:account AND z.nm_id=p.nm_id
          ORDER BY chrt_id LIMIT {SIZE_LIMIT + 1}
        ) s
      ) sz),'[]'::jsonb) AS items
    """
    return text(sql), parameters


def assemble_page(raw, *, context, account_id, query, codec, position):
    source_rows = raw["sources"]
    vector = sorted(
        (s["source"], s["job_id"], s["run_id"], s["revision"]) for s in source_rows
    )
    revision = hashlib.sha256(_json(vector)).hexdigest()
    if position is not None and position.revision != revision:
        raise ProductsReadError("WB_PRODUCTS_CHANGED")
    by_source = {s["source"]: s for s in source_rows}
    sources = []
    for name in ("content", "prices"):
        s = by_source.get(name)
        code = s["error_code"] if s else None
        sources.append(
            SourceState(
                source=name,
                state=s["state"] if s else "idle",
                processed=s["processed"] if s else 0,
                updated_at=s["updated_at"] if s else None,
                error_code=code
                if code in ERROR_CODES
                else ("WB_LIVE_UNAVAILABLE" if code else None),
            )
        )
    items = [Product.model_validate(item) for item in raw["items"][: query.limit]]
    next_cursor = None
    if len(raw["items"]) > query.limit:
        last = items[-1]
        next_cursor = codec.issue(
            context=context,
            account_id=account_id,
            query=query,
            position=ProductPosition(revision, int(last.nm_id), None),
        )
    return ProductsPage(
        marketplace_account_id=account_id,
        items=items,
        next_cursor=next_cursor,
        read_version=revision,
        readiness=source_readiness(sources, has_rows=bool(items)),
        sources=sources,
    )


def read_products_page(session, *, actor, account_id, query, codec, cursor=None):
    from app.wb_live.auth import acquire_read_context

    if session.in_transaction():
        raise ProductsReadError("WB_PRODUCTS_UNAVAILABLE")
    with session.begin():
        guard = acquire_read_context(session, actor, marketplace_account_id=account_id)
        context = (actor.organization_id, actor.user_id, actor.session_id)
        position = (
            codec.parse(cursor, context=context, account_id=account_id, query=query)
            if cursor is not None
            else None
        )
        statement, parameters = page_statement(query, position)
        parameters.update(org=actor.organization_id, account=account_id)
        raw = session.execute(statement, parameters).mappings().one()
        result = assemble_page(
            raw,
            context=context,
            account_id=account_id,
            query=query,
            codec=codec,
            position=position,
        )
        guard.revalidate_before_write()
    return result
