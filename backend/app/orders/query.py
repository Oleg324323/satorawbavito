"""Allowlisted filters over a retained Orders view, not source completeness."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.orders import CanonicalOrderStatus, MappingState, Marketplace


class OrdersReadFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marketplace: Marketplace | None = None
    canonical_status: CanonicalOrderStatus | None = None
    mapping_state: MappingState | None = None
    resolution_state: (
        Literal["resolved", "unmapped", "ambiguous", "stale", "manual_override"] | None
    ) = None
    external_order_id: str | None = Field(default=None, min_length=1, max_length=4096)
    raw_status: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("external_order_id", "raw_status")
    @classmethod
    def exact_text(cls, value):
        if value is not None and (
            value != value.strip()
            or "\x00" in value
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        ):
            raise ValueError("Exact PostgreSQL-compatible filter text required")
        return value

    def cursor_checksum(self, snapshot_query_checksum):
        values = self.model_dump(mode="json", exclude_none=True)
        if not values:
            return snapshot_query_checksum  # Preserve existing unfiltered cursor v1.
        payload = json.dumps(
            {"snapshotQuery": snapshot_query_checksum, "filters": values},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(
            b"orders-read-filters-v1\x00" + payload.encode("ascii")
        ).hexdigest()

    def sql(self):
        paths = {
            "marketplace": "row,observation,identity,marketplace",
            "external_order_id": "row,observation,identity,external_order_id",
            "canonical_status": "row,observation,status,canonical_status",
            "mapping_state": "row,observation,status,mapping_state",
            "raw_status": "row,observation,status,raw_status",
            "resolution_state": "row,resolution,state",
        }
        values = self.model_dump(mode="json", exclude_none=True)
        predicates, parameters = [], {}
        for key, value in values.items():
            parameter = "filter_" + key
            predicates.append(f"row_payload #>> '{{{paths[key]}}}' = :{parameter}")
            parameters[parameter] = value
        return (" AND " + " AND ".join(predicates) if predicates else ""), parameters
