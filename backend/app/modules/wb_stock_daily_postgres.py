"""0078 daily/evidence SQL participant; no public permission or worker policy.

Caller owns live authorization, explanation safety and the final root commit.
No reviewer FK authenticates a caller; no current stock becomes historical fact.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.modules.wb_current_sources_postgres import CurrentSourceTransaction
from app.modules.wb_source_requests import SourceKind
from app.modules.wb_stock_evidence_codec import EvidenceDiffRow, EvidenceProposal, diff_bytes, row_bytes
from app.modules.wb_stock_snapshots import StockCount, WarehouseStockObservation


class StockDailyError(ValueError):
    def __init__(self, code="stock_daily_invalid"):
        self.code = code if code in ("stock_daily_invalid", "stock_daily_conflict", "stock_daily_ineligible") else "stock_daily_invalid"
        super().__init__(self.code)


def _positive(value):
    if type(value) is not int or value <= 0:
        raise StockDailyError()
    return value


def _member(value):
    if _positive(value) >= 2**31:
        raise StockDailyError()
    return value


def _uuid(value):
    if type(value) is not UUID or value.version != 4:
        raise StockDailyError()
    return value


def _integer(value):
    if type(value) is not Decimal or not value.is_finite() or value != value.to_integral_value():
        raise StockDailyError()
    return int(value)


def _reason(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value) is None:
        raise StockDailyError()
    return value


@dataclass(frozen=True, slots=True)
class DailyRevision:
    organization_id: int
    marketplace_account_id: int
    request_checksum: str
    business_date: date
    revision: int
    daily_revision_id: UUID
    command_id: UUID
    source_run_id: UUID
    effective_observation_at: datetime
    created_at: datetime
    actor_kind: str
    actor_membership_id: int | None
    supersedes_revision: int | None
    correction_reason: str | None
    evidence_id: UUID | None
    decision_id: UUID | None


@dataclass(frozen=True, slots=True, repr=False)
class StoredEvidence:
    proposal: EvidenceProposal
    proposed_at: datetime
    proposal_checksum: str

    def __repr__(self):
        return "<StoredEvidence redacted>"


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    evidence_id: UUID
    decision_id: UUID
    command_id: UUID
    outcome: str
    reviewer_membership_id: int
    proposal_checksum: str
    reason_code: str
    decided_at: datetime


class StockDailyTransaction:
    """Account→immutable evidence→head lock, one caller-owned top-level root.

    Methods are persistence primitives, NOT accepted actor authorization. None is
    registered on a public service. Missing read/propose/review/apply/worker policy
    must keep those public surfaces closed, not be substituted by Orders grants.
    """

    def __init__(self, session, request, business_date, *, max_evidence_bytes, max_observations):
        if request.source_kind is not SourceKind.wb_warehouse or type(business_date) is not date:
            raise StockDailyError()
        self._budget = _positive(max_evidence_bytes)
        self._max_rows = _positive(max_observations)
        self._source = CurrentSourceTransaction(session, request)
        self._request, self._day = request, business_date
        self._params = {**self._source._params, "day": business_date, "request_bytes": request.canonical_bytes}
        self._owner = self._source._owner
        self._predicate = self._owner + " AND stock_scope='wb_warehouse' AND request_checksum=:request_hash AND business_date_msk=:day"

    def _execute(self, sql, params):
        return self._source._execute(sql, params)

    def _insert(self, table, values):
        names = tuple(values)
        return self._execute("INSERT INTO " + table + "(" + ",".join(names) + ") VALUES("
                             + ",".join(":" + name for name in names) + ") RETURNING *", values).mappings().one()

    def _owned_values(self):
        return {"organization_id": self._request.organization_id,
                "marketplace_account_id": self._request.marketplace_account_id, "marketplace": "wb"}

    def _day_values(self):
        return {**self._owned_values(), "stock_scope": "wb_warehouse", "request_checksum": self._request.checksum,
                "business_date_msk": self._day}

    def _eligible(self, run_id):
        _uuid(run_id)
        ok = self._execute("SELECT wb_daily_run_eligible(:org,:account,:run,:request_bytes,:request_hash,:day)",
                            {**self._params, "run": run_id}).scalar_one()
        if ok is not True:
            raise StockDailyError("stock_daily_ineligible")
        return self._source._row(run_id)

    def _daily_view(self, row):
        return DailyRevision(row["organization_id"], row["marketplace_account_id"], row["request_checksum"],
            row["business_date_msk"], _integer(row["revision"]), row["daily_revision_id"], row["command_id"],
            row["source_run_id"], row["effective_observation_at"], row["created_at"], row["actor_kind"],
            row["actor_membership_id"], None if row["supersedes_revision"] is None else _integer(row["supersedes_revision"]),
            row["correction_reason"], row["evidence_id"], row["decision_id"])

    def _daily_command(self, command_id):
        return self._execute("SELECT * FROM wb_stock_daily_revisions WHERE " + self._owner
                             + " AND command_id=:command", {**self._params, "command": _uuid(command_id)}).mappings().one_or_none()

    def _matches(self, row, values):
        return all((bytes(row[k]) if isinstance(row[k], memoryview) else row[k]) == v for k, v in values.items())

    def current(self):
        row = self._execute("SELECT r.* FROM wb_stock_daily_heads h JOIN wb_stock_daily_revisions r "
            "ON (r.organization_id,r.marketplace_account_id,r.stock_scope,r.request_checksum,r.business_date_msk,r.revision)="
            "(h.organization_id,h.marketplace_account_id,h.stock_scope,h.request_checksum,h.business_date_msk,h.current_revision) "
            "WHERE h.organization_id=:org AND h.marketplace_account_id=:account AND h.stock_scope='wb_warehouse' "
            "AND h.request_checksum=:request_hash AND h.business_date_msk=:day", self._params).mappings().one_or_none()
        return None if row is None else self._daily_view(row)

    def history(self, *, limit, before_revision=None):
        _positive(limit)
        params, predicate = {**self._params, "limit": limit}, self._predicate
        if before_revision is not None:
            predicate += " AND revision<:before"
            params["before"] = Decimal(_positive(before_revision))
        rows = self._execute("SELECT * FROM wb_stock_daily_revisions WHERE " + predicate
                            + " ORDER BY revision DESC LIMIT :limit", params).mappings().all()
        return tuple(self._daily_view(row) for row in rows)

    def create_initial(self, *, source_run_id, command_id):
        _uuid(source_run_id)
        intent = {**self._day_values(), "command_id": _uuid(command_id), "source_run_id": source_run_id,
                  "revision": Decimal(1), "actor_kind": "worker", "actor_membership_id": None,
                  "supersedes_revision": None, "evidence_id": None, "decision_id": None,
                  "proposal_checksum": None, "correction_reason": None}
        replay = self._daily_command(command_id)
        if replay is not None:
            if not self._matches(replay, intent):
                raise StockDailyError("stock_daily_conflict")
            return self._daily_view(replay)
        run = self._eligible(source_run_id)
        row = self._insert("wb_stock_daily_revisions", {**intent, "daily_revision_id": uuid4(),
            "source_kind": self._request.source_kind.value, "parser_version": self._request.parser_version,
            "request_bytes": self._request.canonical_bytes, "basis": "received", "effective_observation_at": run["received_at"]})
        self._insert("wb_stock_daily_heads", {**self._day_values(), "current_revision": Decimal(1), "version": Decimal(1)})
        return self._daily_view(row)

    def _count_rows(self, run_id):
        rows = self._execute("SELECT * FROM wb_stock_observations WHERE " + self._owner
            + " AND run_id=:run LIMIT :limit", {**self._params, "run": run_id, "limit": self._max_rows + 1}).mappings().all()
        if len(rows) > self._max_rows:
            raise StockDailyError()
        result = {}
        for row in rows:
            item = WarehouseStockObservation(row["nm_id"], row["chrt_id"], row["warehouse_id"],
                StockCount(row["quantity_presence"], row["quantity"]),
                StockCount(row["in_way_to_client_presence"], row["in_way_to_client"]),
                StockCount(row["in_way_from_client_presence"], row["in_way_from_client"]))
            if item.identity in result:
                raise StockDailyError()
            result[item.identity] = row_bytes(item, max_bytes=self._budget)
        return result

    def _diff(self, before_run, after_run):
        before, after = self._count_rows(before_run), self._count_rows(after_run)
        rows = []
        for key in before.keys() | after.keys():
            b, a = before.get(key), after.get(key)
            if b == a:
                continue
            nm, chrt, warehouse = key
            rows.append(EvidenceDiffRow(nm, chrt, warehouse, "added" if b is None else "removed" if a is None else "changed",
                None if b is None else hashlib.sha256(b).hexdigest(), None if a is None else hashlib.sha256(a).hexdigest()))
        return tuple(sorted(rows, key=lambda row: row.identity))

    def _proposal(self, evidence_id):
        row = self._execute("SELECT * FROM wb_stock_revision_evidence WHERE " + self._predicate
            + " AND evidence_id=:evidence", {**self._params, "evidence": _uuid(evidence_id)}).mappings().one_or_none()
        if row is None:
            raise StockDailyError()
        return row

    def get_evidence(self, evidence_id):
        row = self._proposal(evidence_id)
        children = self._execute("SELECT * FROM wb_stock_revision_evidence_diffs WHERE " + self._owner
            + " AND evidence_id=:evidence LIMIT :limit", {**self._params, "evidence": evidence_id,
                                                         "limit": self._max_rows * 2 + 1}).mappings().all()
        if len(children) > self._max_rows * 2:
            raise StockDailyError()
        diffs = tuple(sorted((EvidenceDiffRow(r["nm_id"], r["chrt_id"], r["warehouse_id"], r["change_kind"],
                        r["before_payload_checksum"], r["after_payload_checksum"]) for r in children), key=lambda r: r.identity))
        proposal = EvidenceProposal(self._request.organization_id, self._request.marketplace_account_id,
            str(evidence_id), str(row["before_run_id"]), row["before_manifest_checksum"], str(row["after_run_id"]),
            row["after_manifest_checksum"], self._request.checksum, self._day, _integer(row["before_daily_revision"]),
            row["proposed_by_membership_id"], str(row["proposal_command_id"]), bytes(row["evidence_document_bytes"]),
            row["reviewed_evidence_reference"], diffs)
        if (proposal.canonical_bytes(max_bytes=self._budget) != bytes(row["proposal_bytes"])
                or proposal.checksum(max_bytes=self._budget) != row["proposal_checksum"]):
            raise StockDailyError()
        return StoredEvidence(proposal, row["proposed_at"], row["proposal_checksum"])

    def propose(self, *, command_id, before_daily_revision, after_run_id, proposer_membership_id,
                evidence_document_bytes, reviewed_evidence_reference):
        _uuid(command_id)
        _uuid(after_run_id)
        _positive(before_daily_revision)
        _member(proposer_membership_id)
        if type(evidence_document_bytes) is not bytes or len(evidence_document_bytes) > self._budget:
            raise StockDailyError()
        replay = self._execute("SELECT * FROM wb_stock_revision_evidence WHERE " + self._owner
            + " AND proposal_command_id=:command", {**self._params, "command": command_id}).mappings().one_or_none()
        if replay is not None:
            if not self._matches(replay, {**self._day_values(), "before_daily_revision": Decimal(before_daily_revision),
                    "after_run_id": after_run_id, "proposed_by_membership_id": proposer_membership_id,
                    "evidence_document_bytes": evidence_document_bytes, "reviewed_evidence_reference": reviewed_evidence_reference}):
                raise StockDailyError("stock_daily_conflict")
            return self.get_evidence(replay["evidence_id"])
        parent = self._execute("SELECT * FROM wb_stock_daily_revisions WHERE " + self._predicate
            + " AND revision=:revision", {**self._params, "revision": Decimal(before_daily_revision)}).mappings().one_or_none()
        if parent is None:
            raise StockDailyError()
        before = self._eligible(parent["source_run_id"])
        after = self._eligible(after_run_id)
        diffs = self._diff(before["run_id"], after_run_id)
        evidence_id = uuid4()
        proposal = EvidenceProposal(self._request.organization_id, self._request.marketplace_account_id, str(evidence_id),
            str(before["run_id"]), before["manifest_checksum"], str(after_run_id), after["manifest_checksum"],
            self._request.checksum, self._day, before_daily_revision, proposer_membership_id, str(command_id),
            evidence_document_bytes, reviewed_evidence_reference, diffs)
        raw = proposal.canonical_bytes(max_bytes=self._budget)
        values = {**self._day_values(), "evidence_id": evidence_id, "source_kind": self._request.source_kind.value,
            "parser_version": self._request.parser_version, "grain_version": "nmId/chrtId?/warehouseId",
            "request_bytes": self._request.canonical_bytes, "before_run_id": before["run_id"], "after_run_id": after_run_id,
            "before_manifest_checksum": before["manifest_checksum"], "after_manifest_checksum": after["manifest_checksum"],
            "before_daily_revision": Decimal(before_daily_revision), "proposed_by_membership_id": proposer_membership_id,
            "proposal_command_id": command_id, "proposal_bytes": raw, "proposal_checksum": hashlib.sha256(raw).hexdigest(),
            "evidence_document_bytes": evidence_document_bytes, "evidence_document_checksum": hashlib.sha256(evidence_document_bytes).hexdigest(),
            "reviewed_evidence_reference": reviewed_evidence_reference,
            "diff_checksum": hashlib.sha256(diff_bytes(diffs, max_bytes=self._budget)).hexdigest(),
            **{kind + "_count": Decimal(sum(r.change_kind == kind for r in diffs)) for kind in ("added", "removed", "changed")}}
        stored = self._insert("wb_stock_revision_evidence", values)
        for row in diffs:
            self._insert("wb_stock_revision_evidence_diffs", {**self._owned_values(), "evidence_id": evidence_id,
                "diff_row_id": uuid4(), "nm_id": row.nm_id, "chrt_id": row.chrt_id, "warehouse_id": row.warehouse_id,
                "change_kind": row.change_kind, "before_payload_checksum": row.before_checksum, "after_payload_checksum": row.after_checksum})
        return StoredEvidence(proposal, stored["proposed_at"], stored["proposal_checksum"])

    def _decision_view(self, row):
        return EvidenceDecision(row["evidence_id"], row["decision_id"], row["decision_command_id"], row["outcome"],
            row["reviewed_by_membership_id"], row["reviewed_proposal_checksum"], row["reason_code"], row["decided_at"])

    def get_decision(self, evidence_id):
        self._proposal(evidence_id)  # Full day/request scope, not UUID alone.
        row = self._execute("SELECT * FROM wb_stock_revision_evidence_decisions WHERE " + self._owner
            + " AND evidence_id=:evidence", {**self._params, "evidence": evidence_id}).mappings().one_or_none()
        return None if row is None else self._decision_view(row)

    def decide(self, *, evidence_id, command_id, outcome, reviewer_membership_id, proposal_checksum, reason_code):
        _uuid(evidence_id)
        _uuid(command_id)
        _member(reviewer_membership_id)
        _reason(reason_code)
        if type(outcome) is not str or outcome not in ("accepted", "rejected"):
            raise StockDailyError()
        values = {**self._owned_values(), "evidence_id": evidence_id, "decision_command_id": command_id,
            "outcome": outcome, "reviewed_by_membership_id": reviewer_membership_id,
            "reviewed_proposal_checksum": proposal_checksum, "reason_code": reason_code}
        replay = self._execute("SELECT * FROM wb_stock_revision_evidence_decisions WHERE " + self._owner
            + " AND decision_command_id=:command", {**self._params, "command": command_id}).mappings().one_or_none()
        proposal = self._proposal(evidence_id)
        if replay is not None:
            if not self._matches(replay, values):
                raise StockDailyError("stock_daily_conflict")
            return self._decision_view(replay)
        if proposal["proposal_checksum"] != proposal_checksum:
            raise StockDailyError("stock_daily_conflict")
        existing = self._execute("SELECT decision_id FROM wb_stock_revision_evidence_decisions WHERE " + self._owner
            + " AND evidence_id=:evidence", {**self._params, "evidence": evidence_id}).scalar_one_or_none()
        if existing is not None:
            raise StockDailyError("stock_daily_conflict")
        return self._decision_view(self._insert("wb_stock_revision_evidence_decisions", {**values, "decision_id": uuid4()}))

    def apply(self, *, evidence_id, decision_id, command_id, actor_membership_id, reason_code):
        _uuid(evidence_id)
        _uuid(decision_id)
        _uuid(command_id)
        _member(actor_membership_id)
        _reason(reason_code)
        values = {**self._day_values(), "command_id": command_id, "evidence_id": evidence_id,
            "decision_id": decision_id, "actor_kind": "membership", "actor_membership_id": actor_membership_id,
            "correction_reason": reason_code}
        replay = self._daily_command(command_id)
        if replay is not None:
            if not self._matches(replay, values):
                raise StockDailyError("stock_daily_conflict")
            return self._daily_view(replay)
        proposal = self._proposal(evidence_id)
        decision = self._execute("SELECT * FROM wb_stock_revision_evidence_decisions WHERE " + self._owner
            + " AND evidence_id=:evidence AND decision_id=:decision", {**self._params, "evidence": evidence_id,
                                                                      "decision": decision_id}).mappings().one_or_none()
        if (decision is None or decision["outcome"] != "accepted"
                or decision["reviewed_proposal_checksum"] != proposal["proposal_checksum"]):
            raise StockDailyError("stock_daily_conflict")
        self._eligible(proposal["before_run_id"])
        after = self._eligible(proposal["after_run_id"])
        head = self._execute("SELECT current_revision,version FROM wb_stock_daily_heads WHERE "
            + self._predicate + " FOR UPDATE", self._params).mappings().one_or_none()
        parent = _integer(proposal["before_daily_revision"])
        if head is None or head["version"] != Decimal(parent) or head["current_revision"] != Decimal(parent):
            raise StockDailyError("stock_daily_conflict")
        row = self._insert("wb_stock_daily_revisions", {**values, "daily_revision_id": uuid4(), "revision": Decimal(parent + 1),
            "supersedes_revision": Decimal(parent), "source_kind": self._request.source_kind.value,
            "parser_version": self._request.parser_version, "source_run_id": proposal["after_run_id"],
            "request_bytes": self._request.canonical_bytes, "basis": "received", "effective_observation_at": after["received_at"],
            "proposal_checksum": proposal["proposal_checksum"]})
        updated = self._execute("UPDATE wb_stock_daily_heads SET current_revision=:next,version=:next WHERE "
            + self._predicate + " AND current_revision=:parent AND version=:parent RETURNING version",
            {**self._params, "parent": Decimal(parent), "next": Decimal(parent + 1)}).scalar_one_or_none()
        if updated != Decimal(parent + 1):
            raise StockDailyError("stock_daily_conflict")
        return self._daily_view(row)
