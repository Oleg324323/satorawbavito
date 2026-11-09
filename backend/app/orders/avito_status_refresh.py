"""Bounded application freshness for known Avito orders, not provider chronology."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.infra.db import set_marketplace_account_context
from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    OrderContractValidationError,
    make_avito_source_line_key,
    map_avito_status,
)
from app.orders.bindings import serialize_account_bindings, validate_run_binding
from app.orders.contracts import CatalogResolution
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.ingestion import (
    ObservedOrderItem,
    OrderManifest,
    OrderObservation,
    OrderPage,
    _instant,
    _text,
    compare_observations,
)
from app.orders.projection_repository import OrdersProjectionRepository
from app.orders.publication_service import _event, _status
from app.orders.serialization import deserialize_observation, observation_checksum
from app.platform.integrations.publication_guard import (
    ExpectedCredential,
    PublicationGuardError,
    acquire_publication_guard,
)

POLICY_VERSION = "avito-known-order-status-v1"


def _source_time(value):
    _text(value)
    try:
        result = datetime.fromisoformat(value)
        _instant(result)
        return result.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise OrderContractValidationError("avito_source_time_invalid") from None


def source_descriptor(row: OrderObservation) -> dict:
    """The existing opaque field carries application evidence, never a remote version."""
    try:
        value = json.loads(row.source_revision)
        if (
            type(value) is not dict
            or set(value) != {"policy", "source_updated_at", "nonstatus_sha256"}
            or value["policy"] != POLICY_VERSION
            or row.adapter_version != POLICY_VERSION
            or row.source_kind != "avito-order-management"
            or row.identity.marketplace != "avito"
            or type(value["nonstatus_sha256"]) is not str
            or len(value["nonstatus_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["nonstatus_sha256"])
            or _source_time(value["source_updated_at"]) != row.effective_at
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise OrderContractValidationError("avito_source_baseline_missing") from None
    return value


def normalize_avito_order(
    body, *, identity: ExternalOrderIdentity, observed_at
) -> OrderObservation:
    """One explicit ID response. Terminal traversal is not account completeness."""
    _instant(observed_at)
    if (
        not isinstance(identity, ExternalOrderIdentity)
        or identity.marketplace != "avito"
        or type(body) is not dict
        or body.get("hasMore") is not False
        or type(body.get("orders")) is not list
        or len(body["orders"]) != 1
    ):
        raise OrderContractValidationError("avito_source_scope_incomplete")
    raw = body["orders"][0]
    if (
        type(raw) is not dict
        or type(raw.get("id")) is not str
        or raw["id"] != identity.external_order_id
        or type(raw.get("items")) is not list
        or not raw["items"]
        or any(
            type(raw.get(key)) is not dict
            for key in ("prices", "delivery", "schedules")
        )
    ):
        raise OrderContractValidationError("avito_source_order_invalid")
    _source_time(raw.get("createdAt"))
    effective = _source_time(raw.get("updatedAt"))
    if effective > observed_at.astimezone(UTC):
        raise OrderContractValidationError("avito_source_time_in_future")
    items, seen = [], set()
    for item in raw["items"]:
        if type(item) is not dict or type(item.get("prices")) is not dict:
            raise OrderContractValidationError("avito_source_item_invalid")
        external = item.get("avitoId")
        _text(external)
        _text(item.get("title"))
        if external in seen:
            raise OrderContractValidationError("avito_source_line_identity_unproven")
        seen.add(external)
        items.append(
            ObservedOrderItem(
                ExternalOrderItemIdentity(
                    identity,
                    make_avito_source_line_key(identity.external_order_id, external, 0),
                    external,
                    0,
                ),
                item.get("count"),
            )
        )
    try:
        nonstatus = json.dumps(
            {
                key: value
                for key, value in raw.items()
                if key not in {"status", "updatedAt"}
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise OrderContractValidationError("avito_source_order_invalid") from None
    descriptor = json.dumps(
        {
            "policy": POLICY_VERSION,
            "source_updated_at": raw["updatedAt"],
            "nonstatus_sha256": hashlib.sha256(nonstatus.encode("ascii")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return OrderObservation(
        identity,
        "avito-order-management",
        POLICY_VERSION,
        descriptor,
        effective,
        observed_at,
        map_avito_status(raw.get("status")),
        tuple(items),
    )


@dataclass(frozen=True, slots=True)
class AvitoStatusRefreshResult:
    state: str
    run_id: int
    reason: str | None = None


def production_work_exists(session, organization_id, account_id, order_id):
    """Read existing work only; no Production workflow is created or changed."""
    set_marketplace_account_context(
        session, organization_id=organization_id, marketplace_account_id=account_id
    )
    if (
        session.execute(
            text("SELECT to_regclass('public.production_work_items')")
        ).scalar_one()
        is None
    ):
        return False
    return session.execute(
        text("""SELECT EXISTS(SELECT 1 FROM production_work_items
        WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order)"""),
        {"org": organization_id, "account": account_id, "order": order_id},
    ).scalar_one()


def _current(session, principal, account, external):
    params = {
        "org": principal.organization_id,
        "account": account.marketplace_account_id,
        "external": external,
    }
    parent = (
        session.execute(
            text("""SELECT o.order_id,o.version,o.last_seen_sync_run_id,
        e.observation_id,e.normalized_evidence,e.payload_checksum FROM marketplace_orders o
        JOIN order_sync_memberships m ON (m.organization_id,m.marketplace_account_id,m.order_id,m.sync_run_id)=
        (o.organization_id,o.marketplace_account_id,o.order_id,o.last_seen_sync_run_id) AND m.order_item_id IS NULL
        JOIN order_observations e ON (e.organization_id,e.marketplace_account_id,e.observation_id)=
        (m.organization_id,m.marketplace_account_id,m.observation_id)
        WHERE o.organization_id=:org AND o.marketplace_account_id=:account AND o.external_order_id COLLATE "C"=:external
        FOR UPDATE OF o"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if parent is None:
        raise OrderContractValidationError("avito_known_order_missing")
    params.update(order=parent["order_id"], run=parent["last_seen_sync_run_id"])
    binding = (
        session.execute(
            text("""SELECT * FROM order_sync_runs WHERE organization_id=:org
        AND marketplace_account_id=:account AND sync_run_id=:run AND state='complete'
        AND manifest_state='complete' FOR SHARE"""),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if binding is None:
        raise OrderContractValidationError("avito_source_baseline_missing")
    validate_run_binding(
        principal.organization_id,
        account,
        schema_version=binding["account_binding_schema_version"],
        external_account_id=binding["account_binding_external_account_id"],
        credential_ref=binding["account_binding_credential_ref"],
        payload=binding["account_binding_payload"],
        checksum=binding["account_binding_checksum"],
    )
    row = deserialize_observation(parent["normalized_evidence"])
    if (
        row.identity.organization_id,
        row.identity.marketplace_account_id,
        row.identity.external_order_id,
        row.identity.marketplace,
        observation_checksum(row),
    ) != (
        principal.organization_id,
        account.marketplace_account_id,
        external,
        "avito",
        parent["payload_checksum"],
    ):
        raise OrderContractValidationError("avito_source_baseline_missing")
    items = (
        session.execute(
            text("""SELECT * FROM marketplace_order_items WHERE organization_id=:org
        AND marketplace_account_id=:account AND order_id=:order ORDER BY source_line_key COLLATE "C" FOR UPDATE"""),
            params,
        )
        .mappings()
        .all()
    )
    expected = {i.identity.source_line_key: i for i in row.items}
    if set(expected) != {i["source_line_key"] for i in items} or any(
        (i["external_item_id"], i["occurrence_index"], i["quantity"])
        != (
            expected[i["source_line_key"]].identity.external_item_id,
            expected[i["source_line_key"]].identity.occurrence_index,
            expected[i["source_line_key"]].quantity,
        )
        for i in items
    ):
        raise OrderContractValidationError("avito_current_item_evidence_changed")
    signature = (
        parent["version"],
        parent["last_seen_sync_run_id"],
        parent["observation_id"],
        tuple(
            tuple(
                i[key]
                for key in (
                    "order_item_id",
                    "version",
                    "quantity",
                    "catalog_sku_id",
                    "marketplace_product_id",
                    "marketplace_offer_id",
                    "resolution_state",
                    "resolution_version",
                )
            )
            for i in items
        ),
    )
    return parent, row, items, signature


def _guard(session, principal, account, authorities):
    guard = acquire_publication_guard(
        session,
        principal=principal,
        required_permissions=frozenset({"sync:run", "cabinet:read"}),
        accounts=(account,),
        authorities=authorities,
    )
    set_marketplace_account_context(
        session,
        organization_id=principal.organization_id,
        marketplace_account_id=account.marketplace_account_id,
    )
    return guard


def _reconcile(session, principal, account, parent, reason):
    session.execute(
        text("""INSERT INTO order_lifecycle_events
        (organization_id,marketplace_account_id,order_id,observation_id,source_event_key,event_kind,evidence,observed_at)
        VALUES (:org,:account,:order,:observation,:key,'reconciliation_required',CAST(:evidence AS jsonb),clock_timestamp())"""),
        {
            "org": principal.organization_id,
            "account": account.marketplace_account_id,
            "order": parent["order_id"],
            "observation": parent["observation_id"],
            "key": POLICY_VERSION + ":" + uuid4().hex,
            "evidence": json.dumps({"policy": POLICY_VERSION, "reason": reason}),
        },
    )
    return AvitoStatusRefreshResult(
        "reconciliation_required", parent["last_seen_sync_run_id"], reason
    )


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def refresh_avito_order_status(
    session, *, principal, account, authorities, external_order_id, client
):
    """One injected, credential-bound HTTP client; never retries or activates actions.

    The calling integration must bind the client's authentication to authorities.
    This service checks live metadata, not the client's secret material. Production
    activation and acquisition of actual credentials remain outside this module.
    """
    if not isinstance(session, Session) or session.in_transaction():
        raise PublicationGuardError("publication_context_invalid")
    _text(external_order_id)
    if (
        account.provider != "avito"
        or type(authorities) is not tuple
        or not authorities
        or not all(
            isinstance(a, ExpectedCredential)
            and a.marketplace_account_id == account.marketplace_account_id
            and a.kind in {"avito_oauth_client", "avito_oauth_access"}
            for a in authorities
        )
    ):
        raise PublicationGuardError("publication_authority_invalid")
    try:
        with session.begin():
            guard = _guard(session, principal, account, authorities)
            before = _current(session, principal, account, external_order_id)
            reason = None
            try:
                source_descriptor(before[1])
            except OrderContractValidationError:
                reason = "avito_source_baseline_missing"
            if production_work_exists(
                session,
                principal.organization_id,
                account.marketplace_account_id,
                before[0]["order_id"],
            ):
                reason = "avito_status_refresh_existing_work_item"
            blocked = (
                _reconcile(session, principal, account, before[0], reason)
                if reason
                else None
            )
            guard.revalidate_before_write()
        if blocked is not None:
            return blocked
        body, fetch_error = None, None
        try:
            response = client.get(
                "https://api.avito.ru/order-management/1/orders",
                params={"ids": external_order_id, "page": 1, "limit": 20},
            )
            response.raise_for_status()
            body = json.loads(response.content, object_pairs_hook=_json_object)
        except (httpx.HTTPError, ValueError, RecursionError):
            fetch_error = "avito_source_fetch_invalid"
        with session.begin():
            guard = _guard(session, principal, account, authorities)
            current = _current(session, principal, account, external_order_id)
            parent, old, _items, signature = current
            reason = fetch_error
            incoming = None
            if reason is None:
                try:
                    incoming = normalize_avito_order(
                        body,
                        identity=old.identity,
                        observed_at=session.execute(
                            text("SELECT clock_timestamp()")
                        ).scalar_one(),
                    )
                except OrderContractValidationError as exc:
                    reason = (
                        str(exc)
                        if str(exc).startswith("avito_")
                        else "avito_source_order_invalid"
                    )
            if signature != before[3]:
                reason = "avito_current_changed_during_fetch"
            if production_work_exists(
                session,
                principal.organization_id,
                account.marketplace_account_id,
                parent["order_id"],
            ):
                reason = "avito_status_refresh_existing_work_item"
            if reason is None:
                if compare_observations(old, incoming) == "replay":
                    result = AvitoStatusRefreshResult(
                        "replay", parent["last_seen_sync_run_id"]
                    )
                elif (
                    source_descriptor(old)["nonstatus_sha256"]
                    != source_descriptor(incoming)["nonstatus_sha256"]
                    or old.items != incoming.items
                ):
                    reason = "avito_nonstatus_facts_changed"
                elif incoming.effective_at <= old.effective_at:
                    reason = "avito_source_time_not_newer"
                else:
                    result = _persist_refresh(
                        session, principal, account, current, incoming
                    )
            if reason is not None:
                result = (
                    _persist_refresh(
                        session, principal, account, current, incoming, reason=reason
                    )
                    if incoming is not None
                    else _reconcile(session, principal, account, parent, reason)
                )
            guard.revalidate_before_write()
        return result
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None


def _persist_refresh(session, principal, account, current, incoming, *, reason=None):
    parent, _, items, _ = current
    snapshot = POLICY_VERSION + ":" + uuid4().hex
    manifest = OrderManifest(
        principal.organization_id,
        account.marketplace_account_id,
        "avito",
        incoming.source_kind,
        incoming.adapter_version,
        POLICY_VERSION,
        snapshot,
        "complete" if reason is None else "partial",
        1,
        (OrderPage(1, snapshot, True, (incoming,)),),
    )
    binding = serialize_account_bindings(principal.organization_id, (account,))
    params = {
        "org": principal.organization_id,
        "account": account.marketplace_account_id,
        "key": snapshot,
        "policy": POLICY_VERSION,
        "mapping": incoming.status.mapping_version,
        "binding": binding,
        "checksum": hashlib.sha256(binding).hexdigest(),
        "external": account.external_account_id,
        "ref": account.credential_ref,
    }
    run = session.execute(
        text("""INSERT INTO order_sync_runs
        (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,adapter_version,mapping_version,
         source_contract_version,source_snapshot,account_binding_schema_version,account_binding_external_account_id,
         account_binding_credential_ref,account_binding_payload,account_binding_checksum)
        VALUES (:org,:account,'avito','avito-order-management',:key,:policy,:mapping,:policy,:key,1,:external,:ref,:binding,:checksum)
        RETURNING sync_run_id"""),
        params,
    ).scalar_one()
    params["run"] = run
    record = OrdersEvidenceRepository(
        session, principal.organization_id, account.marketplace_account_id
    ).append(run, incoming)
    _status(session, record, params)
    if incoming.status.canonical_status in {"cancelled", "returning", "returned"}:
        _event(
            session,
            record,
            params,
            "cancellation"
            if incoming.status.canonical_status == "cancelled"
            else "return",
        )
    if reason is None:
        projection = OrdersProjectionRepository(
            session, principal.organization_id, account.marketplace_account_id
        )
        projection.set_parent(
            run, record.observation_id, expected_version=parent["version"]
        )
        for item in items:
            resolution = CatalogResolution(
                item["resolution_state"],
                item["marketplace_product_id"],
                item["marketplace_offer_id"],
                item["catalog_sku_id"],
                item["resolution_version"],
            )
            projection.set_item(
                run,
                record.observation_id,
                item["source_line_key"],
                resolution=resolution,
                expected_version=item["version"],
            )
        session.execute(
            text("""INSERT INTO order_lifecycle_events
            (organization_id,marketplace_account_id,order_id,observation_id,source_event_key,event_kind,evidence,observed_at)
            VALUES (:org,:account,:order,:observation,:key,'status_changed',CAST(:evidence AS jsonb),clock_timestamp())"""),
            dict(
                params,
                order=parent["order_id"],
                observation=record.observation_id,
                key=POLICY_VERSION + ":accepted",
                evidence=json.dumps(
                    {
                        "policy": POLICY_VERSION,
                        "previous_observation_id": parent["observation_id"],
                        "previous_version": parent["version"],
                    }
                ),
            ),
        )
    else:
        _reconcile(
            session,
            principal,
            account,
            dict(
                parent, observation_id=record.observation_id, last_seen_sync_run_id=run
            ),
            reason,
        )
    params.update(
        manifest=manifest.checksum,
        items=len(incoming.items),
        state="complete" if reason is None else "partial",
    )
    session.execute(
        text("""INSERT INTO order_sync_coverage
        (organization_id,marketplace_account_id,sync_run_id,coverage_kind,first_page,last_page,is_complete,manifest_checksum,completed_at)
        VALUES (:org,:account,:run,:policy,1,1,false,:manifest,clock_timestamp())"""),
        params,
    )
    session.execute(
        text("""UPDATE order_sync_runs SET state=:state,manifest_state=:state,page_count=1,
        order_count=1,expected_order_count=1,item_count=:items,payload_checksum=:manifest,completed_at=clock_timestamp()
        WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
        params,
    )
    session.add(
        LkAuditEventRow(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            action="orders.avito_status_refreshed",
            object_type="orders_sync_run",
            object_id=str(run),
            details={
                "policy": POLICY_VERSION,
                "coverage": "single_order_partial_account",
                "previous_version": parent["version"],
                "result": "updated" if reason is None else "reconciliation_required",
                "reason": reason,
            },
        )
    )
    return AvitoStatusRefreshResult(
        "updated" if reason is None else "reconciliation_required", run, reason
    )
