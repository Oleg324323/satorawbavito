"""Dormant P1 commands over accepted Orders provenance and physical witnesses."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.cabinet.permissions import (
    PRODUCTION_ASSIGN_PERMISSIONS,
    PRODUCTION_CREATE_PERMISSIONS,
    PRODUCTION_READ_PERMISSIONS,
)
from app.infra.db import set_marketplace_account_context
from app.modules.orders import OrderContractValidationError
from app.modules.production import (
    AssignmentCommand,
    AssignmentResult,
    OrderCommandConflict,
    assignment_command_checksum,
    deserialize_assignment_result,
    serialize_assignment_command,
    serialize_assignment_result,
)
from app.orders.bindings import validate_run_binding
from app.orders.serialization import deserialize_observation, observation_checksum
from app.platform.integrations.publication_guard import (
    PublicationGuardError,
    acquire_publication_guard,
)


class ProductionServiceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProductionWorkItem:
    work_item_id: int
    organization_id: int
    marketplace_account_id: int
    order_id: int
    order_item_id: int
    source_item_version: int
    required_quantity: int
    planned_quantity: int
    remaining_quantity: int
    catalog_sku_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime
    current_assignment_receipt_id: int | None


@dataclass(frozen=True, slots=True)
class ProductionCreation:
    work_item_id: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ProductionAssignment:
    result: AssignmentResult
    replayed: bool


def _identifier(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ProductionServiceError("PRODUCTION_REQUEST_INVALID")


def _execute(session, principal, account, permissions, operation):
    if not isinstance(session, Session) or session.in_transaction():
        raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")
    error = None
    try:
        with session.begin():
            guard = acquire_publication_guard(
                session,
                principal=principal,
                accounts=(account,),
                authorities=(),
                required_permissions=permissions,
            )
            set_marketplace_account_context(
                session,
                organization_id=principal.organization_id,
                marketplace_account_id=account.marketplace_account_id,
            )
            result = operation(
                session,
                guard,
                {
                    "org": principal.organization_id,
                    "account": account.marketplace_account_id,
                },
            )
            guard.revalidate_before_write()
        return result
    except PublicationGuardError as exc:
        error = (
            "PRODUCTION_STORAGE_UNAVAILABLE"
            if exc.code == "publication_persistence_failed"
            else "PRODUCTION_CONTEXT_INVALID"
            if exc.code == "publication_context_invalid"
            else "PRODUCTION_DENIED"
        )
    except ProductionServiceError as exc:
        error = exc.code
    except OrderCommandConflict as exc:
        error = exc.code
    except OrderContractValidationError:
        error = "PRODUCTION_SOURCE_INVALID"
    except SQLAlchemyError:
        error = "PRODUCTION_STORAGE_UNAVAILABLE"
    # Raise outside the handler so SQL, command text and nested causes are not retained.
    raise ProductionServiceError(error)


def _source(session, scope, account, order_item_id, expected_version=None):
    item = (
        session.execute(
            text("""SELECT * FROM marketplace_order_items
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND order_item_id=:item FOR SHARE"""),
            dict(scope, item=order_item_id),
        )
        .mappings()
        .one_or_none()
    )
    if item is None:
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    order = (
        session.execute(
            text("""SELECT * FROM marketplace_orders
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND order_id=:order FOR SHARE"""),
            dict(scope, order=item["order_id"]),
        )
        .mappings()
        .one_or_none()
    )
    if order is None or order["last_seen_sync_run_id"] is None:
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    params = dict(scope, order=order["order_id"], run=order["last_seen_sync_run_id"])
    run = (
        session.execute(
            text("""SELECT * FROM order_sync_runs
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND sync_run_id=:run FOR SHARE"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if run is None or (run["state"], run["manifest_state"], run["marketplace"]) != (
        "complete",
        "complete",
        account.provider,
    ):
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    validate_run_binding(
        scope["org"],
        account,
        schema_version=run["account_binding_schema_version"],
        external_account_id=run["account_binding_external_account_id"],
        credential_ref=run["account_binding_credential_ref"],
        payload=run["account_binding_payload"],
        checksum=run["account_binding_checksum"],
    )
    evidence = (
        session.execute(
            text("""SELECT e.normalized_evidence,e.payload_checksum
        FROM order_sync_memberships m JOIN order_observations e ON
        (m.organization_id,m.marketplace_account_id,m.order_id,m.observation_id)=
        (e.organization_id,e.marketplace_account_id,e.order_id,e.observation_id)
        WHERE m.organization_id=:org AND m.marketplace_account_id=:account
        AND m.order_id=:order AND m.sync_run_id=:run
        AND m.order_item_id IS NULL AND e.order_item_id IS NULL"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if evidence is None:
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    observation = deserialize_observation(evidence["normalized_evidence"])
    identity, status = observation.identity, observation.status
    if (
        identity.organization_id,
        identity.marketplace_account_id,
        identity.marketplace,
        identity.external_order_id,
        observation.source_kind,
        observation.adapter_version,
        observation_checksum(observation),
        status.raw_status,
        status.canonical_status,
        status.mapping_state,
        status.mapping_version,
    ) != (
        scope["org"],
        scope["account"],
        account.provider,
        order["external_order_id"],
        run["source_kind"],
        run["adapter_version"],
        evidence["payload_checksum"],
        order["raw_status"],
        order["canonical_status"],
        order["mapping_state"],
        order["mapping_version"],
    ):
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    targets = [
        entry
        for entry in observation.items
        if entry.identity.source_line_key == item["source_line_key"]
    ]
    if len(targets) != 1 or (
        targets[0].identity.external_item_id,
        targets[0].identity.occurrence_index,
        targets[0].quantity,
    ) != (item["external_item_id"], item["occurrence_index"], item["quantity"]):
        raise ProductionServiceError("PRODUCTION_SOURCE_INVALID")
    if expected_version is not None and item["version"] != expected_version:
        raise ProductionServiceError("PRODUCTION_SOURCE_CHANGED")
    return item


def _work(session, scope, work_item_id):
    row = (
        session.execute(
            text("""SELECT * FROM production_work_items
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND work_item_id=:work FOR UPDATE"""),
            dict(scope, work=work_item_id),
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ProductionServiceError("PRODUCTION_NOT_FOUND")
    return row


def _coherent_source(session, scope, account, row):
    source = _source(
        session, scope, account, row["order_item_id"], row["source_item_version"]
    )
    if (source["order_id"], source["quantity"]) != (
        row["order_id"],
        row["required_quantity"],
    ):
        raise ProductionServiceError("PRODUCTION_SOURCE_CHANGED")


def _audit(session, guard, principal, scope, work_item_id, action):
    guard.revalidate_before_write()
    session.add(
        LkAuditEventRow(
            organization_id=scope["org"],
            actor_user_id=principal.user_id,
            action=action,
            object_type="production_work_item",
            object_id=str(work_item_id),
            details={
                "marketplace_account_id": scope["account"],
                "actor_membership_id": principal.membership_id,
            },
        )
    )
    session.flush()


def read_production_work_item(session, *, principal, account, work_item_id):
    """Current coherent state only; this is not print/send eligibility."""
    _identifier(work_item_id)

    def read(session, guard, scope):
        row = _work(session, scope, work_item_id)
        _coherent_source(session, scope, account, row)
        return ProductionWorkItem(**row)

    return _execute(session, principal, account, PRODUCTION_READ_PERMISSIONS, read)


def read_production_work_item_by_source(
    session, *, principal, account, order_item_id, expected_source_item_version
):
    """Recover coherent committed state, never permission to retry creation."""
    _identifier(order_item_id)
    _identifier(expected_source_item_version)

    def read(session, guard, scope):
        # The live account lock also serializes creation/assignment. No row
        # mutation or audit is needed for this exact-source recovery read.
        row = (
            session.execute(
                text("""SELECT * FROM production_work_items
            WHERE organization_id=:org AND marketplace_account_id=:account
            AND order_item_id=:item"""),
                dict(scope, item=order_item_id),
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ProductionServiceError("PRODUCTION_NOT_FOUND")
        if row["source_item_version"] != expected_source_item_version:
            raise ProductionServiceError("PRODUCTION_SOURCE_CHANGED")
        _coherent_source(session, scope, account, row)
        return ProductionWorkItem(**row)

    return _execute(session, principal, account, PRODUCTION_READ_PERMISSIONS, read)


def create_production_work_item(
    session, *, principal, account, order_item_id, expected_source_item_version
):
    _identifier(order_item_id)
    _identifier(expected_source_item_version)

    def create(session, guard, scope):
        source = _source(
            session, scope, account, order_item_id, expected_source_item_version
        )
        existing = (
            session.execute(
                text("""SELECT * FROM production_work_items
            WHERE organization_id=:org AND marketplace_account_id=:account
            AND order_item_id=:item FOR UPDATE"""),
                dict(scope, item=order_item_id),
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            _coherent_source(session, scope, account, existing)
            work_id, replayed = existing["work_item_id"], True
        else:
            guard.revalidate_before_write()
            work_id = session.execute(
                text("""INSERT INTO production_work_items
                (organization_id,marketplace_account_id,order_id,order_item_id,source_item_version,required_quantity)
                VALUES(:org,:account,:order,:item,:source_version,:quantity) RETURNING work_item_id"""),
                dict(
                    scope,
                    order=source["order_id"],
                    item=order_item_id,
                    source_version=source["version"],
                    quantity=source["quantity"],
                ),
            ).scalar_one()
            replayed = False
        _audit(
            session,
            guard,
            principal,
            scope,
            work_id,
            "production.creation_replayed"
            if replayed
            else "production.work_item_created",
        )
        return ProductionCreation(work_id, replayed)

    return _execute(session, principal, account, PRODUCTION_CREATE_PERMISSIONS, create)


def assign_production_work_item(session, *, principal, account, command):
    if type(command) is not AssignmentCommand:
        raise ProductionServiceError("PRODUCTION_REQUEST_INVALID")

    def assign(session, guard, scope):
        row = _work(session, scope, command.work_item_id)
        _coherent_source(session, scope, account, row)
        params = dict(scope, work=command.work_item_id, key=command.idempotency_key)
        previous = (
            session.execute(
                text("""SELECT * FROM production_assignment_receipts
            WHERE organization_id=:org AND marketplace_account_id=:account
            AND work_item_id=:work AND idempotency_key COLLATE "C"=:key COLLATE "C"
            """),
                params,
            )
            .mappings()
            .one_or_none()
        )
        payload = serialize_assignment_command(command)
        checksum = assignment_command_checksum(command)
        if previous is not None:
            if (
                previous["request_schema_version"] != 1
                or previous["canonical_request_bytes"] != payload
                or previous["request_checksum"] != checksum
                or previous["request_payload"] != asdict(command)
            ):
                raise OrderCommandConflict("IDEMPOTENCY_CONFLICT")
            result = deserialize_assignment_result(
                previous["result_payload"],
                schema_version=previous["result_schema_version"],
            )
            if (
                result.work_item_id != command.work_item_id
                or result.version != command.expected_version + 1
                or result.catalog_sku_id != command.catalog_sku_id
            ):
                raise ProductionServiceError("PRODUCTION_RECEIPT_INVALID")
            _audit(
                session,
                guard,
                principal,
                scope,
                command.work_item_id,
                "production.assignment_replayed",
            )
            return ProductionAssignment(result, True)
        if command.expected_version != row["version"] or row["version"] == 2**63 - 1:
            raise OrderCommandConflict("VERSION_CONFLICT")
        if (
            session.execute(
                text(
                    "SELECT catalog_sku_id FROM catalog_skus WHERE organization_id=:org AND catalog_sku_id=:sku FOR SHARE"
                ),
                dict(scope, sku=command.catalog_sku_id),
            ).scalar_one_or_none()
            is None
        ):
            raise ProductionServiceError("PRODUCTION_CATALOG_DENIED")
        result = AssignmentResult(
            command.work_item_id,
            row["version"] + 1,
            command.catalog_sku_id,
            row["required_quantity"],
            row["planned_quantity"],
            row["remaining_quantity"],
            row["source_item_version"],
        )
        instant = session.execute(text("SELECT clock_timestamp()")).scalar_one()
        params.update(
            actor=principal.membership_id,
            version=result.version,
            sku=command.catalog_sku_id,
            expected=command.expected_version,
            reason=command.reason,
            instant=instant,
            request=json.dumps(asdict(command)),
            payload=payload,
            checksum=checksum,
            result=json.dumps(serialize_assignment_result(result)),
            old_sku=row["catalog_sku_id"],
        )
        guard.revalidate_before_write()
        params["receipt"] = session.execute(
            text("""INSERT INTO production_assignment_receipts
            (organization_id,marketplace_account_id,work_item_id,idempotency_key,request_schema_version,
             request_payload,canonical_request_bytes,request_checksum,actor_membership_id,
             result_version,result_schema_version,result_payload,created_at)
            VALUES(:org,:account,:work,:key,1,CAST(:request AS jsonb),:payload,:checksum,:actor,
             :version,1,CAST(:result AS jsonb),:instant) RETURNING receipt_id"""),
            params,
        ).scalar_one()
        guard.revalidate_before_write()
        changed = session.execute(
            text("""UPDATE production_work_items SET catalog_sku_id=:sku,
            version=:version,updated_at=:instant,current_assignment_receipt_id=:receipt
            WHERE organization_id=:org AND marketplace_account_id=:account AND work_item_id=:work
            AND version=:expected RETURNING work_item_id"""),
            params,
        ).scalar_one_or_none()
        if changed is None:
            raise OrderCommandConflict("VERSION_CONFLICT")
        guard.revalidate_before_write()
        session.execute(
            text("""INSERT INTO production_assignment_history
            (organization_id,marketplace_account_id,work_item_id,receipt_id,actor_membership_id,
             event_kind,from_version,to_version,previous_catalog_sku_id,catalog_sku_id,reason,occurred_at)
            VALUES(:org,:account,:work,:receipt,:actor,'manual_assignment',:expected,:version,:old_sku,:sku,:reason,:instant)"""),
            params,
        )
        _audit(
            session,
            guard,
            principal,
            scope,
            command.work_item_id,
            "production.manually_assigned",
        )
        return ProductionAssignment(result, False)

    return _execute(session, principal, account, PRODUCTION_ASSIGN_PERMISSIONS, assign)
