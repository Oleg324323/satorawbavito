"""Pure published-history chunk validation; these values grant no write authority."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.modules.orders import OrderContractValidationError, map_wb_statistics_status
from app.orders.ingestion import OrderObservation, compare_observations
from app.orders.serialization import (
    deserialize_observation,
    observation_checksum,
    serialize_observation,
)

SOURCE = "wb-statistics-supplier-orders"
ADAPTER = "wb-statistics-orders-stream-v1"
CHUNK_VERSION = "wb-history-chunk-v1"
CHUNK_SIZE = 1000


def _invalid():
    raise OrderContractValidationError("orders_history_chunk_invalid")


def _integer(value, minimum=1, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()


def _hash(value):
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        _invalid()


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            _invalid()
    except (ValueError, TypeError, AttributeError):
        _invalid()


def _instant(value):
    if type(value) is not datetime or value.utcoffset() is None:
        _invalid()
    return value


@dataclass(frozen=True, slots=True)
class HistoryPageEvidence:
    organization_id: int
    marketplace_account_id: int
    job_id: str
    page_id: str
    run_id: str
    credential_id: str
    credential_generation: int
    account_incarnation: int
    request_checksum: str
    raw_checksum: str
    input_date_from: str
    next_date_from: str
    row_count: int
    terminal: bool
    published_at: datetime
    state: Literal["published"] = "published"

    def __post_init__(self):
        for value in (self.organization_id, self.marketplace_account_id):
            _integer(value, maximum=2**31 - 1)
        for value in (self.credential_generation, self.account_incarnation):
            _integer(value)
        for value in (self.job_id, self.page_id, self.run_id, self.credential_id):
            _uuid(value)
        for value in (self.request_checksum, self.raw_checksum):
            _hash(value)
        _integer(self.row_count, 0, 100000)
        _instant(self.published_at)
        for value in (self.input_date_from, self.next_date_from):
            if (
                type(value) is not str
                or not 10 <= len(value) <= 40
                or value != value.strip()
                or "\x00" in value
            ):
                _invalid()
        if (
            self.state != "published"
            or type(self.terminal) is not bool
            or self.terminal != (self.row_count == 0)
        ):
            _invalid()
        if self.terminal and self.input_date_from != self.next_date_from:
            _invalid()


@dataclass(frozen=True, slots=True)
class HistoryRowDecision:
    ordinal: int
    observation: OrderObservation
    semantic_checksum: str
    source_row_checksum: str
    nm_id: int
    barcode: str | None
    comparison: Literal[
        "new", "replay", "changed", "out_of_order", "reconciliation_required"
    ]


@dataclass(frozen=True, slots=True)
class HistoryChunkPlan:
    page: HistoryPageEvidence
    first_ordinal: int
    next_ordinal: int
    source_run_key: str
    input_checksum: str
    rows: tuple[HistoryRowDecision, ...]
    coverage_state: Literal["partial"] = "partial"


def decode_history_chunk(
    *,
    page: HistoryPageEvidence,
    first_ordinal: int,
    rows: tuple[dict, ...],
    previous: tuple[OrderObservation, ...] = (),
) -> HistoryChunkPlan:
    """Plan only observed rows; prior values are caller evidence, never authority.

    Fixed ordinal boundaries make retries deterministic. A changed/out-of-order
    comparison is not permission to advance a canonical current projection.
    Source request/native dateFrom and EOF proof remain the T1 capture contract;
    this decoder does not reconstruct a provider stream from normalized JSON.
    """
    if type(page) is not HistoryPageEvidence:
        _invalid()
    page.__post_init__()
    _integer(first_ordinal, 0, 100000)
    if (
        type(rows) is not tuple
        or first_ordinal % CHUNK_SIZE
        or first_ordinal > page.row_count
        or (first_ordinal == page.row_count and page.row_count != 0)
        or len(rows) != min(CHUNK_SIZE, page.row_count - first_ordinal)
        or type(previous) is not tuple
        or len(previous) > CHUNK_SIZE
    ):
        _invalid()
    prior = {}
    for observation in previous:
        if type(observation) is not OrderObservation:
            _invalid()
        identity = observation.identity
        if (
            identity.organization_id,
            identity.marketplace_account_id,
            identity.marketplace,
            observation.source_kind,
        ) != (
            page.organization_id,
            page.marketplace_account_id,
            "wb",
            SOURCE,
        ) or identity in prior:
            _invalid()
        prior[identity] = observation
    decisions, seen, checksum_rows = [], set(), []
    previous_time = None
    fields = {
        "ordinal",
        "srid",
        "nm_id",
        "barcode",
        "semantic_checksum",
        "source_row_checksum",
        "observation",
        "source_revision",
        "effective_at",
    }
    for expected, row in enumerate(rows, first_ordinal):
        if (
            type(row) is not dict
            or set(row) != fields
            or type(row["ordinal"]) is not int
            or row["ordinal"] != expected
        ):
            _invalid()
        _integer(row["nm_id"])
        _hash(row["semantic_checksum"])
        _hash(row["source_row_checksum"])
        barcode = row["barcode"]
        if barcode is not None and (
            type(barcode) is not str
            or not 1 <= len(barcode) <= 255
            or barcode != barcode.strip()
            or "\x00" in barcode
            or any(0xD800 <= ord(char) <= 0xDFFF for char in barcode)
        ):
            _invalid()
        observation = deserialize_observation(row["observation"])
        if (
            len(
                json.dumps(
                    serialize_observation(observation),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
            > 16384
        ):
            _invalid()
        identity = observation.identity
        if (
            identity.organization_id,
            identity.marketplace_account_id,
            identity.marketplace,
            identity.external_order_id,
            observation.source_kind,
            observation.adapter_version,
            observation.source_revision,
            observation.effective_at,
            observation_checksum(observation),
        ) != (
            page.organization_id,
            page.marketplace_account_id,
            "wb",
            row["srid"],
            SOURCE,
            ADAPTER,
            row["source_revision"],
            row["effective_at"],
            row["semantic_checksum"],
        ):
            _invalid()
        if (
            len(observation.items) != 1
            or identity in seen
            or not 1 <= len(identity.external_order_id) <= 512
        ):
            _invalid()
        item = observation.items[0]
        if (
            item.quantity,
            item.stable_unit_id,
            item.stable_order_line_id,
            item.identity.external_item_id,
            item.identity.occurrence_index,
        ) != (1, identity.external_order_id, None, str(row["nm_id"]), 0):
            _invalid()
        if observation.status != map_wb_statistics_status(
            None, observation.wb_is_cancelled, observation.wb_cancel_evidence_present
        ):
            _invalid()
        effective = _instant(observation.effective_at)
        if previous_time is not None and effective < previous_time:
            _invalid()
        previous_time = effective
        old = prior.get(identity)
        comparison = "new" if old is None else compare_observations(old, observation)
        decisions.append(
            HistoryRowDecision(
                expected,
                observation,
                row["semantic_checksum"],
                row["source_row_checksum"],
                row["nm_id"],
                barcode,
                comparison,
            )
        )
        checksum_rows.append(
            {
                "ordinal": expected,
                "observation": serialize_observation(observation),
                "semantic_checksum": row["semantic_checksum"],
                "source_row_checksum": row["source_row_checksum"],
                "nm_id": row["nm_id"],
                "barcode": barcode,
            }
        )
        seen.add(identity)
    if not set(prior) <= seen:
        _invalid()
    header = asdict(page)
    header["published_at"] = page.published_at.astimezone(UTC).isoformat(
        timespec="microseconds"
    )
    value = {
        "version": CHUNK_VERSION,
        "page": header,
        "first_ordinal": first_ordinal,
        "rows": checksum_rows,
    }
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    stop = first_ordinal + len(rows)
    return HistoryChunkPlan(
        page,
        first_ordinal,
        stop,
        f"{CHUNK_VERSION}:{page.job_id}:{page.page_id}:{first_ordinal}:{stop}",
        hashlib.sha256(encoded).hexdigest(),
        tuple(decisions),
    )
