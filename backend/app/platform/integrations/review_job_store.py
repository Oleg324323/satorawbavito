"""Scoped immutable authority/control metadata only. T4 owns every transition."""
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import text

from app.infra.db import set_tenant_context
from app.platform.integrations.publication_guard import ExpectedAccountBinding, ExpectedCredential, UserSessionPrincipal
from app.platform.integrations.review_job_contract import (
    Redacted, ReviewAuthorityCapture, ReviewAuthorityPolicy, ReviewExpectedState,
    ReviewCreated, ReviewJobLocator, ReviewReadback, ReviewSendIntent, number, require, timestamp,
)

_OWNER = "organization_id=:org AND marketplace_account_id=:account AND marketplace=:marketplace"
_COMMAND = "command_id,review_id,draft_id,draft_revision,decision_id,operation_kind,idempotency_key,request_payload,request_checksum,binding_checksum,text_checksum,creator_membership_id,created_at,state,version,current_attempt_id,completed_at,result_evidence_id,reason_code,audit_event_id"
_AUTHORITY = "origin_user_id,origin_membership_id,origin_session_id,account_binding_schema_version,expected_external_account_id,expected_credential_ref,credential_id,generation,credential_kind,payload_schema_version,credential_expires_at,policy_reference,policy_version,lease_seconds,authority_expires_at"
_ATTEMPT = "attempt_id,command_id,review_id,sequence,claimed_at,lease_token,state,lease_expires_at,dispatched_at,finished_at,result_evidence_id,reason_code,command_version,audit_event_id"


def scope(session, locator):
    locator.__post_init__()
    for name, value in (("app.organization_id", locator.organization_id), ("app.marketplace_account_id", locator.marketplace_account_id)):
        require(session.scalar(text("SELECT current_setting(:name,true)"), {"name": name}) in
                (None, "", str(value)), "REVIEW_FENCE_INVALID")
    set_tenant_context(session, locator.organization_id)
    session.execute(text("SELECT set_config('app.marketplace_account_id',:value,true)"),
                    {"value": str(locator.marketplace_account_id)})


def now(session):
    return timestamp(session.scalar(text("SELECT clock_timestamp()")))


def owner(locator):
    return {"org": locator.organization_id, "account": locator.marketplace_account_id,
            "marketplace": locator.marketplace, "command": locator.command_id}


def frozen(row):
    if row is None:
        return None
    return MappingProxyType({k: bytes(v) if type(v) is memoryview else v for k, v in row.items()})


def account_lock(session, locator):
    require(session.scalar(text("SELECT marketplace_account_id FROM public.marketplace_accounts WHERE "
        + _OWNER + " FOR UPDATE"), owner(locator)) is not None, "REVIEW_NOT_FOUND")


def command(session, locator, *, lock=False):
    return frozen(session.execute(text("SELECT " + _COMMAND + " FROM public.review_send_commands WHERE "
        + _OWNER + " AND command_id=:command" + (" FOR UPDATE" if lock else "")),
        owner(locator)).mappings().one_or_none())


def authority(session, locator):
    return frozen(session.execute(text("SELECT " + _AUTHORITY + " FROM public.review_send_command_authorities WHERE "
        + _OWNER + " AND command_id=:command"), owner(locator)).mappings().one_or_none())


def attempt(session, locator, attempt_id, *, lock=False):
    if attempt_id is None:
        return None
    result = frozen(session.execute(text("SELECT " + _ATTEMPT + " FROM public.review_send_attempts WHERE "
        + _OWNER + " AND command_id=:command AND attempt_id=:attempt" + (" FOR UPDATE" if lock else "")),
        {**owner(locator), "attempt": attempt_id}).mappings().one_or_none())
    require(result is not None, "REVIEW_CONFLICT")
    return result


def approver(session, intent):
    """Unprivileged locator; guard rechecks the exact decision after sorted locks."""
    row = session.execute(text("""SELECT d.actor_membership_id,m.user_id FROM public.review_decisions d
        JOIN public.iam_memberships m ON m.organization_id=d.organization_id AND m.membership_id=d.actor_membership_id
        WHERE d.organization_id=:org AND d.marketplace_account_id=:account AND d.marketplace=:marketplace
        AND d.review_id=:review AND d.decision_id=:decision AND d.draft_id=:draft
        AND d.draft_revision=CAST(:revision AS numeric) AND d.binding_checksum=:binding AND d.decision_kind='approved'"""),
        {**owner(intent.locator), "review": intent.review_id, "decision": intent.decision_id,
         "draft": intent.draft_id, "revision": str(number(intent.draft_revision)),
         "binding": intent.binding_checksum}).one_or_none()
    require(row is not None, "REVIEW_AUTHORITY_DENIED")
    return tuple(row)


def intent_from(locator, c):
    require(c["operation_kind"] == "review.answer.create.v1", "REVIEW_CONFLICT")
    return ReviewSendIntent(locator, *(c[k] for k in ("review_id", "draft_id", "draft_revision",
        "decision_id", "idempotency_key", "request_payload", "request_checksum", "binding_checksum", "text_checksum")))


def capture_from(locator, h):
    require(h["account_binding_schema_version"] == 1, "REVIEW_CONFLICT")
    return ReviewAuthorityCapture(locator,
        UserSessionPrincipal(locator.organization_id, h["origin_user_id"], h["origin_membership_id"], h["origin_session_id"]),
        ExpectedAccountBinding(locator.marketplace_account_id, locator.marketplace, h["expected_external_account_id"], h["expected_credential_ref"]),
        ExpectedCredential(locator.marketplace_account_id, h["credential_id"], h["credential_kind"], h["generation"], h["payload_schema_version"], h["credential_expires_at"]),
        ReviewAuthorityPolicy(h["policy_reference"], h["policy_version"], h["lease_seconds"]),
        timestamp(h["authority_expires_at"]))


@dataclass(frozen=True, slots=True, repr=False)
class Snapshot(Redacted):
    locator: ReviewJobLocator
    command: object
    authority: object
    attempt: object

    @property
    def intent(self):
        return intent_from(self.locator, self.command)

    @property
    def capture(self):
        return capture_from(self.locator, self.authority)

    @property
    def expected(self):
        a = self.attempt
        return ReviewExpectedState(self.locator, self.command["version"], self.command["state"],
            None if a is None else a["attempt_id"], None if a is None else a["lease_token"],
            None if a is None else a["command_version"], None if a is None else a["state"],
            None if a is None else a["dispatched_at"])

    def readback(self):
        return ReviewReadback(self.expected, None if self.attempt is None else self.attempt["lease_expires_at"],
            self.command["result_evidence_id"], self.command["reason_code"])


def snapshot(session, locator, *, lock=False):
    c, h = command(session, locator, lock=lock), authority(session, locator)
    require(c is not None and h is not None, "REVIEW_NOT_FOUND")
    result = Snapshot(locator, c, h, attempt(session, locator, c["current_attempt_id"], lock=lock))
    require(result.capture.principal.membership_id == c["creator_membership_id"], "REVIEW_CONFLICT")
    require(result.capture.credential.kind == ("wb_api" if locator.marketplace == "wb" else "avito_oauth_access"), "REVIEW_CONFLICT")
    result.intent, result.expected
    return result


def replay(session, intent):
    command_id = session.scalar(text("SELECT command_id FROM public.review_send_commands WHERE " + _OWNER
        + " AND operation_kind='review.answer.create.v1' AND idempotency_key=:idempotency"),
        {**owner(intent.locator), "idempotency": intent.idempotency_key})
    if command_id is None:
        return None
    old = intent.locator
    return snapshot(session, ReviewJobLocator(old.organization_id, old.marketplace_account_id, old.marketplace, command_id))


def authority_values(capture):
    """Exact dict consumed by T4 ReviewSendRepository.create(authority=...)."""
    p, a, c, policy = capture.principal, capture.account, capture.credential, capture.policy
    return {"origin_user_id": p.user_id, "origin_membership_id": p.membership_id, "origin_session_id": p.session_id,
        "account_binding_schema_version": 1, "expected_external_account_id": a.external_account_id,
        "expected_credential_ref": a.credential_ref, "credential_id": c.credential_id, "generation": c.generation,
        "credential_kind": c.kind, "payload_schema_version": c.payload_schema_version, "credential_expires_at": c.expires_at,
        "policy_reference": policy.reference, "policy_version": number(policy.version),
        "lease_seconds": number(policy.lease_seconds), "authority_expires_at": capture.authority_expires_at}


def created(session, snapshot):
    row = session.execute(text("SELECT event_id,occurred_at FROM public.review_send_audit WHERE " + _OWNER
        + " AND command_id=:command AND aggregate_version=1 AND event_kind='send.created'"),
        owner(snapshot.locator)).one_or_none()
    require(row is not None and row.occurred_at == snapshot.command["created_at"], "REVIEW_FENCE_INVALID")
    return ReviewCreated(snapshot.locator, snapshot.command["review_id"], row.occurred_at, row.event_id)


def audit(session, snapshot):
    return frozen(session.execute(text("""SELECT event_kind,actor_kind,actor_membership_id,attempt_id,lease_token,
        aggregate_version,occurred_at FROM public.review_send_audit WHERE """ + _OWNER
        + " AND command_id=:command AND event_id=:audit"),
        {**owner(snapshot.locator), "audit": snapshot.command["audit_event_id"]}).mappings().one_or_none())


def evidence_metadata(session, locator, attempt_id):
    if attempt_id is None:
        return ()
    return tuple(frozen(row) for row in session.execute(text("""SELECT evidence_id,attempt_id,read_id,evidence_kind,
        outcome,observed_at,reconciliation_started_at,answer_checksum FROM public.review_answer_evidence WHERE """
        + _OWNER + " AND command_id=:command AND attempt_id=:attempt ORDER BY evidence_id"),
        {**owner(locator), "attempt": attempt_id}).mappings())
