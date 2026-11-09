"""Transaction-participating evidence storage; no authorization, fetch or publication."""

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.ingestion import OrderObservation, _integer, compare_observations
from app.orders.serialization import (
    deserialize_observation,
    observation_checksum,
    serialize_observation,
)


@dataclass(frozen=True, slots=True)
class StoredOrderEvidence:
    order_id: int
    observation_id: int
    observation: OrderObservation
    replayed: bool


class OrdersEvidenceRepository:
    def __init__(
        self, session: Session, organization_id: int, marketplace_account_id: int
    ):
        _integer(organization_id)
        _integer(marketplace_account_id)
        self.session = session
        self.org = organization_id
        self.account = marketplace_account_id

    def append(self, sync_run_id: int, row: OrderObservation) -> StoredOrderEvidence:
        """Caller holds fresh auth/account locks and must roll back on any error."""
        _integer(sync_run_id)
        if not isinstance(row, OrderObservation) or (
            row.identity.organization_id != self.org
            or row.identity.marketplace_account_id != self.account
        ):
            raise OrderContractValidationError("Observation scope binding differs")
        if not self.session.in_transaction():
            raise OrderContractValidationError("Caller transaction required")
        context = self.session.execute(
            text("SELECT current_setting('app.organization_id',true)")
        ).scalar_one()
        if context != str(self.org):
            raise OrderContractValidationError("Transaction tenant context differs")
        isolation = self.session.execute(
            text("SHOW transaction_isolation")
        ).scalar_one()
        if isolation != "read committed":
            raise OrderContractValidationError("READ COMMITTED transaction required")
        params = {"org": self.org, "account": self.account, "run": sync_run_id}
        account = self.session.execute(
            text("""SELECT marketplace_account_id FROM marketplace_accounts
            WHERE organization_id=:org AND marketplace_account_id=:account FOR UPDATE"""),
            params,
        ).scalar_one_or_none()
        if account is None:
            raise OrderContractValidationError("Account scope binding missing")
        run = (
            self.session.execute(
                text("""SELECT marketplace,source_kind,adapter_version,
            mapping_version,state FROM order_sync_runs
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND sync_run_id=:run FOR UPDATE"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise OrderContractValidationError("Run scope binding missing")
        if run["state"] != "staging":
            raise OrderContractValidationError("Cannot append to terminal run")
        if (
            run["marketplace"],
            run["source_kind"],
            run["adapter_version"],
            run["mapping_version"],
        ) != (
            row.identity.marketplace,
            row.source_kind,
            row.adapter_version,
            row.status.mapping_version,
        ):
            raise OrderContractValidationError("Run source binding differs")
        checksum = observation_checksum(row)
        params.update(
            external=row.identity.external_order_id,
            marketplace=row.identity.marketplace,
        )
        # Fresh exact lookups after the account lock also support unbounded TEXT keys.
        order_id = self.session.execute(
            text("""SELECT order_id FROM marketplace_orders
            WHERE organization_id=:org AND marketplace_account_id=:account
            AND marketplace=:marketplace AND external_order_id COLLATE "C"=:external"""),
            params,
        ).scalar_one_or_none()
        if order_id is None:
            order_id = self.session.execute(
                text("""INSERT INTO marketplace_orders
                (organization_id,marketplace_account_id,marketplace,external_order_id)
                VALUES (:org,:account,:marketplace,:external) RETURNING order_id"""),
                params,
            ).scalar_one()
        params.update(
            order=order_id,
            checksum=checksum,
            source=row.source_kind,
            adapter=row.adapter_version,
        )
        existing_member = self.session.execute(
            text("""SELECT o.payload_checksum
            FROM order_sync_memberships m JOIN order_observations o
            ON (o.organization_id,o.marketplace_account_id,o.observation_id)=
               (m.organization_id,m.marketplace_account_id,m.observation_id)
            WHERE m.organization_id=:org AND m.marketplace_account_id=:account
              AND m.sync_run_id=:run AND m.order_id=:order AND m.order_item_id IS NULL"""),
            params,
        ).scalar_one_or_none()
        if existing_member is not None and existing_member != checksum:
            raise OrderContractValidationError("Run membership evidence changed")
        params.update(
            revision=row.source_revision,
            effective=row.effective_at,
            observed=row.observed_at,
            evidence=json.dumps(serialize_observation(row)),
        )
        stored = (
            self.session.execute(
                text("""SELECT observation_id,normalized_evidence
            FROM order_observations WHERE organization_id=:org AND marketplace_account_id=:account
            AND order_id=:order AND source_kind COLLATE "C"=:source
            AND adapter_version COLLATE "C"=:adapter
            AND payload_checksum=:checksum AND order_item_id IS NULL"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        replayed = stored is not None
        if stored is None:
            stored = (
                self.session.execute(
                    text("""INSERT INTO order_observations
                (organization_id,marketplace_account_id,sync_run_id,order_id,source_kind,
                 adapter_version,source_revision,payload_checksum,evidence_schema_version,
                 normalized_evidence,source_effective_at,observed_at)
                VALUES (:org,:account,:run,:order,:source,:adapter,:revision,:checksum,1,
                        CAST(:evidence AS jsonb),:effective,:observed)
                RETURNING observation_id,normalized_evidence"""),
                    params,
                )
                .mappings()
                .one()
            )
        decoded = deserialize_observation(stored["normalized_evidence"])
        if (
            observation_checksum(decoded) != checksum
            or compare_observations(decoded, row) != "replay"
        ):
            raise OrderContractValidationError(
                "Stored semantic replay integrity failure"
            )
        params["observation"] = stored["observation_id"]
        self.session.execute(
            text("""INSERT INTO order_sync_memberships
            (organization_id,marketplace_account_id,sync_run_id,order_id,observation_id,
             coverage_role,observed_at)
            VALUES (:org,:account,:run,:order,:observation,'observed',:observed)
            ON CONFLICT (organization_id,marketplace_account_id,sync_run_id,order_id)
            WHERE order_item_id IS NULL DO NOTHING"""),
            params,
        )
        return StoredOrderEvidence(
            order_id, stored["observation_id"], decoded, replayed
        )
