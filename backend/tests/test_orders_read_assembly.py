from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import map_avito_status
from app.orders.read_assembly import freeze_orders_view
from app.orders.read_service import read_orders_snapshot
from app.platform.integrations.publication_guard import ExpectedAccountBinding
from tests.test_orders_projection_repository import item_fact
from tests.test_orders_publication_service import (
    authority as _authority_fixture,
)
from tests.test_orders_publication_service import (
    manifest,
    publish,
)
from tests.test_orders_read_service import ACCOUNTS
from tests.test_orders_read_service import prepared as _prepared_fixture
from tests.test_orders_read_service import read_db as _read_db_fixture
from tests.test_orders_schema_candidate import cluster  # noqa: F401

authority = _authority_fixture
prepared = _prepared_fixture
read_db = _read_db_fixture


def freeze(session, principal, run):
    return freeze_orders_view(
        session,
        principal=principal,
        accounts=ACCOUNTS,
        coverage_run_ids=(run,),
        query_checksum="a" * 64,
    )


def read(session, principal, snapshot):
    return read_orders_snapshot(
        session,
        principal=principal,
        accounts=ACCOUNTS,
        snapshot_id=snapshot,
        query_checksum="a" * 64,
    )


def test_view_includes_repeated_items_and_never_claims_provider_readiness(authority):
    _, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        result = publish(session, principal, credential, manifest(row), uuid4().hex)
        snapshot = freeze(session, principal, result.run_id)
        page = read(session, principal, snapshot)
        assert len(page.rows) == 2
        assert page.coverage_state == "partial"
        assert {r.item_identity for r in page.rows} == {i.identity for i in row.items}
        for entry in page.rows:
            assert "source_readiness_unproven" in entry.readiness_blockers
            assert "catalog_unmapped" in entry.readiness_blockers
            assert entry.deadlines == ()
        assert not session.in_transaction()


def test_stored_deadlines_follow_current_observation_and_frozen_snapshot(authority):
    owner, runtime, principal, credential = authority
    row = item_fact()
    identity = row.identity
    with Session(runtime) as session:
        accepted = publish(session, principal, credential, manifest(row), uuid4().hex)
        before = freeze(session, principal, accepted.run_id)
        changed = replace(row, status=map_avito_status("delivered"))
        rejected = publish(
            session, principal, credential, manifest(changed), uuid4().hex
        )

    def insert_deadline(run_id, kind, *, computed=False):
        with owner.begin() as connection:
            connection.execute(
                text("""INSERT INTO order_deadlines
                (organization_id,marketplace_account_id,order_id,observation_id,
                 observed_at,deadline_kind,source_deadline_at,computed_deadline_at,
                 rule_id,rule_version,timezone,evidence_source)
                SELECT organization_id,marketplace_account_id,order_id,observation_id,
                 :observed,:kind,:source,:computed,:rule,:version,:timezone,:evidence
                FROM order_sync_memberships
                WHERE sync_run_id=:run AND organization_id=:org
                AND marketplace_account_id=:account AND order_item_id IS NULL"""),
                {
                    "run": run_id,
                    "org": identity.organization_id,
                    "account": identity.marketplace_account_id,
                    "observed": row.observed_at,
                    "kind": kind,
                    "source": row.observed_at + timedelta(days=1),
                    "computed": row.observed_at + timedelta(days=2)
                    if computed
                    else None,
                    "rule": "synthetic-existing-rule" if computed else None,
                    "version": "synthetic-v1" if computed else None,
                    "timezone": "Europe/Moscow",
                    "evidence": "synthetic-stored-source",
                },
            )

    insert_deadline(accepted.run_id, "z-source")
    insert_deadline(accepted.run_id, "a-computed", computed=True)
    insert_deadline(rejected.run_id, "rejected-observation-deadline")
    with Session(runtime) as session:
        current = freeze(session, principal, accepted.run_id)
        entries = [
            entry
            for entry in read(session, principal, current).rows
            if entry.observation.identity == identity
        ]
        assert len(entries) == 2
        for entry in entries:
            assert [deadline.kind for deadline in entry.deadlines] == [
                "a-computed",
                "z-source",
            ]
            computed, source = entry.deadlines
            assert computed.computed_at == row.observed_at + timedelta(days=2)
            assert computed.rule_id == "synthetic-existing-rule"
            assert computed.rule_version == "synthetic-v1"
            assert source.source_at == row.observed_at + timedelta(days=1)
            assert source.computed_at is None and source.rule_id is None
            assert source.timezone == "Europe/Moscow"
            assert source.observed_at == row.observed_at
            assert source.evidence_source == "synthetic-stored-source"
            assert "source_readiness_unproven" in entry.readiness_blockers
        assert all(
            entry.deadlines == ()
            for entry in read(session, principal, before).rows
            if entry.observation.identity == identity
        )
    insert_deadline(accepted.run_id, "later-recorded-source")
    with Session(runtime) as session:
        assert all(
            len(entry.deadlines) == 2
            for entry in read(session, principal, current).rows
            if entry.observation.identity == identity
        )


def test_mutable_audit_binding_is_not_source_authority(authority):
    owner, runtime, principal, credential = authority
    value, key = manifest(item_fact()), uuid4().hex
    with Session(runtime) as session:
        result = publish(session, principal, credential, value, key)
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE lk_audit_events SET details=(details::jsonb - 'account_binding_checksum')::json "
                "WHERE object_type='orders_sync_run' AND object_id=:id"
            ),
            {"id": str(result.run_id)},
        )
    with Session(runtime) as session:
        assert publish(session, principal, credential, value, key).replayed
        assert read(session, principal, freeze(session, principal, result.run_id)).rows


def test_unbound_run_rejects_replay_and_freeze_even_with_copied_audit(authority):
    owner, runtime, principal, credential = authority
    value, key = manifest(item_fact()), uuid4().hex
    with Session(runtime) as session:
        bound = publish(session, principal, credential, value, uuid4().hex)
    with owner.begin() as connection:
        legacy = connection.execute(
            text("""INSERT INTO order_sync_runs
            (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
             adapter_version,mapping_version,source_contract_version,source_snapshot,
             state,completed_at)
            SELECT organization_id,marketplace_account_id,marketplace,source_kind,:key,
             adapter_version,mapping_version,source_contract_version,source_snapshot,
             'partial',clock_timestamp() FROM order_sync_runs WHERE sync_run_id=:run
            RETURNING sync_run_id"""),
            {"key": key, "run": bound.run_id},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO lk_audit_events
            (organization_id,action,object_type,object_id,details)
            SELECT organization_id,action,object_type,:legacy,details
            FROM lk_audit_events WHERE object_type='orders_sync_run' AND object_id=:bound"""),
            {"legacy": str(legacy), "bound": str(bound.run_id)},
        )
        before = connection.execute(
            text("SELECT count(*) FROM order_read_snapshots")
        ).scalar_one()
    with Session(runtime) as session:
        with pytest.raises(ValueError, match="run binding"):
            publish(session, principal, credential, value, key)
        assert not session.in_transaction()
        with pytest.raises(ValueError, match="run binding"):
            freeze(session, principal, legacy)
        assert not session.in_transaction()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_read_snapshots")
            ).scalar_one()
            == before
        )


def test_bound_staging_projection_is_not_published_by_sealed_coverage(authority):
    owner, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        bound = publish(session, principal, credential, manifest(row), uuid4().hex)
    with owner.begin() as connection:
        staging = connection.execute(
            text("""INSERT INTO order_sync_runs
            (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
             adapter_version,mapping_version,source_contract_version,source_snapshot,
             account_binding_schema_version,account_binding_external_account_id,
             account_binding_credential_ref,account_binding_payload,account_binding_checksum)
            SELECT organization_id,marketplace_account_id,marketplace,source_kind,:key,
             adapter_version,mapping_version,source_contract_version,source_snapshot,
             account_binding_schema_version,account_binding_external_account_id,
             account_binding_credential_ref,account_binding_payload,account_binding_checksum
            FROM order_sync_runs WHERE sync_run_id=:run RETURNING sync_run_id"""),
            {"key": uuid4().hex, "run": bound.run_id},
        ).scalar_one()
        connection.execute(
            text("""INSERT INTO order_sync_memberships
            (organization_id,marketplace_account_id,sync_run_id,order_id,order_item_id,
             observation_id,coverage_role,observed_at)
            SELECT organization_id,marketplace_account_id,:staging,order_id,order_item_id,
             observation_id,coverage_role,observed_at FROM order_sync_memberships
            WHERE sync_run_id=:run"""),
            {"staging": staging, "run": bound.run_id},
        )
        connection.execute(
            text(
                "UPDATE marketplace_orders SET last_seen_sync_run_id=:staging, "
                "version=version+1 WHERE last_seen_sync_run_id=:run"
            ),
            {"staging": staging, "run": bound.run_id},
        )
    try:
        with (
            Session(runtime) as session,
            pytest.raises(ValueError, match="publication"),
        ):
            freeze(session, principal, bound.run_id)
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_orders SET last_seen_sync_run_id=:run, "
                    "version=version+1 WHERE last_seen_sync_run_id=:staging"
                ),
                {"staging": staging, "run": bound.run_id},
            )


def test_partial_cancellation_blocks_old_projection_without_rewriting_history(
    authority,
):
    _, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        initial = publish(session, principal, credential, manifest(row), uuid4().hex)
        historical = freeze(session, principal, initial.run_id)
        cancelled = replace(row, status=map_avito_status("canceled"))
        partial = publish(
            session,
            principal,
            credential,
            manifest(cancelled, complete=False),
            uuid4().hex,
        )
        current = read(session, principal, freeze(session, principal, partial.run_id))
        selected = [r for r in current.rows if r.observation.identity == row.identity]
        assert len(selected) == 2
        assert all(
            "source_reconciliation_required" in r.readiness_blockers for r in selected
        )
        assert all(r.observation.status == row.status for r in selected)
        old = read(session, principal, historical)
        historical_selected = [
            r for r in old.rows if r.observation.identity == row.identity
        ]
        assert len(historical_selected) == 2
        assert all(
            "source_reconciliation_required" not in r.readiness_blockers
            for r in historical_selected
        )
        assert all(r.observation.status == row.status for r in historical_selected)


def test_view_rejects_cross_account_coverage_and_rolls_back_snapshot(authority):
    owner, runtime, principal, credential = authority
    with owner.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM order_read_snapshots")
        ).scalar_one()
    with Session(runtime) as session:
        result = publish(
            session, principal, credential, manifest(item_fact()), uuid4().hex
        )
        with pytest.raises(ValueError, match="coverage"):
            freeze(session, principal, result.run_id + 1000000)
        assert not session.in_transaction()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_read_snapshots")
            ).scalar_one()
            == before
        )


def test_rebound_account_cannot_relabel_previously_published_evidence(authority):
    owner, runtime, principal, credential = authority
    with Session(runtime) as session:
        result = publish(
            session, principal, credential, manifest(item_fact()), uuid4().hex
        )
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
            )
        )
    try:
        with (
            Session(runtime) as session,
            pytest.raises(ValueError, match="run binding"),
        ):
            freeze_orders_view(
                session,
                principal=principal,
                accounts=(
                    ExpectedAccountBinding(91101, "avito", "synthetic-rebound", None),
                ),
                coverage_run_ids=(result.run_id,),
                query_checksum="a" * 64,
            )
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                )
            )


def test_broken_current_membership_is_not_silently_omitted(authority):
    owner, runtime, principal, credential = authority
    row = item_fact()
    with Session(runtime) as session:
        first = publish(session, principal, credential, manifest(row), uuid4().hex)
        other = publish(
            session, principal, credential, manifest(item_fact()), uuid4().hex
        )
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_orders SET last_seen_sync_run_id=:run,version=version+1 WHERE external_order_id=:external"
            ),
            {"run": other.run_id, "external": row.identity.external_order_id},
        )
    try:
        with Session(runtime) as session, pytest.raises(ValueError, match="membership"):
            freeze(session, principal, other.run_id)
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_orders SET last_seen_sync_run_id=:run,version=version+1 WHERE external_order_id=:external"
                ),
                {"run": first.run_id, "external": row.identity.external_order_id},
            )
