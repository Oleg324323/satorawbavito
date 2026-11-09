"""Atomic normalized manifests; provider completeness remains the adapter's obligation."""

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.modules.orders import (
    AVITO_ORDER_STATUS_MAPPING_VERSION,
    WB_STATISTICS_STATUS_MAPPING_VERSION,
    OrderContractValidationError,
)
from app.orders.bindings import (
    ACCOUNT_BINDING_SCHEMA_VERSION,
    account_binding_checksum,
    serialize_account_bindings,
    validate_run_binding,
)
from app.orders.catalog_resolution import resolve_order_catalog
from app.orders.contracts import CatalogResolution
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.ingestion import OrderManifest, _text, compare_observations
from app.orders.projection_repository import OrdersProjectionRepository
from app.orders.serialization import deserialize_observation
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    ExpectedCredential,
    ExpectedIngestionToken,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)

_ACTION = "orders.manifest_published"


@dataclass(frozen=True, slots=True)
class OrdersPublicationResult:
    run_id: int
    state: str
    replayed: bool
    reconciliation_count: int


def _status(session, record, params):
    row = record.observation
    values = dict(
        params,
        order=record.order_id,
        observation=record.observation_id,
        effective=row.effective_at,
        observed=row.observed_at,
        raw=row.status.raw_status,
        canonical=row.status.canonical_status,
        mapping=row.status.mapping_state,
        version=row.status.mapping_version,
        source=row.status.evidence_source,
    )
    existing = session.execute(
        text("""SELECT raw_status,canonical_status,mapping_state,
        mapping_version,evidence_source,source_effective_at,observed_at FROM order_status_observations
        WHERE organization_id=:org AND marketplace_account_id=:account AND observation_id=:observation"""),
        values,
    ).one_or_none()
    expected = (
        values["raw"],
        values["canonical"],
        values["mapping"],
        values["version"],
        values["source"],
        values["effective"],
        values["observed"],
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise OrderContractValidationError("Stored status evidence differs")
        return
    session.execute(
        text("""INSERT INTO order_status_observations
        (organization_id,marketplace_account_id,order_id,observation_id,raw_status,
         canonical_status,mapping_state,mapping_version,evidence_source,source_effective_at,observed_at)
        VALUES (:org,:account,:order,:observation,:raw,:canonical,:mapping,:version,:source,:effective,:observed)"""),
        values,
    )


def _event(session, record, params, kind):
    evidence = {
        "schema_version": 1,
        "reason": "source_ordering_unproven"
        if kind == "reconciliation_required"
        else "mapped_source_status",
    }
    values = dict(
        params,
        order=record.order_id,
        observation=record.observation_id,
        key="orders-publication-v1:" + kind,
        kind=kind,
        evidence=json.dumps(evidence),
        effective=record.observation.effective_at,
        observed=record.observation.observed_at,
    )
    existing = session.execute(
        text("""SELECT event_kind,evidence FROM order_lifecycle_events
        WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
        AND observation_id=:observation AND source_event_key COLLATE "C"=:key"""),
        values,
    ).one_or_none()
    if existing is not None:
        if tuple(existing) != (kind, evidence):
            raise OrderContractValidationError("Stored lifecycle evidence differs")
        return
    session.execute(
        text("""INSERT INTO order_lifecycle_events
        (organization_id,marketplace_account_id,order_id,observation_id,source_event_key,
         event_kind,evidence,source_effective_at,observed_at)
        VALUES (:org,:account,:order,:observation,:key,:kind,CAST(:evidence AS jsonb),:effective,:observed)"""),
        values,
    )


def _replay(session, existing, manifest, params):
    expected = (
        manifest.marketplace,
        manifest.adapter_version,
        params["mapping"],
        manifest.source_contract_version,
        manifest.source_snapshot,
        manifest.coverage_state,
        manifest.coverage_state,
        len(manifest.pages),
        len(manifest.observations),
        sum(len(row.items) for row in manifest.observations),
        manifest.expected_order_count,
        manifest.checksum,
    )
    if (
        tuple(
            existing[key]
            for key in (
                "marketplace",
                "adapter_version",
                "mapping_version",
                "source_contract_version",
                "source_snapshot",
                "state",
                "manifest_state",
                "page_count",
                "order_count",
                "item_count",
                "expected_order_count",
                "payload_checksum",
            )
        )
        != expected
    ):
        raise OrderContractValidationError("Run replay conflict")
    params = dict(params, run=existing["sync_run_id"])
    rows = (
        session.execute(
            text("""SELECT e.normalized_evidence FROM order_sync_memberships m
        JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.observation_id)=
        (m.organization_id,m.marketplace_account_id,m.observation_id)
        WHERE m.organization_id=:org AND m.marketplace_account_id=:account AND m.sync_run_id=:run
        AND m.order_item_id IS NULL"""),
            params,
        )
        .scalars()
        .all()
    )
    stored = {row.identity: row for row in map(deserialize_observation, rows)}
    if len(rows) != len(manifest.observations) or set(stored) != {
        row.identity for row in manifest.observations
    }:
        raise OrderContractValidationError("Run membership replay differs")
    if any(
        compare_observations(stored[row.identity], row) != "replay"
        for row in manifest.observations
    ):
        raise OrderContractValidationError("Run semantic replay differs")
    details = session.execute(
        text("""SELECT details FROM lk_audit_events
        WHERE organization_id=:org AND object_type='orders_sync_run' AND object_id=:id AND action=:action"""),
        dict(params, id=str(existing["sync_run_id"]), action=_ACTION),
    ).scalar_one()
    if (
        not isinstance(details, dict)
        or details.get("manifest_checksum") != manifest.checksum
        or type(details.get("reconciliation_count")) is not int
    ):
        raise OrderContractValidationError("Run receipt integrity failure")
    return OrdersPublicationResult(
        existing["sync_run_id"],
        existing["state"],
        True,
        details["reconciliation_count"],
    )


def publish_orders_manifest(
    session: Session,
    *,
    principal: UserSessionPrincipal,
    account: ExpectedAccountBinding,
    authorities: tuple[ExpectedCredential | ExpectedIngestionToken, ...],
    manifest: OrderManifest,
    source_run_key: str,
) -> OrdersPublicationResult:
    """No fetch inside. Initial complete facts may project; changed facts reconcile.

    A complete normalized manifest is a trusted adapter assertion, not proof of a
    provider's pagination guarantee. No live adapter is activated by this service.
    Every authority actually used for fetch must be supplied; worker principals
    are unsupported. Neither timestamps nor opaque revisions authorize progression.
    """
    if not isinstance(session, Session) or session.in_transaction():
        raise PublicationGuardError("publication_context_invalid")
    _text(source_run_key)
    if (
        not isinstance(manifest, OrderManifest)
        or not isinstance(principal, UserSessionPrincipal)
        or not isinstance(account, ExpectedAccountBinding)
    ):
        raise OrderContractValidationError("Publication contracts required")
    if (
        manifest.organization_id,
        manifest.marketplace_account_id,
        manifest.marketplace,
    ) != (principal.organization_id, account.marketplace_account_id, account.provider):
        raise PublicationGuardError("publication_binding_changed")
    if type(authorities) is not tuple or not authorities:
        raise PublicationGuardError("publication_authority_invalid")
    if manifest.source_kind == "avito-browser":
        valid = any(
            isinstance(value, ExpectedIngestionToken)
            and value.scope == "avito.browser_snapshot.write"
            for value in authorities
        )
    else:
        kinds = (
            {"wb_api"}
            if manifest.marketplace == "wb"
            else {"avito_oauth_client", "avito_oauth_access"}
        )
        valid = any(
            isinstance(value, ExpectedCredential) and value.kind in kinds
            for value in authorities
        )
    if not valid:
        raise PublicationGuardError("publication_authority_invalid")
    try:
        with session.begin():
            guard = acquire_publication_guard(
                session,
                principal=principal,
                required_permissions=frozenset({"sync:run"}),
                accounts=(account,),
                authorities=authorities,
            )
            result = _persist_orders_manifest(
                session,
                manifest=manifest,
                account=account,
                source_run_key=source_run_key,
                actor_user_id=principal.user_id,
            )
            guard.revalidate_before_write()
        return result
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None


def _persist_orders_manifest(
    session: Session,
    *,
    manifest: OrderManifest,
    account: ExpectedAccountBinding,
    source_run_key: str,
    actor_user_id: str | None,
) -> OrdersPublicationResult:
    """Persist inside a caller-owned, already authorized physical transaction.

    Entry points own authority acquisition and final commit fencing. This helper
    neither authorizes callers nor commits/revalidates a different principal.
    """
    if (
        not isinstance(session, Session)
        or not session.in_transaction()
        or session.in_nested_transaction()
        or not session.is_active
    ):
        raise PublicationGuardError("publication_context_invalid")
    params = {
        "org": manifest.organization_id,
        "account": manifest.marketplace_account_id,
        "source": manifest.source_kind,
        "key": source_run_key,
        "binding": account_binding_checksum(manifest.organization_id, (account,)),
        "binding_version": ACCOUNT_BINDING_SCHEMA_VERSION,
        "binding_external": account.external_account_id,
        "binding_ref": account.credential_ref,
        "binding_payload": serialize_account_bindings(
            manifest.organization_id, (account,)
        ),
        "mapping": AVITO_ORDER_STATUS_MAPPING_VERSION
        if manifest.marketplace == "avito"
        else WB_STATISTICS_STATUS_MAPPING_VERSION,
    }
    existing = (
        session.execute(
            text("""SELECT * FROM order_sync_runs
        WHERE organization_id=:org AND marketplace_account_id=:account
        AND source_kind COLLATE "C"=:source AND source_run_key COLLATE "C"=:key FOR UPDATE"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        validate_run_binding(
            manifest.organization_id,
            account,
            schema_version=existing["account_binding_schema_version"],
            external_account_id=existing["account_binding_external_account_id"],
            credential_ref=existing["account_binding_credential_ref"],
            payload=existing["account_binding_payload"],
            checksum=existing["account_binding_checksum"],
        )
        result = _replay(session, existing, manifest, params)
    else:
        params.update(
            marketplace=manifest.marketplace,
            adapter=manifest.adapter_version,
            contract=manifest.source_contract_version,
            snapshot=manifest.source_snapshot,
        )
        run = session.execute(
            text("""INSERT INTO order_sync_runs
            (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
             adapter_version,mapping_version,source_contract_version,source_snapshot,
             account_binding_schema_version,account_binding_external_account_id,
             account_binding_credential_ref,account_binding_payload,account_binding_checksum)
            VALUES (:org,:account,:marketplace,:source,:key,:adapter,:mapping,:contract,:snapshot,
                    :binding_version,:binding_external,:binding_ref,:binding_payload,:binding)
            RETURNING sync_run_id"""),
            params,
        ).scalar_one()
        params["run"] = run
        reconciliation = 0
        evidence = OrdersEvidenceRepository(
            session, manifest.organization_id, manifest.marketplace_account_id
        )
        projections = OrdersProjectionRepository(
            session, manifest.organization_id, manifest.marketplace_account_id
        )
        for row in manifest.observations:
            record = evidence.append(run, row)
            _status(session, record, params)
            if row.status.canonical_status in (
                "cancelled",
                "returning",
                "returned",
            ):
                _event(
                    session,
                    record,
                    params,
                    "cancellation"
                    if row.status.canonical_status == "cancelled"
                    else "return",
                )
            if manifest.coverage_state != "complete":
                continue
            current = session.execute(
                text("""SELECT version,last_seen_sync_run_id FROM marketplace_orders
                WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order FOR UPDATE"""),
                dict(params, order=record.order_id),
            ).one()
            if current.last_seen_sync_run_id is None and current.version == 1:
                projections.set_parent(run, record.observation_id, expected_version=1)
                for item in row.items:
                    resolution = (
                        resolve_order_catalog(
                            session,
                            manifest.organization_id,
                            manifest.marketplace_account_id,
                            item.identity.external_item_id,
                        )
                        if item.identity.external_item_id is not None
                        else CatalogResolution(
                            "unmapped",
                            None,
                            None,
                            None,
                            "orders-initial-unmapped-v1",
                        )
                    )
                    projections.set_item(
                        run,
                        record.observation_id,
                        item.identity.source_line_key,
                        resolution=resolution,
                        expected_version=None,
                    )
            else:
                previous = session.execute(
                    text("""SELECT e.normalized_evidence FROM order_sync_memberships m
                    JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.observation_id)=
                    (m.organization_id,m.marketplace_account_id,m.observation_id)
                    WHERE m.organization_id=:org AND m.marketplace_account_id=:account AND m.sync_run_id=:last
                    AND m.order_id=:order AND m.order_item_id IS NULL"""),
                    dict(
                        params,
                        last=current.last_seen_sync_run_id,
                        order=record.order_id,
                    ),
                ).scalar_one_or_none()
                old = (
                    deserialize_observation(previous) if previous is not None else None
                )
                if (
                    old is None
                    or old.source_kind != row.source_kind
                    or compare_observations(old, row) != "replay"
                ):
                    _event(session, record, params, "reconciliation_required")
                    reconciliation += 1
        pages = [page.number for page in manifest.pages]
        params.update(
            state=manifest.coverage_state,
            checksum=manifest.checksum,
            pages=len(pages),
            orders=len(manifest.observations),
            items=sum(len(row.items) for row in manifest.observations),
            expected=manifest.expected_order_count,
            first=min(pages) if pages else None,
            last=max(pages) if pages else None,
            complete=manifest.coverage_state == "complete",
        )
        session.execute(
            text("""INSERT INTO order_sync_coverage
            (organization_id,marketplace_account_id,sync_run_id,coverage_kind,first_page,last_page,
             is_complete,manifest_checksum,completed_at)
            VALUES (:org,:account,:run,'orders-manifest-v1',:first,:last,:complete,:checksum,clock_timestamp())"""),
            params,
        )
        session.execute(
            text("""UPDATE order_sync_runs SET state=:state,manifest_state=:state,
            payload_checksum=:checksum,page_count=:pages,order_count=:orders,item_count=:items,
            expected_order_count=:expected,completed_at=clock_timestamp()
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
            params,
        )
        session.add(
            LkAuditEventRow(
                organization_id=manifest.organization_id,
                actor_user_id=actor_user_id,
                action=_ACTION,
                object_type="orders_sync_run",
                object_id=str(run),
                details={
                    "manifest_checksum": manifest.checksum,
                    "reconciliation_count": reconciliation,
                    "account_binding_checksum": params["binding"],
                },
            )
        )
        result = OrdersPublicationResult(
            run, manifest.coverage_state, False, reconciliation
        )
    return result
