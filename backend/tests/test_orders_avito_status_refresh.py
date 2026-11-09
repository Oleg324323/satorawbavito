"""Actual guarded Orders revision flow; fake HTTP and disposable PostgreSQL only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infra.db import set_marketplace_account_context
from app.modules.orders import ExternalOrderIdentity
from app.orders.avito_status_refresh import (
    normalize_avito_order,
    refresh_avito_order_status,
)
from app.orders.read_assembly import freeze_orders_view
from app.orders.read_service import read_orders_snapshot
from app.orders.serialization import deserialize_observation
from app.platform.integrations.publication_guard import PublicationGuardError
from tests.test_orders_avito_status_source import NOW, source_body
from tests.test_orders_publication_service import (  # noqa: F401
    authority,
    manifest,
    prepared,
    publish,
)
from tests.test_orders_read_service import ACCOUNTS, read_db  # noqa: F401
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401


@pytest.fixture
def known(authority):  # noqa: F811
    owner, runtime, principal, credential = authority
    body = source_body()
    external = "000-synthetic-" + uuid4().hex
    body["orders"][0]["id"] = external
    identity = ExternalOrderIdentity(91001, 91101, "avito", external)
    row = normalize_avito_order(body, identity=identity, observed_at=NOW)
    with Session(runtime) as session:
        initial = publish(
            session, principal, credential, manifest(row), "synthetic-" + uuid4().hex
        )
    return owner, runtime, principal, credential, identity, body, initial


def refresh(known, body, during_fetch=None):
    _, runtime, principal, credential, identity, _, _ = known
    calls = []
    with Session(runtime) as session:

        def respond(request):
            assert not session.in_transaction()
            assert (
                str(request.url.copy_with(query=None))
                == "https://api.avito.ru/order-management/1/orders"
            )
            assert dict(request.url.params) == {
                "ids": identity.external_order_id,
                "page": "1",
                "limit": "20",
            }
            assert request.method == "GET"
            calls.append(request)
            if during_fetch is not None:
                during_fetch()
            return httpx.Response(200, json=body)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result = refresh_avito_order_status(
                session,
                principal=principal,
                account=ACCOUNTS[0],
                authorities=(credential,),
                external_order_id=identity.external_order_id,
                client=client,
            )
        assert not session.in_transaction()
    return result, calls


def view(known, run):
    _, runtime, principal, _, identity, _, _ = known
    with Session(runtime) as session:
        snapshot = freeze_orders_view(
            session,
            principal=principal,
            accounts=ACCOUNTS,
            coverage_run_ids=(run,),
            query_checksum="a" * 64,
        )
        result = read_orders_snapshot(
            session,
            principal=principal,
            accounts=ACCOUNTS,
            snapshot_id=snapshot,
            query_checksum="a" * 64,
        )
    return result, next(
        row for row in result.rows if row.observation.identity == identity
    )


def test_fake_source_revision_is_visible_but_old_snapshot_is_immutable(known):
    _, runtime, principal, _, _, body, initial = known
    old_snapshot, old = view(known, initial.run_id)
    changed = deepcopy(body)
    changed["orders"][0].update(status="in_transit", updatedAt="2026-09-09T01:00:00Z")
    result, calls = refresh(known, changed)
    assert result.state == "updated" and len(calls) == 1
    latest, row = view(known, result.run_id)
    assert row.observation.status.canonical_status == "in_delivery"
    assert row.row_version == old.row_version + 1
    assert latest.coverage_state == "partial"
    assert "application_freshness_not_provider_chronology" in row.readiness_blockers
    with Session(runtime) as session:
        historical = read_orders_snapshot(
            session,
            principal=principal,
            accounts=ACCOUNTS,
            snapshot_id=old_snapshot.snapshot_id,
            query_checksum="a" * 64,
        )
    assert historical.rows == old_snapshot.rows
    replay, calls = refresh(known, changed)
    assert (
        replay.state == "replay" and replay.run_id == result.run_id and len(calls) == 1
    )
    _, replay_row = view(known, replay.run_id)
    assert replay_row.row_version == row.row_version


@pytest.mark.parametrize(
    "change",
    [
        "equal_changed",
        "older",
        "future",
        "invalid",
        "missing_time",
        "amount",
        "quantity",
        "partial",
        "missing_order",
        "different_order",
        "repeated_listing",
    ],
)
def test_unproven_changes_reconcile_without_promoting(known, change):
    *_, body, initial = known
    changed = deepcopy(body)
    raw = changed["orders"][0]
    raw.update(status="delivered", updatedAt="2026-09-09T01:00:00Z")
    if change in {"equal_changed", "older", "future", "invalid", "missing_time"}:
        raw["updatedAt"] = {
            "equal_changed": "2026-09-09T00:00:00Z",
            "older": "2026-09-08T01:00:00Z",
            "future": "2099-01-01T00:00:00Z",
            "invalid": "synthetic-invalid",
            "missing_time": None,
        }[change]
    elif change == "amount":
        raw["prices"]["total"] += 1
    elif change == "quantity":
        raw["items"][0]["count"] += 1
    elif change == "partial":
        changed["hasMore"] = True
    elif change == "missing_order":
        changed["orders"] = []
    elif change == "different_order":
        raw["id"] = "synthetic-other"
    else:
        raw["items"].append(deepcopy(raw["items"][0]))
    result, calls = refresh(known, changed)
    assert result.state == "reconciliation_required" and len(calls) == 1
    _, row = view(known, initial.run_id)
    assert row.observation.status.canonical_status == "pending_confirmation"
    assert "avito_status_refresh_reconciliation_required" in row.readiness_blockers


def test_unknown_status_remains_raw_and_unmapped(known):
    *_, body, _ = known
    changed = deepcopy(body)
    changed["orders"][0].update(
        status="synthetic-unknown", updatedAt="2026-09-09T01:00:00Z"
    )
    result, _ = refresh(known, changed)
    _, row = view(known, result.run_id)
    assert row.observation.status.raw_status == "synthetic-unknown"
    assert row.observation.status.canonical_status is None
    assert row.observation.status.mapping_state == "unmapped"


def test_concurrent_projection_change_is_not_blindly_retried(known):
    owner, _, _, _, identity, body, initial = known
    changed = deepcopy(body)
    changed["orders"][0].update(status="delivered", updatedAt="2026-09-09T01:00:00Z")

    def change_current():
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_orders SET version=version+1 WHERE external_order_id=:id"
                ),
                {"id": identity.external_order_id},
            )

    result, calls = refresh(known, changed, change_current)
    assert len(calls) == 1 and result.reason == "avito_current_changed_during_fetch"
    _, row = view(known, initial.run_id)
    assert row.observation.status.canonical_status == "pending_confirmation"


def test_revocation_during_fetch_denies_publication(known):
    owner, _, _, credential, _, body, _ = known

    def revoke():
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_account_credentials SET revoked_at=clock_timestamp(),"
                    "revocation_reason_code='operator_revoked' WHERE credential_id=:id"
                ),
                {"id": credential.credential_id},
            )

    with pytest.raises(PublicationGuardError):
        refresh(known, body, revoke)


def test_existing_work_item_blocks_fetch_and_is_explicit_in_read(known):
    _, runtime, _, _, identity, body, initial = known
    with Session(runtime) as session, session.begin():
        set_marketplace_account_context(
            session, organization_id=91001, marketplace_account_id=91101
        )
        session.execute(
            text(
                "SELECT marketplace_account_id FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE"
            )
        )
        session.execute(
            text("""INSERT INTO production_work_items
            (organization_id,marketplace_account_id,order_id,order_item_id,source_item_version,required_quantity)
            SELECT i.organization_id,i.marketplace_account_id,i.order_id,i.order_item_id,i.version,i.quantity
            FROM marketplace_order_items i JOIN marketplace_orders o ON o.order_id=i.order_id
            WHERE o.external_order_id=:external AND i.organization_id=91001 AND i.marketplace_account_id=91101"""),
            {"external": identity.external_order_id},
        )
    result, calls = refresh(known, body)
    assert result.reason == "avito_status_refresh_existing_work_item" and calls == []
    _, row = view(known, initial.run_id)
    assert "avito_status_refresh_existing_work_item" in row.readiness_blockers


def test_account_binding_change_during_fetch_denies_return(known):
    owner, _, _, _, _, body, _ = known

    def rebind():
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
                )
            )

    try:
        with pytest.raises(PublicationGuardError):
            refresh(known, body, rebind)
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                )
            )


def test_rejected_valid_observation_is_durable_without_changing_projection(known):
    _, runtime, _, _, identity, body, initial = known
    changed = deepcopy(body)
    changed["orders"][0]["status"] = "delivered"
    # Equal source time with changed status must be retained, never promoted.
    result, _ = refresh(known, changed)
    assert result.state == "reconciliation_required"
    with Session(runtime) as session, session.begin():
        scope(session)
        values = (
            session.execute(
                text("""SELECT e.normalized_evidence FROM order_observations e
            JOIN marketplace_orders o ON o.order_id=e.order_id AND o.organization_id=e.organization_id
            WHERE o.external_order_id=:external AND e.order_item_id IS NULL"""),
                {"external": identity.external_order_id},
            )
            .scalars()
            .all()
        )
    assert any(
        deserialize_observation(value).status.raw_status == "delivered"
        for value in values
    )
    _, current = view(known, initial.run_id)
    assert current.observation.status.raw_status == "on_confirmation"


def test_ordinary_conflicting_publication_is_not_hidden_by_status_policy(known):
    _, runtime, principal, credential, identity, body, _ = known
    changed = deepcopy(body)
    changed["orders"][0].update(status="in_transit", updatedAt="2026-09-09T01:00:00Z")
    accepted, _ = refresh(known, changed)
    _, current = view(known, accepted.run_id)
    assert "source_reconciliation_required" not in current.readiness_blockers
    changed["orders"][0].update(status="delivered", updatedAt="2026-09-09T02:00:00Z")
    changed["orders"][0]["prices"]["total"] += 1
    incoming = normalize_avito_order(changed, identity=identity, observed_at=NOW)
    with Session(runtime) as session:
        conflicting = publish(
            session,
            principal,
            credential,
            manifest(incoming),
            "synthetic-conflict-" + uuid4().hex,
        )
    assert conflicting.reconciliation_count == 1
    _, current = view(known, accepted.run_id)
    assert current.observation.status.raw_status == "in_transit"
    assert "source_reconciliation_required" in current.readiness_blockers


def test_two_inflight_fetches_only_one_cas_can_advance(known):
    *_, body, _ = known
    barrier = Barrier(2)
    first, second = deepcopy(body), deepcopy(body)
    first["orders"][0].update(status="in_transit", updatedAt="2026-09-09T01:00:00Z")
    second["orders"][0].update(status="delivered", updatedAt="2026-09-09T02:00:00Z")
    # Both independent Sessions release their capture transaction before either fetch completes.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(refresh, known, value, lambda: barrier.wait(timeout=10))
            for value in (first, second)
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result.state for result, _ in results) == [
        "reconciliation_required",
        "updated",
    ]
    assert all(len(calls) == 1 for _, calls in results)
    denied = next(
        result for result, _ in results if result.state == "reconciliation_required"
    )
    assert denied.reason == "avito_current_changed_during_fetch"
