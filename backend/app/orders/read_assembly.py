"""Publish a conservative current view, separately from cache-only HTTP reads."""

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.avito_status_refresh import POLICY_VERSION, production_work_exists
from app.orders.bindings import bound_high_water_mark, validate_run_binding
from app.orders.catalog_resolution import resolve_order_catalog
from app.orders.contracts import (
    AccountCoverage,
    CatalogResolution,
    DeadlineEvidence,
    OrderReadRow,
)
from app.orders.history_publication import SOURCE_CONTRACT as HISTORY_SOURCE_CONTRACT
from app.orders.history_publication import require_history_projection_receipt
from app.orders.ingestion import _integer
from app.orders.serialization import (
    deserialize_observation,
    observation_checksum,
    serialize_read_row,
)
from app.orders.snapshot_repository import OrdersSnapshotRepository
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)


def freeze_orders_view(
    session: Session,
    *,
    principal: UserSessionPrincipal,
    accounts: tuple[ExpectedAccountBinding, ...],
    coverage_run_ids: tuple[int, ...],
    query_checksum: str,
) -> int:
    """Explicit persisted run per account; no latest-source or completeness inference.

    Coverage stays partial: normalized manifests alone do not prove provider scope.
    Mixed source/adapter projections cannot fit the existing one-source-per-account
    wire contract and fail closed, rather than silently dropping older orders.
    Catalog resolution is the stored decision, with explicit stale-evidence blockers.
    Stored order-level deadlines are bound to the exact current observation.
    This does not calculate deadlines or issue Production eligibility.
    """
    if not isinstance(session, Session) or session.in_transaction():
        raise PublicationGuardError("publication_context_invalid")
    if type(coverage_run_ids) is not tuple or len(coverage_run_ids) != len(accounts):
        raise OrderContractValidationError("Invalid coverage selection")
    for run in coverage_run_ids:
        _integer(run)
    try:
        with session.begin():
            guard = acquire_publication_guard(
                session,
                principal=principal,
                accounts=accounts,
                authorities=(),
                required_permissions=frozenset({"sync:run", "cabinet:read"}),
            )
            rows, coverage, parents = [], [], {}
            for account, run in sorted(
                zip(accounts, coverage_run_ids, strict=True),
                key=lambda pair: pair[0].marketplace_account_id,
            ):
                params = {
                    "org": principal.organization_id,
                    "account": account.marketplace_account_id,
                    "run": run,
                }
                header = (
                    session.execute(
                        text("""SELECT source_kind,adapter_version,source_snapshot,
                    requested_from,requested_to FROM order_sync_runs
                    WHERE organization_id=:org AND marketplace_account_id=:account
                    AND sync_run_id=:run AND state IN ('complete','partial') FOR SHARE"""),
                        params,
                    )
                    .mappings()
                    .one_or_none()
                )
                if header is None:
                    raise OrderContractValidationError("Sealed coverage run missing")
                coverage.append(
                    AccountCoverage(
                        account.marketplace_account_id,
                        header["source_kind"],
                        header["adapter_version"],
                        "partial",
                        header["source_snapshot"],
                        header["requested_from"],
                        header["requested_to"],
                    )
                )
                orders = (
                    session.execute(
                        text("""SELECT o.order_id,o.version,o.last_seen_sync_run_id,e.observation_id,
                    e.normalized_evidence,e.payload_checksum FROM marketplace_orders o
                    JOIN order_sync_memberships m ON
                    (m.organization_id,m.marketplace_account_id,m.order_id,m.sync_run_id)=
                    (o.organization_id,o.marketplace_account_id,o.order_id,o.last_seen_sync_run_id)
                    AND m.order_item_id IS NULL
                    JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.observation_id)=
                    (m.organization_id,m.marketplace_account_id,m.observation_id)
                    WHERE o.organization_id=:org AND o.marketplace_account_id=:account
                    ORDER BY o.order_id FOR SHARE OF o"""),
                        params,
                    )
                    .mappings()
                    .all()
                )
                current_count = session.execute(
                    text("""SELECT count(*) FROM marketplace_orders
                    WHERE organization_id=:org AND marketplace_account_id=:account
                    AND last_seen_sync_run_id IS NOT NULL"""),
                    params,
                ).scalar_one()
                if current_count != len(orders):
                    raise OrderContractValidationError(
                        "Current membership binding missing"
                    )
                current_runs = {order["last_seen_sync_run_id"] for order in orders}
                for source_run in {run, *current_runs}:
                    source_binding = (
                        session.execute(
                            text("""SELECT * FROM order_sync_runs
                        WHERE organization_id=:org AND marketplace_account_id=:account
                        AND marketplace=:provider AND sync_run_id=:run FOR SHARE"""),
                            dict(params, provider=account.provider, run=source_run),
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if source_binding is None:
                        raise OrderContractValidationError(
                            "Source run account binding missing or changed"
                        )
                    history_receipt = (
                        source_binding["source_contract_version"]
                        == HISTORY_SOURCE_CONTRACT
                    )
                    if history_receipt:
                        require_history_projection_receipt(
                            session,
                            organization_id=principal.organization_id,
                            marketplace_account_id=account.marketplace_account_id,
                            run=source_binding,
                        )
                    if (
                        source_run in current_runs
                        and not history_receipt
                        and (
                            source_binding["state"] != "complete"
                            or source_binding["manifest_state"] != "complete"
                        )
                    ):
                        raise OrderContractValidationError(
                            "Current source publication incomplete"
                        )
                    validate_run_binding(
                        principal.organization_id,
                        account,
                        schema_version=source_binding["account_binding_schema_version"],
                        external_account_id=source_binding[
                            "account_binding_external_account_id"
                        ],
                        credential_ref=source_binding["account_binding_credential_ref"],
                        payload=source_binding["account_binding_payload"],
                        checksum=source_binding["account_binding_checksum"],
                    )
                for order in orders:
                    observation = deserialize_observation(order["normalized_evidence"])
                    if (observation.source_kind, observation.adapter_version) != (
                        header["source_kind"],
                        header["adapter_version"],
                    ):
                        raise OrderContractValidationError(
                            "Mixed source coverage requires versioned contract"
                        )
                    if observation_checksum(observation) != order["payload_checksum"]:
                        raise OrderContractValidationError(
                            "Stored observation checksum differs"
                        )
                    scoped = dict(
                        params,
                        order=order["order_id"],
                        observation=order["observation_id"],
                    )
                    deadlines = tuple(
                        DeadlineEvidence(
                            kind=deadline["deadline_kind"],
                            source_at=deadline["source_deadline_at"],
                            computed_at=deadline["computed_deadline_at"],
                            rule_id=deadline["rule_id"],
                            rule_version=deadline["rule_version"],
                            timezone=deadline["timezone"],
                            evidence_source=deadline["evidence_source"],
                            observed_at=deadline["observed_at"],
                        )
                        for deadline in session.execute(
                            text("""SELECT deadline_kind,source_deadline_at,
                            computed_deadline_at,rule_id,rule_version,timezone,
                            evidence_source,observed_at FROM order_deadlines
                            WHERE organization_id=:org AND marketplace_account_id=:account
                            AND order_id=:order AND observation_id=:observation
                            AND order_item_id IS NULL
                            ORDER BY deadline_kind COLLATE "C",deadline_id"""),
                            scoped,
                        ).mappings()
                    )
                    divergent = session.execute(
                        text("""SELECT EXISTS(SELECT 1 FROM order_observations
                        WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                        AND order_item_id IS NULL AND observation_id<>:observation)"""),
                        scoped,
                    ).scalar_one()
                    refresh_reconciliation = False
                    existing_work = False
                    if observation.adapter_version == POLICY_VERSION:
                        refresh_reconciliation = session.execute(
                            text("""SELECT EXISTS(SELECT 1 FROM order_lifecycle_events
                            WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                            AND event_kind='reconciliation_required' AND evidence->>'policy'=:policy)"""),
                            dict(scoped, policy=POLICY_VERSION),
                        ).scalar_one()
                        # Only the committed accepted-predecessor chain is explained history.
                        divergent = session.execute(
                            text("""WITH RECURSIVE accepted(observation_id) AS (
                                SELECT CAST(:observation AS bigint)
                                UNION
                                SELECT CAST(l.evidence->>'previous_observation_id' AS bigint)
                                FROM accepted a JOIN order_lifecycle_events l ON l.observation_id=a.observation_id
                                JOIN order_observations predecessor ON predecessor.observation_id=
                                    CAST(l.evidence->>'previous_observation_id' AS bigint)
                                WHERE l.organization_id=:org AND l.marketplace_account_id=:account AND l.order_id=:order
                                AND predecessor.organization_id=:org AND predecessor.marketplace_account_id=:account
                                AND predecessor.order_id=:order AND predecessor.order_item_id IS NULL
                                AND l.event_kind='status_changed' AND l.source_event_key=:key AND l.evidence->>'policy'=:policy
                            ) SELECT EXISTS(SELECT 1 FROM order_observations
                                WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                                AND order_item_id IS NULL AND observation_id NOT IN (SELECT observation_id FROM accepted))"""),
                            dict(
                                scoped,
                                policy=POLICY_VERSION,
                                key=POLICY_VERSION + ":accepted",
                            ),
                        ).scalar_one()
                        existing_work = production_work_exists(
                            session,
                            principal.organization_id,
                            account.marketplace_account_id,
                            order["order_id"],
                        )
                    items = (
                        session.execute(
                            text("""SELECT * FROM marketplace_order_items
                        WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order
                        ORDER BY source_line_key COLLATE "C" FOR SHARE"""),
                            scoped,
                        )
                        .mappings()
                        .all()
                    )
                    expected = {
                        item.identity.source_line_key: item
                        for item in observation.items
                    }
                    if set(expected) != {item["source_line_key"] for item in items}:
                        raise OrderContractValidationError(
                            "Current item set differs from observation"
                        )
                    if items:
                        parents[observation.identity] = order["version"]
                    for item in items:
                        source_item = expected[item["source_line_key"]]
                        resolution = CatalogResolution(
                            item["resolution_state"],
                            item["marketplace_product_id"],
                            item["marketplace_offer_id"],
                            item["catalog_sku_id"],
                            item["resolution_version"],
                        )
                        blockers = ["source_readiness_unproven", "coverage_unproven"]
                        if observation.adapter_version == POLICY_VERSION:
                            blockers.append(
                                "application_freshness_not_provider_chronology"
                            )
                        if refresh_reconciliation:
                            blockers.append(
                                "avito_status_refresh_reconciliation_required"
                            )
                        if existing_work:
                            blockers.append("avito_status_refresh_existing_work_item")
                        if divergent:
                            blockers.append("source_reconciliation_required")
                        if resolution.state not in ("resolved", "manual_override"):
                            blockers.append("catalog_" + resolution.state)
                        elif resolution.state == "resolved":
                            external = source_item.identity.external_item_id
                            checked = (
                                resolve_order_catalog(
                                    session,
                                    principal.organization_id,
                                    account.marketplace_account_id,
                                    external,
                                    previous=resolution,
                                )
                                if external is not None
                                else None
                            )
                            if checked is None or checked.state != "resolved":
                                blockers.append("catalog_evidence_changed")
                        if observation.identity.marketplace == "wb":
                            blockers.append("wb_fulfillment_source_missing")
                        canonical = observation.status.canonical_status
                        if canonical is None:
                            blockers.append("marketplace_status_unmapped")
                        elif canonical != "ready_for_fulfillment":
                            blockers.append("marketplace_status_" + canonical)
                        rows.append(
                            OrderReadRow(
                                observation,
                                source_item.identity,
                                item["version"],
                                resolution,
                                tuple(blockers),
                                deadlines,
                            )
                        )
            encoded = json.dumps(
                [
                    sorted(
                        (account.marketplace_account_id, run)
                        for account, run in zip(accounts, coverage_run_ids, strict=True)
                    ),
                    [serialize_read_row(row) for row in rows],
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            high_water_mark = bound_high_water_mark(
                hashlib.sha256(encoded.encode("ascii")).hexdigest(),
                principal.organization_id,
                accounts,
            )
            snapshot = OrdersSnapshotRepository(
                session, principal.organization_id
            ).freeze(
                tuple(rows),
                tuple(coverage),
                high_water_mark,
                query_checksum,
                parent_versions=parents,
            )
            guard.revalidate_before_write()
        return snapshot
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None
