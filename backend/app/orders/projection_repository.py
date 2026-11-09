"""Projection primitives; service must first approve source progression and authority."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.contracts import CatalogResolution
from app.orders.ingestion import _integer, _text
from app.orders.serialization import deserialize_observation, observation_checksum


class OrdersProjectionRepository:
    def __init__(
        self, session: Session, organization_id: int, marketplace_account_id: int
    ):
        _integer(organization_id)
        _integer(marketplace_account_id)
        self.session, self.org, self.account = (
            session,
            organization_id,
            marketplace_account_id,
        )

    def _load_member(self, run_id: int, observation_id: int):
        for value in (run_id, observation_id):
            _integer(value)
        if not self.session.in_transaction():
            raise OrderContractValidationError("Caller transaction required")
        if self.session.execute(
            text("SELECT current_setting('app.organization_id',true)")
        ).scalar_one() != str(self.org):
            raise OrderContractValidationError("Transaction tenant context differs")
        params = {
            "org": self.org,
            "account": self.account,
            "run": run_id,
            "observation": observation_id,
        }
        if (
            self.session.execute(text("SHOW transaction_isolation")).scalar_one()
            != "read committed"
        ):
            raise OrderContractValidationError("READ COMMITTED transaction required")
        if (
            self.session.execute(
                text("""SELECT marketplace_account_id FROM marketplace_accounts
            WHERE organization_id=:org AND marketplace_account_id=:account FOR UPDATE"""),
                params,
            ).scalar_one_or_none()
            is None
        ):
            raise OrderContractValidationError("Account scope binding missing")
        run = (
            self.session.execute(
                text("""SELECT state,source_kind,adapter_version,mapping_version
            FROM order_sync_runs WHERE organization_id=:org AND marketplace_account_id=:account
            AND sync_run_id=:run FOR UPDATE"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise OrderContractValidationError("Run membership scope missing")
        if run["state"] != "staging":
            raise OrderContractValidationError("Cannot project terminal run")
        record = (
            self.session.execute(
                text("""SELECT e.order_id,e.normalized_evidence,e.payload_checksum,
            o.external_order_id,o.marketplace FROM order_sync_memberships m
            JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.observation_id)=
                (m.organization_id,m.marketplace_account_id,m.observation_id)
            JOIN marketplace_orders o ON (o.organization_id,o.marketplace_account_id,o.order_id)=
                (e.organization_id,e.marketplace_account_id,e.order_id)
            WHERE m.organization_id=:org AND m.marketplace_account_id=:account AND m.sync_run_id=:run
              AND m.observation_id=:observation AND m.order_item_id IS NULL AND e.order_item_id IS NULL"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            raise OrderContractValidationError("Observation membership missing")
        row = deserialize_observation(record["normalized_evidence"])
        if (
            row.identity.organization_id,
            row.identity.marketplace_account_id,
            row.identity.external_order_id,
            row.identity.marketplace,
            row.source_kind,
            row.adapter_version,
            row.status.mapping_version,
            observation_checksum(row),
        ) != (
            self.org,
            self.account,
            record["external_order_id"],
            record["marketplace"],
            run["source_kind"],
            run["adapter_version"],
            run["mapping_version"],
            record["payload_checksum"],
        ):
            raise OrderContractValidationError(
                "Stored projection source binding changed"
            )
        params["order"] = record["order_id"]
        return row, params

    def set_parent(
        self, run_id: int, observation_id: int, *, expected_version: int
    ) -> int:
        _integer(expected_version)
        row, params = self._load_member(run_id, observation_id)
        params.update(
            expected=expected_version,
            raw=row.status.raw_status,
            canonical=row.status.canonical_status,
            mapping=row.status.mapping_state,
            mapping_version=row.status.mapping_version,
            effective=row.effective_at,
        )
        version = self.session.execute(
            text("""UPDATE marketplace_orders SET raw_status=:raw,
            canonical_status=:canonical,mapping_state=:mapping,mapping_version=:mapping_version,
            source_updated_at=:effective,last_seen_sync_run_id=:run,version=version+1,updated_at=clock_timestamp()
            WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
            AND version=:expected RETURNING version"""),
            params,
        ).scalar_one_or_none()
        if version is None:
            raise OrderContractValidationError("Parent version conflict")
        return version

    def set_item(
        self,
        run_id: int,
        observation_id: int,
        source_line_key: str,
        *,
        resolution: CatalogResolution,
        expected_version: int | None,
    ) -> tuple[int, int]:
        """None requires absence; a version requires CAS. Replay is decided by service."""
        _text(source_line_key)
        if not isinstance(resolution, CatalogResolution):
            raise OrderContractValidationError("Catalog resolution required")
        if expected_version is not None:
            _integer(expected_version)
        row, params = self._load_member(run_id, observation_id)
        item = next(
            (
                item
                for item in row.items
                if item.identity.source_line_key == source_line_key
            ),
            None,
        )
        if item is None:
            raise OrderContractValidationError(
                "Source line missing from stored evidence"
            )
        params.update(
            line=source_line_key,
            external=item.identity.external_item_id,
            occurrence=item.identity.occurrence_index,
            quantity=item.quantity,
            state=resolution.state,
            product=resolution.marketplace_product_id,
            offer=resolution.marketplace_offer_id,
            sku=resolution.catalog_sku_id,
            resolution=resolution.evidence_version,
            effective=row.effective_at,
            expected=expected_version,
        )
        current = (
            self.session.execute(
                text("""SELECT order_item_id,external_item_id,
            occurrence_index,version FROM marketplace_order_items
            WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
            AND source_line_key COLLATE "C"=:line FOR UPDATE"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            if expected_version is not None:
                raise OrderContractValidationError("Item version conflict")
            result = self.session.execute(
                text("""INSERT INTO marketplace_order_items
                (organization_id,marketplace_account_id,order_id,source_line_key,external_item_id,
                 occurrence_index,quantity,resolution_state,resolution_version,
                 marketplace_product_id,marketplace_offer_id,catalog_sku_id,source_updated_at)
                VALUES (:org,:account,:order,:line,:external,:occurrence,:quantity,:state,
                        :resolution,:product,:offer,:sku,:effective)
                RETURNING order_item_id,version"""),
                params,
            ).one()
        else:
            if current["version"] != expected_version:
                raise OrderContractValidationError("Item version conflict")
            if (current["external_item_id"], current["occurrence_index"]) != (
                item.identity.external_item_id,
                item.identity.occurrence_index,
            ):
                raise OrderContractValidationError("Item identity changed")
            params["item"] = current["order_item_id"]
            result = self.session.execute(
                text("""UPDATE marketplace_order_items SET
                quantity=:quantity,resolution_state=:state,resolution_version=:resolution,
                marketplace_product_id=:product,marketplace_offer_id=:offer,catalog_sku_id=:sku,
                source_updated_at=:effective,version=version+1,updated_at=clock_timestamp()
                WHERE organization_id=:org AND marketplace_account_id=:account
                AND order_id=:order AND order_item_id=:item AND version=:expected
                RETURNING order_item_id,version"""),
                params,
            ).one_or_none()
            if result is None:
                raise OrderContractValidationError("Item version conflict")
        return result[0], result[1]
