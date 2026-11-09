"""Encrypted account order-status preview. Never an Orders writer or full run."""

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from app.avito.orders import AvitoOrdersFetchRequest, LiveAvitoOrdersClient
from app.control_plane.auth import ActorContext
from app.modules.orders import AVITO_ORDER_STATUS_MAPPING_VERSION, map_avito_status
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
    ResolvedCredentialForFetch,
    _resolve_fetch_in_session,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    ExpectedCredential,
    PublicationGuardError,
    acquire_publication_guard,
)
from app.wb_live.auth import require_live_actor
from app.wb_live.contracts import WbLiveError


class AccountOrderStatusError(ValueError):
    def __init__(self, code="AVITO_ORDER_STATUS_UNAVAILABLE"):
        self.code = (
            code
            if type(code) is str
            and code
            in {
                "AVITO_ORDER_STATUS_UNAVAILABLE",
                "AVITO_ORDER_STATUS_DISABLED",
                "AVITO_ORDER_STATUS_ACCESS_DENIED",
                "AVITO_ORDER_STATUS_INVALID_REQUEST",
            }
            else "AVITO_ORDER_STATUS_UNAVAILABLE"
        )
        super().__init__(self.code)


def validate_preview_request(date_from, page):
    if type(date_from) is not date or type(page) is not int or not 0 < page < 2**63:
        raise AccountOrderStatusError("AVITO_ORDER_STATUS_INVALID_REQUEST")


def _exact(value):
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value
        )
    ):
        raise AccountOrderStatusError()
    return value


def _source_timestamp(value):
    if (
        type(value) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            value,
        )
        is None
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return value  # exact source text, not a fabricated freshness/deadline.
    except ValueError:
        return None
    return None


@dataclass(frozen=True, slots=True, repr=False)
class OrderStatusPreviewRow:
    order_id: str
    raw_status: str
    canonical_status: str | None
    mapping_state: str
    mapping_version: str
    created_at: str | None
    updated_at: str | None
    account_evidence: str


@dataclass(frozen=True, slots=True, repr=False)
class OrderStatusPreviewPage:
    external_account_id: str
    has_more: bool | None
    rows: tuple[OrderStatusPreviewRow, ...]


def _preview_body(body, external):
    if (
        type(body) is not dict
        or type(body.get("orders")) is not list
        or len(body["orders"]) > 20
    ):
        raise AccountOrderStatusError()
    rows, sanitized, seen = [], [], set()
    # The existing adapter's evidenced aliases are row-level. Do not silently
    # ignore a conflicting/new top-level owner shape or assign it buyer semantics.
    if any(key in body for key in ("accountId", "userId", "sellerId")):
        raise AccountOrderStatusError()
    for raw in body["orders"]:
        if type(raw) is not dict:
            raise AccountOrderStatusError()
        identity = _exact(raw.get("id"))
        if identity in seen:
            raise AccountOrderStatusError()
        seen.add(identity)
        evidence = "credential_scope"
        for key in ("accountId", "userId", "sellerId"):
            if key not in raw:
                continue
            candidate = raw[key]
            if type(candidate) not in (str, int) or str(candidate) != external:
                raise AccountOrderStatusError()
            evidence = "provider_account_id"
        raw_status = _exact(raw.get("status"))
        mapped = map_avito_status(raw_status)
        rows.append(
            OrderStatusPreviewRow(
                identity,
                raw_status,
                str(mapped.canonical_status)
                if mapped.canonical_status is not None
                else None,
                str(mapped.mapping_state),
                AVITO_ORDER_STATUS_MAPPING_VERSION,
                _source_timestamp(raw.get("createdAt")),
                _source_timestamp(raw.get("updatedAt")),
                evidence,
            )
        )
        # Feed only supported identity/status facts into the existing adapter.
        # Never run legacy float money/quantity-default projection over this DTO.
        item = {"id": identity}
        if raw_status is not None:
            item["status"] = raw_status
        sanitized.append(item)
    has_more = body.get("hasMore")
    return OrderStatusPreviewPage(
        external, has_more if type(has_more) is bool else None, tuple(rows)
    ), {"orders": sanitized}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccountOrderStatusError()
        result[key] = value
    return result


def _invalid_constant(value):
    raise AccountOrderStatusError()


class _BoundedOrdersHTTP:
    def __init__(self, client, external):
        self.client, self.external = client, external
        self.called, self.page = False, None

    def get(self, url, *, params, headers):
        if (
            self.called
            or url != "https://api.avito.ru/order-management/1/orders"
            or params.get("limit") != 20
        ):
            raise AccountOrderStatusError()
        self.called = True
        start = time.monotonic()
        maximum = 4 * 1024 * 1024
        with self.client.stream(
            "GET",
            url,
            params=params,
            headers={**headers, "Accept-Encoding": "identity"},
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").strip().lower()
                != "identity"
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise AccountOrderStatusError()
            declared = response.headers.get("content-length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]{1,10}", declared, re.ASCII)
                or int(declared) > maximum
            ):
                raise AccountOrderStatusError()
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=65536):
                if len(body) + len(chunk) > maximum or time.monotonic() - start > 30:
                    raise AccountOrderStatusError()
                body.extend(chunk)
            if time.monotonic() - start > 30 or (
                declared is not None and len(body) != int(declared)
            ):
                raise AccountOrderStatusError()
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
        self.page, sanitized = _preview_body(value, self.external)
        return httpx.Response(200, json=sanitized, request=httpx.Request("GET", url))


class BoundedAvitoOrderStatusClient:
    def __init__(self, resolved, *, transport=None):
        self._resolved, self._transport = resolved, transport

    def fetch_preview(self, *, date_from, page):
        validate_preview_request(date_from, page)
        try:
            resolved = self._resolved
            if type(resolved) is not ResolvedCredentialForFetch:
                raise AccountOrderStatusError()
            binding, identity = resolved.binding, resolved.binding.credential_identity
            external = _exact(binding.external_account_id)
            if (
                not re.fullmatch(r"[1-9][0-9]{0,127}", external, re.ASCII)
                or binding.owner.provider != "avito"
                or identity.provider != "avito"
                or identity.credential_kind != "avito_oauth_access"
                or identity.payload_schema_version != 1
                or identity.organization_id != binding.owner.organization_id
                or identity.marketplace_account_id
                != binding.owner.marketplace_account_id
                or type(identity.expires_at) is not datetime
                or identity.expires_at.utcoffset() is None
                or identity.expires_at <= datetime.now(UTC)
            ):
                raise AccountOrderStatusError()
            secret = resolved.secret.reveal()
            if (
                set(secret) != {"accessToken", "expiresAt"}
                or type(secret["accessToken"]) is not str
                or not secret["accessToken"].strip()
                or secret["expiresAt"]
                != identity.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            ):
                raise AccountOrderStatusError()
            request = AvitoOrdersFetchRequest(
                dateFrom=date_from, statuses=[], limit=20, page=page
            )
            parser = LiveAvitoOrdersClient(secret["accessToken"])
            with httpx.Client(
                transport=self._transport,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                edge = _BoundedOrdersHTTP(client, external)
                result = parser.fetch_orders(request, http_client=edge)
            if (
                result.status != "synced"
                or edge.page is None
                or len(result.orders) != len(edge.page.rows)
            ):
                raise AccountOrderStatusError()
            for parsed, original in zip(result.orders, edge.page.rows):
                if parsed.orderId != original.order_id or parsed.rawStatus != (
                    original.raw_status or "unknown"
                ):
                    raise AccountOrderStatusError()
            return edge.page
        except Exception:  # noqa: BLE001, S110 -- never return raw provider, token or parser details.
            pass
        raise AccountOrderStatusError()


def _disabled(_org, _account):
    return False


def _version(session, org, account):
    return session.scalar(
        select(MarketplaceAccountRow.ingestion_binding_version)
        .where(
            MarketplaceAccountRow.organization_id == org,
            MarketplaceAccountRow.marketplace_account_id == account,
            MarketplaceAccountRow.marketplace == "avito",
            MarketplaceAccountRow.status == "connected",
        )
        .with_for_update()
    )


def _read_guard(session, principal, account, credential, version):
    def incarnation(current):
        if (
            current.new
            or current.dirty
            or current.deleted
            or _version(
                current, principal.organization_id, account.marketplace_account_id
            )
            != version
        ):
            raise AccountOrderStatusError()

    # Existing public guard stays last; account lock spans final check/commit.
    event.listen(session, "before_commit", incarnation)
    acquire_publication_guard(
        session,
        principal=principal,
        required_permissions=frozenset({"cabinet:read"}),
        accounts=(account,),
        authorities=(credential,),
    )
    if (
        _version(session, principal.organization_id, account.marketplace_account_id)
        != version
    ):
        raise AccountOrderStatusError()


def _wire_rows(result, external):
    if (
        type(result) is not OrderStatusPreviewPage
        or result.external_account_id != external
        or type(result.has_more) not in (bool, type(None))
        or type(result.rows) is not tuple
        or len(result.rows) > 20
    ):
        raise AccountOrderStatusError()
    seen, rows = set(), []
    for row in result.rows:
        if type(row) is not OrderStatusPreviewRow:
            raise AccountOrderStatusError()
        identity = _exact(row.order_id)
        mapped = map_avito_status(_exact(row.raw_status))
        canonical = (
            str(mapped.canonical_status)
            if mapped.canonical_status is not None
            else None
        )
        if (
            identity in seen
            or row.canonical_status != canonical
            or row.mapping_state != str(mapped.mapping_state)
            or row.mapping_version != AVITO_ORDER_STATUS_MAPPING_VERSION
            or row.account_evidence not in ("provider_account_id", "credential_scope")
            or (
                row.created_at is not None and _source_timestamp(row.created_at) is None
            )
            or (
                row.updated_at is not None and _source_timestamp(row.updated_at) is None
            )
        ):
            raise AccountOrderStatusError()
        seen.add(identity)
        rows.append(
            {
                "orderId": identity,
                "rawStatus": row.raw_status,
                "canonicalStatus": canonical,
                "mappingState": row.mapping_state,
                "mappingVersion": row.mapping_version,
                "createdAt": row.created_at,
                "updatedAt": row.updated_at,
                "accountEvidence": row.account_evidence,
            }
        )
    return rows


class AccountAvitoOrderStatusService:
    """Fresh live read authority on both sides of provider I/O; no Orders writes."""

    def __init__(
        self, *, engine, keyring_loader, client_factory, enabled_for=_disabled
    ):
        if (
            not isinstance(engine, Engine)
            or engine.dialect.name != "postgresql"
            or not all(
                callable(value)
                for value in (keyring_loader, client_factory, enabled_for)
            )
        ):
            raise AccountOrderStatusError()
        self._engine, self._keyring_loader = engine, keyring_loader
        self._client_factory, self._enabled_for = client_factory, enabled_for

    def preview(self, actor, *, marketplace_account_id, date_from, page):
        validate_preview_request(date_from, page)
        if (
            type(marketplace_account_id) is not int
            or not 0 < marketplace_account_id < 2**31
        ):
            raise AccountOrderStatusError("AVITO_ORDER_STATUS_INVALID_REQUEST")
        if (
            type(actor) is not ActorContext
            or type(actor.organization_id) is not int
            or not 0 < actor.organization_id < 2**31
            or not actor.session_id
        ):
            raise AccountOrderStatusError("AVITO_ORDER_STATUS_ACCESS_DENIED")
        failure = "AVITO_ORDER_STATUS_UNAVAILABLE"
        try:
            if (
                self._enabled_for(actor.organization_id, marketplace_account_id)
                is not True
            ):
                raise AccountOrderStatusError("AVITO_ORDER_STATUS_DISABLED")
            with Session(self._engine) as session, session.begin():
                principal, _ = require_live_actor(
                    session,
                    actor,
                    permission="cabinet:read",
                    account_id=marketplace_account_id,
                )
                version = _version(
                    session, actor.organization_id, marketplace_account_id
                )
                if type(version) is not int or version <= 0:
                    raise AccountOrderStatusError("AVITO_ORDER_STATUS_ACCESS_DENIED")
                resolved = _resolve_fetch_in_session(
                    session,
                    MarketplaceAccountCredentialOwner(
                        actor.organization_id, marketplace_account_id, "avito"
                    ),
                    "avito_oauth_access",
                    self._keyring_loader(),
                )
                binding, identity = (
                    resolved.binding,
                    resolved.binding.credential_identity,
                )
                external = _exact(binding.external_account_id)
                if not re.fullmatch(r"[1-9][0-9]{0,127}", external, re.ASCII):
                    raise AccountOrderStatusError()
                account = ExpectedAccountBinding(
                    marketplace_account_id, "avito", external, binding.credential_ref
                )
                credential = ExpectedCredential(
                    marketplace_account_id,
                    identity.credential_id,
                    "avito_oauth_access",
                    identity.generation,
                    identity.payload_schema_version,
                    identity.expires_at,
                )
                _read_guard(session, principal, account, credential, version)
            result = self._client_factory(resolved).fetch_preview(
                date_from=date_from, page=page
            )
            rows = _wire_rows(result, external)
            with Session(self._engine) as session, session.begin():
                # Same captured authority, not a fresh credential substituted for
                # the one that authorized HTTP. No key inventory/decrypt here.
                _read_guard(session, principal, account, credential, version)
            return {
                "marketplaceAccountId": marketplace_account_id,
                "provider": "avito",
                "externalAccountId": external,
                "dateFrom": date_from.isoformat(),
                "page": str(page),
                "limit": 20,
                "coverageState": "partial",
                "hasMore": result.has_more,
                "rows": rows,
            }
        except AccountOrderStatusError as error:
            failure = error.code
        except (WbLiveError, PublicationGuardError):
            failure = "AVITO_ORDER_STATUS_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- no SQL/credential/provider fallback or sensitive error chain.
            failure = "AVITO_ORDER_STATUS_UNAVAILABLE"
        raise AccountOrderStatusError(failure)
