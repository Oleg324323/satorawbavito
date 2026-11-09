"""HTTP wire models: SQL BIGINT versions are decimal strings, not JS numbers."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.orders import ExternalOrderItemIdentity
from app.orders.contracts import AccountCoverage, CatalogResolution, DeadlineEvidence
from app.orders.ingestion import OrderObservation


class OrdersReadRowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    observation: OrderObservation
    item_identity: ExternalOrderItemIdentity
    row_version: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    resolution: CatalogResolution
    readiness_blockers: tuple[str, ...]
    deadlines: tuple[DeadlineEvidence, ...]

    @field_validator("row_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is int and 0 < value <= 2**63 - 1:
            return str(value)
        if (
            type(value) is str
            and value.isascii()
            and value.isdecimal()
            and not value.startswith("0")
            and 0 < int(value) <= 2**63 - 1
        ):
            return value
        raise ValueError("Invalid Orders row version")


class OrdersReadPageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    organization_id: int
    marketplace_account_ids: tuple[int, ...]
    snapshot_id: str
    high_water_mark: str
    published_at: datetime
    coverage_state: Literal["complete", "partial", "missing"]
    rows: tuple[OrdersReadRowResponse, ...]
    next_cursor: str | None
    account_coverage: tuple[AccountCoverage, ...]


class OrdersSavedSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selection_kind: Literal["saved_snapshot"] = "saved_snapshot"
    organization_id: int
    marketplace_account_ids: tuple[int, ...]
    snapshot_id: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    query_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    high_water_mark: str
    published_at: datetime
    coverage_state: Literal["complete", "partial", "missing"]
    account_coverage: tuple[AccountCoverage, ...]
    row_count: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]
