"""Dormant scoped PostgreSQL transaction adapter for accepted migration 0066.

This is NOT the committed-return PriceApprovalRepository service. The trusted
caller owns a clean Engine-bound root Session, authorization-before-account lock
order, physical commit/rollback and sanitizing physical COMMIT failures. Values
returned here are uncommitted and never authorize a provider call. No workers,
routers, fallback storage or current repricer consumers are connected.
"""

from dataclasses import fields
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context
from app.modules.wb_repricing import (
    ApprovalConflictError,
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    block_approval,
    build_action_key,
    claim_approval,
    reject_approval,
)
from app.modules.wb_repricing_dispatch import (
    ApplyAttempt,
    ApplyOutcome,
    AttemptStatus,
    CanonicalApplyRequest,
    _uuid4,
    finish_attempt,
    mark_dispatched,
    reserve_attempt,
)
from app.modules.wb_repricing_repository import (
    ApprovalIdentityBlockedError,
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
    bridge_actor_membership_id,
    bridge_internal_id,
    bridge_snapshot_identity,
)


class ApprovalPersistenceError(RuntimeError):
    def __init__(self):
        super().__init__("approval_persistence_failed")


def _integer(value):
    if type(value) is int:
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        return int(value)
    raise ApprovalPersistenceError()


def _snapshot(row):
    values = {field.name: row[field.name] for field in fields(PriceApprovalSnapshot)}
    for key in ("organization_id", "nm_id", "recommended_price_kopecks", "version"):
        values[key] = _integer(values[key])
    for key in (
        "marketplace_account_id",
        "catalog_sku_id",
        "claimed_by_membership_id",
        "decided_by_membership_id",
    ):
        values[key] = None if values[key] is None else str(_integer(values[key]))
    values["status"] = ApprovalStatus(values["status"])
    return PriceApprovalSnapshot(**values)


def _mutable(snapshot):
    values = {
        key: getattr(snapshot, key)
        for key in (
            "status",
            "version",
            "updated_at",
            "claimed_by_membership_id",
            "decided_by_membership_id",
            "reason_code",
            "safe_error_code",
            "wb_upload_id",
            "result_code",
        )
    }
    values["status"] = snapshot.status.value
    for key in ("claimed_by_membership_id", "decided_by_membership_id"):
        if values[key] is not None:
            values[key] = int(values[key])
    return values


class ApprovalTransaction:
    """Internal transaction participant. Membership ownership is not authentication."""

    def __init__(self, session: Session, scope: ApprovalRepositoryScope):
        if type(scope) is not ApprovalRepositoryScope:
            raise ApprovalValidationError("approval scope required")
        self.session, self.scope = session, scope
        set_marketplace_account_context(
            session,
            organization_id=scope.organization_id,
            marketplace_account_id=scope.marketplace_account_id,
        )
        self.root = session.get_transaction()
        account = self._sql(
            "SELECT marketplace_account_id FROM marketplace_accounts WHERE organization_id=:org AND marketplace_account_id=:account AND marketplace='wb' FOR UPDATE"
        ).scalar_one_or_none()
        if account is None:
            raise ApprovalValidationError("approval account unavailable")

    def _sql(self, sql, values=None):
        if self.session.get_transaction() is not self.root or not self.root.is_active:
            raise ApprovalValidationError("approval root transaction ended")
        try:
            return self.session.execute(
                text(sql),
                {
                    "org": self.scope.organization_id,
                    "account": self.scope.marketplace_account_id,
                    "approval": self.scope.approval_id,
                    **(values or {}),
                },
            )
        except SQLAlchemyError as error:
            if getattr(getattr(error, "orig", None), "sqlstate", None) == "23505":
                raise ApprovalConflictError("approval unique conflict") from None
            raise ApprovalPersistenceError() from None

    def _now(self):
        return self._sql("SELECT clock_timestamp()").scalar_one()

    def _actor(self, actor):
        identity = bridge_actor_membership_id(self.scope, actor)
        if not self._sql(
            "SELECT membership_id FROM iam_memberships WHERE organization_id=:org AND membership_id=:member AND is_active",
            {"member": actor.membership_id},
        ).scalar_one_or_none():
            raise ApprovalValidationError("approval membership unavailable")
        return identity

    def _row(self, required=True):
        row = (
            self._sql(
                "SELECT * FROM wb_repricer_price_approvals WHERE organization_id=:org AND marketplace_account_id=:account AND approval_id=:approval"
            )
            .mappings()
            .one_or_none()
        )
        if row is None and required:
            raise ApprovalConflictError("approval unavailable")
        return row

    def get(self):
        row = self._row(False)
        return None if row is None else _snapshot(row)

    def _insert(self, table, values):
        # Table/column names below are closed internal constants, never client input.
        return (
            self._sql(
                f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join(':' + key for key in values)}) RETURNING *",
                values,
            )
            .mappings()
            .one()
        )

    def _audit(
        self,
        row,
        kind,
        event_id,
        *,
        before=None,
        actor=None,
        attempt=None,
        before_attempt=None,
    ):
        values = {
            "audit_event_id": event_id,
            "organization_id": self.scope.organization_id,
            "marketplace_account_id": self.scope.marketplace_account_id,
            "approval_row_id": row["approval_row_id"],
            "event_kind": kind,
            "actor_kind": "membership" if actor is not None else "repricer_worker",
            "actor_membership_id": None if actor is None else actor.membership_id,
            "before_status": None if before is None else before["status"],
            "after_status": row["status"],
            "before_version": None if before is None else before["version"],
            "after_version": row["version"],
            "occurred_at": row["updated_at"] if attempt is None else attempt.updated_at,
            "attempt_id": None if attempt is None else attempt.attempt_id,
            "before_attempt_version": None
            if before_attempt is None
            else before_attempt.version,
            "after_attempt_version": None if attempt is None else attempt.version,
            **{
                key: row[key]
                for key in (
                    "reason_code",
                    "safe_error_code",
                    "wb_upload_id",
                    "result_code",
                )
            },
        }
        if kind == "approval.imported":
            values.update(
                actor_kind="backfill",
                actor_membership_id=None,
                occurred_at=row["created_at"],
                reason_code=None,
                safe_error_code=None,
                wb_upload_id=None,
                result_code=None,
            )
        self._insert("wb_repricer_price_approval_audit", values)

    def create_intent(
        self, request: CanonicalApplyRequest, actor: AuthenticatedApprovalActor
    ):
        if type(request) is not CanonicalApplyRequest or request.scope != self.scope:
            raise ApprovalValidationError("request scope mismatch")
        self._actor(actor)
        old = self._row(False)
        if old is not None:
            if (
                old["request_format"] != "wb-price-apply/v1"
                or bytes(old["canonical_request_bytes"]) != request.canonical_bytes
            ):
                raise ApprovalConflictError("approval immutable intent mismatch")
            return _snapshot(old)
        now, witness = self._now(), uuid4()
        row = self._insert(
            "wb_repricer_price_approvals",
            {
                "approval_row_id": uuid4(),
                "organization_id": self.scope.organization_id,
                "marketplace_account_id": self.scope.marketplace_account_id,
                "marketplace": "wb",
                "approval_id": self.scope.approval_id,
                "catalog_sku_id": request.catalog_sku_id,
                "nm_id": request.nm_id,
                "article_id": request.article_id,
                "recommended_price_kopecks": request.price_kopecks,
                "request_checksum": request.checksum,
                "action_key": build_action_key(
                    self.scope.organization_id,
                    str(self.scope.marketplace_account_id),
                    self.scope.approval_id,
                    request.checksum,
                ),
                "request_format": "wb-price-apply/v1",
                "canonical_request_bytes": request.canonical_bytes,
                "discount_pct": request.discount_pct,
                "size_id": request.size_id,
                "min_price_kopecks": request.min_price_kopecks,
                "status": "pending",
                "version": 0,
                "created_at": now,
                "updated_at": now,
                "created_audit_id": witness,
            },
        )
        self._audit(row, "approval.created", witness, actor=actor)
        return _snapshot(row)

    def _update_approval(self, before, after, witness_column, witness):
        values = {**_mutable(after), witness_column: witness}
        row = (
            self._sql(
                f"UPDATE wb_repricer_price_approvals SET {','.join(key + '=:' + key for key in values)} WHERE organization_id=:org AND marketplace_account_id=:account AND approval_id=:approval AND status=:old_status AND version=:old_version AND request_format='wb-price-apply/v1' RETURNING *",
                {
                    **values,
                    "old_status": before["status"],
                    "old_version": before["version"],
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApprovalConflictError("approval CAS conflict")
        return row

    def _decision(self, expected_version, actor, kind, reason=None):
        member = self._actor(actor)
        before = self._row()
        snapshot, now = _snapshot(before), self._now()
        if kind == "claimed":
            after = claim_approval(snapshot, expected_version, member, now)
        elif kind == "rejected":
            after = reject_approval(snapshot, expected_version, member, reason, now)
        else:
            after = block_approval(snapshot, expected_version, member, reason, now)
        witness = uuid4()
        row = self._update_approval(
            before,
            after,
            "claimed_audit_id" if kind == "claimed" else "decision_audit_id",
            witness,
        )
        self._audit(row, "approval." + kind, witness, before=before, actor=actor)
        return _snapshot(row)

    def claim(self, expected_version, actor):
        return self._decision(expected_version, actor, "claimed")

    def reject(self, expected_version, actor, reason_code):
        return self._decision(expected_version, actor, "rejected", reason_code)

    def block(self, expected_version, actor, safe_blocker_code):
        return self._decision(expected_version, actor, "blocked", safe_blocker_code)

    def _attempt(self, row, attempt_id=None):
        value = (
            self._sql(
                "SELECT * FROM wb_repricer_price_apply_attempts WHERE organization_id=:org AND marketplace_account_id=:account AND approval_row_id=:row_id",
                {"row_id": row["approval_row_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if value is None:
            if attempt_id is not None:
                raise ApprovalConflictError("attempt unavailable")
            return None
        if attempt_id is not None and str(value["attempt_id"]) != attempt_id:
            raise ApprovalConflictError("attempt unavailable")
        outcome = (
            None
            if value["finished_at"] is None
            else ApplyOutcome(
                AttemptStatus(value["status"]),
                value["safe_error_code"],
                value["wb_upload_id"],
                value["result_code"],
            )
        )
        return ApplyAttempt(
            self.scope,
            str(value["attempt_id"]),
            value["action_key"],
            value["request_checksum"],
            _integer(value["claim_version"]),
            value["claimed_by_membership_id"],
            AttemptStatus(value["status"]),
            value["version"],
            value["reserved_at"],
            value["updated_at"],
            value["dispatch_at"],
            value["finished_at"],
            outcome,
        )

    def reserve_attempt(self, expected_version, actor):
        self._actor(actor)
        row = self._row()
        if row["request_format"] != "wb-price-apply/v1":
            raise ApprovalConflictError("legacy approval cannot dispatch")
        old = self._attempt(row)
        request = CanonicalApplyRequest(
            self.scope,
            row["catalog_sku_id"],
            _integer(row["nm_id"]),
            row["article_id"],
            _integer(row["recommended_price_kopecks"]),
            row["discount_pct"],
            None if row["size_id"] is None else _integer(row["size_id"]),
            None
            if row["min_price_kopecks"] is None
            else _integer(row["min_price_kopecks"]),
        )
        after = reserve_attempt(
            _snapshot(row), request, expected_version, actor, str(uuid4()), self._now()
        )
        if old is not None:
            if (
                old.status is not AttemptStatus.reserved
                or old.claim_version != expected_version
                or old.claimed_by_membership_id != actor.membership_id
            ):
                raise ApprovalConflictError("attempt already dispatched or closed")
            return old
        witness = uuid4()
        self._insert(
            "wb_repricer_price_apply_attempts",
            {
                "attempt_id": after.attempt_id,
                "organization_id": self.scope.organization_id,
                "marketplace_account_id": self.scope.marketplace_account_id,
                "approval_row_id": row["approval_row_id"],
                "action_key": after.action_key,
                "request_checksum": after.request_checksum,
                "dispatch_key": after.dispatch_key,
                "claim_version": after.claim_version,
                "claimed_by_membership_id": after.claimed_by_membership_id,
                "status": "reserved",
                "version": 0,
                "reserved_at": after.reserved_at,
                "updated_at": after.updated_at,
                "reserved_audit_id": witness,
            },
        )
        self._audit(
            row, "attempt.reserved", witness, before=row, actor=actor, attempt=after
        )
        return after

    def _update_attempt(self, before, after, witness_column, witness):
        values = {
            "status": after.status.value,
            "version": after.version,
            "updated_at": after.updated_at,
            "dispatch_at": after.dispatch_at,
            "finished_at": after.finished_at,
            witness_column: witness,
            "safe_error_code": None
            if after.outcome is None
            else after.outcome.safe_error_code,
            "wb_upload_id": None
            if after.outcome is None
            else after.outcome.wb_upload_id,
            "result_code": None if after.outcome is None else after.outcome.result_code,
        }
        result = self._sql(
            f"UPDATE wb_repricer_price_apply_attempts SET {','.join(key + '=:' + key for key in values)} WHERE organization_id=:org AND marketplace_account_id=:account AND attempt_id=CAST(:attempt AS uuid) AND version=:old_version AND status=:old_status RETURNING attempt_id",
            {
                **values,
                "attempt": before.attempt_id,
                "old_version": before.version,
                "old_status": before.status.value,
            },
        ).scalar_one_or_none()
        if result is None:
            raise ApprovalConflictError("attempt CAS conflict")

    def mark_dispatch(
        self, attempt_id, expected_version, expected_attempt_version, actor
    ):
        _uuid4(attempt_id)
        self._actor(actor)
        row = self._row()
        old = self._attempt(row, attempt_id)
        after = mark_dispatched(
            _snapshot(row),
            old,
            expected_version,
            expected_attempt_version,
            actor,
            self._now(),
        )
        witness = uuid4()
        self._update_attempt(old, after, "dispatched_audit_id", witness)
        self._audit(
            row,
            "attempt.dispatched",
            witness,
            before=row,
            actor=actor,
            attempt=after,
            before_attempt=old,
        )
        return after

    def record_attempt_outcome(
        self, attempt_id, expected_version, expected_attempt_version, outcome
    ):
        _uuid4(attempt_id)
        row = self._row()
        old = self._attempt(row, attempt_id)
        result = finish_attempt(
            _snapshot(row),
            old,
            expected_version,
            expected_attempt_version,
            outcome,
            self._now(),
        )
        witness = uuid4()
        self._update_attempt(old, result.attempt, "outcome_audit_id", witness)
        after = self._update_approval(row, result.approval, "outcome_audit_id", witness)
        kind = (
            "apply.succeeded"
            if outcome.status is AttemptStatus.applied
            else "apply." + outcome.status.value
        )
        self._audit(
            after, kind, witness, before=row, attempt=result.attempt, before_attempt=old
        )
        return result


class LegacyApprovalImportTransaction:
    """Owner-only, caller-committed import of already resolved immutable values.

    No legacy files are loaded and no identities, hashes, states, timestamps or
    money are repaired. The caller must supply verified internal integer catalog
    and membership identities. Database guards additionally forbid legacy writes
    by runtime roles, all subsequent lifecycle changes and attempts.
    """

    def __init__(self, session: Session, scope: ApprovalRepositoryScope):
        self._transaction = ApprovalTransaction(session, scope)
        owner = self._transaction._sql(
            "SELECT current_user = pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid='public.wb_repricer_price_approvals'::regclass"
        ).scalar_one()
        if owner is not True:
            raise ApprovalValidationError("legacy import requires table owner")

    def insert_snapshot(
        self,
        snapshot,
        *,
        catalog_sku_id,
        claimed_by_membership_id,
        decided_by_membership_id,
    ):
        transaction = self._transaction
        bridge_snapshot_identity(
            scope=transaction.scope, catalog_sku_id=catalog_sku_id, snapshot=snapshot
        )
        for field, identity in (
            ("claimed_by_membership_id", claimed_by_membership_id),
            ("decided_by_membership_id", decided_by_membership_id),
        ):
            kernel_identity = None if identity is None else bridge_internal_id(identity)
            if getattr(snapshot, field) != kernel_identity:
                raise ApprovalIdentityBlockedError(field)
        old = transaction._row(False)
        if old is not None:
            if old["request_format"] != "legacy" or _snapshot(old) != snapshot:
                raise ApprovalConflictError("legacy immutable snapshot mismatch")
            return _snapshot(old)
        witness = uuid4()
        values = {
            field.name: getattr(snapshot, field.name)
            for field in fields(PriceApprovalSnapshot)
        }
        values.update(
            approval_row_id=uuid4(),
            marketplace="wb",
            request_format="legacy",
            marketplace_account_id=transaction.scope.marketplace_account_id,
            catalog_sku_id=catalog_sku_id,
            claimed_by_membership_id=claimed_by_membership_id,
            decided_by_membership_id=decided_by_membership_id,
            status=snapshot.status.value,
            canonical_request_bytes=None,
            discount_pct=None,
            size_id=None,
            min_price_kopecks=None,
            created_audit_id=witness,
        )
        row = transaction._insert("wb_repricer_price_approvals", values)
        transaction._audit(row, "approval.imported", witness)
        return _snapshot(row)
