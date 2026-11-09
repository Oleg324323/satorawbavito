"""Encrypted account metadata preview, not Review ingestion or send authority."""

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from app.avito.reviews import AvitoReviewsFetchRequest, LiveAvitoReviewsClient
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


class AccountReviewsError(ValueError):
    def __init__(self, code="AVITO_REVIEWS_PREVIEW_UNAVAILABLE"):
        self.code = (
            code
            if type(code) is str
            and code
            in {
                "AVITO_REVIEWS_PREVIEW_UNAVAILABLE",
                "AVITO_REVIEWS_PREVIEW_DISABLED",
                "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED",
                "AVITO_REVIEWS_PREVIEW_INVALID_REQUEST",
            }
            else "AVITO_REVIEWS_PREVIEW_UNAVAILABLE"
        )
        super().__init__(self.code)


def validate_offset(offset):
    if type(offset) is not int or not 0 <= offset < 2**63:
        raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_INVALID_REQUEST")


def _text(value):
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value
        )
    ):
        raise AccountReviewsError()
    return value


def _identity(value):
    if type(value) is int:
        if not 0 < value < 10**128:
            raise AccountReviewsError()
        return str(value)
    value = _text(value)
    # Existing Review identity semantics: opaque strings and leading zeros
    # survive; numeric-like floats/exponents must not become IDs.
    if re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value
    ) and not re.fullmatch(r"\d+", value):
        raise AccountReviewsError()
    return value


def _optional(value, validator):
    return None if value is None else validator(value)


def _boolean(value):
    if type(value) is not bool:
        raise AccountReviewsError()
    return value


def _count(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise AccountReviewsError()
    return str(value)


def _score(value):
    if type(value) is not int or not 1 <= value <= 5:
        raise AccountReviewsError()
    return value


def _rating_score(value):
    if type(value) not in (int, Decimal) or not 0 <= value <= 5:
        raise AccountReviewsError()
    if type(value) is Decimal and (
        not value.is_finite() or value.as_tuple().exponent < -20
    ):
        raise AccountReviewsError()
    if type(value) is Decimal and value == 0:
        value = value.copy_abs()  # Mathematical zero, keep source fractional scale.
    return str(value) if type(value) is int else format(value, "f")


def _source_time(value):
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _object(value):
    if type(value) is not dict:
        raise AccountReviewsError()
    return value


def _no_unproven_owner(value):
    # Existing ratings fixtures/parser prove no owner aliases. Sender is a
    # buyer, not an account binding. Do not infer scope from unknown aliases.
    if any(k in value for k in ("accountId", "userId", "sellerId")):
        raise AccountReviewsError()


@dataclass(frozen=True, slots=True, repr=False)
class ReviewPreviewRow:
    review_id: str
    score: int | None
    stage: str | None
    used_in_score: bool | None
    can_answer: bool | None
    created_at: str | None
    item_id: str | None
    answer_id: str | None
    answer_status: str | None


@dataclass(frozen=True, slots=True, repr=False)
class ReviewPreviewRating:
    is_enabled: bool | None
    score: str | None
    reviews_count: str | None
    reviews_with_score_count: str | None


@dataclass(frozen=True, slots=True, repr=False)
class ReviewsPreviewPage:
    external_account_id: str
    rating: ReviewPreviewRating
    total: str | None
    rows: tuple[ReviewPreviewRow, ...]


def _rating(body):
    _no_unproven_owner(_object(body))
    value = {} if body.get("rating") is None else _object(body["rating"])
    _no_unproven_owner(value)
    return ReviewPreviewRating(
        _optional(body.get("isEnabled"), _boolean),
        _optional(value.get("score"), _rating_score),
        _optional(value.get("reviewsCount"), _count),
        _optional(value.get("reviewsWithScoreCount"), _count),
    )


def _rows(body):
    _no_unproven_owner(_object(body))
    raw_rows = body.get("reviews")
    if type(raw_rows) is not list or len(raw_rows) > 50:
        raise AccountReviewsError()
    rows, seen = [], set()
    for raw in raw_rows:
        _no_unproven_owner(_object(raw))
        identity = _identity(raw.get("id"))
        if identity in seen:
            raise AccountReviewsError()
        seen.add(identity)
        item = {} if raw.get("item") is None else _object(raw["item"])
        answer = {} if raw.get("answer") is None else _object(raw["answer"])
        rows.append(
            ReviewPreviewRow(
                identity,
                _optional(raw.get("score"), _score),
                _optional(raw.get("stage"), _text),
                _optional(raw.get("usedInScore"), _boolean),
                _optional(raw.get("canAnswer"), _boolean),
                _source_time(raw.get("createdAt")),
                _optional(item.get("id"), _identity),
                _optional(answer.get("id"), _identity),
                _optional(answer.get("status"), _text),
            )
        )
    return tuple(rows), _optional(body.get("total"), _count)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AccountReviewsError()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise AccountReviewsError()


class _BoundedReviewsHTTP:
    def __init__(self, client, offset):
        self.client, self.offset = client, offset
        self.calls, self.rating, self.rows, self.total = 0, None, None, None
        self.started = time.monotonic()

    def get(self, url, *, headers, params=None):
        expected = (
            ("https://api.avito.ru/ratings/v1/info", None),
            (
                "https://api.avito.ru/ratings/v1/reviews",
                {"limit": 50, "offset": self.offset},
            ),
        )
        if (
            self.calls >= 2
            or (url, params) != expected[self.calls]
            or time.monotonic() - self.started > 30
        ):
            raise AccountReviewsError()
        self.calls += 1
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
                raise AccountReviewsError()
            declared = response.headers.get("content-length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]{1,10}", declared) or int(declared) > maximum
            ):
                raise AccountReviewsError()
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=65536):
                if (
                    len(body) + len(chunk) > maximum
                    or time.monotonic() - self.started > 30
                ):
                    raise AccountReviewsError()
                body.extend(chunk)
            if time.monotonic() - self.started > 30 or (
                declared is not None and len(body) != int(declared)
            ):
                raise AccountReviewsError()
        raw = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
        if self.calls == 1:
            self.rating = _rating(raw)
            sanitized = {"isEnabled": False}  # Internal parser input, never evidence.
        else:
            self.rows, self.total = _rows(raw)
            sanitized = {"reviews": [{"id": row.review_id} for row in self.rows]}
        return httpx.Response(200, json=sanitized, request=httpx.Request("GET", url))


class BoundedAvitoReviewsClient:
    def __init__(self, resolved, *, transport=None):
        self._resolved, self._transport = resolved, transport

    def fetch_preview(self, *, offset):
        validate_offset(offset)
        try:
            resolved = self._resolved
            if type(resolved) is not ResolvedCredentialForFetch:
                raise AccountReviewsError()
            binding, identity = resolved.binding, resolved.binding.credential_identity
            external = _text(binding.external_account_id)
            if (
                not re.fullmatch(r"[1-9][0-9]{0,127}", external)
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
                raise AccountReviewsError()
            secret = resolved.secret.reveal()
            if (
                set(secret) != {"accessToken", "expiresAt"}
                or type(secret["accessToken"]) is not str
                or not secret["accessToken"].strip()
                or secret["expiresAt"]
                != identity.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            ):
                raise AccountReviewsError()
            parser = LiveAvitoReviewsClient(secret["accessToken"])
            with httpx.Client(
                transport=self._transport,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                edge = _BoundedReviewsHTTP(client, offset)
                result = parser.fetch_reviews(
                    AvitoReviewsFetchRequest(limit=50, offset=offset), http_client=edge
                )
            if (
                result.status != "synced"
                or edge.calls != 2
                or edge.rating is None
                or edge.rows is None
                or tuple(row.reviewId for row in result.reviews)
                != tuple(row.review_id for row in edge.rows)
            ):
                raise AccountReviewsError()
            return ReviewsPreviewPage(external, edge.rating, edge.total, edge.rows)
        except Exception:  # noqa: BLE001, S110 -- provider/parser/token details never escape.
            pass
        raise AccountReviewsError()


def preview_wire(result, external):
    if (
        type(result) is not ReviewsPreviewPage
        or result.external_account_id != external
        or type(result.rating) is not ReviewPreviewRating
        or type(result.rows) is not tuple
        or len(result.rows) > 50
    ):
        raise AccountReviewsError()
    rating = result.rating

    def count(value):
        if value is not None and (
            type(value) is not str
            or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value)
            or int(value) >= 2**63
        ):
            raise AccountReviewsError()
        return value

    score = rating.score
    if score is not None:
        if type(score) is not str or not re.fullmatch(
            r"[0-5](?:\.[0-9]{1,20})?", score
        ):
            raise AccountReviewsError()
        _rating_score(Decimal(score))
    rows, seen = [], set()
    for row in result.rows:
        if type(row) is not ReviewPreviewRow:
            raise AccountReviewsError()
        identity = _identity(row.review_id)
        if type(row.review_id) is not str or identity in seen:
            raise AccountReviewsError()
        seen.add(identity)
        if row.created_at is not None:
            if type(row.created_at) is not str or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                row.created_at,
            ):
                raise AccountReviewsError()
            datetime.fromisoformat(row.created_at)
        rows.append(
            {
                "reviewId": identity,
                "score": _optional(row.score, _score),
                "stage": _optional(row.stage, _text),
                "usedInScore": _optional(row.used_in_score, _boolean),
                "canAnswer": _optional(row.can_answer, _boolean),
                "createdAt": row.created_at,
                "itemId": _optional(row.item_id, _identity),
                "answerId": _optional(row.answer_id, _identity),
                "answerStatus": _optional(row.answer_status, _text),
                "accountEvidence": "credential_scope",
            }
        )
    return {
        "rating": {
            "isEnabled": _optional(rating.is_enabled, _boolean),
            "score": score,
            "reviewsCount": count(rating.reviews_count),
            "reviewsWithScoreCount": count(rating.reviews_with_score_count),
        },
        "total": count(result.total),
        "rows": rows,
    }


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
            raise AccountReviewsError()

    # The shared public guard must remain last. Final account lock spans commit.
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
        raise AccountReviewsError()


class AccountAvitoReviewsService:
    """Same live actor/credential authority before and after closed-root HTTP."""

    def __init__(
        self, *, engine, keyring_loader, client_factory, enabled_for=_disabled
    ):
        if (
            not isinstance(engine, Engine)
            or engine.dialect.name != "postgresql"
            or not all(
                callable(v) for v in (keyring_loader, client_factory, enabled_for)
            )
        ):
            raise AccountReviewsError()
        self._engine, self._keyring_loader = engine, keyring_loader
        self._client_factory, self._enabled_for = client_factory, enabled_for

    def preview(self, actor, *, marketplace_account_id, offset):
        validate_offset(offset)
        if (
            type(marketplace_account_id) is not int
            or not 0 < marketplace_account_id < 2**31
        ):
            raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_INVALID_REQUEST")
        if (
            type(actor) is not ActorContext
            or type(actor.organization_id) is not int
            or not 0 < actor.organization_id < 2**31
            or not actor.session_id
        ):
            raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_ACCESS_DENIED")
        failure = "AVITO_REVIEWS_PREVIEW_UNAVAILABLE"
        try:
            if (
                self._enabled_for(actor.organization_id, marketplace_account_id)
                is not True
            ):
                raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_DISABLED")
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
                    raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_ACCESS_DENIED")
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
                external = _text(binding.external_account_id)
                if not re.fullmatch(r"[1-9][0-9]{0,127}", external):
                    raise AccountReviewsError()
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
            result = self._client_factory(resolved).fetch_preview(offset=offset)
            data = preview_wire(result, external)
            with Session(self._engine) as session, session.begin():
                # No replacement credential/principal capture, key read or decryption.
                _read_guard(session, principal, account, credential, version)
            return {
                "marketplaceAccountId": marketplace_account_id,
                "provider": "avito",
                "externalAccountId": external,
                "offset": str(offset),
                "limit": 50,
                "coverageState": "partial",
                **data,
            }
        except AccountReviewsError as error:
            failure = error.code
        except (WbLiveError, PublicationGuardError):
            failure = "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- no DB/cache/credential fallback or raw error chain.
            failure = "AVITO_REVIEWS_PREVIEW_UNAVAILABLE"
        raise AccountReviewsError(failure)
