"""Private scoped SQL operations. No connection factory, domain transitions or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import text

from app.infra.db import set_tenant_context
from app.platform.integrations.publication_guard import ExpectedAccountBinding, ExpectedCredential, UserSessionPrincipal
from app.platform.integrations.repricer_job_contract import (
    OPERATION, Redacted, RepricerApprovalBinding, RepricerAuthorityMetadata, RepricerAuthorityPolicy, RepricerExpectedState,
    RepricerJobError, RepricerJobLocator, RepricerJobView, RepricerReadback, RepricerReceiptView,
    integer, timestamp,
)


def scope(session, organization_id, marketplace_account_id):
    integer(organization_id)
    integer(marketplace_account_id)
    for name, value in (("app.organization_id", organization_id), ("app.marketplace_account_id", marketplace_account_id)):
        old = session.scalar(text("SELECT current_setting(:name,true)"), {"name": name})
        if old not in (None,"",str(value)):
            raise RepricerJobError("REPRICER_FENCE_INVALID")
    set_tenant_context(session, organization_id)
    session.execute(text("SELECT set_config('app.marketplace_account_id',:value,true)"), {"value": str(marketplace_account_id)})


def now(session):
    return timestamp(session.scalar(text("SELECT clock_timestamp()")))


def owner(locator):
    return {"org": locator.organization_id, "account": locator.marketplace_account_id, "job": locator.job_id}


def frozen(row):
    return None if row is None else MappingProxyType(dict(row))


def account_lock(session, locator):
    found = session.scalar(text("""SELECT marketplace_account_id FROM public.marketplace_accounts
        WHERE organization_id=:org AND marketplace_account_id=:account AND marketplace='wb' FOR UPDATE"""), owner(locator))
    if found is None:
        raise RepricerJobError("REPRICER_NOT_FOUND")


def approval(session, binding, *, lock=True):
    row = session.execute(text("""SELECT * FROM public.wb_repricer_price_approvals
        WHERE organization_id=:org AND marketplace_account_id=:account AND approval_row_id=:approval"""
        + (" FOR UPDATE" if lock else "")), {"org": binding.organization_id,
            "account": binding.marketplace_account_id, "approval": binding.approval_row_id}).mappings().one_or_none()
    if row is None:
        raise RepricerJobError("REPRICER_NOT_FOUND")
    if approval_binding(row) != binding:
        raise RepricerJobError("REPRICER_CONFLICT")
    return frozen(row)


def approval_binding(row):
    if row["request_format"] != "wb-price-apply/v1" or row["marketplace"] != "wb":
        raise RepricerJobError("REPRICER_CONFLICT")
    raw = row["canonical_request_bytes"]
    if type(raw) is memoryview:
        raw = raw.tobytes()
    return RepricerApprovalBinding(row["organization_id"], row["marketplace_account_id"], row["approval_row_id"],
        row["approval_id"], row["action_key"], row["request_checksum"], raw)


def attempt(session, binding, *, lock=True):
    row = session.execute(text("""SELECT * FROM public.wb_repricer_price_apply_attempts
        WHERE organization_id=:org AND marketplace_account_id=:account AND approval_row_id=:approval"""
        + (" FOR UPDATE" if lock else "")), {"org": binding.organization_id,
            "account": binding.marketplace_account_id, "approval": binding.approval_row_id}).mappings().one_or_none()
    return frozen(row)


@dataclass(frozen=True, slots=True, repr=False)
class JobSnapshot(Redacted):
    row: object
    authority: object
    binding: RepricerApprovalBinding

    @property
    def locator(self):
        return RepricerJobLocator(self.row["organization_id"], self.row["marketplace_account_id"], self.row["job_id"])

    @property
    def principal(self):
        return UserSessionPrincipal(self.row["organization_id"], self.row["initiator_user_id"],
            self.row["initiator_membership_id"], self.row["initiator_session_id"])

    @property
    def account(self):
        return ExpectedAccountBinding(self.row["marketplace_account_id"], "wb",
            self.authority["expected_external_account_id"], self.authority["expected_credential_ref"])

    @property
    def credential(self):
        a = self.authority
        return ExpectedCredential(self.row["marketplace_account_id"], a["credential_id"], "wb_api",
            a["generation"], a["payload_schema_version"], a["expires_at"])

    @property
    def policy(self):
        return RepricerAuthorityPolicy(self.authority["policy_reference"], self.authority["policy_version"])

    @property
    def captured_authority(self):
        a = self.authority
        return RepricerAuthorityMetadata(self.locator,a["expected_external_account_id"],a["expected_credential_ref"],
            a["credential_id"],a["generation"],a["payload_schema_version"],a["expires_at"],a["authority_expires_at"],self.policy)

    def view(self):
        return RepricerJobView(self.locator, self.binding, self.row["created_at"], self.row["created_audit_id"])


def read_job(session, locator):
    """Immutable job SELECT; owning account lock serializes mutation participants."""
    row = session.execute(text("""SELECT * FROM public.wb_repricing_jobs
        WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job"""), owner(locator)).mappings().one_or_none()
    if row is None:
        raise RepricerJobError("REPRICER_NOT_FOUND")
    auth = session.execute(text("""SELECT * FROM public.wb_repricing_job_authorities
        WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job"""), owner(locator)).mappings().one_or_none()
    a = session.execute(text("""SELECT * FROM public.wb_repricer_price_approvals
        WHERE organization_id=:org AND marketplace_account_id=:account AND approval_row_id=:approval"""),
        {**owner(locator), "approval": row["approval_row_id"]}).mappings().one_or_none()
    if (auth is None or a is None or row["operation_kind"] != OPERATION or row["provider"] != "wb"
            or auth["provider"] != "wb" or auth["credential_kind"] != "wb_api"
            or (row["action_key"],row["request_checksum"]) != (a["action_key"],a["request_checksum"])):
        raise RepricerJobError("REPRICER_CONFLICT")
    result = JobSnapshot(frozen(row), frozen(auth), approval_binding(a))
    # Construct all strict captured contracts before exposing stored origin.
    result.principal, result.account, result.credential, result.captured_authority, result.view()
    timestamp(auth["authority_expires_at"])
    return result


def find_by_approval(session, binding):
    job_id = session.scalar(text("""SELECT job_id FROM public.wb_repricing_jobs
        WHERE organization_id=:org AND marketplace_account_id=:account AND approval_row_id=:approval"""),
        {"org": binding.organization_id, "account": binding.marketplace_account_id, "approval": binding.approval_row_id})
    return None if job_id is None else read_job(session, RepricerJobLocator(binding.organization_id,binding.marketplace_account_id,job_id))


def create_job(session, binding, principal, account, credential, policy, expires_at):
    locator = RepricerJobLocator(binding.organization_id,binding.marketplace_account_id,uuid4())
    audit_id = uuid4()
    values = {**owner(locator), "approval": binding.approval_row_id, "action": binding.action_key,
        "checksum": binding.request_checksum, "user": principal.user_id, "login": principal.session_id,
        "member": principal.membership_id, "audit": audit_id}
    created = session.scalar(text("""INSERT INTO public.wb_repricing_jobs
        (job_id,organization_id,marketplace_account_id,provider,approval_row_id,action_key,request_checksum,
         operation_kind,initiator_user_id,initiator_session_id,initiator_membership_id,created_at,created_audit_id)
        VALUES(:job,:org,:account,'wb',:approval,:action,:checksum,'wb.price_apply.v1',:user,:login,:member,clock_timestamp(),:audit)
        RETURNING created_at"""), values)
    session.execute(text("""INSERT INTO public.wb_repricing_job_authorities
        (organization_id,marketplace_account_id,job_id,provider,expected_external_account_id,expected_credential_ref,
         credential_id,credential_kind,generation,payload_schema_version,expires_at,authority_expires_at,policy_reference,policy_version)
        VALUES(:org,:account,:job,'wb',:external,:ref,:credential,'wb_api',:generation,1,NULL,:expires,:policy,:version)"""),
        {**owner(locator), "external": account.external_account_id, "ref": account.credential_ref,
         "credential": credential.credential_id, "generation": credential.generation,
         "expires": expires_at, "policy": policy.reference, "version": policy.version})
    session.execute(text("""INSERT INTO public.wb_repricing_job_audit
        (audit_event_id,organization_id,marketplace_account_id,job_id,approval_row_id,event_kind,actor_kind,actor_membership_id,occurred_at)
        VALUES(:audit,:org,:account,:job,:approval,'repricer_job.created','membership',:member,:created)"""),
        {**values,"created": created})
    return read_job(session, locator)


def expected_state(binding, a, t):
    return RepricerExpectedState(binding, a["version"], None if t is None else t["attempt_id"],
        None if t is None else t["version"], None if t is None else t["dispatch_key"],
        None if t is None else t["claim_version"])


def receipt_view(locator, r):
    return RepricerReceiptView(locator, *(r[k] for k in ("receipt_id","approval_row_id","attempt_id",
        "action_key","request_checksum","dispatch_key","wb_upload_id","observed_at","recorded_at","created_audit_id")))


def read_receipt(session, snapshot, attempt_id):
    row = session.execute(text("""SELECT * FROM public.wb_repricing_upload_receipts
        WHERE organization_id=:org AND marketplace_account_id=:account AND attempt_id=:attempt"""),
        {**owner(snapshot.locator), "attempt": attempt_id}).mappings().one_or_none()
    if row is None:
        return None
    if (row["job_id"],row["approval_row_id"],row["action_key"],row["request_checksum"]) != (
            snapshot.locator.job_id,snapshot.binding.approval_row_id,snapshot.binding.action_key,snapshot.binding.request_checksum):
        raise RepricerJobError("REPRICER_CONFLICT")
    return receipt_view(snapshot.locator, row)


def insert_receipt(session, snapshot, attempt_row, observation):
    existing = read_receipt(session,snapshot,attempt_row["attempt_id"])
    if existing is not None:
        if (existing.wb_upload_id,existing.dispatch_key) != (observation.wb_upload_id,attempt_row["dispatch_key"]):
            raise RepricerJobError("REPRICER_CONFLICT")
        return existing
    values = {**owner(snapshot.locator),"receipt": uuid4(),"approval": snapshot.binding.approval_row_id,
        "attempt": attempt_row["attempt_id"],"action": snapshot.binding.action_key,
        "checksum": snapshot.binding.request_checksum,"dispatch": attempt_row["dispatch_key"],
        "upload": observation.wb_upload_id,"observed": observation.observed_at,"audit": uuid4()}
    row = session.execute(text("""INSERT INTO public.wb_repricing_upload_receipts
        (receipt_id,organization_id,marketplace_account_id,provider,job_id,approval_row_id,attempt_id,
         action_key,request_checksum,dispatch_key,wb_upload_id,observed_at,recorded_at,created_audit_id)
        VALUES(:receipt,:org,:account,'wb',:job,:approval,:attempt,:action,:checksum,:dispatch,:upload,:observed,clock_timestamp(),:audit)
        RETURNING *"""), values).mappings().one()
    session.execute(text("""INSERT INTO public.wb_repricing_upload_receipt_audit
        (audit_event_id,organization_id,marketplace_account_id,job_id,approval_row_id,attempt_id,receipt_id,
         event_kind,actor_kind,actor_membership_id,occurred_at)
        VALUES(:audit,:org,:account,:job,:approval,:attempt,:receipt,'provider_upload.receipt_observed','repricer_worker',NULL,:recorded)"""),
        {**values,"recorded": row["recorded_at"]})
    return receipt_view(snapshot.locator,row)


def readback(session, snapshot, a, t):
    receipt = None if t is None else read_receipt(session,snapshot,t["attempt_id"])
    return RepricerReadback(snapshot.locator,a["status"],expected_state(snapshot.binding,a,t),
        None if t is None else t["status"],receipt)
