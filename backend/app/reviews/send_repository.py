"""0074 audit-first physical participant, not send/worker authorization.

Caller owns a guarded READ COMMITTED root, current source/policy/approval checks,
authentic evidence, and commit before I/O. No provider, queue, internal commit,
savepoint, credential decrypt or automatic retry. Returned rows are PRIVATE.
"""

import json
import unicodedata
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import bindparam, insert, select, text, update

from app.platform.integrations.orm import MarketplaceAccountRow
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.send_payloads import encode_review_answer_evidence, encode_review_enqueue, encode_review_send_audit
from app.reviews.send_tables import ATTEMPT, AUDIT, AUTHORITY, COMMAND, ENQUEUE, EVIDENCE
from app.reviews.storage_payloads import encode_review_send


class ReviewSendStorageError(ValueError):
    def __init__(self, code="REVIEW_SEND_STORAGE_CONFLICT"):
        allowed = {"REVIEW_SEND_STORAGE_CONFLICT", "REVIEW_SEND_STORAGE_INVALID",
                   "REVIEW_SEND_STORAGE_UNAVAILABLE", "REVIEW_SEND_UNICODE_INCOMPATIBLE"}
        self.code = code if code in allowed else "REVIEW_SEND_STORAGE_UNAVAILABLE"
        super().__init__(self.code)


def _require(value, code="REVIEW_SEND_STORAGE_CONFLICT"):
    if not value:
        raise ReviewSendStorageError(code)


def _decoded(row, prefix, encoder):
    try:
        raw = bytes(row[prefix + "_payload"])
        value = json.loads(raw)
        encoded = encoder(value)
    except (ValueError, TypeError, RecursionError):
        raise ReviewSendStorageError("REVIEW_SEND_STORAGE_UNAVAILABLE") from None
    _require(encoded.canonical_bytes == raw and encoded.checksum == row[prefix + "_checksum"],
             "REVIEW_SEND_STORAGE_UNAVAILABLE")
    return value


class ReviewSendRepository:
    def __init__(self, connection, binding: ReviewBindingDescriptor):
        _require(type(binding) is ReviewBindingDescriptor, "REVIEW_SEND_STORAGE_INVALID")
        binding.__post_init__()
        _require(connection.in_transaction() and connection.dialect.name == "postgresql",
                 "REVIEW_SEND_STORAGE_UNAVAILABLE")
        self.connection, self.binding = connection, binding
        self.owner = {"organization_id": binding.organization_id,
                      "marketplace_account_id": binding.marketplace_account_id, "marketplace": binding.marketplace}
        context = connection.execute(text("SELECT current_setting('transaction_isolation'), "
            "current_setting('app.organization_id',true), current_setting('app.marketplace_account_id',true)")).one()
        _require(tuple(context) == ("read committed", str(binding.organization_id), str(binding.marketplace_account_id)),
                 "REVIEW_SEND_STORAGE_INVALID")
        account = connection.execute(select(MarketplaceAccountRow.external_account_id,
            MarketplaceAccountRow.credential_ref).where(
                MarketplaceAccountRow.organization_id == binding.organization_id,
                MarketplaceAccountRow.marketplace_account_id == binding.marketplace_account_id,
                MarketplaceAccountRow.marketplace == binding.marketplace).with_for_update()).one_or_none()
        _require(account is not None and tuple(account) == (binding.external_account_id, binding.credential_ref))
        # Do not require connected/current source for all post-marker closing.
        # The dedicated T1 closing guard determines the permitted operation.

    def _scope(self, table, **keys):
        return [table.c[key].is_(None) if value is None else table.c[key] == bindparam(None, value, type_=table.c[key].type)
                for key, value in dict(self.owner, **keys).items()]

    def _compatible_writer(self):
        marker = self.connection.scalar(text("SELECT public.review_send_unicode_version()"))
        _require(marker == "14.0.0" and unicodedata.unidata_version == marker,
                 "REVIEW_SEND_UNICODE_INCOMPATIBLE")

    def get(self, table, *, lock=False, **keys):
        _require(table in (COMMAND, AUTHORITY, ATTEMPT, AUDIT, EVIDENCE, ENQUEUE), "REVIEW_SEND_STORAGE_INVALID")
        _require(not lock or table in (COMMAND, ATTEMPT), "REVIEW_SEND_STORAGE_INVALID")
        query = select(table).where(*self._scope(table, **keys))
        if lock:
            query = query.with_for_update()
        return self.connection.execute(query).mappings().one_or_none()

    def _audit(self, *, command_id, review_id, version, kind, before, after,
               actor_kind, actor_membership_id, attempt_id=None, lease_token=None,
               draft_id=None, decision_id=None, reason=None, evidence_id=None):
        # Time, payload, claim UUID/token and ALL physical witnesses are DB-owned.
        row = self.connection.execute(insert(AUDIT).values(**self.owner,
            event_id=uuid4(), aggregate_id=command_id, command_id=command_id, review_id=review_id,
            aggregate_version=version, event_kind=kind, actor_kind=actor_kind,
            actor_membership_id=actor_membership_id, draft_id=draft_id, decision_id=decision_id,
            policy_id=None, attempt_id=attempt_id, lease_token=lease_token,
            before_state=before, after_state=after, reason_code=reason, result_evidence_id=evidence_id,
        ).returning(AUDIT)).mappings().one()
        _decoded(row, "audit", encode_review_send_audit)
        return row

    def _enqueue(self, audit):
        payload = {"schemaVersion": "review-enqueue-v1", "organizationId": self.binding.organization_id,
                   "marketplaceAccountId": self.binding.marketplace_account_id, "marketplace": self.binding.marketplace,
                   "commandId": str(audit["command_id"]), "commandVersion": int(audit["aggregate_version"]),
                   "eventKind": "send.ready"}
        encoded = encode_review_enqueue(payload)
        self.connection.execute(insert(ENQUEUE).values(**self.owner, command_id=audit["command_id"],
            command_version=audit["aggregate_version"], event_kind="send.ready", audit_event_id=audit["event_id"],
            enqueue_payload=encoded.canonical_bytes, enqueue_checksum=encoded.checksum))

    def _creation_result(self, row):
        first = self.get(AUDIT, command_id=row["command_id"], aggregate_version=1)
        _require(first is not None, "REVIEW_SEND_STORAGE_UNAVAILABLE")
        original = _decoded(first, "audit", encode_review_send_audit)
        _require(original["eventKind"] == "send.created"
                 and first["review_id"] == row["review_id"]
                 and first["draft_id"] == row["draft_id"] and first["decision_id"] == row["decision_id"]
                 and first["actor_membership_id"] == row["creator_membership_id"]
                 and first["occurred_at"] == row["created_at"], "REVIEW_SEND_STORAGE_UNAVAILABLE")
        # Stable original result; current mutable lifecycle is a separate read.
        fields = (*self.owner, "command_id", "review_id", "draft_id", "draft_revision", "decision_id",
                  "operation_kind", "idempotency_key", "request_payload", "request_checksum",
                  "binding_checksum", "text_checksum", "creator_membership_id", "created_at")
        return {**{key: row[key] for key in fields}, "created_audit_event_id": first["event_id"]}

    def create(self, *, command_id: UUID, review_id: UUID, idempotency_key: UUID,
               actor_membership_id: int, intent: dict, authority: dict):
        """Authority fields come from future concrete T1 guard, not an HTTP body.

        Fresh authorization precedes replay. Fresh business eligibility is required
        for NEW creation, not a substitute for returning an exact existing intent.
        """
        self._compatible_writer()
        encoded = encode_review_send(intent)
        _require((intent["organizationId"], intent["marketplaceAccountId"], intent["marketplace"])
                 == tuple(self.owner.values()), "REVIEW_SEND_STORAGE_INVALID")
        old = self.get(COMMAND, lock=True, operation_kind=intent["operationKind"], idempotency_key=idempotency_key)
        if old is not None:
            _require(old["creator_membership_id"] == actor_membership_id and old["review_id"] == review_id
                     and bytes(old["request_payload"]) == encoded.canonical_bytes and old["request_checksum"] == encoded.checksum)
            original = self.get(AUTHORITY, command_id=old["command_id"])
            _require(original is not None
                     and original["expected_external_account_id"] == self.binding.external_account_id
                     and original["expected_credential_ref"] == self.binding.credential_ref)
            _decoded(old, "request", encode_review_send)
            return self._creation_result(old)
        authority_fields = set(AUTHORITY.c.keys()) - set(self.owner) - {"command_id"}
        _require(set(authority) == authority_fields and authority["origin_membership_id"] == actor_membership_id
                 and authority["expected_external_account_id"] == self.binding.external_account_id
                 and authority["expected_credential_ref"] == self.binding.credential_ref, "REVIEW_SEND_STORAGE_INVALID")
        audit = self._audit(command_id=command_id, review_id=review_id, version=1, kind="send.created",
            before=None, after="queued", actor_kind="membership", actor_membership_id=actor_membership_id,
            draft_id=UUID(intent["draftId"]), decision_id=UUID(intent["decisionId"]))
        row = self.connection.execute(insert(COMMAND).values(**self.owner, command_id=command_id,
            review_id=review_id, draft_id=UUID(intent["draftId"]), draft_revision=intent["draftRevision"],
            decision_id=UUID(intent["decisionId"]), operation_kind=intent["operationKind"], idempotency_key=idempotency_key,
            request_payload=encoded.canonical_bytes, request_checksum=encoded.checksum,
            binding_checksum=intent["bindingChecksum"], text_checksum=intent["textChecksum"],
            creator_membership_id=actor_membership_id, created_at=audit["occurred_at"], state="queued", version=1,
            current_attempt_id=None, completed_at=None, result_evidence_id=None, reason_code=None,
            audit_event_id=audit["event_id"]).returning(COMMAND)).mappings().one()
        self.connection.execute(insert(AUTHORITY).values(**self.owner, command_id=command_id, **authority))
        self._enqueue(audit)
        return self._creation_result(row)

    def transition(self, *, command_id: UUID, expected_version: int,
                   expected_attempt_id: UUID | None, lease_token: UUID | None,
                   event_kind: str, actor_kind: str, actor_membership_id: int | None,
                   reason: str | None = None, evidence_id: UUID | None = None):
        """Physical CAS only: actor enums/tokens do NOT authorize this method."""
        self._compatible_writer()
        _require(type(expected_version) is int and expected_version > 0, "REVIEW_SEND_STORAGE_INVALID")
        states = {"send.claimed": "leased", "send.lease_renewed": "leased", "send.dispatched": "leased",
                  "send.reclaimed": "queued", "send.ambiguous": "ambiguous", "send.sent": "sent",
                  "send.conflict": "conflict", "send.blocked": "blocked", "send.cancelled": "cancelled"}
        _require(event_kind in states, "REVIEW_SEND_STORAGE_INVALID")
        current = self.get(COMMAND, lock=True, command_id=command_id)
        _require(current is not None and current["version"] == expected_version
                 and current["current_attempt_id"] == expected_attempt_id)
        audit = self._audit(command_id=command_id, review_id=current["review_id"], version=expected_version + 1,
            kind=event_kind, before=current["state"], after=states[event_kind], actor_kind=actor_kind,
            actor_membership_id=actor_membership_id, attempt_id=expected_attempt_id, lease_token=lease_token,
            reason=reason, evidence_id=evidence_id)
        if audit["attempt_id"] is not None:
            values = {"state": audit["attempt_after_state"], "lease_expires_at": audit["lease_after_expires_at"],
                      "dispatched_at": audit["attempt_dispatched_at"], "finished_at": audit["attempt_finished_at"],
                      "result_evidence_id": audit["result_evidence_id"], "reason_code": audit["reason_code"],
                      "command_version": audit["aggregate_version"], "audit_event_id": audit["event_id"]}
            if event_kind == "send.claimed":
                self.connection.execute(insert(ATTEMPT).values(**self.owner, **values,
                    attempt_id=audit["attempt_id"], command_id=command_id, review_id=current["review_id"],
                    sequence=audit["attempt_sequence"], claimed_at=audit["attempt_claimed_at"], lease_token=audit["lease_token"]))
            else:
                changed = self.connection.execute(update(ATTEMPT).where(*self._scope(ATTEMPT,
                    attempt_id=audit["attempt_id"], command_id=command_id, lease_token=audit["lease_token"],
                    command_version=audit["attempt_before_command_version"], state=audit["attempt_before_state"],
                    lease_expires_at=audit["lease_before_expires_at"])).values(**values).returning(ATTEMPT.c.attempt_id)).all()
                _require(len(changed) == 1)
        row = self.connection.execute(update(COMMAND).where(*self._scope(COMMAND,
            command_id=command_id, version=expected_version, current_attempt_id=expected_attempt_id)).values(
                state=audit["after_state"], version=audit["aggregate_version"], current_attempt_id=audit["current_attempt_id"],
                completed_at=audit["command_completed_at"], result_evidence_id=audit["result_evidence_id"],
                reason_code=None if event_kind == "send.reclaimed" else audit["reason_code"], audit_event_id=audit["event_id"],
            ).returning(COMMAND)).mappings().one_or_none()
        _require(row is not None)
        if event_kind == "send.reclaimed":
            self._enqueue(audit)
        return dict(row), dict(audit)

    def append_evidence(self, payload: dict):
        """Caller must supply authenticated evidence, not a user-asserted outcome."""
        self._compatible_writer()
        encoded = encode_review_answer_evidence(payload)
        _require((payload["organizationId"], payload["marketplaceAccountId"], payload["marketplace"])
                 == tuple(self.owner.values()), "REVIEW_SEND_STORAGE_INVALID")
        identifier = UUID(payload["evidenceId"])
        old = self.get(EVIDENCE, evidence_id=identifier)
        if old is not None:
            _require(bytes(old["evidence_payload"]) == encoded.canonical_bytes and old["evidence_checksum"] == encoded.checksum)
            return _decoded(old, "evidence", encode_review_answer_evidence)
        row = self.connection.execute(insert(EVIDENCE).values(**self.owner, evidence_id=identifier,
            review_id=UUID(payload["reviewId"]), command_id=UUID(payload["commandId"]), attempt_id=UUID(payload["attemptId"]),
            read_id=None if payload["readId"] is None else UUID(payload["readId"]),
            evidence_kind=payload["evidenceKind"], evidence_version=payload["evidenceVersion"], outcome=payload["outcome"],
            observed_at=datetime.fromisoformat(payload["observedAt"]),
            reconciliation_started_at=None if payload["reconciliationStartedAt"] is None else datetime.fromisoformat(payload["reconciliationStartedAt"]),
            provider_answer_id_utf8=None if payload["providerAnswerId"] is None else payload["providerAnswerId"].encode("utf-8"),
            answer_checksum=payload["answerChecksum"], verifier_version=payload["verifierVersion"],
            evidence_payload=encoded.canonical_bytes, evidence_checksum=encoded.checksum).returning(EVIDENCE)).mappings().one()
        return _decoded(row, "evidence", encode_review_answer_evidence)
