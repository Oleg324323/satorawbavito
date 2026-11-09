"""Dormant explicit browser ingestion router and authenticated token lifecycle.

No router is mounted here. Trusted bootstrap supplies the actual strict domain
decoder, durable sink, async Redis adapter and approved server peer resolver.
Sync DB/sink calls run in a worker thread; no Redis/network call holds DB locks.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner
from app.platform.integrations.ingestion_limits import IngestionLimitError, IngestionLimits, IngestionPolicy
from app.platform.integrations.ingestion_publication_guard import acquire_ingestion_publication_guard
from app.platform.integrations.ingestion_tokens import (
    IngestionTokenStoreError, IssuedIngestionToken, VerifiedIngestionToken,
    _database_now, _get_ingestion_token_status_in_session, _issue_ingestion_token_in_session,
    _revoke_ingestion_tokens_in_session,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding, ExpectedIngestionToken, PublicationGuardError, UserSessionPrincipal,
    _integer, _physical_connection, acquire_publication_guard,
)

_ERROR_STATUS = {
    "ingestion_access_denied": 403,
    "ingestion_token_invalid": 401,
    "ingestion_body_invalid": 400,
    "ingestion_body_too_large": 413,
    "ingestion_encoding_unsupported": 415,
    "ingestion_conflict": 409,
    "ingestion_policy_unavailable": 503,
    "ingestion_peer_unavailable": 503,
    "ingestion_limits_unavailable": 503,
    "ingestion_rate_limited": 429,
    "ingestion_unavailable": 503,
    "ingestion_issue_outcome_unknown": 503,
}


class IngestionAPIError(ValueError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _ERROR_STATUS else "ingestion_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CommittedIngestionAcknowledgement:
    """Sink constructs/returns this only after its physical root committed.

    This is a bounded response DTO, not an authorization or commit certificate.
    """

    run_id: int
    state: str
    replayed: bool

    def __post_init__(self):
        if (not _integer(self.run_id, 2**63 - 1) or type(self.state) is not str
                or self.state != "partial" or type(self.replayed) is not bool):
            raise IngestionAPIError("ingestion_unavailable")


class BrowserDecoder(Protocol):
    def __call__(self, payload: bytes, *, organization_id: int,
                 marketplace_account_id: int, observed_at: datetime) -> object: ...


class DurableIngestionSink(Protocol):
    def __call__(self, *, raw_bearer: str, admission: VerifiedIngestionToken,
                 envelope: object, observed_at: datetime) -> CommittedIngestionAcknowledgement: ...


@dataclass(frozen=True, slots=True, repr=False)
class TrustedIngestionPeerResolver:
    """Server bootstrap's explicit direct-peer/proxy policy, never request input.

    resolve(request) must return the numeric IP from the approved server path.
    In particular, a forwarded header without validated proxy hops is forbidden.
    This module never reads X-Forwarded-For, Forwarded or client-supplied peer IDs.
    """

    policy_reference: str
    policy_version: int
    resolve: Callable[[Request], str]


def _safe_error(error) -> JSONResponse:
    code = getattr(error, "code", None)
    if type(code) is not str or code not in _ERROR_STATUS:
        code = "ingestion_unavailable"
    return JSONResponse({"code": code}, status_code=_ERROR_STATUS[code],
                        headers={"Cache-Control": "no-store"})


@contextmanager
def _root(factory, *, issuing=False):
    session = factory()
    if (not isinstance(session, Session) or session.in_transaction() or not session.is_active
            or session.new or session.dirty or session.deleted):
        raise IngestionAPIError("ingestion_unavailable")
    try:
        session.begin()
        if _physical_connection(session).get_isolation_level() != "READ COMMITTED":
            raise IngestionAPIError("ingestion_unavailable")
        yield session
        try:
            session.commit()
        except SQLAlchemyError:
            # Never disclose a secret after uncertain commit or retry issuance.
            raise IngestionAPIError("ingestion_issue_outcome_unknown" if issuing else "ingestion_unavailable") from None
    except (SQLAlchemyError, IngestionTokenStoreError):
        raise IngestionAPIError("ingestion_unavailable") from None
    except PublicationGuardError:
        raise IngestionAPIError("ingestion_access_denied") from None
    finally:
        try:
            if session.in_transaction():
                session.rollback()
        except SQLAlchemyError:
            pass
        try:
            session.close()
        except SQLAlchemyError:
            pass


class AuthenticatedIngestionTokens:
    """Live login/member/account and fixed integrations:write for every method.

    Cached ActorContext permissions do not authorize writes. Token TTL is a
    separate approved policy, never derived from the issuing login's expiry.
    """

    def __init__(self, *, session_factory, policy: IngestionPolicy | None,
                 decoder_max_body_bytes: int):
        self._factory = session_factory
        self.policy = policy
        self.decoder_max_body_bytes = decoder_max_body_bytes

    def require_policy(self) -> IngestionPolicy:
        if type(self.policy) is not IngestionPolicy:
            raise IngestionLimitError("ingestion_policy_unavailable")
        self.policy.require_decoder_limit(self.decoder_max_body_bytes)
        return self.policy

    def _guard(self, session, actor, account_id):
        if (type(actor) is not ActorContext or not _integer(actor.organization_id)
                or actor.session_id is None or not _integer(account_id)):
            raise IngestionAPIError("ingestion_access_denied")
        set_tenant_context(session, actor.organization_id)
        m = IamMembershipRow
        membership_id = session.scalar(select(m.membership_id).where(
            m.organization_id == actor.organization_id, m.user_id == actor.user_id))
        a = MarketplaceAccountRow
        row = session.execute(select(a.external_account_id, a.credential_ref).where(
            a.organization_id == actor.organization_id, a.marketplace_account_id == account_id,
            a.marketplace == "avito")).one_or_none()
        if membership_id is None or row is None:
            raise IngestionAPIError("ingestion_access_denied")
        principal = UserSessionPrincipal(actor.organization_id, actor.user_id, membership_id, actor.session_id)
        binding = ExpectedAccountBinding(account_id, "avito", row.external_account_id, row.credential_ref)
        guard = acquire_publication_guard(session, principal=principal,
            required_permissions=frozenset({"integrations:write"}), accounts=(binding,), authorities=())
        return guard, MarketplaceAccountCredentialOwner(actor.organization_id, account_id, "avito")

    def issue(self, *, authenticated_actor: ActorContext, marketplace_account_id: int,
              lifetime_seconds: int) -> IssuedIngestionToken:
        policy = self.require_policy()
        if not _integer(lifetime_seconds) or lifetime_seconds > policy.max_token_lifetime_seconds:
            raise IngestionAPIError("ingestion_body_invalid")
        issued = None
        try:
            with _root(self._factory, issuing=True) as session:
                guard, owner = self._guard(session, authenticated_actor, marketplace_account_id)
                now = guard.revalidate_before_write()
                expires_at = now + timedelta(seconds=lifetime_seconds)
                issued = _issue_ingestion_token_in_session(session, owner, expires_at=expires_at,
                                                          actor_user_id=authenticated_actor.user_id)
                # Newly issued authority participates in the SAME existing user
                # guard, so waiting until final commit cannot publish an expired
                # token or a token from a changed issuance incarnation.
                guard._tokens = (ExpectedIngestionToken(owner.marketplace_account_id,
                    issued.metadata.token_id, issued.metadata.scope, issued.metadata.expires_at),)
                guard.revalidate_before_write()
            return issued
        except Exception:
            if issued is not None:
                # Destroy the transient wrapper's secret without revealing it.
                issued._raw_bearer = None
            raise

    def status(self, *, authenticated_actor: ActorContext, marketplace_account_id: int):
        with _root(self._factory) as session:
            guard, owner = self._guard(session, authenticated_actor, marketplace_account_id)
            result = _get_ingestion_token_status_in_session(session, owner)
            guard.revalidate_before_write()
        return result

    def revoke(self, *, authenticated_actor: ActorContext, marketplace_account_id: int):
        with _root(self._factory) as session:
            guard, owner = self._guard(session, authenticated_actor, marketplace_account_id)
            result = _revoke_ingestion_tokens_in_session(session, owner, "operator_revoked",
                                                        actor_user_id=authenticated_actor.user_id)
            guard.revalidate_before_write()
        return result

    def verify_for_admission(self, *, raw_bearer: str) -> VerifiedIngestionToken:
        """Short prelookup root released before Redis/decoder; not sink authority."""
        try:
            with _root(self._factory) as session:
                guard = acquire_ingestion_publication_guard(session, raw_bearer=raw_bearer)
                verified = guard._verified
            return verified
        except IngestionAPIError:
            raise IngestionAPIError("ingestion_token_invalid") from None
        finally:
            raw_bearer = None


def _account_id(raw: str) -> int:
    if (type(raw) is not str or not 1 <= len(raw) <= 10 or not raw.isascii()
            or not raw.isdecimal() or raw.startswith("0") or not _integer(int(raw))):
        raise IngestionAPIError("ingestion_access_denied")
    return int(raw)


def _bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise IngestionAPIError("ingestion_token_invalid")
    value = values[0]
    if not value.startswith("Bearer ") or len(value) > 128 or value.count(" ") != 1:
        raise IngestionAPIError("ingestion_token_invalid")
    return value[7:]


async def _body(request: Request, maximum: int) -> bytes:
    # Even identity encoding must be explicitly absent. No decompression here.
    if request.headers.getlist("content-encoding"):
        raise IngestionAPIError("ingestion_encoding_unsupported")
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
        raise IngestionAPIError("ingestion_body_invalid")
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise IngestionAPIError("ingestion_body_invalid")
    if lengths:
        value = lengths[0]
        if len(value) > 10 or not value.isascii() or not value.isdecimal():
            raise IngestionAPIError("ingestion_body_invalid")
        if int(value) > maximum:
            raise IngestionAPIError("ingestion_body_too_large")
    data = bytearray()
    chunk = None
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > maximum:
                raise IngestionAPIError("ingestion_body_too_large")
            data.extend(chunk)
        if not data or (lengths and len(data) != int(lengths[0])):
            raise IngestionAPIError("ingestion_body_invalid")
        return bytes(data)
    finally:
        data.clear()
        chunk = None


def _issue_lifetime(payload):
    def pairs(items):
        if len(items) != 1 or items[0][0] != "lifetime_seconds":
            raise IngestionAPIError("ingestion_body_invalid")
        return dict(items)
    try:
        body = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
        if type(body) is not dict or set(body) != {"lifetime_seconds"} or not _integer(body["lifetime_seconds"]):
            raise ValueError()
        return body["lifetime_seconds"]
    except Exception:
        raise IngestionAPIError("ingestion_body_invalid") from None


def _metadata_wire(metadata):
    if metadata is None:
        return None
    values = asdict(metadata)
    values["token_id"] = str(metadata.token_id)
    for name in ("issued_at", "expires_at", "revoked_at", "last_used_at"):
        value = values[name]
        values[name] = None if value is None else value.isoformat()
    return values


def create_ingestion_router(*, token_service: AuthenticatedIngestionTokens,
                            limits: IngestionLimits | None,
                            trusted_peer: TrustedIngestionPeerResolver | None,
                            decoder: BrowserDecoder, decoder_max_body_bytes: int,
                            sink: DurableIngestionSink) -> APIRouter:
    """Explicit composition only, with no fallback decoder/sink and no mounting.

    The actual T3 decoder receives bytes and verified owner/server receipt time.
    The sync sink must open its OWN fresh root, verify raw_bearer there, compare
    guard owner/account binding/version/token/expiry to admission and the envelope,
    require_participation(session), enforce exact idempotency/body conflict, then
    commit before returning CommittedIngestionAcknowledgement. No network in root.
    """
    if (type(token_service) is not AuthenticatedIngestionTokens or not callable(decoder)
            or not callable(sink) or not _integer(decoder_max_body_bytes)
            or token_service.decoder_max_body_bytes != decoder_max_body_bytes):
        raise IngestionAPIError("ingestion_unavailable")
    router = APIRouter()

    def active_policy():
        policy = token_service.require_policy()
        if (type(limits) is not IngestionLimits or limits.policy != policy
                or type(trusted_peer) is not TrustedIngestionPeerResolver or not callable(trusted_peer.resolve)
                or (trusted_peer.policy_reference, trusted_peer.policy_version) !=
                   (policy.peer_policy_reference, policy.peer_policy_version)):
            raise IngestionLimitError("ingestion_policy_unavailable")
        return policy

    @router.post("/ingestion/avito/browser-snapshots")
    async def ingest(request: Request):
        raw_bearer = payload = envelope = admission = peer = result = None
        try:
            policy = active_policy()
            try:
                peer = trusted_peer.resolve(request)
            except Exception:
                raise IngestionAPIError("ingestion_peer_unavailable") from None
            await limits.check_peer(peer)
            peer = None
            raw_bearer = _bearer(request)
            payload = await _body(request, policy.max_body_bytes)
            admission = await run_in_threadpool(token_service.verify_for_admission, raw_bearer=raw_bearer)
            await limits.check_verified_token(admission)
            observed_at = datetime.now(timezone.utc)
            try:
                envelope = decoder(payload, organization_id=admission.owner.organization_id,
                                   marketplace_account_id=admission.owner.marketplace_account_id, observed_at=observed_at)
            except Exception:
                raise IngestionAPIError("ingestion_body_invalid") from None
            payload = None
            result = await run_in_threadpool(sink, raw_bearer=raw_bearer, admission=admission,
                                            envelope=envelope, observed_at=observed_at)
            if type(result) is not CommittedIngestionAcknowledgement:
                raise IngestionAPIError("ingestion_unavailable")
            result.__post_init__()
            return JSONResponse({"run_id": str(result.run_id), "state": result.state,
                                 "replayed": result.replayed}, headers={"Cache-Control": "no-store"})
        except (IngestionAPIError, IngestionLimitError) as error:
            return _safe_error(error)
        except (PublicationGuardError, IngestionTokenStoreError):
            return _safe_error(IngestionAPIError("ingestion_token_invalid"))
        except Exception:
            return _safe_error(IngestionAPIError("ingestion_unavailable"))
        finally:
            raw_bearer = payload = envelope = admission = peer = result = None

    @router.post("/accounts/{marketplace_account_id}/ingestion-token")
    async def issue(request: Request, marketplace_account_id: str):
        payload = issued = raw = None
        try:
            policy = token_service.require_policy()
            try:
                actor = actor_from_request(request)
            except Exception:
                raise IngestionAPIError("ingestion_access_denied") from None
            payload = await _body(request, min(policy.max_body_bytes, 4096))
            lifetime = _issue_lifetime(payload)
            payload = None
            issued = await run_in_threadpool(token_service.issue, authenticated_actor=actor,
                marketplace_account_id=_account_id(marketplace_account_id), lifetime_seconds=lifetime)
            metadata = _metadata_wire(issued.metadata)
            raw = issued.reveal()
            return JSONResponse({"token": raw, "metadata": metadata}, headers={"Cache-Control": "no-store"})
        except (IngestionAPIError, IngestionLimitError) as error:
            return _safe_error(error)
        except Exception:
            return _safe_error(IngestionAPIError("ingestion_unavailable"))
        finally:
            if issued is not None:
                issued._raw_bearer = None
            payload = issued = raw = None

    async def lifecycle(request, account_id, *, revoke):
        try:
            try:
                actor = actor_from_request(request)
            except Exception:
                raise IngestionAPIError("ingestion_access_denied") from None
            method = token_service.revoke if revoke else token_service.status
            result = await run_in_threadpool(method, authenticated_actor=actor, marketplace_account_id=_account_id(account_id))
            body = {"revoked": [_metadata_wire(row) for row in result]} if revoke else {"metadata": _metadata_wire(result)}
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        except IngestionAPIError as error:
            return _safe_error(error)
        except Exception:
            return _safe_error(IngestionAPIError("ingestion_unavailable"))

    @router.get("/accounts/{marketplace_account_id}/ingestion-token")
    async def status(request: Request, marketplace_account_id: str):
        return await lifecycle(request, marketplace_account_id, revoke=False)

    @router.delete("/accounts/{marketplace_account_id}/ingestion-token")
    async def revoke(request: Request, marketplace_account_id: str):
        return await lifecycle(request, marketplace_account_id, revoke=True)

    return router
