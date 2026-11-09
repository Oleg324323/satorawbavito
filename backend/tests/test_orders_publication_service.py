from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    make_wb_source_line_key,
    map_wb_statistics_status,
)
from app.orders.bindings import serialize_account_bindings
from app.orders.ingestion import (
    ObservedOrderItem,
    OrderManifest,
    OrderObservation,
    OrderPage,
)
from app.orders.publication_service import publish_orders_manifest
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountRow,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    ExpectedCredential,
    PublicationGuardError,
)
from tests.test_orders_ingestion import NOW
from tests.test_orders_projection_repository import item_fact
from tests.test_orders_read_service import ACCOUNTS, read_db  # noqa: F401
from tests.test_orders_read_service import prepared as _prepared_fixture
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401

prepared = _prepared_fixture


@pytest.fixture
def authority(prepared):
    owner, runtime, principal, _ = prepared
    credential_id = uuid4()
    with Session(owner) as session, session.begin():
        session.execute(
            text(
                'UPDATE iam_memberships SET permissions=\'["cabinet:read","sync:run"]\' WHERE membership_id=:id'
            ),
            {"id": principal.membership_id},
        )
        # Unusable synthetic ciphertext: the guard inspects metadata, never decrypts.
        session.add(
            MarketplaceAccountCredentialRow(
                credential_id=credential_id,
                organization_id=91001,
                marketplace_account_id=91101,
                provider="avito",
                credential_kind="avito_oauth_client",
                key_version=1,
                nonce=uuid4().bytes[:12],
                ciphertext=b"synthetic-not-a-real-ciphertext",
                generation=1,
            )
        )
    yield (
        owner,
        runtime,
        principal,
        ExpectedCredential(91101, credential_id, "avito_oauth_client", 1, 1, None),
    )
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_account_credentials SET revoked_at=clock_timestamp(),revocation_reason_code='operator_revoked' WHERE credential_id=:id"
            ),
            {"id": credential_id},
        )


def manifest(row, *, complete=True):
    return OrderManifest(
        91001,
        91101,
        "avito",
        row.source_kind,
        row.adapter_version,
        "synthetic-contract-v1",
        "synthetic-snapshot",
        "complete" if complete else "partial",
        1 if complete else 2,
        (OrderPage(1, "synthetic-snapshot", complete, (row,)),),
    )


def publish(session, principal, credential, value, key):
    return publish_orders_manifest(
        session,
        principal=principal,
        account=ACCOUNTS[0],
        authorities=(credential,),
        manifest=value,
        source_run_key=key,
    )


def test_complete_manifest_commits_once_with_status_items_coverage_audit(authority):
    _, runtime, principal, credential = authority
    row, key = item_fact(), "synthetic-" + uuid4().hex
    with Session(runtime) as session:
        first = publish(session, principal, credential, manifest(row), key)
        replay = publish(session, principal, credential, manifest(row), key)
        assert first.run_id == replay.run_id and not first.replayed and replay.replayed
        assert not session.in_transaction()
        with session.begin():
            scope(session)
            state = session.execute(
                text(
                    "SELECT state,manifest_state,order_count,item_count FROM order_sync_runs WHERE sync_run_id=:id"
                ),
                {"id": first.run_id},
            ).one()
            assert tuple(state) == ("complete", "complete", 1, 2)
            binding = session.execute(
                text(
                    "SELECT account_binding_schema_version, account_binding_payload "
                    "FROM order_sync_runs WHERE sync_run_id=:id"
                ),
                {"id": first.run_id},
            ).one()
            assert tuple(binding) == (1, serialize_account_bindings(91001, ACCOUNTS))
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM order_sync_coverage WHERE sync_run_id=:id AND is_complete"
                    ),
                    {"id": first.run_id},
                ).scalar_one()
                == 1
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM lk_audit_events WHERE object_type='orders_sync_run' AND object_id=:id"
                    ),
                    {"id": str(first.run_id)},
                ).scalar_one()
                == 1
            )


def test_partial_manifest_keeps_observations_without_current_projection(authority):
    _, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        result = publish(
            session,
            principal,
            credential,
            manifest(row, complete=False),
            "synthetic-" + uuid4().hex,
        )
        with session.begin():
            scope(session)
            record = session.execute(
                text(
                    "SELECT version,last_seen_sync_run_id FROM marketplace_orders WHERE external_order_id=:id"
                ),
                {"id": row.identity.external_order_id},
            ).one()
            assert tuple(record) == (1, None)
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM order_observations WHERE sync_run_id=:id"
                    ),
                    {"id": result.run_id},
                ).scalar_one()
                == 1
            )


def test_changed_fact_requires_reconciliation_without_ordering_authority(authority):
    _, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        publish(
            session, principal, credential, manifest(row), "synthetic-" + uuid4().hex
        )
        changed = replace(row, items=(replace(row.items[0], quantity=5), row.items[1]))
        result = publish(
            session,
            principal,
            credential,
            manifest(changed),
            "synthetic-" + uuid4().hex,
        )
        assert result.reconciliation_count == 1
        with session.begin():
            scope(session)
            assert (
                session.execute(
                    text(
                        "SELECT version FROM marketplace_orders WHERE external_order_id=:id"
                    ),
                    {"id": row.identity.external_order_id},
                ).scalar_one()
                == 2
            )


def test_fetched_publication_cannot_omit_authority(authority):
    _, runtime, principal, _ = authority
    with Session(runtime) as session, pytest.raises(PublicationGuardError):
        publish_orders_manifest(
            session,
            principal=principal,
            account=ACCOUNTS[0],
            authorities=(),
            manifest=manifest(item_fact()),
            source_run_key="synthetic-empty-authority",
        )


def test_initial_publication_uses_exact_existing_catalog_link(authority):
    from tests.test_orders_catalog_resolution import seed

    _, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        _, _, sku = seed(session, 91101, row.items[0].identity.external_item_id)
    with Session(runtime) as session:
        result = publish(
            session, principal, credential, manifest(row), "synthetic-" + uuid4().hex
        )
        with session.begin():
            scope(session)
            resolved = session.execute(
                text(
                    "SELECT i.catalog_sku_id,i.resolution_state FROM marketplace_order_items i JOIN marketplace_orders o USING (organization_id,marketplace_account_id,order_id) WHERE o.last_seen_sync_run_id=:id"
                ),
                {"id": result.run_id},
            ).all()
            assert [tuple(value) for value in resolved] == [
                (sku, "resolved"),
                (sku, "resolved"),
            ]


def test_wb_statistics_publication_does_not_invent_fulfillment_status(prepared):
    owner, runtime, principal, _ = prepared
    credential_id = uuid4()
    with Session(owner) as session, session.begin():
        account = MarketplaceAccountRow(
            organization_id=91001,
            marketplace="wb",
            external_account_id="synthetic-wb-" + uuid4().hex,
            status="connected",
        )
        session.add(account)
        session.flush()
        account_id = account.marketplace_account_id
        binding = ExpectedAccountBinding(
            account_id, "wb", account.external_account_id, None
        )
        session.execute(
            text(
                "UPDATE iam_memberships SET permissions='[\"sync:run\"]',scope_mode='all' WHERE membership_id=:id"
            ),
            {"id": principal.membership_id},
        )
        session.add(
            MarketplaceAccountCredentialRow(
                credential_id=credential_id,
                organization_id=91001,
                marketplace_account_id=account_id,
                provider="wb",
                credential_kind="wb_api",
                key_version=1,
                nonce=uuid4().bytes[:12],
                ciphertext=b"synthetic-not-a-real-ciphertext",
                generation=1,
            )
        )
    identity = ExternalOrderIdentity(
        91001, account_id, "wb", "synthetic-order-" + uuid4().hex
    )
    unit = "synthetic-srid-unit"
    item = ObservedOrderItem(
        ExternalOrderItemIdentity(identity, make_wb_source_line_key(unit), None, 0),
        2,
        stable_unit_id=unit,
    )
    row = OrderObservation(
        identity,
        "wb-statistics-supplier-orders",
        "synthetic-v1",
        None,
        None,
        NOW,
        map_wb_statistics_status(None, False, False),
        (item,),
        False,
        False,
    )
    value = OrderManifest(
        91001,
        account_id,
        "wb",
        row.source_kind,
        row.adapter_version,
        "synthetic-v1",
        "synthetic-snapshot",
        "complete",
        1,
        (OrderPage(1, "synthetic-snapshot", True, (row,)),),
    )
    with Session(runtime) as session:
        result = publish_orders_manifest(
            session,
            principal=principal,
            account=binding,
            authorities=(
                ExpectedCredential(account_id, credential_id, "wb_api", 1, 1, None),
            ),
            manifest=value,
            source_run_key="synthetic-" + uuid4().hex,
        )
        with session.begin():
            scope(session)
            record = session.execute(
                text(
                    "SELECT raw_status,canonical_status,mapping_state FROM marketplace_orders WHERE last_seen_sync_run_id=:id"
                ),
                {"id": result.run_id},
            ).one()
            assert tuple(record) == (None, None, "unmapped")


def test_two_sessions_publish_one_manifest_and_one_audit(authority):
    _, runtime, principal, credential = authority
    row, key = item_fact(), "synthetic-" + uuid4().hex
    barrier = Barrier(2)

    def writer(_):
        with Session(runtime) as session:
            pids = []

            def started(session, transaction, connection):
                pids.append(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                )
                barrier.wait(timeout=5)

            event.listen(session, "after_begin", started)
            result = publish(session, principal, credential, manifest(row), key)
            return pids[0], result

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(writer, range(2)))
    assert results[0][0] != results[1][0]
    assert results[0][1].run_id == results[1][1].run_id
    assert sorted(result.replayed for _, result in results) == [False, True]
    with Session(runtime) as session, session.begin():
        scope(session)
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM lk_audit_events WHERE object_type='orders_sync_run' AND object_id=:id"
                ),
                {"id": str(results[0][1].run_id)},
            ).scalar_one()
            == 1
        )


def test_same_run_key_changed_manifest_conflicts(authority):
    _, runtime, principal, credential = authority
    row, key = item_fact(), "synthetic-" + uuid4().hex
    with Session(runtime) as session:
        publish(session, principal, credential, manifest(row), key)
        with pytest.raises(ValueError, match="replay conflict"):
            publish(
                session,
                principal,
                credential,
                manifest(replace(row, source_revision="synthetic-new")),
                key,
            )


def test_credential_generation_changed_after_fetch_cannot_publish(authority):
    owner, runtime, principal, credential = authority
    row, key = item_fact(), "synthetic-" + uuid4().hex
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_account_credentials SET generation=generation+1 WHERE credential_id=:id"
            ),
            {"id": credential.credential_id},
        )
    with Session(runtime) as session, pytest.raises(PublicationGuardError):
        publish(session, principal, credential, manifest(row), key)
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": key},
            ).scalar_one()
            == 0
        )


def test_final_authorization_failure_rolls_back_every_publication_effect(authority):
    owner, runtime, principal, credential = authority
    row, key = item_fact(), "synthetic-" + uuid4().hex
    with Session(runtime) as session:

        def remove_permission(session):
            session.execute(
                text(
                    "UPDATE iam_memberships SET permissions='[]' WHERE membership_id=:id"
                ),
                {"id": principal.membership_id},
            )

        event.listen(session, "before_commit", remove_permission)
        with pytest.raises(PublicationGuardError):
            publish(session, principal, credential, manifest(row), key)
        assert not session.in_transaction()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": key},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM marketplace_orders WHERE external_order_id=:id"
                ),
                {"id": row.identity.external_order_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM lk_audit_events WHERE actor_user_id=:id AND action='orders.manifest_published'"
                ),
                {"id": principal.user_id},
            ).scalar_one()
            == 0
        )
