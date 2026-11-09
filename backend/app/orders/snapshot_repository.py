"""Sealed storage snapshots. Authentication and signed cursors belong to the service."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import ExternalOrderIdentity, OrderContractValidationError
from app.orders.contracts import AccountCoverage, OrderReadPage, OrderReadRow
from app.orders.ingestion import _integer
from app.orders.query import OrdersReadFilters
from app.orders.serialization import (
    deserialize_observation,
    deserialize_read_row,
    observation_checksum,
    serialize_read_row,
)


@dataclass(frozen=True, slots=True)
class SnapshotChunk:
    snapshot_id: int
    high_water_mark: str
    published_at: datetime
    rows: tuple[OrderReadRow, ...]
    next_position: int | None
    account_coverage: tuple[AccountCoverage, ...]
    coverage_state: str


class OrdersSnapshotRepository:
    def __init__(self, session: Session, organization_id: int):
        _integer(organization_id)
        self.session, self.org = session, organization_id

    def _prepare(self, checksum):
        if (
            not isinstance(checksum, str)
            or re.fullmatch("[0-9a-f]{64}", checksum) is None
        ):
            raise OrderContractValidationError("Invalid query checksum")
        if not self.session.in_transaction():
            raise OrderContractValidationError("Caller transaction required")
        if self.session.execute(
            text("SELECT current_setting('app.organization_id',true)")
        ).scalar_one() != str(self.org):
            raise OrderContractValidationError("Transaction tenant context differs")

    def freeze(
        self,
        rows: tuple[OrderReadRow, ...],
        coverage: tuple[AccountCoverage, ...],
        high_water_mark: str,
        query_checksum: str,
        *,
        parent_versions: dict[ExternalOrderIdentity, int],
    ) -> int:
        self._prepare(query_checksum)
        if type(coverage) is not tuple or any(
            type(value) is not AccountCoverage for value in coverage
        ):
            raise OrderContractValidationError("Immutable account coverage required")
        accounts = tuple(sorted(value.marketplace_account_id for value in coverage))
        if not accounts:
            raise OrderContractValidationError(
                "Empty scope must not persist a snapshot"
            )
        states = {value.state for value in coverage}
        aggregate = (
            "complete"
            if states == {"complete"}
            else "missing"
            if states <= {"missing"}
            else "partial"
        )
        published = self.session.execute(text("SELECT clock_timestamp()")).scalar_one()
        OrderReadPage(
            self.org,
            accounts,
            "pending",
            high_water_mark,
            published,
            aggregate,
            rows,
            None,
            coverage,
        )
        if type(parent_versions) is not dict or set(parent_versions) != {
            row.observation.identity for row in rows
        }:
            raise OrderContractValidationError("Snapshot parent version scope differs")
        for version in parent_versions.values():
            _integer(version)
        bound = []
        # Lock the concrete projections in a deterministic identity order before freezing.
        ordered = sorted(
            rows,
            key=lambda row: (
                row.observation.identity.marketplace_account_id,
                row.observation.identity.external_order_id,
                row.item_identity.source_line_key,
            ),
        )
        for row in ordered:
            identity = row.observation.identity
            params = {
                "org": self.org,
                "account": identity.marketplace_account_id,
                "external": identity.external_order_id,
                "line": row.item_identity.source_line_key,
                "checksum": observation_checksum(row.observation),
                "source": row.observation.source_kind,
                "adapter": row.observation.adapter_version,
            }
            record = (
                self.session.execute(
                    text("""SELECT i.order_id,i.order_item_id,i.version,i.quantity,
                o.version AS parent_version,o.raw_status,o.canonical_status,o.mapping_state,o.mapping_version,
                i.external_item_id,i.occurrence_index,i.resolution_state,i.resolution_version,
                i.marketplace_product_id,i.marketplace_offer_id,i.catalog_sku_id,
                e.observation_id,e.normalized_evidence
                FROM marketplace_order_items i JOIN marketplace_orders o
                  ON (o.organization_id,o.marketplace_account_id,o.order_id)=
                     (i.organization_id,i.marketplace_account_id,i.order_id)
                JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.order_id)=
                     (i.organization_id,i.marketplace_account_id,i.order_id)
                WHERE i.organization_id=:org AND i.marketplace_account_id=:account
                  AND o.external_order_id=:external AND i.source_line_key=:line
                  AND e.source_kind=:source AND e.adapter_version=:adapter
                  AND e.payload_checksum=:checksum AND e.order_item_id IS NULL
                FOR SHARE OF i,o"""),
                    params,
                )
                .mappings()
                .one_or_none()
            )
            item = next(
                item
                for item in row.observation.items
                if item.identity == row.item_identity
            )
            resolution = row.resolution
            if record is None or (
                record["version"],
                record["quantity"],
                record["external_item_id"],
                record["occurrence_index"],
                record["resolution_state"],
                record["resolution_version"],
                record["marketplace_product_id"],
                record["marketplace_offer_id"],
                record["catalog_sku_id"],
            ) != (
                row.row_version,
                item.quantity,
                row.item_identity.external_item_id,
                row.item_identity.occurrence_index,
                resolution.state,
                resolution.evidence_version,
                resolution.marketplace_product_id,
                resolution.marketplace_offer_id,
                resolution.catalog_sku_id,
            ):
                raise OrderContractValidationError(
                    "Snapshot projection binding changed"
                )
            if (
                deserialize_observation(record["normalized_evidence"])
                != row.observation
            ):
                raise OrderContractValidationError(
                    "Snapshot observation binding changed"
                )
            status = row.observation.status
            if (
                record["parent_version"],
                record["raw_status"],
                record["canonical_status"],
                record["mapping_state"],
                record["mapping_version"],
            ) != (
                parent_versions[identity],
                status.raw_status,
                status.canonical_status,
                status.mapping_state,
                status.mapping_version,
            ):
                raise OrderContractValidationError("Snapshot parent projection changed")
            bound.append((row, record))
        encoded_coverage = json.dumps(
            [asdict(value) for value in coverage],
            default=lambda value: value.isoformat(),
        )
        snapshot = self.session.execute(
            text("""INSERT INTO order_read_snapshots
            (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,
             coverage_state,row_count,published_at)
            VALUES (:org,:hwm,:accounts,:query,CAST(:coverage AS jsonb),:state,:count,:published)
            RETURNING snapshot_id"""),
            {
                "org": self.org,
                "hwm": high_water_mark,
                "accounts": list(accounts),
                "query": query_checksum,
                "coverage": encoded_coverage,
                "state": aggregate,
                "count": len(bound),
                "published": published,
            },
        ).scalar_one()
        for position, (row, record) in enumerate(bound, 1):
            self.session.execute(
                text("""INSERT INTO order_read_snapshot_rows
                (organization_id,marketplace_account_id,snapshot_id,order_id,order_item_id,
                 observation_id,position,row_version,payload_schema_version,row_payload)
                VALUES (:org,:account,:snapshot,:order,:item,:observation,:position,:version,1,CAST(:payload AS jsonb))"""),
                {
                    "org": self.org,
                    "account": row.observation.identity.marketplace_account_id,
                    "snapshot": snapshot,
                    "order": record["order_id"],
                    "item": record["order_item_id"],
                    "observation": record["observation_id"],
                    "position": position,
                    "version": row.row_version,
                    "payload": json.dumps(serialize_read_row(row)),
                },
            )
        return snapshot

    def read(
        self,
        snapshot_id: int,
        accounts: tuple[int, ...],
        query_checksum: str,
        *,
        after_position: int = 0,
        limit: int = 100,
        filters: OrdersReadFilters | None = None,
    ) -> SnapshotChunk:
        self._prepare(query_checksum)
        for value in (snapshot_id, limit):
            _integer(value)
        _integer(after_position, 0)
        if (
            type(accounts) is not tuple
            or not accounts
            or len(set(accounts)) != len(accounts)
        ):
            raise OrderContractValidationError("Invalid snapshot account scope")
        for account in accounts:
            _integer(account)
        header = (
            self.session.execute(
                text("""SELECT * FROM order_read_snapshots
            WHERE organization_id=:org AND snapshot_id=:snapshot
              AND (expires_at IS NULL OR expires_at>clock_timestamp())"""),
                {"org": self.org, "snapshot": snapshot_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            header is None
            or tuple(header["account_scope"]) != tuple(sorted(accounts))
            or header["query_checksum"] != query_checksum
        ):
            raise OrderContractValidationError(
                "Snapshot scope missing, changed or expired"
            )
        if after_position > header["row_count"]:
            raise OrderContractValidationError("Snapshot position outside bounds")
        filter_sql, filter_parameters = (filters or OrdersReadFilters()).sql()
        records = (
            self.session.execute(
                text(f"""SELECT position,row_version,marketplace_account_id,row_payload
            FROM order_read_snapshot_rows WHERE organization_id=:org AND snapshot_id=:snapshot
            AND position>:position {filter_sql} ORDER BY position LIMIT :limit"""),
                {
                    "org": self.org,
                    "snapshot": snapshot_id,
                    "position": after_position,
                    "limit": limit + 1,
                    **filter_parameters,
                },
            )
            .mappings()
            .all()
        )
        rows = []
        for record in records[:limit]:
            row = deserialize_read_row(record["row_payload"])
            if (
                row.observation.identity.organization_id != self.org
                or row.observation.identity.marketplace_account_id
                != record["marketplace_account_id"]
                or row.row_version != record["row_version"]
            ):
                raise OrderContractValidationError(
                    "Stored snapshot row binding invalid"
                )
            rows.append(row)
        next_position = records[limit - 1]["position"] if len(records) > limit else None
        coverage = []
        if type(header["account_coverage"]) is not list:
            raise OrderContractValidationError("Stored coverage array required")
        for value in header["account_coverage"]:
            if type(value) is not dict or set(value) != set(
                AccountCoverage.__dataclass_fields__
            ):
                raise OrderContractValidationError("Stored coverage fields invalid")
            data = dict(value)
            try:
                for field in ("requested_from", "requested_to"):
                    if data[field] is not None:
                        if type(data[field]) is not str:
                            raise ValueError("invalid timestamp")
                        data[field] = datetime.fromisoformat(data[field])
                coverage.append(AccountCoverage(**data))
            except (ValueError, TypeError) as exc:
                raise OrderContractValidationError("Stored coverage invalid") from exc
        # Reuse aggregate/source checks without treating a numeric position as a signed cursor.
        OrderReadPage(
            self.org,
            tuple(sorted(accounts)),
            str(snapshot_id),
            header["high_water_mark"],
            header["published_at"],
            header["coverage_state"],
            tuple(rows),
            None,
            tuple(coverage),
        )
        return SnapshotChunk(
            snapshot_id,
            header["high_water_mark"],
            header["published_at"],
            tuple(rows),
            next_position,
            tuple(coverage),
            header["coverage_state"],
        )
