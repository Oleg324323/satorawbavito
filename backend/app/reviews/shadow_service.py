"""Dormant WB received-DTO composition, not an HTTP/worker authentication API.

Trusted manual caller reserves before provider I/O with the paired fetch binding.
Finish receives those same DTOs; neither operation fetches, decrypts or logs them.
Running reservations survive failed publication; no automated recovery is implied.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    CredentialFetchBinding,
    MarketplaceAccountCredentialOwner,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    ExpectedCredential,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.canonical_contract import ReviewNormalizationError, normalize_wb_review
from app.reviews.canonical_repository import (
    ReviewFactsRepository,
    ReviewOwner,
    ReviewRunReference,
)
from app.reviews.canonical_wb_fetch import CanonicalWbFeedbackRow
from app.reviews.ingestion_contract import ReviewRepositoryError, snapshot_manifest
from app.security.marketplace_credentials import CredentialIdentity
from app.wb_api.feedbacks_runtime import WbFeedbackRow

_PERMISSION = frozenset({"reviews:write"})
_CODES = frozenset(
    {
        "REVIEW_SHADOW_INVALID",
        "REVIEW_SHADOW_DENIED",
        "REVIEW_SHADOW_AUTHORITY_CHANGED",
        "REVIEW_SHADOW_EXPIRED",
        "REVIEW_SHADOW_STORAGE_UNAVAILABLE",
        "REVIEW_SHADOW_CONFLICT",
    }
)


class ReviewShadowError(ValueError):
    def __init__(self, code="REVIEW_SHADOW_INVALID"):
        self.code = (
            code if type(code) is str and code in _CODES else "REVIEW_SHADOW_INVALID"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class ReviewShadowTicket:
    """Internal metadata, never an authenticated bearer/client payload."""

    principal: UserSessionPrincipal
    account: ExpectedAccountBinding
    authority: ExpectedCredential
    run: ReviewRunReference
    source_run_id: str
    request_checksum: str


@dataclass(frozen=True, slots=True)
class ReviewShadowReceipt:
    sync_run_id: UUID
    manifest_checksum: str
    observed_count: int


@contextmanager
def _safe_errors():
    try:
        yield
    except ReviewShadowError:
        raise
    except PublicationGuardError as error:
        code = {
            "publication_context_invalid": "REVIEW_SHADOW_INVALID",
            "publication_access_denied": "REVIEW_SHADOW_DENIED",
            "publication_binding_changed": "REVIEW_SHADOW_AUTHORITY_CHANGED",
            "publication_authority_invalid": "REVIEW_SHADOW_AUTHORITY_CHANGED",
            "publication_expired": "REVIEW_SHADOW_EXPIRED",
        }.get(error.code, "REVIEW_SHADOW_STORAGE_UNAVAILABLE")
        raise ReviewShadowError(code) from None
    except ReviewRepositoryError as error:
        code = (
            "REVIEW_SHADOW_CONFLICT"
            if error.code in {"REVIEW_REPLAY_CONFLICT", "REVIEW_VERSION_CONFLICT", "REVIEW_HISTORY_BINDING_CONFLICT"}
            else "REVIEW_SHADOW_STORAGE_UNAVAILABLE"
            if error.code == "REVIEW_STORAGE_UNAVAILABLE"
            else "REVIEW_SHADOW_INVALID"
        )
        raise ReviewShadowError(code) from None
    except SQLAlchemyError:
        # Includes failures at physical Session.commit/context exit.
        raise ReviewShadowError("REVIEW_SHADOW_STORAGE_UNAVAILABLE") from None
    except (ReviewNormalizationError, UnicodeError, ValueError, TypeError):
        raise ReviewShadowError() from None


def _engine(engine):
    if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
        raise ReviewShadowError()
    return engine


def _binding(actor, binding):
    if type(actor) is not ActorContext or not actor.session_id:
        raise ReviewShadowError("REVIEW_SHADOW_DENIED")
    if (
        type(binding) is not CredentialFetchBinding
        or type(binding.owner) is not MarketplaceAccountCredentialOwner
        or type(binding.credential_identity) is not CredentialIdentity
    ):
        raise ReviewShadowError()
    owner, identity = binding.owner, binding.credential_identity
    if (
        owner.provider != "wb"
        or identity.provider != "wb"
        or identity.credential_kind != "wb_api"
        or owner.organization_id != actor.organization_id
        or identity.organization_id != owner.organization_id
        or identity.marketplace_account_id != owner.marketplace_account_id
    ):
        raise ReviewShadowError("REVIEW_SHADOW_AUTHORITY_CHANGED")
    return (
        ExpectedAccountBinding(
            owner.marketplace_account_id,
            "wb",
            binding.external_account_id,
            binding.credential_ref,
        ),
        ExpectedCredential(
            identity.marketplace_account_id,
            identity.credential_id,
            identity.credential_kind,
            identity.generation,
            identity.payload_schema_version,
            identity.expires_at,
        ),
    )


def _repository(session, principal, account):
    return ReviewFactsRepository(
        session.connection(),
        ReviewOwner(
            principal.organization_id,
            account.marketplace_account_id,
            "wb",
            account.external_account_id,
            account.credential_ref,
        ),
        command_savepoints=False,
    )


def begin_review_shadow(engine, *, actor, binding, source_run_id, request_checksum):
    with _safe_errors():
        account, authority = _binding(actor, binding)
        with Session(_engine(engine)) as session, session.begin():
            set_tenant_context(session, actor.organization_id)
            member = session.execute(
                select(IamMembershipRow.membership_id).where(
                    IamMembershipRow.organization_id == actor.organization_id,
                    IamMembershipRow.user_id == actor.user_id,
                )
            ).scalar_one_or_none()
            if member is None:
                raise ReviewShadowError("REVIEW_SHADOW_DENIED")
            principal = UserSessionPrincipal(
                actor.organization_id, actor.user_id, member, actor.session_id
            )
            guard = acquire_publication_guard(
                session,
                principal=principal,
                required_permissions=_PERMISSION,
                accounts=(account,),
                authorities=(authority,),
            )
            now = guard.revalidate_before_write()
            run = _repository(session, principal, account).reserve_run(
                source_run_id=source_run_id,
                request_checksum=request_checksum,
                started_at=now,
            )
            ticket = ReviewShadowTicket(
                principal, account, authority, run, source_run_id, request_checksum
            )
        return ticket


def publish_received_review_rows(engine, *, ticket, rows, coverage):
    with _safe_errors():
        if (
            type(ticket) is not ReviewShadowTicket
            or type(ticket.run) is not ReviewRunReference
            or type(rows) is not tuple
            or not all(
                type(row) in (WbFeedbackRow, CanonicalWbFeedbackRow) for row in rows
            )
            or type(ticket.account) is not ExpectedAccountBinding
            or ticket.account.provider != "wb"
            or type(ticket.authority) is not ExpectedCredential
            or ticket.authority.kind != "wb_api"
        ):
            raise ReviewShadowError()
        with Session(_engine(engine)) as session, session.begin():
            guard = acquire_publication_guard(
                session,
                principal=ticket.principal,
                required_permissions=_PERMISSION,
                accounts=(ticket.account,),
                authorities=(ticket.authority,),
            )
            now = guard.revalidate_before_write()
            facts = tuple(
                normalize_wb_review(
                    row,
                    ticket.principal.organization_id,
                    ticket.account.marketplace_account_id,
                    ticket.source_run_id,
                    now,
                )
                for row in rows
            )
            snapshot_manifest(
                facts,
                coverage,
                "partial",
                {f.identity.external_review_id: 0 for f in facts},
            )
            repo = _repository(session, ticket.principal, ticket.account)
            run = repo.reserve_run(
                source_run_id=ticket.source_run_id,
                request_checksum=ticket.request_checksum,
                started_at=now,
            )
            if run != ticket.run:
                raise ReviewShadowError("REVIEW_SHADOW_CONFLICT")
            versions = {}
            for fact in facts:
                current = repo.get_fact(fact.identity.external_review_id)
                versions[fact.identity.external_review_id] = (
                    0 if current is None else current.version
                )
            now = guard.revalidate_before_write()
            manifest = repo.ingest(
                run.sync_run_id,
                facts=facts,
                coverage=coverage,
                completeness="partial",
                completed_at=now,
                expected_versions=versions,
            )
            receipt = ReviewShadowReceipt(run.sync_run_id, manifest, len(facts))
        return receipt


def _avito_received_page(
    rows, *, organization_id, marketplace_account_id, source_run_id, observed_at
):
    """Pure page admission only. This creates neither a guard nor a reservation."""
    from app.reviews.canonical_avito_decode import decode_avito_review

    try:
        if type(rows) is not tuple or len(rows) > 50:
            raise ReviewShadowError()
        facts = tuple(
            decode_avito_review(
                row,
                organization_id=organization_id,
                marketplace_account_id=marketplace_account_id,
                source_run_id=source_run_id,
                observed_at=observed_at,
            )
            for row in rows
        )
        coverage = {
            "from": None,
            "to": None,
            "streams": [{"name": "reviews", "terminalReached": False}],
            "pagesObserved": 1,
            "providerEndReached": False,
        }
        snapshot_manifest(
            facts, coverage, "partial",
            {fact.identity.external_review_id: 0 for fact in facts},
        )
        return facts, coverage
    except (ValueError, TypeError):
        pass
    raise ReviewShadowError()


@dataclass(frozen=True, slots=True, repr=False)
class AvitoReviewShadowTicket:
    """Internal original-caller correlation, NEVER an HTTP/queue capability."""

    principal: UserSessionPrincipal
    account: ExpectedAccountBinding
    authority: ExpectedCredential
    account_incarnation: int
    run: ReviewRunReference
    source_run_id: str
    request_checksum: str


@contextmanager
def _avito_source_errors():
    failures = []
    failure = None
    try:
        yield failures
    except Exception as error:  # noqa: BLE001 -- including physical commit failure, no raw context escapes.
        if isinstance(error, ReviewShadowError):
            failure = error.code
        elif isinstance(error, PublicationGuardError):
            failure = {
                "publication_context_invalid": "REVIEW_SHADOW_INVALID",
                "publication_access_denied": "REVIEW_SHADOW_DENIED",
                "publication_binding_changed": "REVIEW_SHADOW_AUTHORITY_CHANGED",
                "publication_authority_invalid": "REVIEW_SHADOW_AUTHORITY_CHANGED",
                "publication_expired": "REVIEW_SHADOW_EXPIRED",
            }.get(error.code, "REVIEW_SHADOW_STORAGE_UNAVAILABLE")
        elif isinstance(error, ReviewRepositoryError):
            failure = (
                "REVIEW_SHADOW_CONFLICT"
                if error.code in {"REVIEW_REPLAY_CONFLICT", "REVIEW_VERSION_CONFLICT", "REVIEW_HISTORY_BINDING_CONFLICT"}
                else "REVIEW_SHADOW_STORAGE_UNAVAILABLE"
                if error.code == "REVIEW_STORAGE_UNAVAILABLE"
                else "REVIEW_SHADOW_INVALID"
            )
        elif isinstance(error, (ValueError, TypeError)):
            failure = "REVIEW_SHADOW_INVALID"
        else:
            failure = "REVIEW_SHADOW_STORAGE_UNAVAILABLE"
    if failure is not None:
        failures.append(failure)


def _avito_source_binding(actor, binding):
    if type(actor) is not ActorContext or not actor.session_id:
        raise ReviewShadowError("REVIEW_SHADOW_DENIED")
    if (type(binding) is not CredentialFetchBinding
        or type(binding.owner) is not MarketplaceAccountCredentialOwner
        or type(binding.credential_identity) is not CredentialIdentity):
        raise ReviewShadowError()
    owner, identity = binding.owner, binding.credential_identity
    if (owner.provider != "avito" or identity.provider != "avito"
        or identity.credential_kind != "avito_oauth_access"
        or owner.organization_id != actor.organization_id
        or identity.organization_id != owner.organization_id
        or identity.marketplace_account_id != owner.marketplace_account_id):
        raise ReviewShadowError("REVIEW_SHADOW_AUTHORITY_CHANGED")
    return (
        ExpectedAccountBinding(owner.marketplace_account_id, "avito", binding.external_account_id, binding.credential_ref),
        ExpectedCredential(identity.marketplace_account_id, identity.credential_id,
            identity.credential_kind, identity.generation, identity.payload_schema_version, identity.expires_at),
    )


def _avito_incarnation(session, principal, account):
    from app.platform.integrations.orm import MarketplaceAccountRow

    # No pre-guard domain lock: shared guard takes locks in its canonical order.
    # Once acquired, that account lock remains held across these checks/commit.
    return session.scalar(select(MarketplaceAccountRow.ingestion_binding_version).where(
        MarketplaceAccountRow.organization_id == principal.organization_id,
        MarketplaceAccountRow.marketplace_account_id == account.marketplace_account_id,
        MarketplaceAccountRow.marketplace == "avito",
        MarketplaceAccountRow.status == "connected",
    ))


def _avito_source_guard(session, principal, account, authority, incarnation):
    # Identity comparison only; do not call, replace or reorder the shared hook.
    from app.platform.integrations.publication_guard import _before_commit

    if type(incarnation) is not int or incarnation <= 0:
        raise ReviewShadowError("REVIEW_SHADOW_AUTHORITY_CHANGED")

    def closing(current):
        if (tuple(current.dispatch.before_commit)[-2:] != (closing, _before_commit)
            or current.new or current.dirty or current.deleted):
            raise ReviewShadowError()
        if _avito_incarnation(current, principal, account) != incarnation:
            raise ReviewShadowError("REVIEW_SHADOW_AUTHORITY_CHANGED")

    # T1-confirmed conditional Core-only extension. Empty ORM state guarantees
    # shared final flush cannot queue an incarnation change after this check.
    # Effective class/instance dispatch order, not registration time, is checked.
    event.listen(session, "before_commit", closing)
    guard = acquire_publication_guard(session, principal=principal,
        required_permissions=frozenset({"reviews:write"}),
        accounts=(account,), authorities=(authority,))
    if _avito_incarnation(session, principal, account) != incarnation:
        raise ReviewShadowError("REVIEW_SHADOW_AUTHORITY_CHANGED")
    return guard


def _avito_source_repository(session, principal, account):
    return ReviewFactsRepository(session.connection(), ReviewOwner(
        principal.organization_id, account.marketplace_account_id, "avito",
        account.external_account_id, account.credential_ref), command_savepoints=False)


def begin_avito_review_shadow(
    engine, *, actor, settings, binding, source_run_id, request_checksum
):
    """Reserve under a real live original reviews:write principal, before I/O.

    Trusted in-process caller only. Binding is paired fetch metadata, not auth;
    every component is checked against PostgreSQL by the public user guard.
    """
    from app.config import Settings, is_review_shadow_enabled

    with _avito_source_errors() as failures:
        account, authority = _avito_source_binding(actor, binding)
        if type(settings) is not Settings or not is_review_shadow_enabled(
            settings, organization_id=actor.organization_id,
            marketplace_account_id=account.marketplace_account_id,
        ):
            raise ReviewShadowError("REVIEW_SHADOW_DENIED")
        with Session(_engine(engine), autoflush=False) as session, session.begin():
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id,
            ))
            if member is None:
                raise ReviewShadowError("REVIEW_SHADOW_DENIED")
            principal = UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id)
            incarnation = _avito_incarnation(session, principal, account)
            guard = _avito_source_guard(session, principal, account, authority, incarnation)
            now = guard.revalidate_before_write()
            run = _avito_source_repository(session, principal, account).reserve_run(
                source_run_id=source_run_id, request_checksum=request_checksum, started_at=now)
            ticket = AvitoReviewShadowTicket(principal, account, authority, incarnation,
                run, source_run_id, request_checksum)
    # Raise in the ordinary caller frame, outside contextlib.throw's exception.
    if failures:
        raise ReviewShadowError(failures[0])
    return ticket


def publish_received_avito_review_rows(engine, *, ticket, rows):
    """Publish a received partial page with SAME original authority; no fetch.

    No Session, callback or caller-provided coverage crosses this boundary.
    Failed publication leaves the durable running reservation, not a fallback.
    """
    with _avito_source_errors() as failures:
        if (type(ticket) is not AvitoReviewShadowTicket
            or type(ticket.principal) is not UserSessionPrincipal
            or type(ticket.account) is not ExpectedAccountBinding
            or ticket.account.provider != "avito"
            or type(ticket.authority) is not ExpectedCredential
            or ticket.authority.kind != "avito_oauth_access"
            or ticket.authority.marketplace_account_id != ticket.account.marketplace_account_id
            or type(ticket.run) is not ReviewRunReference):
            raise ReviewShadowError()
        with Session(_engine(engine), autoflush=False) as session, session.begin():
            guard = _avito_source_guard(session, ticket.principal, ticket.account,
                ticket.authority, ticket.account_incarnation)
            now = guard.revalidate_before_write()
            facts, coverage = _avito_received_page(rows,
                organization_id=ticket.principal.organization_id,
                marketplace_account_id=ticket.account.marketplace_account_id,
                source_run_id=ticket.source_run_id, observed_at=now)
            repository = _avito_source_repository(session, ticket.principal, ticket.account)
            run = repository.reserve_run(source_run_id=ticket.source_run_id,
                request_checksum=ticket.request_checksum, started_at=now)
            if run != ticket.run:
                raise ReviewShadowError("REVIEW_SHADOW_CONFLICT")
            versions = {}
            for fact in facts:
                current = repository.get_fact(fact.identity.external_review_id)
                versions[fact.identity.external_review_id] = 0 if current is None else current.version
            manifest = repository.ingest(run.sync_run_id, facts=facts,
                coverage=coverage, completeness="partial",
                completed_at=guard.revalidate_before_write(), expected_versions=versions)
            receipt = ReviewShadowReceipt(run.sync_run_id, manifest, len(facts))
    if failures:
        raise ReviewShadowError(failures[0])
    return receipt
