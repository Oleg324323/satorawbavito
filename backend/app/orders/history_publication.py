"""Positive partial history participant; T1 owns authority, receipt and commit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.modules.orders import (
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
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.history_bridge import (
    ADAPTER,
    SOURCE,
    HistoryChunkPlan,
    decode_history_chunk,
)
from app.orders.projection_repository import OrdersProjectionRepository
from app.orders.publication_service import OrdersPublicationResult, _event, _status
from app.orders.serialization import deserialize_observation, observation_checksum
from app.platform.integrations.publication_guard import PublicationGuardError

if TYPE_CHECKING:
    from app.platform.integrations.wb_history_projection import (
        WbHistoryProjectionHandle,
    )

SOURCE_CONTRACT = "wb-history-positive-partial-v1"


def _validate_reconciliation_count(actual, expected):
    if type(actual) is not int or actual < 0 or actual != expected:
        raise OrderContractValidationError("History decision evidence differs")
    return actual


def _can_project_initial(
    *,
    comparison,
    parent_version,
    current_run_id,
    observation_run_id,
    run_id,
    has_other_observations,
    has_earlier_memberships,
):
    return (
        comparison == "new"
        and parent_version == 1
        and current_run_id is None
        and observation_run_id == run_id
        and not has_other_observations
        and not has_earlier_memberships
    )


def is_history_partial_run(run):
    return (
        run["marketplace"],
        run["source_kind"],
        run["adapter_version"],
        run["mapping_version"],
        run["source_contract_version"],
        run["state"],
        run["manifest_state"],
    ) == (
        "wb",
        SOURCE,
        ADAPTER,
        WB_STATISTICS_STATUS_MAPPING_VERSION,
        SOURCE_CONTRACT,
        "partial",
        "partial",
    )


def require_history_projection_receipt(
    session, *, organization_id, marketplace_account_id, run
):
    """Data proof inside an existing guarded root, not read authorization."""
    if not is_history_partial_run(run):
        raise OrderContractValidationError("History partial source contract differs")
    # Lazy dependency keeps legacy Orders import/read paths independent of dormant T7.
    from app.platform.integrations.wb_history_projection import (
        load_history_projection_receipt,
    )

    receipt = load_history_projection_receipt(
        session,
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        sync_run_id=run["sync_run_id"],
    )
    if (
        receipt.organization_id,
        receipt.marketplace_account_id,
        receipt.sync_run_id,
        receipt.input_checksum,
        receipt.source_run_key,
        receipt.source_snapshot,
        receipt.source_contract_version,
        receipt.coverage_state,
    ) != (
        organization_id,
        marketplace_account_id,
        run["sync_run_id"],
        run["payload_checksum"],
        run["source_run_key"],
        run["source_snapshot"],
        SOURCE_CONTRACT,
        "partial",
    ):
        raise OrderContractValidationError("History projection receipt differs")
    return receipt


def _validate_replay_run(run, plan: HistoryChunkPlan, account, snapshot):
    validate_run_binding(
        plan.page.organization_id,
        account,
        schema_version=run["account_binding_schema_version"],
        external_account_id=run["account_binding_external_account_id"],
        credential_ref=run["account_binding_credential_ref"],
        payload=run["account_binding_payload"],
        checksum=run["account_binding_checksum"],
    )
    if not is_history_partial_run(run) or (
        run["organization_id"],
        run["marketplace_account_id"],
        run["payload_checksum"],
        run["source_run_key"],
        run["source_snapshot"],
        run["requested_from"],
        run["requested_to"],
        run["page_count"],
        run["order_count"],
        run["item_count"],
        run["expected_order_count"],
    ) != (
        plan.page.organization_id,
        plan.page.marketplace_account_id,
        plan.input_checksum,
        plan.source_run_key,
        snapshot,
        None,
        None,
        1,
        len(plan.rows),
        len(plan.rows),
        None,
    ):
        raise OrderContractValidationError("History chunk replay differs")


def _previous(session, scope, plan):
    if not plan.rows:
        return ()
    rows = (
        session.execute(
            text("""SELECT o.external_order_id,o.last_seen_sync_run_id,
        e.normalized_evidence,e.payload_checksum FROM marketplace_orders o
        LEFT JOIN order_sync_memberships m ON
        (m.organization_id,m.marketplace_account_id,m.order_id,m.sync_run_id)=
        (o.organization_id,o.marketplace_account_id,o.order_id,o.last_seen_sync_run_id)
        AND m.order_item_id IS NULL
        LEFT JOIN order_observations e ON
        (e.organization_id,e.marketplace_account_id,e.order_id,e.observation_id)=
        (m.organization_id,m.marketplace_account_id,m.order_id,m.observation_id)
        AND e.order_item_id IS NULL
        WHERE o.organization_id=:org AND o.marketplace_account_id=:account AND o.marketplace='wb'
        AND o.external_order_id=ANY(:ids) ORDER BY o.external_order_id COLLATE "C" FOR UPDATE OF o"""),
            dict(
                scope,
                ids=[row.observation.identity.external_order_id for row in plan.rows],
            ),
        )
        .mappings()
        .all()
    )
    previous = []
    for row in rows:
        if row["last_seen_sync_run_id"] is None:
            continue
        if row["normalized_evidence"] is None:
            raise OrderContractValidationError("Current history membership missing")
        observed = deserialize_observation(row["normalized_evidence"])
        if (
            observed.identity.organization_id,
            observed.identity.marketplace_account_id,
            observed.identity.marketplace,
            observed.identity.external_order_id,
            observation_checksum(observed),
        ) != (
            scope["org"],
            scope["account"],
            "wb",
            row["external_order_id"],
            row["payload_checksum"],
        ):
            raise OrderContractValidationError("Current history membership differs")
        previous.append(observed)
    return tuple(previous)


def persist_wb_history_chunk(
    session: Session, *, handle: WbHistoryProjectionHandle
) -> OrdersPublicationResult:
    """Return a provisional result; never begin/commit/rollback or create authority."""
    from app.platform.integrations.wb_history_projection import (
        WbHistoryProjectionHandle,
        apply_history_initial_parent,
        load_history_reconciliation_count,
    )

    if type(handle) is not WbHistoryProjectionHandle:
        raise PublicationGuardError("publication_context_invalid")
    handle.require_root(session)
    page, first, captured = handle.page, handle.first_ordinal, handle.captured_rows()
    baseline = decode_history_chunk(page=page, first_ordinal=first, rows=captured)
    account, snapshot = handle.account_binding, handle.source_snapshot
    if (
        baseline.input_checksum != handle.input_checksum
        or baseline.source_run_key != handle.source_run_key
        or handle.source_contract_version != SOURCE_CONTRACT
        or snapshot != f"wb-history-run-v1:{page.job_id}:{page.run_id}"
        or (account.marketplace_account_id, account.provider)
        != (page.marketplace_account_id, "wb")
    ):
        raise OrderContractValidationError("History chunk handle binding differs")
    scope = {"org": page.organization_id, "account": page.marketplace_account_id}
    parameters = dict(scope, source=SOURCE, key=baseline.source_run_key)
    existing = (
        session.execute(
            text("""SELECT * FROM order_sync_runs
        WHERE organization_id=:org AND marketplace_account_id=:account AND source_kind=:source
        AND source_run_key COLLATE "C"=:key FOR UPDATE"""),
            parameters,
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        _validate_replay_run(existing, baseline, account, snapshot)
        receipt = require_history_projection_receipt(
            session,
            organization_id=page.organization_id,
            marketplace_account_id=page.marketplace_account_id,
            run=existing,
        )
        handle.require_root(session)
        return OrdersPublicationResult(
            existing["sync_run_id"], "partial", True, receipt.reconciliation_count
        )
    prior = _previous(session, scope, baseline)
    plan = decode_history_chunk(
        page=page, first_ordinal=first, rows=captured, previous=prior
    )
    if (
        plan.input_checksum != handle.input_checksum
        or plan.source_run_key != handle.source_run_key
    ):
        raise OrderContractValidationError("History chunk input changed")
    parameters.update(
        adapter=ADAPTER,
        mapping=WB_STATISTICS_STATUS_MAPPING_VERSION,
        contract=SOURCE_CONTRACT,
        snapshot=snapshot,
        binding_version=ACCOUNT_BINDING_SCHEMA_VERSION,
        binding_external=account.external_account_id,
        binding_ref=account.credential_ref,
        binding_payload=serialize_account_bindings(page.organization_id, (account,)),
        binding=account_binding_checksum(page.organization_id, (account,)),
    )
    handle.revalidate_before_write()
    run_id = session.execute(
        text("""INSERT INTO order_sync_runs
        (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
         adapter_version,mapping_version,source_contract_version,source_snapshot,
         account_binding_schema_version,account_binding_external_account_id,
         account_binding_credential_ref,account_binding_payload,account_binding_checksum)
        VALUES(:org,:account,'wb',:source,:key,:adapter,:mapping,:contract,:snapshot,
         :binding_version,:binding_external,:binding_ref,:binding_payload,:binding) RETURNING sync_run_id"""),
        parameters,
    ).scalar_one()
    parameters["run"] = run_id
    evidence = OrdersEvidenceRepository(
        session, page.organization_id, page.marketplace_account_id
    )
    projections = OrdersProjectionRepository(
        session, page.organization_id, page.marketplace_account_id
    )
    reconciliation_count = 0
    for decision in plan.rows:
        handle.revalidate_before_write()
        record = evidence.append(run_id, decision.observation)
        handle.revalidate_before_write()
        _status(session, record, parameters)
        if decision.observation.status.canonical_status == "cancelled":
            handle.revalidate_before_write()
            _event(session, record, parameters, "cancellation")
        current = session.execute(
            text("""SELECT version,last_seen_sync_run_id FROM marketplace_orders
            WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order FOR UPDATE"""),
            dict(scope, order=record.order_id),
        ).one()
        if (
            decision.comparison == "new"
            and current.version == 1
            and current.last_seen_sync_run_id is None
        ):
            provenance = session.execute(
                text("""SELECT e.sync_run_id AS observation_run_id,
                EXISTS(SELECT 1 FROM order_observations
                WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                AND observation_id<>:observation AND order_item_id IS NULL) AS has_other_observations,
                EXISTS(SELECT 1 FROM order_sync_memberships
                WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                AND sync_run_id<>:run AND order_item_id IS NULL) AS has_earlier_memberships
                FROM order_observations e
                WHERE e.organization_id=:org AND e.marketplace_account_id=:account
                AND e.order_id=:order AND e.observation_id=:observation AND e.order_item_id IS NULL"""),
                dict(
                    scope,
                    order=record.order_id,
                    observation=record.observation_id,
                    run=run_id,
                ),
            ).one()
            if _can_project_initial(
                comparison=decision.comparison,
                parent_version=current.version,
                current_run_id=current.last_seen_sync_run_id,
                observation_run_id=provenance.observation_run_id,
                run_id=run_id,
                has_other_observations=provenance.has_other_observations,
                has_earlier_memberships=provenance.has_earlier_memberships,
            ):
                handle.revalidate_before_write()
                apply_history_initial_parent(
                    session,
                    handle=handle,
                    sync_run_id=run_id,
                    order_id=record.order_id,
                )
                for item in record.observation.items:
                    resolution = resolve_order_catalog(
                        session,
                        page.organization_id,
                        page.marketplace_account_id,
                        item.identity.external_item_id,
                    )
                    handle.revalidate_before_write()
                    item_id, _ = projections.set_item(
                        run_id,
                        record.observation_id,
                        item.identity.source_line_key,
                        resolution=resolution,
                        expected_version=None,
                    )
                    child_params = dict(
                        parameters,
                        order=record.order_id,
                        item=item_id,
                        parent_observation=record.observation_id,
                        parent_checksum=observation_checksum(record.observation),
                        line=item.identity.source_line_key,
                    )
                    handle.revalidate_before_write()
                    child_observation = session.execute(
                        text("""INSERT INTO order_observations
                        (organization_id,marketplace_account_id,sync_run_id,order_id,
                         order_item_id,source_kind,adapter_version,source_event_id,
                         source_revision,payload_checksum,evidence_schema_version,
                         normalized_evidence,source_effective_at,observed_at)
                        SELECT e.organization_id,e.marketplace_account_id,e.sync_run_id,e.order_id,
                          i.order_item_id,e.source_kind,e.adapter_version,e.source_event_id,
                          e.source_revision,e.payload_checksum,e.evidence_schema_version,
                          e.normalized_evidence,e.source_effective_at,e.observed_at
                        FROM order_observations e JOIN marketplace_order_items i ON
                          (i.organization_id,i.marketplace_account_id,i.order_id)=
                          (e.organization_id,e.marketplace_account_id,e.order_id)
                        WHERE e.organization_id=:org AND e.marketplace_account_id=:account
                          AND e.sync_run_id=:run AND e.order_id=:order
                          AND e.observation_id=:parent_observation AND e.order_item_id IS NULL
                          AND e.payload_checksum=:parent_checksum
                          AND i.order_item_id=:item AND i.source_line_key COLLATE "C"=:line
                          AND i.version=1
                        RETURNING observation_id"""),
                        child_params,
                    ).scalar_one()
                    child_params["child_observation"] = child_observation
                    handle.revalidate_before_write()
                    session.execute(
                        text("""INSERT INTO order_sync_memberships
                        (organization_id,marketplace_account_id,sync_run_id,order_id,
                         order_item_id,observation_id,coverage_role,observed_at)
                        SELECT m.organization_id,m.marketplace_account_id,m.sync_run_id,m.order_id,
                          e.order_item_id,e.observation_id,'observed',m.observed_at
                        FROM order_sync_memberships m JOIN order_observations e ON
                          (e.organization_id,e.marketplace_account_id,e.sync_run_id,e.order_id)=
                          (m.organization_id,m.marketplace_account_id,m.sync_run_id,m.order_id)
                        WHERE m.organization_id=:org AND m.marketplace_account_id=:account
                          AND m.sync_run_id=:run AND m.order_id=:order
                          AND m.order_item_id IS NULL AND m.observation_id=:parent_observation
                          AND m.coverage_role='observed'
                          AND e.order_item_id=:item AND e.observation_id=:child_observation
                          AND e.payload_checksum=:parent_checksum
                        RETURNING observation_id"""),
                        child_params,
                    ).scalar_one()
                continue
        if decision.comparison != "replay":
            handle.revalidate_before_write()
            _event(session, record, parameters, "reconciliation_required")
            reconciliation_count += 1
    parameters.update(checksum=plan.input_checksum, count=len(plan.rows))
    handle.revalidate_before_write()
    session.execute(
        text("""INSERT INTO order_sync_coverage
        (organization_id,marketplace_account_id,sync_run_id,coverage_kind,first_page,last_page,
         is_complete,manifest_checksum,completed_at)
        VALUES(:org,:account,:run,:contract,1,1,false,:checksum,clock_timestamp())"""),
        parameters,
    )
    handle.revalidate_before_write()
    session.execute(
        text("""UPDATE order_sync_runs SET state='partial',manifest_state='partial',
        payload_checksum=:checksum,page_count=1,order_count=:count,item_count=:count,
        expected_order_count=NULL,completed_at=clock_timestamp()
        WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
        parameters,
    )
    session.flush()
    handle.require_root(session)
    reconciliation_count = _validate_reconciliation_count(
        load_history_reconciliation_count(session, handle=handle, sync_run_id=run_id),
        reconciliation_count,
    )
    handle.revalidate_before_write()
    session.add(
        LkAuditEventRow(
            organization_id=page.organization_id,
            actor_user_id=handle.initiating_user_id,
            action="orders.history_chunk_projected",
            object_type="orders_sync_run",
            object_id=str(run_id),
            details={
                "input_checksum": plan.input_checksum,
                "reconciliation_count": reconciliation_count,
            },
        )
    )
    session.flush()
    handle.require_root(session)
    return OrdersPublicationResult(run_id, "partial", False, reconciliation_count)
