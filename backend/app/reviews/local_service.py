"""Live-session local Review service, guarded through physical commit.

Requires platform0071 and its shared exact-account context helper. No credentials
are resolved: local authoring/approval is not permission to send to a marketplace.
"""

import json
from contextlib import contextmanager
from datetime import UTC

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, is_review_shadow_enabled
from app.control_plane.auth import ActorContext
from app.infra.db import (
    MarketplaceAccountContextError,
    set_marketplace_account_context,
    set_tenant_context,
)
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.canonical_contract import ReviewNormalizationError
from app.reviews.historical_binding import (
    ReviewBindingDescriptor,
    ReviewBindingDescriptorError,
)
from app.reviews.ingestion_contract import ReviewRepositoryError
from app.reviews.local_command_payloads import encode_review_local_request
from app.reviews.local_repository import (
    ReviewLocalError,
    ReviewLocalRepository,
    _encoded,
    _require,
    _uuid,
)
from app.reviews.local_tables import DECISION, DRAFT, POLICY, POLICY_HEAD, WORKFLOW_HEAD
from app.reviews.storage_payloads import (
    StoragePayloadError,
    encode_review_generation,
    encode_review_policy,
)


@contextmanager
def _unit(engine, *, actor, settings, account_id, marketplace, permission):
    try:
        _require(type(actor) is ActorContext and bool(actor.session_id), "REVIEW_LOCAL_DENIED")
        _require(type(account_id) is int and 0 < account_id <= 2**31 - 1
                 and marketplace in {"wb", "avito"}, "REVIEW_LOCAL_INVALID")
        try:
            enabled = is_review_shadow_enabled(settings, organization_id=actor.organization_id,
                                               marketplace_account_id=account_id)
        except RuntimeError:
            raise ReviewLocalError("REVIEW_LOCAL_CONFIGURATION_INVALID") from None
        _require(enabled, "REVIEW_LOCAL_DISABLED")
        _require(isinstance(engine, Engine) and engine.dialect.name == "postgresql",
                 "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
        with Session(engine, autoflush=False) as session, session.begin():
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id))
            account = session.execute(select(MarketplaceAccountRow.external_account_id,
                MarketplaceAccountRow.credential_ref).where(
                MarketplaceAccountRow.organization_id == actor.organization_id,
                MarketplaceAccountRow.marketplace_account_id == account_id,
                MarketplaceAccountRow.marketplace == marketplace)).one_or_none()
            _require(member is not None and account is not None, "REVIEW_LOCAL_DENIED")
            expected = ExpectedAccountBinding(account_id, marketplace, account.external_account_id, account.credential_ref)
            guard = acquire_publication_guard(session,
                principal=UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id),
                required_permissions=frozenset({permission}), accounts=(expected,), authorities=())
            set_marketplace_account_context(session, organization_id=actor.organization_id,
                                            marketplace_account_id=account_id)
            repository = ReviewLocalRepository(session.connection(), ReviewBindingDescriptor(
                actor.organization_id, account_id, marketplace, expected.external_account_id, expected.credential_ref))
            yield repository, member
            guard.revalidate_before_write()
        # Context manager return is only reached after the Engine-owned physical commit.
    except PublicationGuardError as error:
        code = "REVIEW_LOCAL_STORAGE_UNAVAILABLE" if error.code in {
            "publication_context_invalid", "publication_persistence_failed"} else "REVIEW_LOCAL_DENIED"
        raise ReviewLocalError(code) from None
    except IntegrityError:
        # Constraint text/SQL parameters can contain review text: never expose/log them.
        raise ReviewLocalError("REVIEW_LOCAL_CONFLICT") from None
    except SQLAlchemyError:
        raise ReviewLocalError("REVIEW_LOCAL_STORAGE_UNAVAILABLE") from None
    except MarketplaceAccountContextError:
        raise ReviewLocalError("REVIEW_LOCAL_STORAGE_UNAVAILABLE") from None
    except ReviewRepositoryError as error:
        code = "REVIEW_LOCAL_CONFLICT" if error.code in {
            "REVIEW_HISTORY_BINDING_CONFLICT", "REVIEW_SCOPE_DENIED"} else "REVIEW_LOCAL_STORAGE_UNAVAILABLE"
        raise ReviewLocalError(code) from None
    except (ReviewBindingDescriptorError, StoragePayloadError, ReviewNormalizationError,
            KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ReviewLocalError("REVIEW_LOCAL_INVALID") from None


def execute_local_review(engine: Engine, *, actor: ActorContext, settings: Settings, request: object):
    # Snapshot canonical bytes up front, so mutable nested input cannot change between auth and write.
    try:
        encoded = encode_review_local_request(request)
        command = json.loads(encoded.canonical_bytes)
    except (StoragePayloadError, ValueError, TypeError):
        raise ReviewLocalError("REVIEW_LOCAL_INVALID") from None
    _require(type(actor) is ActorContext and command["organizationId"] == actor.organization_id,
             "REVIEW_LOCAL_DENIED")
    permission = "reviews:approve" if command["operationKind"] == "review.decision.record.v1" else "reviews:write"
    with _unit(engine, actor=actor, settings=settings, account_id=command["marketplaceAccountId"],
               marketplace=command["marketplace"], permission=permission) as (repository, member):
        _require(command["actorMembershipId"] == member, "REVIEW_LOCAL_DENIED")
        completed_at = repository.connection.scalar(select(func.clock_timestamp())).astimezone(UTC)
        result = repository.execute(command, completed_at=completed_at)
    return result


def execute_local_review_with_notifications(engine: Engine, *, actor: ActorContext,
                                           settings: Settings, request: object):
    """Explicit NEW0074 producer entrypoint. Existing0071 callers stay unchanged.

    Only genuinely new draft publication creates approval_required in the same
    physical root. Exact old command replay never backfills a missing event.
    """
    from app.notification_repository import NotificationRepository

    try:
        encoded = encode_review_local_request(request)
        command = json.loads(encoded.canonical_bytes)
    except (StoragePayloadError, ValueError, TypeError):
        raise ReviewLocalError("REVIEW_LOCAL_INVALID") from None
    _require(type(actor) is ActorContext and command["organizationId"] == actor.organization_id,
             "REVIEW_LOCAL_DENIED")
    permission = "reviews:approve" if command["operationKind"] == "review.decision.record.v1" else "reviews:write"
    with _unit(engine, actor=actor, settings=settings, account_id=command["marketplaceAccountId"],
               marketplace=command["marketplace"], permission=permission) as (repository, member):
        _require(command["actorMembershipId"] == member, "REVIEW_LOCAL_DENIED")
        original = repository.replay(command, encoded)
        if original is not None:
            result = original
        else:
            completed_at = repository.connection.scalar(select(func.clock_timestamp())).astimezone(UTC)
            result = repository.execute(command, completed_at=completed_at)
            if command["operationKind"] == "review.draft.publish.v1":
                saved = json.loads(result.canonical_bytes)
                NotificationRepository(repository.connection, repository.binding).publish(
                    review_id=_uuid(command["input"]["reviewId"]), entity_id=_uuid(saved["draftId"]),
                    source_version=saved["draftRevision"], kind="approval_required")
    return result


def read_local_review_context(engine: Engine, *, actor: ActorContext, settings: Settings,
                              marketplace_account_id: int, marketplace: str,
                              review_id: str | None = None, external_review_id: str | None = None):
    """Read current authoring context, never a reusable grant or a send authorization.

Epoch/source/CAS fields must be echoed in a prepared command. A policy/source/head
change between this read and publication produces conflict, not silent regeneration.
"""
    with _unit(engine, actor=actor, settings=settings, account_id=marketplace_account_id,
               marketplace=marketplace, permission="reviews:read") as (repository, member):
        data = {"schemaVersion": "review-local-context-v1", "organizationId": actor.organization_id,
                    "marketplaceAccountId": marketplace_account_id, "marketplace": marketplace,
                    "actorMembershipId": member, "policy": None, "policyHead": None, "review": None, "draft": None, "decision": None,
                    "workflowHead": None}
        # Same account fence serializes local reads with all writers; source lock precedes heads.
        if review_id is not None or external_review_id is not None:
            _require(review_id is not None and external_review_id is not None, "REVIEW_LOCAL_INVALID")
            source = repository.source({"reviewId": review_id, "externalReviewId": external_review_id})
            data["review"] = {"reviewId": str(source.review_id), "externalReviewId": external_review_id,
                "sourceObservationId": str(source.current_observation_id), "sourceChecksum": source.content_checksum,
                "text": source.text, "answered": source.answered, "canAnswer": source.can_answer,
                "sourceOrderState": source.source_order_state}
        ph = repository.get(POLICY_HEAD, lock=True)
        if ph is not None:
            p = repository.get(POLICY, policy_id=ph["current_policy_id"], version=ph["current_policy_version"])
            _require(p is not None, "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
            data["policy"] = _encoded(p, "policy", encode_review_policy)
            data["policyHead"] = {"headId": str(ph["head_id"]), "version": int(ph["version"]),
                                      "policyChecksum": ph["current_policy_checksum"]}
        if data["review"] is not None:
            h = repository.get(WORKFLOW_HEAD, lock=True, review_id=_uuid(review_id))
            if h is not None:
                d = repository.get(DRAFT, review_id=_uuid(review_id), draft_id=h["current_draft_id"])
                _require(d is not None, "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
                generation = _encoded(d, "generation", encode_review_generation)
                data["draft"] = {"draftId": str(d["draft_id"]), "revision": int(d["revision"]),
                    "text": bytes(d["text_utf8"]).decode("utf-8"), "textChecksum": d["text_checksum"],
                    "bindingChecksum": d["binding_checksum"], "generation": generation,
                    "policyHeadId": str(d["policy_head_id"]), "policyHeadVersion": int(d["policy_head_version"])}
                data["workflowHead"] = {"headId": str(h["head_id"]), "version": int(h["version"])}
                if h["current_decision_id"] is not None:
                    decision = repository.get(DECISION, review_id=_uuid(review_id), decision_id=h["current_decision_id"])
                    _require(decision is not None, "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
                    data["decision"] = {"decisionId": str(decision["decision_id"]),
                        "draftId": str(decision["draft_id"]), "draftRevision": int(decision["draft_revision"]),
                        "decisionKind": decision["decision_kind"], "actorMembershipId": decision["actor_membership_id"]}
    return data
