"""Actual immutable reservation provenance, not a codec-only acceptance."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.ingestion_contract import ReviewRepositoryError, snapshot_manifest
from app.reviews.run_binding_storage import decode_review_run_binding
from tests import test_review_facts_repository as facts
from tests.test_orders_schema_candidate import scope

cluster = facts.cluster
db = facts.db


def test_new_reservation_has_exact_binding_at_insert(db):
    with db[1].begin() as connection:
        scope(connection)
        reference = facts.repository(connection).reserve_run(
            source_run_id=uuid4().hex, request_checksum="a" * 64,
            started_at=facts.NOW,
        )
        row = connection.execute(
            text("SELECT * FROM review_sync_runs_v2 WHERE sync_run_id=:id"),
            {"id": reference.sync_run_id},
        ).mappings().one()
        assert decode_review_run_binding(row) == ReviewBindingDescriptor(
            91001, 91101, "avito", "synthetic-a", None,
        )


@pytest.mark.parametrize("reference", [None, "", " ", "synthetic-ref-🚀"])
def test_reservation_roundtrips_exact_credential_reference(db, reference):
    try:
        with db[0].begin() as connection:
            connection.execute(text(
                "UPDATE marketplace_accounts SET credential_ref=:ref WHERE marketplace_account_id=91101"
            ), {"ref": reference})
        with db[1].begin() as connection:
            scope(connection)
            repo = ReviewFactsRepository(connection, ReviewOwner(
                91001, 91101, "avito", "synthetic-a", reference,
            ))
            key = uuid4().hex
            first = repo.reserve_run(source_run_id=key, request_checksum="a" * 64, started_at=facts.NOW)
            assert repo.reserve_run(source_run_id=key, request_checksum="a" * 64, started_at=facts.NOW) == first
            row = connection.execute(text(
                "SELECT * FROM review_sync_runs_v2 WHERE sync_run_id=:id"
            ), {"id": first.sync_run_id}).mappings().one()
            assert decode_review_run_binding(row).credential_ref == reference
    finally:
        with db[0].begin() as connection:
            connection.execute(text(
                "UPDATE marketplace_accounts SET credential_ref=NULL WHERE marketplace_account_id=91101"
            ))


@pytest.mark.parametrize("operation", ["reserve", "ingest", "read"])
def test_unbound_history_is_not_promoted_or_consumed(db, operation):
    key = uuid4().hex
    with db[0].begin() as connection:
        if operation == "read":
            facts.schema.complete_fact(connection, external_review_id=key)
            run_id = None
        else:
            run_id, _ = facts.schema.run(connection, source_run_id=key)
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"), db[1].begin() as connection:
        scope(connection)
        repo = facts.repository(connection)
        if operation == "reserve":
            repo.reserve_run(source_run_id=key, request_checksum="a" * 64, started_at=facts.NOW)
        elif operation == "ingest":
            repo.ingest(run_id, facts=(), coverage=facts.COVERAGE,
                        completeness="complete", completed_at=facts.NOW, expected_versions={})
        else:
            repo.get_fact(key)
    if run_id is not None:
        with db[0].begin() as connection:
            row = connection.execute(text(
                "SELECT * FROM review_sync_runs_v2 WHERE sync_run_id=:id"
            ), {"id": run_id}).mappings().one()
            assert decode_review_run_binding(row) is None
            assert row["status"] == "running"


@pytest.mark.parametrize("changed_reference", ["", " ", "synthetic-new-ref"])
def test_new_live_reference_cannot_read_or_replay_old_fact(db, changed_reference):
    key, source = uuid4().hex, uuid4().hex
    with db[1].begin() as connection:
        scope(connection)
        repo = facts.repository(connection)
        run = repo.reserve_run(source_run_id=source, request_checksum="a" * 64, started_at=facts.NOW)
        value = facts.fact(key, source)
        facts.ingest(repo, run, [value], {key: 0})
    try:
        with db[0].begin() as connection:
            connection.execute(text(
                "UPDATE marketplace_accounts SET credential_ref=:ref WHERE marketplace_account_id=91101"
            ), {"ref": changed_reference})
        for operation in ("read", "replay"):
            with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"), db[1].begin() as connection:
                scope(connection)
                repo = ReviewFactsRepository(connection, ReviewOwner(
                    91001, 91101, "avito", "synthetic-a", changed_reference,
                ))
                if operation == "read":
                    repo.get_fact(key)
                else:
                    facts.ingest(repo, run, [value], {key: 0})
    finally:
        with db[0].begin() as connection:
            connection.execute(text(
                "UPDATE marketplace_accounts SET credential_ref=NULL WHERE marketplace_account_id=91101"
            ))


@pytest.mark.parametrize("source_role", ["current", "ambiguous", "watermark", "history-only"])
def test_every_contributing_history_source_requires_binding(db, source_role):
    key, source = uuid4().hex, uuid4().hex
    with db[1].begin() as connection:
        scope(connection)
        repo = facts.repository(connection)
        original = repo.reserve_run(source_run_id=source, request_checksum="a" * 64, started_at=facts.NOW)
        facts.ingest(repo, original, [facts.fact(key, source, updated=facts.NOW)], {key: 0})
        identity = repo.get_fact(key)
    with db[0].begin() as connection:
        unbound_run, sequence = facts.schema.run(connection)
        observation = facts.schema.observation(
            connection, identity.review_id, unbound_run,
            revision=2, source_updated_at=facts.NOW,
        )
        facts.schema.item(connection, identity.review_id, observation, unbound_run)
        changes = {}
        if source_role == "current":
            changes["current_observation_id"] = observation
        elif source_role == "ambiguous":
            changes.update(ambiguous_observation_id=observation, source_order_state="ambiguous")
        elif source_role == "watermark":
            changes.update(last_source_run_id=unbound_run, last_source_run_sequence=sequence)
        if changes:
            assignments = ",".join(f"{name}=:{name}" for name in changes)
            connection.execute(text(
                f"UPDATE review_facts SET {assignments},version=version+1 WHERE review_id=:id"
            ), {**changes, "id": identity.review_id})
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"), db[1].begin() as connection:
        scope(connection)
        repo = facts.repository(connection)
        if source_role != "history-only":
            repo.get_fact(key)
        else:
            # Read does not consult unrelated old timestamps; ingestion does.
            assert repo.get_fact(key).text == "A"
            next_source = uuid4().hex
            next_run = repo.reserve_run(source_run_id=next_source, request_checksum="a" * 64, started_at=facts.NOW)
            facts.ingest(repo, next_run, [facts.fact(key, next_source)], {key: 1})


def test_provenance_failure_rolls_back_earlier_fact_in_same_root(db):
    good_key, bad_key = "a-" + uuid4().hex, "z-" + uuid4().hex
    with db[0].begin() as connection:
        facts.schema.complete_fact(connection, external_review_id=bad_key)
    source = uuid4().hex
    with db[1].begin() as connection:
        scope(connection)
        run = facts.repository(connection).reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=facts.NOW,
        )
    completed_fact_updates = []

    def observe_update(_connection, cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE review_facts SET ") and cursor.rowcount == 1:
            completed_fact_updates.append(True)

    with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"), db[1].begin() as connection:
        scope(connection)
        event.listen(connection, "after_cursor_execute", observe_update)
        repo = ReviewFactsRepository(connection, ReviewOwner(
            91001, 91101, "avito", "synthetic-a",
        ), command_savepoints=False)
        try:
            facts.ingest(repo, run, [facts.fact(good_key, source), facts.fact(bad_key, source)],
                         {good_key: 0, bad_key: 1})
        finally:
            event.remove(connection, "after_cursor_execute", observe_update)
    assert completed_fact_updates == [True]
    with db[1].begin() as connection:
        scope(connection)
        assert facts.repository(connection).get_fact(good_key) is None
        assert connection.execute(text(
            "SELECT count(*) FROM review_sync_run_items WHERE sync_run_id=:id"
        ), {"id": run.sync_run_id}).scalar_one() == 0
        assert connection.execute(text(
            "SELECT status FROM review_sync_runs_v2 WHERE sync_run_id=:id"
        ), {"id": run.sync_run_id}).scalar_one() == "running"


def test_bound_terminal_replay_rejects_unbound_item_observation(db):
    key, source = uuid4().hex, uuid4().hex
    value = facts.fact(key, source)
    _, coverage, manifest = snapshot_manifest((value,), facts.COVERAGE, "complete", {key: 0})
    with db[1].begin() as connection:
        scope(connection)
        run = facts.repository(connection).reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=facts.NOW,
        )
    with db[0].begin() as connection:
        unbound, sequence = facts.schema.run(connection)
        rid = facts.schema.identity(connection, external_review_id=key)
        oid = facts.schema.observation(connection, rid, unbound, content_checksum=value.content_checksum)
        facts.schema.advance(connection, rid, oid, last_source_run_id=unbound, last_source_run_sequence=sequence)
        facts.schema.item(connection, rid, oid, run.sync_run_id, content_checksum=value.content_checksum)
        connection.execute(text(
            "UPDATE review_sync_runs_v2 SET status='complete',completeness='complete',"
            "completed_at=:now,observed_count=1,manifest_checksum=:manifest,coverage=NULL,"
            "coverage_utf8=:coverage WHERE sync_run_id=:id"
        ), {"id": run.sync_run_id, "now": facts.NOW, "manifest": manifest,
            "coverage": json.dumps(coverage).encode("utf-8")})
    with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"), db[1].begin() as connection:
        scope(connection)
        facts.ingest(facts.repository(connection), run, [value], {key: 0})
