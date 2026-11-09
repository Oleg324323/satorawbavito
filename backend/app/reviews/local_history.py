"""Bounded immutable workflow audit history under the existing live read guard.

The upper version is captured once, not a mutable latest-head pagination cursor.
It is a filter, not a capability: every page rechecks account/member/binding.
"""

from sqlalchemy import and_, bindparam, select

from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.local_repository import FACT, _encoded, _require, _uuid
from app.reviews.local_service import _unit
from app.reviews.local_tables import AUDIT, RECEIPT, WORKFLOW_HEAD
from app.reviews.run_binding_storage import decode_review_run_binding
from app.reviews.storage_payloads import encode_review_local_audit


def read_local_review_history(engine, *, actor, settings, marketplace_account_id: int,
                              marketplace: str, review_id: str, limit: int = 50,
                              head_id: str | None = None, through_version: int | None = None,
                              after_version: int = 0):
    _require(type(limit) is int and 1 <= limit <= 200, "REVIEW_LOCAL_INVALID")
    _require(type(after_version) is int and after_version >= 0, "REVIEW_LOCAL_INVALID")
    _require((head_id is None) == (through_version is None), "REVIEW_LOCAL_INVALID")
    _require(through_version is None or type(through_version) is int and through_version > 0,
             "REVIEW_LOCAL_INVALID")
    _require(after_version == 0 or through_version is not None, "REVIEW_LOCAL_INVALID")
    rid = _uuid(review_id)
    _require(rid is not None and rid.int != 0 and str(rid) == review_id, "REVIEW_LOCAL_INVALID")
    if head_id is not None:
        hid = _uuid(head_id)
        _require(hid is not None and hid.int != 0 and str(hid) == head_id, "REVIEW_LOCAL_INVALID")
    with _unit(engine, actor=actor, settings=settings, account_id=marketplace_account_id,
               marketplace=marketplace, permission="reviews:read") as (repository, _member):
        fact = repository.get(FACT, lock=True, review_id=rid)
        _require(fact is not None, "REVIEW_LOCAL_NOT_FOUND")
        binding = repository.binding
        facts = ReviewFactsRepository(repository.connection, ReviewOwner(
            binding.organization_id, binding.marketplace_account_id, binding.marketplace,
            binding.external_account_id, binding.credential_ref), command_savepoints=False)
        # Metadata-only0068 identity fence also applies to empty/terminal pages.
        # Do not load source text just to inspect an audit trail.
        facts._require_identity_binding(fact)
        head = repository.get(WORKFLOW_HEAD, lock=True, review_id=rid)
        result = {"schemaVersion": "review-local-history-v1", "organizationId": actor.organization_id,
                  "marketplaceAccountId": marketplace_account_id, "marketplace": marketplace,
                  "reviewId": str(rid), "headId": None, "throughVersion": 0,
                  "events": [], "nextAfterVersion": None}
        if head is None:
            _require(head_id is None and after_version == 0)
        else:
            bound = int(head["version"]) if through_version is None else through_version
            _require((head_id is None or _uuid(head_id) == head["head_id"])
                     and after_version <= bound <= head["version"])
            fields = ("schema_version", "external_account_id", "credential_ref", "payload", "checksum")
            query = select(AUDIT, *(RECEIPT.c["account_binding_" + field] for field in fields)).join(
                RECEIPT, and_(
                    *(AUDIT.c[key] == RECEIPT.c[key] for key in repository.owner),
                    AUDIT.c.local_command_id == RECEIPT.c.local_command_id,
                    AUDIT.c.event_id == RECEIPT.c.audit_event_id,
                )).where(
                *repository._scope(AUDIT, aggregate_kind="workflow_head", aggregate_id=head["head_id"], review_id=rid),
                *repository._scope(RECEIPT),
                AUDIT.c.aggregate_version > bindparam("after", after_version, type_=AUDIT.c.aggregate_version.type),
                AUDIT.c.aggregate_version <= bindparam("through", bound, type_=AUDIT.c.aggregate_version.type),
            ).order_by(AUDIT.c.aggregate_version).limit(limit + 1)
            rows = repository.connection.execute(query).mappings().all()
            # A committed head witnesses every local workflow version. Missing
            # joined receipts/history must not masquerade as an empty final page.
            _require(len(rows) == min(limit + 1, bound - after_version), "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
            for index, row in enumerate(rows):
                _require(row["aggregate_version"] == after_version + index + 1, "REVIEW_LOCAL_STORAGE_UNAVAILABLE")
                _require(decode_review_run_binding(row) == repository.binding)
            events = [_encoded(row, "audit", encode_review_local_audit) for row in rows[:limit]]
            result.update(headId=str(head["head_id"]), throughVersion=bound, events=events,
                          nextAfterVersion=after_version + limit if len(rows) > limit else None)
    return result
