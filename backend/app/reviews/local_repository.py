"""0071 local transaction participant. Caller owns live guard and physical commit.

No provider, credential resolution, generation, fallback, DDL or internal commit.
All table names are fixed mappings; all row access is explicitly account scoped.
"""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import bindparam, insert, select, update

from app.reviews.canonical_orm import CanonicalReviewFactRow
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.local_command_payloads import (
    encode_review_local_request,
    encode_review_local_result,
)
from app.reviews.local_tables import (
    AUDIT,
    DECISION,
    DRAFT,
    POLICY,
    POLICY_HEAD,
    RECEIPT,
    WORKFLOW_HEAD,
)
from app.reviews.run_binding_storage import (
    decode_review_run_binding,
    encode_review_run_binding,
)
from app.reviews.storage_payloads import (
    EncodedStoragePayload,
    _finish,
    encode_review_generation,
    encode_review_local_audit,
    encode_review_policy,
)

FACT = CanonicalReviewFactRow.__table__


class ReviewLocalError(ValueError):
    def __init__(self, code="REVIEW_LOCAL_CONFLICT"):
        self.code = code
        super().__init__(code)


def _require(value, code="REVIEW_LOCAL_CONFLICT"):
    if not value:
        raise ReviewLocalError(code)


def _uuid(value):
    try:
        return None if value is None else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ReviewLocalError("REVIEW_LOCAL_INVALID") from None


def _time(value):
    return datetime.fromisoformat(value)


def _encoded(row, prefix, encoder):
    raw = bytes(row[prefix + "_payload"])
    value = json.loads(raw)
    result = encoder(value)
    _require(result.canonical_bytes == raw and result.checksum == row[prefix + "_checksum"],
             "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
    return value


class ReviewLocalRepository:
    def __init__(self, connection, binding: ReviewBindingDescriptor):
        self.connection, self.binding = connection, binding
        self.owner = {"organization_id": binding.organization_id,
                      "marketplace_account_id": binding.marketplace_account_id,
                      "marketplace": binding.marketplace}

    def _scope(self, table, **keys):
        # Comparisons otherwise infer BIGINT from a Python int, even for an
        # unbounded NUMERIC column; psycopg then rejects valid >BIGINT versions.
        return [table.c[key] == bindparam(None, value, type_=table.c[key].type)
                for key, value in dict(self.owner, **keys).items()]

    def get(self, table, *, lock=False, **keys):
        query = select(table).where(*self._scope(table, **keys))
        if lock:
            query = query.with_for_update(read=table is FACT)
        return self.connection.execute(query).mappings().one_or_none()

    def _insert(self, table, row):
        self.connection.execute(insert(table).values(**self.owner, **row))

    def _head(self, table, old, values, **keys):
        if old is None:
            self._insert(table, dict(values, **keys))
            return
        rows = self.connection.execute(update(table).where(
            *self._scope(table, **keys), table.c.head_id == old["head_id"],
            table.c.version == old["version"],
        ).values(**values).returning(table.c.version)).all()
        _require(len(rows) == 1)

    def replay(self, request, encoded):
        old = self.get(RECEIPT, local_command_id=_uuid(request["localCommandId"]))
        if old is None:
            return None
        _require(old["actor_membership_id"] == request["actorMembershipId"]
                 and old["operation_kind"] == request["operationKind"]
                 and bytes(old["request_payload"]) == encoded.canonical_bytes
                 and old["request_checksum"] == encoded.checksum
                 and decode_review_run_binding(old) == self.binding)
        result = _encoded(old, "result", encode_review_local_result)
        _require(result["localCommandId"] == request["localCommandId"]
                 and result["operationKind"] == request["operationKind"],
                 "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
        return EncodedStoragePayload(bytes(old["result_payload"]), old["result_checksum"])

    def source(self, data):
        fact = self.get(FACT, lock=True, review_id=_uuid(data["reviewId"]))
        _require(fact is not None, "REVIEW_LOCAL_NOT_FOUND")
        # Full source content and historical account binding checked by0068 reader.
        b = self.binding
        reader = ReviewFactsRepository(self.connection, ReviewOwner(
            b.organization_id, b.marketplace_account_id, b.marketplace,
            b.external_account_id, b.credential_ref), command_savepoints=False)
        current = reader.get_fact(data["externalReviewId"])
        _require(current is not None and current.review_id == fact["review_id"])
        _require(current.source_order_state == "current", "REVIEW_LOCAL_SOURCE_CHANGED")
        return current

    def policy_epoch(self, data):
        head = self.get(POLICY_HEAD, lock=True)
        _require(head is not None and head["head_id"] == _uuid(data["expectedPolicyHeadId"])
                 and head["version"] == data["expectedPolicyHeadVersion"], "REVIEW_LOCAL_POLICY_CHANGED")
        policy = self.get(POLICY, policy_id=head["current_policy_id"], version=head["current_policy_version"])
        _require(policy is not None, "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
        _encoded(policy, "policy", encode_review_policy)
        _require(policy["policy_checksum"] == head["current_policy_checksum"], "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
        epoch = self.get(AUDIT, aggregate_kind="policy_head", aggregate_id=head["head_id"],
                         aggregate_version=head["version"])
        _require(epoch is not None and epoch["event_kind"] == "policy.selected"
                 and epoch["policy_id"] == policy["policy_id"] and epoch["policy_version"] == policy["version"],
                 "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
        _encoded(epoch, "audit", encode_review_local_audit)
        return head, policy, epoch

    def execute(self, request: dict, *, completed_at: datetime):
        encoded = encode_review_local_request(request)
        # Defense in depth: the service must already have checked owner and actor.
        _require((request["organizationId"], request["marketplaceAccountId"], request["marketplace"]) ==
                 (self.binding.organization_id, self.binding.marketplace_account_id, self.binding.marketplace),
                 "REVIEW_LOCAL_DENIED")
        old = self.replay(request, encoded)
        if old is not None:
            return old
        data, op, actor = request["input"], request["operationKind"], request["actorMembershipId"]
        event, key = uuid4(), _uuid(request["localCommandId"])
        at = completed_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        result = {"schemaVersion": "review-local-command-result-v1", "localCommandId": str(key),
                      "operationKind": op, "auditEventId": str(event), "completedAt": at,
                      "policyId": None, "policyVersion": None, "draftId": None, "draftRevision": None,
                      "decisionId": None, "headId": None, "headVersion": None}
        audit = {"event_id": event, "actor_membership_id": actor, "local_command_id": key, "occurred_at": completed_at,
                     "review_id": None, "policy_id": None, "policy_version": None, "draft_id": None,
                     "draft_revision": None, "decision_id": None, "before_state": None, "after_state": None}
        if op == "review.policy.create.v1":
            p = data["policy"]
            payload = encode_review_policy(p)
            self._insert(POLICY, {"policy_id": _uuid(p["policyId"]), "version": p["version"],
                "approval_mode": p["approvalMode"], "template_version": p["templateVersion"], "model_version": p["modelVersion"],
                "policy_payload": payload.canonical_bytes, "policy_checksum": payload.checksum,
                "actor_membership_id": actor, "created_at": completed_at, "audit_event_id": event})
            result.update(policyId=p["policyId"], policyVersion=p["version"])
            audit.update(aggregate_kind="policy_version", aggregate_id=_uuid(p["policyId"]),
                         aggregate_version=p["version"], event_kind="policy.created",
                         policy_id=_uuid(p["policyId"]), policy_version=p["version"])
        elif op == "review.policy.select.v1":
            h = self.get(POLICY_HEAD, lock=True)
            _require((0 if h is None else h["version"]) == data["expectedHeadVersion"])
            p = self.get(POLICY, policy_id=_uuid(data["policyId"]), version=data["policyVersion"])
            _require(p is not None and p["policy_checksum"] == data["policyChecksum"])
            _encoded(p, "policy", encode_review_policy)
            hid, version = (uuid4() if h is None else h["head_id"]), data["expectedHeadVersion"] + 1
            self._head(POLICY_HEAD, h, {"head_id": hid, "version": version, "current_policy_id": p["policy_id"],
                "current_policy_version": p["version"], "current_policy_checksum": p["policy_checksum"], "updated_at": completed_at})
            result.update(policyId=str(p["policy_id"]), policyVersion=int(p["version"]), headId=str(hid), headVersion=version)
            audit.update(aggregate_kind="policy_head", aggregate_id=hid, aggregate_version=version,
                         event_kind="policy.selected", policy_id=p["policy_id"], policy_version=p["version"],
                         before_state=None if h is None else "policy_selected", after_state="policy_selected")
        else:
            current = self.source(data)
            ph, policy, epoch = self.policy_epoch(data)
            h = self.get(WORKFLOW_HEAD, lock=True, review_id=current.review_id)
            _require((0 if h is None else h["version"]) == data["expectedHeadVersion"])
            hid, version = (uuid4() if h is None else h["head_id"]), data["expectedHeadVersion"] + 1
            before = None if h is None else "draft_current" if h["current_decision_id"] is None else "decision_current"
            if op == "review.draft.publish.v1":
                g = data["generation"]
                _require((0 if h is None else h["current_draft_revision"]) == data["expectedDraftRevision"])
                _require(_uuid(g["sourceObservationId"]) == current.current_observation_id
                         and g["sourceChecksum"] == current.content_checksum, "REVIEW_LOCAL_SOURCE_CHANGED")
                _require((_uuid(g["policyId"]), g["policyVersion"], g["policyChecksum"], g["templateVersion"], g["modelVersion"]) ==
                         (policy["policy_id"], policy["version"], policy["policy_checksum"], policy["template_version"], policy["model_version"]),
                         "REVIEW_LOCAL_POLICY_CHANGED")
                if g["mode"] == "manual_edit":
                    _require(h is not None and h["current_draft_id"] == _uuid(g["previousDraftId"]))
                elif g["mode"] == "manual":
                    _require(h is None and data["expectedDraftRevision"] == 0)
                _require(self.get(DRAFT, generation_id=_uuid(g["generationId"])) is None)
                revision = data["expectedDraftRevision"] + 1
                raw_text = data["text"].encode("utf-8")
                text_checksum = hashlib.sha256(raw_text).hexdigest()
                binding = _finish({"contract": "review-decision-v1",
                    "owner": [request["organizationId"], request["marketplaceAccountId"], request["marketplace"], data["externalReviewId"]],
                    "draft": [data["draftId"], revision, text_checksum],
                    "source": [g["sourceObservationId"], g["sourceChecksum"]],
                    "policy": [g["policyId"], g["policyVersion"], g["policyChecksum"], g["templateVersion"], g["modelVersion"]]})
                generation = encode_review_generation(g)
                self._insert(DRAFT, {"review_id": current.review_id, "draft_id": _uuid(data["draftId"]), "revision": revision,
                    "source_observation_id": current.current_observation_id, "source_checksum": current.content_checksum,
                    "policy_id": policy["policy_id"], "policy_version": policy["version"], "policy_checksum": policy["policy_checksum"],
                    "text_utf8": raw_text, "text_checksum": text_checksum, "binding_payload": binding.canonical_bytes,
                    "binding_checksum": binding.checksum, "generation_id": _uuid(g["generationId"]), "generation_mode": g["mode"],
                    "template_version": g["templateVersion"], "model_version": g["modelVersion"], "generation_started_at": _time(g["startedAt"]),
                    "generation_completed_at": _time(g["completedAt"]), "previous_draft_id": _uuid(g.get("previousDraftId")),
                    "generation_payload": generation.canonical_bytes, "generation_checksum": generation.checksum,
                    "policy_head_id": ph["head_id"], "policy_head_version": ph["version"], "policy_selection_event_id": epoch["event_id"],
                    "actor_membership_id": actor, "created_at": completed_at, "audit_event_id": event})
                result.update(policyId=str(policy["policy_id"]), policyVersion=int(policy["version"]),
                              draftId=data["draftId"], draftRevision=revision)
                audit.update(event_kind="draft.published", policy_id=policy["policy_id"], policy_version=policy["version"],
                             draft_id=_uuid(data["draftId"]), draft_revision=revision, after_state="draft_current")
            else:
                _require(h is not None and h["current_draft_id"] == _uuid(data["draftId"])
                         and h["current_draft_revision"] == data["draftRevision"])
                draft = self.get(DRAFT, review_id=current.review_id, draft_id=_uuid(data["draftId"]))
                _require(draft is not None and draft["binding_checksum"] == data["bindingChecksum"])
                _require(draft["source_observation_id"] == current.current_observation_id == _uuid(data["sourceObservationId"])
                         and draft["source_checksum"] == current.content_checksum, "REVIEW_LOCAL_SOURCE_CHANGED")
                _require((draft["policy_head_id"], draft["policy_head_version"], draft["policy_selection_event_id"]) ==
                         (ph["head_id"], ph["version"], epoch["event_id"]), "REVIEW_LOCAL_POLICY_CHANGED")
                if data["decisionKind"] == "approved":
                    _require(not current.answered and current.can_answer is True, "REVIEW_LOCAL_NOT_ANSWERABLE")
                did = uuid4()
                self._insert(DECISION, {"review_id": current.review_id, "decision_id": did, "draft_id": draft["draft_id"],
                    "draft_revision": draft["revision"], "binding_checksum": draft["binding_checksum"], "decision_kind": data["decisionKind"],
                    "actor_membership_id": actor, "decided_at": completed_at, "audit_event_id": event})
                result.update(draftId=str(draft["draft_id"]), draftRevision=int(draft["revision"]), decisionId=str(did))
                audit.update(event_kind="decision." + data["decisionKind"], draft_id=draft["draft_id"],
                             draft_revision=draft["revision"], decision_id=did, after_state="decision_current")
            self._head(WORKFLOW_HEAD, h, {"head_id": hid, "version": version, "current_draft_id": _uuid(result["draftId"]),
                "current_draft_revision": result["draftRevision"], "current_decision_id": _uuid(result["decisionId"]),
                "updated_at": completed_at}, review_id=current.review_id)
            result.update(headId=str(hid), headVersion=version)
            audit.update(aggregate_kind="workflow_head", aggregate_id=hid, aggregate_version=version,
                         review_id=current.review_id, before_state=before)
        envelope = {"schemaVersion": "review-audit-v1", "organizationId": request["organizationId"],
            "marketplaceAccountId": request["marketplaceAccountId"], "marketplace": request["marketplace"],
            "eventId": str(event), "aggregateId": str(audit["aggregate_id"]), "aggregateVersion": int(audit["aggregate_version"]),
            "eventKind": audit["event_kind"], "occurredAt": at, "actorKind": "membership", "actorMembershipId": actor,
            "commandId": None, "attemptId": None, "reasonCode": None, "policyId": None if audit["policy_id"] is None else str(audit["policy_id"]),
            "draftId": None if audit["draft_id"] is None else str(audit["draft_id"]),
            "decisionId": None if audit["decision_id"] is None else str(audit["decision_id"]),
            "beforeState": audit["before_state"], "afterState": audit["after_state"]}
        encoded_audit = encode_review_local_audit(envelope)
        self._insert(AUDIT, dict(audit, audit_payload=encoded_audit.canonical_bytes, audit_checksum=encoded_audit.checksum))
        encoded_result = encode_review_local_result(result)
        receipt = dict(local_command_id=key, actor_membership_id=actor, operation_kind=op,
            request_payload=encoded.canonical_bytes, request_checksum=encoded.checksum,
            **encode_review_run_binding(self.binding), result_payload=encoded_result.canonical_bytes,
            result_checksum=encoded_result.checksum, completed_at=completed_at, audit_event_id=event,
            review_id=_uuid(data.get("reviewId")), expected_head_version=data.get("expectedHeadVersion"),
            expected_draft_revision=data.get("expectedDraftRevision"), expected_policy_head_id=_uuid(data.get("expectedPolicyHeadId")),
            expected_policy_head_version=data.get("expectedPolicyHeadVersion"))
        for source, target in (("policyId", "policy_id"), ("policyVersion", "policy_version"),
                               ("draftId", "draft_id"), ("draftRevision", "draft_revision"),
                               ("decisionId", "decision_id"), ("headId", "head_id"), ("headVersion", "head_version")):
            receipt["result_" + target] = _uuid(result[source]) if source.endswith("Id") else result[source]
        self._insert(RECEIPT, receipt)
        return encoded_result
