"""Account-scoped statistics projection; no legacy cache or credential fallback."""

import json
import re
import time
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from app.avito.stats import (
    AvitoStatsFetchRequest,
    AvitoStatsFetchResult,
    LiveAvitoStatsClient,
)
from app.control_plane.auth import ActorContext
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

_METRICS = (
    "impressions",
    "views",
    "contactsMessenger",
    "contacts",
    "contactsShowPhone",
    "contactsShowPhoneAndMessenger",
    "favorites",
    "spendKopecks",
    "orders",
    "buyouts",
)


class AccountStatsError(ValueError):
    def __init__(self, code="AVITO_ACCOUNT_STATS_UNAVAILABLE"):
        self.code = (
            code
            if type(code) is str
            and code
            in {
                "AVITO_ACCOUNT_STATS_UNAVAILABLE",
                "AVITO_ACCOUNT_STATS_DISABLED",
                "AVITO_ACCOUNT_STATS_ACCESS_DENIED",
                "AVITO_ACCOUNT_STATS_INVALID_REQUEST",
            }
            else "AVITO_ACCOUNT_STATS_UNAVAILABLE"
        )
        super().__init__(self.code)


def validate_period(date_from, date_to):
    # Existing application window, not a claim about a provider-imposed limit.
    if (
        type(date_from) is not date
        or type(date_to) is not date
        or not 0 <= (date_to - date_from).days < 270
    ):
        raise AccountStatsError("AVITO_ACCOUNT_STATS_INVALID_REQUEST")


def _metrics(row):
    result = {}
    for name in _METRICS:
        value = getattr(row, name)
        if value is not None and (type(value) is not int or value < 0):
            raise AccountStatsError()
        result[name] = None if value is None else str(value)
    return result


def project_stats_result(result, *, external_id, date_from, date_to):
    validate_period(date_from, date_to)
    if (
        type(result) is not AvitoStatsFetchResult
        or result.status not in ("synced", "partial")
        or len(result.accounts) != 1
        or len(result.items) != 1
        or len(result.daily) > (date_to - date_from).days + 1
    ):
        raise AccountStatsError()
    if any(
        row.accountId != external_id
        for row in (*result.accounts, *result.items, *result.daily)
    ):
        raise AccountStatsError()
    if result.items[0].itemId != f"account:{external_id}:totals":
        raise AccountStatsError()
    seen = set()
    for row in result.daily:
        if (
            type(row.date) is not date
            or not date_from <= row.date <= date_to
            or row.date in seen
        ):
            raise AccountStatsError()
        seen.add(row.date)
    if result.items[0].sourceStatus not in ("fresh", "partial", "stale"):
        raise AccountStatsError()
    return {
        "externalAccountId": external_id,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "status": result.status,
        "rows": [
            {
                "itemId": row.itemId,
                "sourceStatus": row.sourceStatus,
                "metrics": _metrics(row),
            }
            for row in result.items
        ],
        "daily": [
            {"date": row.date.isoformat(), "metrics": _metrics(row)}
            for row in result.daily
        ],
    }


def _external_id(value):
    if type(value) is not str or not re.fullmatch(
        r"[1-9][0-9]{0,127}", value, re.ASCII
    ):
        raise AccountStatsError()
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccountStatsError()
        result[key] = value
    return result


def _no_constant(_):
    raise AccountStatsError()


def _validate_payload(payload, request):
    """Reject lossy coercions before reusing the legacy parser.

    Explicit totals nulls remain absent observations, not measured zeros. Daily
    aggregation requires every date and the same fully observed metric set.
    Otherwise the legacy parser would silently turn partial days into a total.
    """
    result = payload.get("result") if type(payload) is dict else None
    if type(result) is not dict:
        raise AccountStatsError()
    groups = result.get("groupings")
    daily = False
    if groups is not None and "metrics" in result:
        raise AccountStatsError()
    if "dataTotalCount" in result and (
        type(result["dataTotalCount"]) is not int
        or result["dataTotalCount"] != (len(groups) if type(groups) is list else 1)
    ):
        # No silently truncated totals page and no speculative follow-up call.
        raise AccountStatsError()
    if groups is None and type(result.get("metrics")) is list:
        metric_lists = [result["metrics"]]
    elif (
        type(groups) is list
        and 0 < len(groups) <= (request.dateTo - request.dateFrom).days + 1
    ):
        kinds, seen = set(), set()
        for group in groups:
            if type(group) is not dict:
                raise AccountStatsError()
            kind = str(group.get("type", "")).lower()
            if kind in ("totals", "total"):
                if len(groups) != 1:
                    raise AccountStatsError()
                kinds.add("total")
            elif kind in ("date", "dates", "day", "days"):
                day = LiveAvitoStatsClient._v2_totals_group_date(group)
                if (
                    day is None
                    or not request.dateFrom <= day <= request.dateTo
                    or day in seen
                ):
                    raise AccountStatsError()
                seen.add(day)
                kinds.add("day")
            else:
                raise AccountStatsError()
        if len(kinds) != 1:
            raise AccountStatsError()
        daily = kinds == {"day"}
        if daily and len(seen) != (request.dateTo - request.dateFrom).days + 1:
            raise AccountStatsError()
        metric_lists = [group.get("metrics") for group in groups]
    else:
        raise AccountStatsError()
    expected_daily_metrics = None
    for metrics in metric_lists:
        if type(metrics) is not list or len(metrics) > 1000:
            raise AccountStatsError()
        seen = set()
        retained = []
        for metric in metrics:
            if (
                type(metric) is not dict
                or type(metric.get("slug")) is not str
                or not metric["slug"]
            ):
                raise AccountStatsError()
            if metric["slug"] in seen or "value" not in metric:
                raise AccountStatsError()
            seen.add(metric["slug"])
            value = metric["value"]
            if value is None:
                if daily:
                    raise AccountStatsError()
                continue
            if type(value) is not int or value < 0:
                raise AccountStatsError()
            retained.append(metric)
        if daily:
            if not seen or (
                expected_daily_metrics is not None and seen != expected_daily_metrics
            ):
                raise AccountStatsError()
            expected_daily_metrics = seen
        metrics[:] = retained
    return payload


class _BoundedTotalsHTTP:
    """Small post-only edge consumed by the existing totals method."""

    def __init__(self, client, request, external_id):
        self.client, self.request = client, request
        self.url = f"https://api.avito.ru/stats/v2/accounts/{external_id}/items"
        self.called = False

    def post(self, url, *, json, headers):
        if (
            self.called
            or url != self.url
            or json.get("grouping") != "totals"
            or json.get("limit") != 1
            or json.get("offset") != 0
        ):
            raise AccountStatsError()
        self.called = True
        started = time.monotonic()
        # Same 4 MiB budget as the bounded provider adapters; not a provider limit.
        maximum = 4 * 1024 * 1024
        with self.client.stream(
            "POST", url, json=json, headers={**headers, "Accept-Encoding": "identity"}
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
                raise AccountStatsError()
            declared = response.headers.get("content-length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]{1,10}", declared, re.ASCII)
                or int(declared) > maximum
            ):
                raise AccountStatsError()
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=65536):
                if len(body) + len(chunk) > maximum or time.monotonic() - started > 30:
                    raise AccountStatsError()
                body.extend(chunk)
            if time.monotonic() - started > 30 or (
                declared is not None and len(body) != int(declared)
            ):
                raise AccountStatsError()
        parsed = _validate_payload(
            _decode_payload(body),
            self.request,
        )
        # Construct only a bounded in-memory response for the unchanged parser.
        return httpx.Response(200, json=parsed, request=httpx.Request("POST", url))


def _decode_payload(body):
    return json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_no_constant,
    )


class BoundedAvitoTotalsClient:
    """Explicit encrypted authority, one bounded request, existing parser reuse."""

    def __init__(self, resolved, *, transport=None):
        self._resolved, self._transport = resolved, transport

    def fetch_stats(self, request):
        try:
            resolved = self._resolved
            if type(resolved) is not ResolvedCredentialForFetch:
                raise AccountStatsError()
            binding, identity = resolved.binding, resolved.binding.credential_identity
            external = _external_id(binding.external_account_id)
            if (
                identity.provider != "avito"
                or identity.credential_kind != "avito_oauth_access"
                or identity.organization_id != binding.owner.organization_id
                or identity.marketplace_account_id
                != binding.owner.marketplace_account_id
                or binding.owner.provider != "avito"
                or identity.payload_schema_version != 1
                or type(identity.expires_at) is not datetime
                or identity.expires_at.tzinfo is None
                or identity.expires_at <= datetime.now(UTC)
                or request.accountIds != [external]
                or request.grouping != "totals"
            ):
                raise AccountStatsError()
            validate_period(request.dateFrom, request.dateTo)
            # Unwrap only here, immediately at the actual provider edge.
            payload = resolved.secret.reveal()
            if (
                set(payload) != {"accessToken", "expiresAt"}
                or type(payload["accessToken"]) is not str
                or not payload["accessToken"].strip()
                or payload["expiresAt"]
                != identity.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            ):
                raise AccountStatsError()
            parser = LiveAvitoStatsClient(payload["accessToken"])
            with httpx.Client(
                transport=self._transport,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                edge = _BoundedTotalsHTTP(client, request, external)
                accounts = parser._accounts(edge, request.accountIds)
                items, daily = parser._stats_totals(edge, request, accounts)
            return AvitoStatsFetchResult(
                status="synced", accounts=accounts, items=items, daily=daily
            )
        except Exception:  # noqa: BLE001, S110 -- discard sensitive transport/credential exception chain.
            pass
        raise AccountStatsError()


def _disabled(_org, _account):
    return False


def _account_version(session, organization_id, account_id):
    return session.scalar(
        select(MarketplaceAccountRow.ingestion_binding_version)
        .where(
            MarketplaceAccountRow.organization_id == organization_id,
            MarketplaceAccountRow.marketplace_account_id == account_id,
            MarketplaceAccountRow.marketplace == "avito",
            MarketplaceAccountRow.status == "connected",
        )
        .with_for_update()
    )


def _guard(session, principal, account, credential, version):
    def check_incarnation(current):
        if (
            current.new
            or current.dirty
            or current.deleted
            or _account_version(
                current, principal.organization_id, account.marketplace_account_id
            )
            != version
        ):
            raise AccountStatsError()

    # Register before the shared guard: its existing final authorization/expiry
    # callback must remain the last effective before_commit listener.
    event.listen(session, "before_commit", check_incarnation)
    acquire_publication_guard(
        session,
        principal=principal,
        required_permissions=frozenset({"cabinet:read"}),
        accounts=(account,),
        authorities=(credential,),
    )
    if (
        _account_version(
            session, principal.organization_id, account.marketplace_account_id
        )
        != version
    ):
        raise AccountStatsError()


class AccountAvitoStatsService:
    """Read-only fresh auth → closed root → HTTP → same authority → closed root.

    No current DB state is substituted for the identity that authorized fetch.
    No Session/ORM, secret or diagnostics are included in the returned DTO.
    """

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
            raise AccountStatsError()
        self._engine, self._keyring_loader = engine, keyring_loader
        self._client_factory, self._enabled_for = client_factory, enabled_for

    def fetch(self, actor, *, marketplace_account_id, date_from, date_to):
        validate_period(date_from, date_to)
        if (
            type(marketplace_account_id) is not int
            or not 0 < marketplace_account_id < 2**31
        ):
            raise AccountStatsError("AVITO_ACCOUNT_STATS_INVALID_REQUEST")
        if (
            type(actor) is not ActorContext
            or type(actor.organization_id) is not int
            or not 0 < actor.organization_id < 2**31
            or not actor.session_id
        ):
            raise AccountStatsError("AVITO_ACCOUNT_STATS_ACCESS_DENIED")
        failure = "AVITO_ACCOUNT_STATS_UNAVAILABLE"
        try:
            if (
                self._enabled_for(actor.organization_id, marketplace_account_id)
                is not True
            ):
                raise AccountStatsError("AVITO_ACCOUNT_STATS_DISABLED")
            with Session(self._engine) as session, session.begin():
                principal, _ = require_live_actor(
                    session,
                    actor,
                    permission="cabinet:read",
                    account_id=marketplace_account_id,
                )
                version = _account_version(
                    session, actor.organization_id, marketplace_account_id
                )
                if type(version) is not int or version <= 0:
                    raise AccountStatsError("AVITO_ACCOUNT_STATS_ACCESS_DENIED")
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
                external = _external_id(binding.external_account_id)
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
                _guard(session, principal, account, credential, version)
            result = self._client_factory(resolved).fetch_stats(
                AvitoStatsFetchRequest(
                    dateFrom=date_from,
                    dateTo=date_to,
                    accountIds=[external],
                    grouping="totals",
                )
            )
            projected = project_stats_result(
                result, external_id=external, date_from=date_from, date_to=date_to
            )
            with Session(self._engine) as session, session.begin():
                # The public guard authenticates the original principal and
                # compares the original account/credential evidence, without
                # another key inventory read, decrypt or authority recapture.
                _guard(session, principal, account, credential, version)
            return {
                "marketplaceAccountId": marketplace_account_id,
                "provider": "avito",
                **projected,
            }
        except AccountStatsError as error:
            failure = error.code
        except (WbLiveError, PublicationGuardError):
            failure = "AVITO_ACCOUNT_STATS_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- no cache/plaintext fallback or raw exception chain.
            failure = "AVITO_ACCOUNT_STATS_UNAVAILABLE"
        raise AccountStatsError(failure)
