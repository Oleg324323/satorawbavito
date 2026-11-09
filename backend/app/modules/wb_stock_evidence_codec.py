"""Proposed v1 stock evidence bytes only, not evidence review or publication.

Caller must prove complete scoped runs, exact full diff, daily eligibility and
sanitized explanation provenance. This codec cannot authenticate a reference,
detect secrets, establish permissions, or authorize a historical correction.
The explicit byte budget is a caller resource limit, not a retention policy.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date

from app.modules.wb_stock_snapshots import WarehouseStockObservation


class StockEvidenceEncodingError(ValueError):
    def __init__(self):
        super().__init__("invalid_stock_evidence_encoding")


def _integer(value, maximum=None):
    if (
        type(value) is not int
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        raise StockEvidenceEncodingError()


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise StockEvidenceEncodingError()


def _uuid(value):
    if (
        type(value) is not str
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise StockEvidenceEncodingError()


def _identity(nm_id, chrt_id, warehouse_id):
    _integer(nm_id, 2**63 - 1)
    _integer(warehouse_id, 2**63 - 1)
    if chrt_id is not None:
        _integer(chrt_id, 2**63 - 1)
    return (
        "nmId",
        str(nm_id),
        "chrtId",
        "null" if chrt_id is None else str(chrt_id),
        "warehouseId",
        str(warehouse_id),
    )


def _encode(value, budget):
    _integer(budget)
    try:
        raw = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    except (ValueError, TypeError, OverflowError):
        raise StockEvidenceEncodingError() from None
    if len(raw) > budget:
        raise StockEvidenceEncodingError()
    return raw


def source_identity(row: WarehouseStockObservation) -> tuple[str, ...]:
    if type(row) is not WarehouseStockObservation:
        raise StockEvidenceEncodingError()
    return _identity(row.nm_id, row.chrt_id, row.warehouse_id)


def row_bytes(row: WarehouseStockObservation, *, max_bytes: int) -> bytes:
    source_identity(row)
    return _encode(
        [
            "wb-stock-evidence-row/v1",
            *(
                [count.presence, count.value]
                for count in (
                    row.quantity,
                    row.in_way_to_client,
                    row.in_way_from_client,
                )
            ),
        ],
        max_bytes,
    )


@dataclass(frozen=True, slots=True)
class EvidenceDiffRow:
    nm_id: int
    chrt_id: int | None
    warehouse_id: int
    change_kind: str
    before_checksum: str | None
    after_checksum: str | None

    def __post_init__(self):
        _identity(self.nm_id, self.chrt_id, self.warehouse_id)
        if type(self.change_kind) is not str or self.change_kind not in (
            "added",
            "removed",
            "changed",
        ):
            raise StockEvidenceEncodingError()
        for value in (self.before_checksum, self.after_checksum):
            if value is not None:
                _hash(value)
        if (
            (
                self.change_kind == "added"
                and (self.before_checksum is not None or self.after_checksum is None)
            )
            or (
                self.change_kind == "removed"
                and (self.before_checksum is None or self.after_checksum is not None)
            )
            or (
                self.change_kind == "changed"
                and (
                    self.before_checksum is None
                    or self.after_checksum is None
                    or self.before_checksum == self.after_checksum
                )
            )
        ):
            raise StockEvidenceEncodingError()

    @property
    def identity(self):
        return _identity(self.nm_id, self.chrt_id, self.warehouse_id)


def _diffs(rows):
    if type(rows) is not tuple or not all(type(row) is EvidenceDiffRow for row in rows):
        raise StockEvidenceEncodingError()
    if len({row.identity for row in rows}) != len(rows):
        raise StockEvidenceEncodingError()


def diff_bytes(rows: tuple[EvidenceDiffRow, ...], *, max_bytes: int) -> bytes:
    _integer(max_bytes)
    # Every encoded entry requires more than one byte. Bound before allocating.
    if type(rows) is not tuple or len(rows) > max_bytes:
        raise StockEvidenceEncodingError()
    _diffs(rows)
    return _encode(
        [
            "wb-stock-evidence-diff/v1",
            [
                [row.identity, row.change_kind, row.before_checksum, row.after_checksum]
                for row in sorted(rows, key=lambda row: row.identity)
            ],
        ],
        max_bytes,
    )


@dataclass(frozen=True, slots=True)
class EvidenceProposal:
    organization_id: int
    marketplace_account_id: int
    evidence_id: str
    before_run_id: str
    before_manifest_checksum: str
    after_run_id: str
    after_manifest_checksum: str
    request_checksum: str
    business_date: date
    before_daily_revision: int
    proposer_membership_id: int
    proposal_command_id: str
    evidence_document_bytes: bytes
    reviewed_evidence_reference: str
    diffs: tuple[EvidenceDiffRow, ...]

    def __post_init__(self):
        for value in (
            self.organization_id,
            self.marketplace_account_id,
            self.proposer_membership_id,
        ):
            _integer(value, 2**31 - 1)
        _integer(self.before_daily_revision)
        for value in (
            self.evidence_id,
            self.before_run_id,
            self.after_run_id,
            self.proposal_command_id,
        ):
            _uuid(value)
        if (
            self.before_run_id == self.after_run_id
            or type(self.business_date) is not date
        ):
            raise StockEvidenceEncodingError()
        for value in (
            self.before_manifest_checksum,
            self.after_manifest_checksum,
            self.request_checksum,
        ):
            _hash(value)
        if type(self.evidence_document_bytes) is not bytes:
            raise StockEvidenceEncodingError()
        reference = self.reviewed_evidence_reference
        if (
            type(reference) is not str
            or not reference
            or reference.strip() != reference
            or "\x00" in reference
        ):
            raise StockEvidenceEncodingError()
        try:
            reference.encode("utf-8")
            document = self.evidence_document_bytes.decode("utf-8")
        except UnicodeError:
            raise StockEvidenceEncodingError() from None
        if not document.strip():
            raise StockEvidenceEncodingError()
        _diffs(self.diffs)
        if not self.diffs:
            raise StockEvidenceEncodingError()

    def canonical_bytes(self, *, max_bytes: int) -> bytes:
        _integer(max_bytes)
        if (
            len(self.evidence_document_bytes) > max_bytes
            or len(self.reviewed_evidence_reference) > max_bytes
        ):
            raise StockEvidenceEncodingError()
        diff_hash = hashlib.sha256(
            diff_bytes(self.diffs, max_bytes=max_bytes)
        ).hexdigest()
        return _encode(
            [
                "wb-stock-evidence-proposal/v1",
                self.organization_id,
                self.marketplace_account_id,
                self.evidence_id,
                self.before_run_id,
                self.before_manifest_checksum,
                self.after_run_id,
                self.after_manifest_checksum,
                "wb_warehouse_v1",
                "wb-warehouse-stocks/v1",
                "nmId/chrtId?/warehouseId",
                self.request_checksum,
                self.business_date.isoformat(),
                self.before_daily_revision,
                self.proposer_membership_id,
                self.proposal_command_id,
                hashlib.sha256(self.evidence_document_bytes).hexdigest(),
                self.reviewed_evidence_reference,
                diff_hash,
                *(
                    sum(row.change_kind == kind for row in self.diffs)
                    for kind in ("added", "removed", "changed")
                ),
            ],
            max_bytes,
        )

    def checksum(self, *, max_bytes: int) -> str:
        return hashlib.sha256(self.canonical_bytes(max_bytes=max_bytes)).hexdigest()
