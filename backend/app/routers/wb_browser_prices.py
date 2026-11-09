"""Account-bound browser observations; never writes marketplace prices."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.cabinet.orm import LkSessionRow
from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import get_session_factory
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.wb_credentials import WbCredentialBindingError, resolve_bound_wb_credential
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest, build_wb_client
from app.wb_api.price_units import wb_goods_price_to_kopecks
from app.wb_live.auth import require_live_actor
from app.wb_live.contracts import WbLiveError

router = APIRouter(prefix="/api/v1/wb/browser-prices", tags=["wb-browser-prices"])
CONNECTION = "wb_browser_prices_connection"
PRICES = "wb_browser_prices"
TOKEN_KEY = "wb_browser_prices_token_"
MAX_AGE = timedelta(minutes=30)
REFRESH_MAX_PAGES = 100
REFRESH_COOLDOWN = 60
PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marketplaceAccountId: PositiveInt


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nmId: PositiveInt
    sizeId: PositiveInt
    sellerPriceKopecks: PositiveInt
    buyerPriceNoWalletKopecks: PositiveInt
    buyerPriceWithWalletKopecks: PositiveInt | None
    originalPriceKopecks: PositiveInt | None = None
    observedAt: AwareDatetime

    @field_validator("observedAt", mode="before")
    @classmethod
    def iso_timestamp(cls, value):
        if not isinstance(value, str):
            raise ValueError("ISO timestamp required")
        return value

    @model_validator(mode="after")
    def compatible_prices(self):
        if self.originalPriceKopecks is not None and self.originalPriceKopecks < self.buyerPriceNoWalletKopecks:
            raise ValueError("Original price is below buyer price")
        if self.buyerPriceNoWalletKopecks > self.sellerPriceKopecks:
            raise ValueError("Buyer price exceeds seller price")
        if self.buyerPriceWithWalletKopecks is not None and self.buyerPriceWithWalletKopecks > self.buyerPriceNoWalletKopecks:
            raise ValueError("Wallet price exceeds buyer price")
        return self


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goodsRevision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    items: Annotated[list[Observation], Field(min_length=1, max_length=100)]


def _deny(code="WB_BROWSER_ACCESS_DENIED", status=403):
    raise HTTPException(status, detail={"code": code})


def _date(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        return parsed.astimezone(timezone.utc) if isinstance(parsed, datetime) and parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _now(session):
    return session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)


@contextmanager
def _session():
    try:
        with get_session_factory()() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            yield session
    except (WbLiveError, WbCredentialBindingError):
        _deny()
    except SQLAlchemyError:
        _deny("WB_BROWSER_STORAGE_UNAVAILABLE", 503)


def _cache(session, org, key, *, write=False):
    return session.scalar(select(WbRepricerSourceCacheRow).where(
        WbRepricerSourceCacheRow.organization_id == org,
        WbRepricerSourceCacheRow.source_key == key,
    ).with_for_update(read=not write).execution_options(populate_existing=True))


def _save(session, org, key, payload, now):
    row = _cache(session, org, key, write=True)
    if row is None:
        session.add(WbRepricerSourceCacheRow(organization_id=org, source_key=key, payload=payload, fetched_at=now))
    else:
        row.payload, row.fetched_at = payload, now


def _account(session, actor, account_id, *, write=False):
    require_live_actor(session, actor, permission="integrations:write", account_id=account_id)
    account = session.scalar(select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace_account_id == account_id,
    ).with_for_update(read=not write))
    if account is None or account.marketplace != "wb" or account.status != "connected":
        _deny()
    connected = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected",
    ).limit(2)).all()
    if connected != [account_id]:
        _deny("WB_BROWSER_ACCOUNT_AMBIGUOUS", 409)
    return account


def _binding_matches(account, payload):
    return (payload.get("organizationId"), payload.get("marketplaceAccountId"),
            payload.get("externalAccountId"), payload.get("credentialRef"), payload.get("ingestionBindingVersion")) == (
        account.organization_id, account.marketplace_account_id,
        account.external_account_id, account.credential_ref, account.ingestion_binding_version)


def _issuer(payload):
    if (type(payload.get("organizationId")) is not int or payload["organizationId"] <= 0
            or type(payload.get("marketplaceAccountId")) is not int or payload["marketplaceAccountId"] <= 0
            or not isinstance(payload.get("issuerUserId"), str) or not payload["issuerUserId"]
            or not isinstance(payload.get("issuerSessionId"), str) or not payload["issuerSessionId"]):
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    return ActorContext(payload["issuerUserId"], payload["issuerUserId"], payload["organizationId"],
                        "custom", frozenset(), session_id=payload["issuerSessionId"])


def _check_metadata(session, account, payload, now):
    expiry = _date(payload.get("expiresAt"))
    if not _binding_matches(account, payload) or expiry is None or expiry <= now:
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    actor = _issuer(payload)
    require_live_actor(session, actor, permission="integrations:write", account_id=account.marketplace_account_id)
    login = session.get(LkSessionRow, actor.session_id)
    if expiry > login.expires_at:
        _deny("WB_BROWSER_TOKEN_INVALID", 401)


def _bearer(request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not re.fullmatch(r"sat_wbp_[A-Za-z0-9_-]{43}", token):
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    return hashlib.sha256(token.encode()).hexdigest()


def _extension(session, digest, *, write=False):
    rows = session.scalars(select(WbRepricerSourceCacheRow).where(
        WbRepricerSourceCacheRow.source_key == TOKEN_KEY + digest).limit(2)).all()
    if len(rows) != 1 or not isinstance(rows[0].payload, dict):
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    payload = rows[0].payload
    actor = _issuer(payload)
    if rows[0].organization_id != actor.organization_id:
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    account = _account(session, actor, payload["marketplaceAccountId"], write=write)
    connection = _cache(session, actor.organization_id, CONNECTION, write=write)
    active = connection.payload if connection is not None and isinstance(connection.payload, dict) else {}
    if not hmac.compare_digest(str(active.get("activeHash") or ""), digest) or any(active.get(k) != v for k, v in payload.items()):
        _deny("WB_BROWSER_TOKEN_INVALID", 401)
    now = _now(session)
    _check_metadata(session, account, payload, now)
    return account, now


def _references(session, org):
    # Match list_cached_goods' vendor-code dedupe and first-size selection, while
    # retaining the source timestamp and blocking updates to these source pages.
    pages = session.scalars(select(WbRepricerGoodsCacheRow).where(
        WbRepricerGoodsCacheRow.organization_id == org,
    ).order_by(WbRepricerGoodsCacheRow.page_offset, WbRepricerGoodsCacheRow.page_limit).with_for_update(read=True)).all()
    seen, items = set(), {}
    for page in pages:
        for good in page.goods_payload or []:
            if not isinstance(good, dict):
                continue
            identity = str(good.get("vendorCode") or good.get("nmID") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            sizes = good.get("sizes")
            first = sizes[0] if isinstance(sizes, list) and sizes and isinstance(sizes[0], dict) else {}
            nm, size = good.get("nmID"), first.get("sizeID")
            if type(nm) is not int or nm <= 0 or type(size) is not int or size <= 0:
                continue
            try:
                price = wb_goods_price_to_kopecks(first.get("discountedPrice")) or wb_goods_price_to_kopecks(first.get("price"))
            except (ValueError, OverflowError):
                continue
            if price > 0:
                items.setdefault(f"{nm}:{size}", {"nmId": nm, "sizeId": size, "sellerPriceKopecks": price,
                                                  "sellerPriceObservedAt": _date(page.fetched_at).isoformat()})
    encoded = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), items


def _retained_items(payload, account, now):
    """Keep saved values until a newer observation replaces that exact offer.

    Catalogue refreshes, partial passes and elapsed time must not prune another
    offer's last observation. Foreign account bindings remain excluded.
    """
    if (payload.get("marketplaceAccountId") != account.marketplace_account_id
            or payload.get("accountBindingVersion") != account.ingestion_binding_version
            or not isinstance(payload.get("items"), dict)):
        return {}
    result = {}
    for key, item in payload["items"].items():
        if not isinstance(item, dict):
            continue
        times = [_date(item.get("observedAt")), _date(item.get("sellerPriceObservedAt"))]
        if (key == f"{item.get('nmId')}:{item.get('sizeId')}"
                and all(value is not None and value <= now + timedelta(seconds=30) for value in times)):
            result[key] = item
    return result


def _fresh_items(payload, account, references, now):
    """Saved observations compatible with the current exact seller-price reference."""
    result = {}
    for key, item in _retained_items(payload, account, now).items():
        ref = references.get(key)
        if not ref or item.get("sellerPriceKopecks") != ref["sellerPriceKopecks"]:
            continue
        reference_at = _date(ref["sellerPriceObservedAt"])
        if reference_at is not None and reference_at <= now + timedelta(seconds=30):
            result[key] = item
    return result


def _status(session, account, now):
    result = {"marketplaceAccountId": account.marketplace_account_id, "configured": False,
              "tokenPrefix": None, "expiresAt": None, "lastObservedAt": None, "knownPrices": 0}
    connection = _cache(session, account.organization_id, CONNECTION)
    metadata = connection.payload if connection is not None and isinstance(connection.payload, dict) else {}
    digest = metadata.get("activeHash")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return result
    token = _cache(session, account.organization_id, TOKEN_KEY + digest)
    if token is None or not isinstance(token.payload, dict) or any(metadata.get(k) != v for k, v in token.payload.items()):
        return result
    try:
        _check_metadata(session, account, token.payload, now)
    except (HTTPException, WbLiveError):
        return result
    _, references = _references(session, account.organization_id)
    stored = _cache(session, account.organization_id, PRICES)
    items = _fresh_items(stored.payload if stored is not None else {}, account, references, now)
    return {**result, "configured": True, "tokenPrefix": metadata.get("tokenPrefix"), "expiresAt": metadata["expiresAt"],
            "knownPrices": len(items), "lastObservedAt": max((item["observedAt"] for item in items.values()), default=None)}


@router.get("/accounts")
def get_accounts(request: Request):
    actor = actor_from_request(request)
    with _session() as session:
        _, membership = require_live_actor(session, actor, permission="integrations:write")
        statement = select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == actor.organization_id,
            MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")
        if membership.scope_mode != "all":
            statement = statement.where(MarketplaceAccountRow.marketplace_account_id.in_(
                [int(value) for value in membership.allowed_account_ids]))
        accounts = session.scalars(statement.order_by(MarketplaceAccountRow.marketplace_account_id)).all()
        result = {"data": [{"marketplaceAccountId": account.marketplace_account_id, "provider": "wb",
                            "externalAccountId": account.external_account_id, "displayName": None,
                            "status": "connected"} for account in accounts]}
    return result


@router.get("/connection")
def get_connection(request: Request, marketplaceAccountId: int = Query(gt=0)):
    actor = actor_from_request(request)
    with _session() as session:
        account = _account(session, actor, marketplaceAccountId)
        result = _status(session, account, _now(session))
    return result


@router.post("/connection")
def create_connection(request: Request, payload: ConnectionRequest):
    actor = actor_from_request(request)
    with _session() as session:
        account = _account(session, actor, payload.marketplaceAccountId, write=True)
        now = _now(session)
        expiry = min(now + timedelta(days=30), _date(session.get(LkSessionRow, actor.session_id).expires_at))
        if expiry <= now:
            _deny()
        token = "sat_wbp_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        metadata = {"organizationId": actor.organization_id, "marketplaceAccountId": account.marketplace_account_id,
                    "issuerUserId": actor.user_id, "issuerSessionId": actor.session_id,
                    "externalAccountId": account.external_account_id, "credentialRef": account.credential_ref,
                    "ingestionBindingVersion": account.ingestion_binding_version, "expiresAt": expiry.isoformat()}
        old = _cache(session, actor.organization_id, CONNECTION, write=True)
        if old is not None and isinstance(old.payload, dict) and old.payload.get("activeHash"):
            session.execute(delete(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.organization_id == actor.organization_id,
                            WbRepricerSourceCacheRow.source_key == TOKEN_KEY + str(old.payload["activeHash"])))
        _save(session, actor.organization_id, TOKEN_KEY + digest, metadata, now)
        _save(session, actor.organization_id, CONNECTION, {**metadata, "activeHash": digest, "tokenPrefix": token[:16]}, now)
        _save(session, actor.organization_id, PRICES, {"marketplaceAccountId": account.marketplace_account_id,
              "accountBindingVersion": account.ingestion_binding_version, "items": {}}, now)
        result = {"marketplaceAccountId": account.marketplace_account_id, "configured": True,
                  "tokenPrefix": token[:16], "expiresAt": expiry.isoformat(), "lastObservedAt": None, "knownPrices": 0, "token": token}
    return result


@router.delete("/connection")
def delete_connection(request: Request, marketplaceAccountId: int = Query(gt=0)):
    actor = actor_from_request(request)
    with _session() as session:
        account = _account(session, actor, marketplaceAccountId, write=True)
        now = _now(session)
        old = _cache(session, actor.organization_id, CONNECTION, write=True)
        if old is not None and isinstance(old.payload, dict) and old.payload.get("activeHash"):
            session.execute(delete(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.organization_id == actor.organization_id,
                            WbRepricerSourceCacheRow.source_key == TOKEN_KEY + str(old.payload["activeHash"])))
        _save(session, actor.organization_id, CONNECTION, {"marketplaceAccountId": marketplaceAccountId, "activeHash": None}, now)
        _save(session, actor.organization_id, PRICES, {"marketplaceAccountId": marketplaceAccountId,
              "accountBindingVersion": account.ingestion_binding_version, "items": {}}, now)
        result = {"marketplaceAccountId": marketplaceAccountId, "configured": False, "tokenPrefix": None,
                  "expiresAt": None, "lastObservedAt": None, "knownPrices": 0}
    return result


@router.get("/catalog")
def get_catalog(request: Request, page: int = Query(default=1, ge=1), pageSize: int = Query(default=100, ge=1, le=100)):
    digest = _bearer(request)
    with _session() as session:
        account, now = _extension(session, digest)
        revision, references = _references(session, account.organization_id)
        items = [ref for ref in references.values() if now - MAX_AGE <= _date(ref["sellerPriceObservedAt"]) <= now + timedelta(seconds=30)]
        if not items:
            _deny("WB_BROWSER_GOODS_STALE", 409)
        result = {"marketplaceAccountId": account.marketplace_account_id, "goodsRevision": revision,
                  "page": page, "pageSize": pageSize, "total": len(items), "items": items[(page - 1) * pageSize:page * pageSize],
                  "maxAgeSeconds": 1800}
    return result


def _retry(code, status=503, seconds=REFRESH_COOLDOWN):
    seconds = max(REFRESH_COOLDOWN, math.ceil(seconds))
    raise HTTPException(status, detail={"code": code, "retryAfterSeconds": seconds},
                        headers={"Retry-After": str(seconds)})


@contextmanager
def _refresh_lock(organization_id):
    # Session-level lock on a dedicated autocommit connection: no DB transaction
    # or user/account row locks survive while the provider is being called.
    try:
        with get_session_factory()() as session:
            engine = session.get_bind()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            params = {"org": organization_id}
            if not connection.scalar(text("SELECT pg_try_advisory_lock(1463964240, :org)"), params):
                _retry("WB_BROWSER_REFRESH_BUSY", 409)
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(1463964240, :org)"), params)
    except SQLAlchemyError:
        _retry("WB_BROWSER_STORAGE_UNAVAILABLE")


def _refresh_failed(digest, *, status=503, seconds=REFRESH_COOLDOWN):
    seconds = max(REFRESH_COOLDOWN, seconds)
    with _session() as session:
        account, now = _extension(session, digest, write=True)
        row = _cache(session, account.organization_id, CONNECTION, write=True)
        row.payload = {**row.payload, "catalogRefreshNotBefore": (now + timedelta(seconds=seconds)).isoformat()}
    _retry("WB_BROWSER_REFRESH_COOLDOWN" if status == 429 else "WB_BROWSER_REFRESH_UNAVAILABLE", status, seconds)


@router.post("/catalog/refresh")
def refresh_catalog(request: Request):
    digest = _bearer(request)
    with _session() as session:
        account, _ = _extension(session, digest)
        org = account.organization_id
    with _refresh_lock(org):
        with _session() as session:
            account, now = _extension(session, digest, write=True)
            old_revision, references = _references(session, org)
            if references and all(now - MAX_AGE <= _date(ref["sellerPriceObservedAt"]) <= now + timedelta(seconds=30)
                                  for ref in references.values()):
                return {"ok": True, "goodsRevision": old_revision, "total": len(references)}
            connection = _cache(session, org, CONNECTION, write=True)
            retry_at = _date(connection.payload.get("catalogRefreshNotBefore"))
            if retry_at is not None and retry_at > now:
                _retry("WB_BROWSER_REFRESH_COOLDOWN", 429, (retry_at - now).total_seconds())
            _, credential_ref, secret = resolve_bound_wb_credential(session, org,
                marketplace_account_id=account.marketplace_account_id, credential_ref=account.credential_ref)
            connection.payload = {**connection.payload,
                                  "catalogRefreshNotBefore": (now + timedelta(seconds=REFRESH_COOLDOWN)).isoformat()}

        try:
            provider = build_wb_client(token_override=secret)
        except Exception:
            _refresh_failed(digest)
        client = RateLimitedWbApiClient(inner=provider)
        deadline, sleep = time.monotonic() + 120, client._sleep
        def bounded_sleep(seconds):
            if time.monotonic() + seconds >= deadline:
                raise TimeoutError()
            sleep(seconds)
        client._sleep = bounded_sleep  # Retain the existing shared provider quota.
        pages, seen = [], set()
        try:
            for page in range(REFRESH_MAX_PAGES):
                with _session() as session:
                    account, _ = _extension(session, digest)
                    resolve_bound_wb_credential(session, org, marketplace_account_id=account.marketplace_account_id,
                                                credential_ref=credential_ref, wb_token=secret)
                if time.monotonic() >= deadline:
                    _refresh_failed(digest)
                try:
                    response = client.request(WbApiRequest(method="GET", path="/api/v2/list/goods/filter",
                                                          query={"limit": 1000, "offset": page * 1000}))
                except Exception:
                    _refresh_failed(digest)
                if not response.ok:
                    retry = response.rateLimit.retryAfterSeconds if response.rateLimit is not None else None
                    _refresh_failed(digest, status=429 if response.statusCode == 429 else 503,
                                    seconds=max(REFRESH_COOLDOWN, retry or 0))
                payload = response.data
                if isinstance(payload, dict) and payload.get("error") is not True and isinstance(payload.get("data"), dict):
                    payload = payload["data"]
                goods = payload.get("listGoods") if isinstance(payload, dict) and payload.get("error") is not True else None
                if (not isinstance(goods, list) or len(goods) > 1000
                        or any(not isinstance(good, dict) or type(good.get("nmID")) is not int or good["nmID"] <= 0
                               or good["nmID"] in seen for good in goods)):
                    _refresh_failed(digest)
                seen.update(good["nmID"] for good in goods)
                pages.append((page * 1000, goods, datetime.now(timezone.utc), response.wbRequestId))
                if len(goods) < 1000:
                    break
            else:
                _refresh_failed(digest)
        finally:
            transport = getattr(provider, "_client", None)
            if transport is not None:
                transport.close()

        with _session() as session:
            account, _ = _extension(session, digest, write=True)
            resolve_bound_wb_credential(session, org, marketplace_account_id=account.marketplace_account_id,
                                        credential_ref=credential_ref, wb_token=secret, lock=True)
            if _references(session, org)[0] != old_revision:
                _deny("WB_BROWSER_GOODS_CHANGED", 409)
            session.execute(delete(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id == org))
            session.add_all([WbRepricerGoodsCacheRow(organization_id=org, page_offset=offset, page_limit=1000,
                goods_payload=goods, fetched_at=fetched_at, wb_request_id=request_id)
                for offset, goods, fetched_at, request_id in pages])
            session.flush()
            revision, references = _references(session, org)
            result = {"ok": True, "goodsRevision": revision, "total": len(references)}
        from app.repricer_cache import store
        store._redis_delete(store._goods_meta_cache_key(org), store._goods_list_cache_key(org))
    return result


@router.post("/snapshots")
def post_snapshots(request: Request, payload: SnapshotRequest):
    digest = _bearer(request)
    with _session() as session:
        account, now = _extension(session, digest, write=True)
        revision, references = _references(session, account.organization_id)
        if revision != payload.goodsRevision:
            _deny("WB_BROWSER_GOODS_CHANGED", 409)
        for item in payload.items:
            ref = references.get(f"{item.nmId}:{item.sizeId}")
            if (ref is None or item.sellerPriceKopecks != ref["sellerPriceKopecks"]
                    or not now - MAX_AGE <= _date(ref["sellerPriceObservedAt"]) <= now + timedelta(seconds=30)):
                _deny("WB_BROWSER_GOODS_CHANGED", 409)
            if not now - timedelta(minutes=5) <= item.observedAt <= now + timedelta(seconds=30):
                _deny("WB_BROWSER_OBSERVATION_TIME_INVALID", 422)
        stored = _cache(session, account.organization_id, PRICES, write=True)
        previous = stored.payload if stored is not None and isinstance(stored.payload, dict) else {}
        items = _retained_items(previous, account, now)
        accepted = ignored = 0
        for item in payload.items:
            key = f"{item.nmId}:{item.sizeId}"
            if key in items and item.observedAt <= _date(items[key]["observedAt"]):
                ignored += 1
                continue
            items[key] = {**item.model_dump(mode="json"), "observedAt": item.observedAt.astimezone(timezone.utc).isoformat(),
                          "sellerPriceObservedAt": references[key]["sellerPriceObservedAt"], "source": "wb_browser"}
            accepted += 1
        if accepted or previous.get("items") != items:
            _save(session, account.organization_id, PRICES, {"marketplaceAccountId": account.marketplace_account_id,
                  "accountBindingVersion": account.ingestion_binding_version, "items": items}, now)
        result = {"ok": True, "accepted": accepted, "ignored": ignored, "observedAt": now.isoformat()}
    return result
