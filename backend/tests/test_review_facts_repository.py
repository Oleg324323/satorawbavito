"""Dormant repository contract against migrated PostgreSQL and real runtime ACLs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.reviews.canonical_contract import normalize_avito_review
from tests import test_review_facts_schema as schema
from tests.test_orders_schema_candidate import scope

NOW = datetime(2026, 9, 9, tzinfo=UTC)
cluster = schema.cluster
db = schema.db
COVERAGE = {
    "from": None,
    "to": None,
    "streams": [{"name": "reviews", "terminalReached": True}],
    "pagesObserved": 1,
    "providerEndReached": True,
}


def fact(key, source, body="A", updated=None):
    return normalize_avito_review(
        {
            "id": key,
            "createdAt": NOW,
            "updatedAt": updated,
            "text": body,
            "answered": False,
        },
        91001,
        91101,
        source,
        NOW,
    )


def ingest(repo, ref, facts, expected):
    return repo.ingest(
        ref.sync_run_id,
        facts=tuple(facts),
        coverage=COVERAGE,
        completeness="complete",
        completed_at=NOW,
        expected_versions=expected,
    )


def repository(connection, account=91101, provider="avito", external="synthetic-a"):
    from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner

    return ReviewFactsRepository(
        connection, ReviewOwner(91001, account, provider, external)
    )


def test_reserve_exact_replay_and_conflict_leave_original_unchanged(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    key = uuid4().hex
    with db[1].begin() as connection:
        scope(connection)
        repo = repository(connection)
        first = repo.reserve_run(
            source_run_id=key, request_checksum="a" * 64, started_at=NOW
        )
        assert first.run_sequence > 0
        assert (
            repo.reserve_run(
                source_run_id=key, request_checksum="a" * 64, started_at=NOW
            )
            == first
        )
        with pytest.raises(ReviewRepositoryError, match="REVIEW_REPLAY_CONFLICT"):
            repo.reserve_run(
                source_run_id=key, request_checksum="b" * 64, started_at=NOW
            )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_sync_runs_v2 WHERE "
                    "COALESCE(source_run_id_utf8,convert_to(source_run_id,'UTF8'))=:key"
                ),
                {"key": key.encode("utf-8")},
            ).scalar_one()
            == 1
        )
    # A new connection sees the durable identity, not process-local state.
    with db[1].begin() as connection:
        scope(connection)
        assert (
            repository(connection).reserve_run(
                source_run_id=key, request_checksum="a" * 64, started_at=NOW
            )
            == first
        )


def test_ingest_nulls_replay_manifest_and_unchanged_watermark(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    key, source = uuid4().hex, uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        ref = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        value = fact(key, source)
        result = ingest(repo, ref, [value], {key: 0})
        current = repo.get_fact(key)
        assert (
            current.version,
            current.text,
            current.can_answer,
            current.revision,
        ) == (1, "A", None, 1)
        assert ingest(repo, ref, [value], {key: 0}) == result
        for altered in (
            [],
            [fact(key, source, "changed")],
            [value, fact("extra", source)],
        ):
            with pytest.raises(ReviewRepositoryError, match="REVIEW_REPLAY_CONFLICT"):
                ingest(
                    repo,
                    ref,
                    altered,
                    {v.identity.external_review_id: 0 for v in altered},
                )
        other = uuid4().hex
        second = repo.reserve_run(
            source_run_id=other, request_checksum="a" * 64, started_at=NOW
        )
        ingest(repo, second, [fact(key, other)], {key: 1})
        current = repo.get_fact(key)
        assert (
            current.version,
            current.revision,
            current.last_source_run_sequence,
        ) == (2, 1, second.run_sequence)


def test_aba_and_stale_cas_roll_back_entire_command(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        for version, body in enumerate(("A", "B", "A")):
            source = uuid4().hex
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            ingest(repo, run, [fact(key, source, body)], {key: version})
        assert (repo.get_fact(key).revision, repo.get_fact(key).text) == (3, "A")
        source = uuid4().hex
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        new_key = "000-" + uuid4().hex
        with pytest.raises(ReviewRepositoryError, match="REVIEW_VERSION_CONFLICT"):
            ingest(
                repo,
                run,
                [fact(new_key, source), fact(key, source, "C")],
                {new_key: 0, key: 1},
            )
        assert repo.get_fact(new_key) is None
        assert repo.get_fact(key).version == 3
        assert c.execute(
            text(
                "SELECT status,observed_count FROM review_sync_runs_v2 WHERE sync_run_id=:rid"
            ),
            {"rid": run.sync_run_id},
        ).one() == ("running", 0)


def test_late_sequence_and_older_provider_timestamp_cannot_regress(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        sources = [uuid4().hex for _ in range(4)]
        runs = [
            repo.reserve_run(source_run_id=s, request_checksum="a" * 64, started_at=NOW)
            for s in sources
        ]
        ingest(repo, runs[0], [fact(key, sources[0], updated=NOW)], {key: 0})
        ingest(repo, runs[2], [fact(key, sources[2], updated=NOW)], {key: 1})
        ingest(
            repo,
            runs[1],
            [fact(key, sources[1], "late", NOW + timedelta(days=1))],
            {key: 2},
        )
        assert (
            repo.get_fact(key).text,
            repo.get_fact(key).last_source_run_sequence,
        ) == ("A", runs[2].run_sequence)
        ingest(
            repo,
            runs[3],
            [fact(key, sources[3], "old", NOW - timedelta(days=1))],
            {key: 2},
        )
        assert (repo.get_fact(key).text, repo.get_fact(key).version) == ("A", 2)


def test_ambiguity_survives_restart_repeats_and_clears_only_newer_evidence(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        for version, body in enumerate(("A", "B")):
            source = uuid4().hex
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            ingest(repo, run, [fact(key, source, body, NOW)], {key: version})
        assert (repo.get_fact(key).text, repo.get_fact(key).source_order_state) == (
            "A",
            "ambiguous",
        )
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        assert repo.get_fact(key).source_order_state == "ambiguous"
        for version, body, stamp in (
            (2, "A", NOW),
            (3, "C", None),
            (4, "D", NOW + timedelta(days=1)),
        ):
            source = uuid4().hex
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            ingest(repo, run, [fact(key, source, body, stamp)], {key: version})
            assert repo.get_fact(key).source_order_state == "ambiguous"
            assert repo.get_fact(key).text == "A"


def test_known_ambiguity_clears_with_strictly_newer_timestamp(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        for version, body, stamp in (
            (0, "A", NOW),
            (1, "B", NOW),
            (2, "C", NOW + timedelta(seconds=1)),
        ):
            source = uuid4().hex
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            ingest(repo, run, [fact(key, source, body, stamp)], {key: version})
        assert (repo.get_fact(key).text, repo.get_fact(key).source_order_state) == (
            "C",
            "current",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "sentinel-private"},
        {"pagesObserved": True},
        {"pagesObserved": -1},
        {"providerEndReached": 1},
        {"streams": []},
        {"streams": [{"name": "reviews", "terminalReached": False}]},
        {"from": "not-a-date"},
        {"from": "2026-09-09T00:00:00"},
        {
            "streams": [
                {"name": "reviews", "terminalReached": True, "body": "sentinel-private"}
            ]
        },
        {"streams": [{"name": "reviews", "terminalReached": True}] * 2},
    ],
)
def test_invalid_coverage_rejected_without_writes(db, change):
    from app.reviews.canonical_repository import ReviewRepositoryError

    source, key = uuid4().hex, uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        with pytest.raises(ReviewRepositoryError) as caught:
            repo.ingest(
                run.sync_run_id,
                facts=(fact(key, source),),
                coverage={**COVERAGE, **change},
                completeness="complete",
                completed_at=NOW,
                expected_versions={key: 0},
            )
        assert "sentinel-private" not in str(caught.value)
        assert repo.get_fact(key) is None


def test_duplicates_forged_checksum_wrong_run_and_cross_account_rejected(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    source, key = uuid4().hex, uuid4().hex
    value = fact(key, source)
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        for values in (
            (value, value),
            (replace(value, content_checksum="0" * 64),),
            (fact(key, "another-run"),),
            (
                normalize_avito_review(
                    {"id": key, "createdAt": NOW, "answered": False},
                    91001,
                    91102,
                    source,
                    NOW,
                ),
            ),
        ):
            with pytest.raises(ReviewRepositoryError):
                ingest(repo, run, values, {key: 0})
        assert repo.get_fact(key) is None


@pytest.mark.parametrize("context", [None, 91002])
def test_missing_or_wrong_tenant_rejected(db, context):
    from app.reviews.canonical_repository import ReviewRepositoryError

    with db[1].begin() as c:
        if context is not None:
            scope(c, context)
        with pytest.raises(ReviewRepositoryError, match="REVIEW_SCOPE_DENIED"):
            repository(c).reserve_run(
                source_run_id=uuid4().hex, request_checksum="a" * 64, started_at=NOW
            )


@pytest.mark.parametrize(
    "account,provider,external",
    [
        (91101, "wb", "synthetic-a"),
        (91101, "avito", "wrong-binding"),
        (91201, "wb", "synthetic-d"),
    ],
)
def test_wrong_account_binding_rejected(db, account, provider, external):
    from app.reviews.canonical_repository import ReviewRepositoryError

    with db[1].begin() as c:
        scope(c)
        with pytest.raises(ReviewRepositoryError, match="REVIEW_SCOPE_DENIED"):
            repository(c, account, provider, external).get_fact(uuid4().hex)


def test_same_opaque_id_is_separate_in_two_accounts_and_long_run_replay(db):
    key = "000-" + "".join(uuid4().hex for _ in range(260))
    source = "run-" + "".join(uuid4().hex for _ in range(260))
    identities = []
    with db[1].begin() as c:
        scope(c)
        for account, external in ((91101, "synthetic-a"), (91102, "synthetic-b")):
            repo = repository(c, account=account, external=external)
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            value = normalize_avito_review(
                {"id": key, "createdAt": NOW, "answered": False},
                91001,
                account,
                source,
                NOW,
            )
            ingest(repo, run, [value], {key: 0})
            identities.append(repo.get_fact(key).review_id)
            assert (
                repo.reserve_run(
                    source_run_id=source, request_checksum="a" * 64, started_at=NOW
                )
                == run
            )
    assert len(set(identities)) == 2


def test_partial_empty_snapshot_does_not_remove_absent_fact(db):
    key, source = uuid4().hex, uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        ingest(repo, run, [fact(key, source)], {key: 0})
        before = repo.get_fact(key)
        next_run = repo.reserve_run(
            source_run_id=uuid4().hex, request_checksum="a" * 64, started_at=NOW
        )
        repo.ingest(
            next_run.sync_run_id,
            facts=(),
            coverage={**COVERAGE, "providerEndReached": False},
            completeness="partial",
            completed_at=NOW,
            expected_versions={},
        )
        assert repo.get_fact(key) == before


def test_two_sessions_reserve_same_run_without_duplicate_or_blind_retry(db):
    source = uuid4().hex
    barrier = Barrier(2)

    def reserve():
        with db[1].begin() as c:
            scope(c)
            barrier.wait(timeout=10)
            return repository(c).reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0] == results[1]


def test_outer_rollback_removes_ingestion_and_no_commit_is_hidden(db):
    source, key = uuid4().hex, uuid4().hex
    with db[1].connect() as c:
        transaction = c.begin()
        scope(c)
        repo = repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        ingest(repo, run, [fact(key, source)], {key: 0})
        transaction.rollback()
    with db[1].begin() as c:
        scope(c)
        assert repository(c).get_fact(key) is None


def test_two_updates_with_same_expected_version_have_one_winner(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    key, source = uuid4().hex, uuid4().hex
    sources = [uuid4().hex, uuid4().hex]
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        first = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        ingest(repo, first, [fact(key, source)], {key: 0})
        runs = [
            repo.reserve_run(source_run_id=s, request_checksum="a" * 64, started_at=NOW)
            for s in sources
        ]
    barrier = Barrier(2)

    def publish(index):
        with db[1].begin() as c:
            scope(c)
            barrier.wait(timeout=10)
            try:
                ingest(
                    repository(c),
                    runs[index],
                    [fact(key, sources[index], str(index))],
                    {key: 1},
                )
            except ReviewRepositoryError as exc:
                return exc.code
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, index) for index in (0, 1)]
        assert sorted(f.result(timeout=15) for f in futures) == [
            "REVIEW_VERSION_CONFLICT",
            "accepted",
        ]
    with db[1].begin() as c:
        scope(c)
        assert repository(c).get_fact(key).version == 2
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM review_sync_run_items WHERE sync_run_id IN (:a,:b)"
                ),
                {"a": runs[0].sync_run_id, "b": runs[1].sync_run_id},
            ).scalar_one()
            == 1
        )


def test_disconnected_account_refused_after_reservation(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    source = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        run = repository(c).reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
    # Change only this synthetic account, and always restore its fixture state.
    try:
        with db[0].begin() as c:
            c.execute(
                text(
                    "UPDATE marketplace_accounts SET status='disconnected' WHERE marketplace_account_id=91101"
                )
            )
        with db[1].begin() as c:
            scope(c)
            with pytest.raises(ReviewRepositoryError, match="REVIEW_SCOPE_DENIED"):
                ingest(repository(c), run, [], {})
    finally:
        with db[0].begin() as c:
            c.execute(
                text(
                    "UPDATE marketplace_accounts SET status='connected' WHERE marketplace_account_id=91101"
                )
            )


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_repeatable_snapshots_rejected_before_identity_decision(db, level):
    from app.reviews.canonical_repository import ReviewRepositoryError

    with db[1].connect().execution_options(isolation_level=level) as c, c.begin():
        scope(c)
        with pytest.raises(ReviewRepositoryError, match="REVIEW_TRANSACTION_REQUIRED"):
            repository(c).reserve_run(
                source_run_id=uuid4().hex, request_checksum="a" * 64, started_at=NOW
            )


def test_runtime_database_failure_is_safe_and_has_no_fallback(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    with db[1].connect() as c, c.begin():
        scope(c)
        # Server-side timeout exercises a real DB error, without changing the server.
        c.execute(text("SET LOCAL statement_timeout='1ms'"))
        with pytest.raises(DBAPIError):
            c.execute(text("SELECT pg_sleep(0.02)"))
        with pytest.raises(ReviewRepositoryError, match="REVIEW_STORAGE_UNAVAILABLE"):
            repository(c).get_fact("synthetic-private-sentinel")


def test_terminal_replay_rejects_changed_coverage(db):
    from app.reviews.canonical_repository import ReviewRepositoryError

    source = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=NOW
        )
        result = ingest(repo, run, [], {})
        with pytest.raises(ReviewRepositoryError, match="REVIEW_REPLAY_CONFLICT"):
            repo.ingest(
                run.sync_run_id,
                facts=(),
                coverage={**COVERAGE, "pagesObserved": 2},
                completeness="complete",
                completed_at=NOW,
                expected_versions={},
            )
        assert ingest(repo, run, [], {}) == result


def test_late_equal_timestamp_conflict_marks_ambiguity_without_regression(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        sources = [uuid4().hex, uuid4().hex]
        runs = [
            repo.reserve_run(source_run_id=s, request_checksum="a" * 64, started_at=NOW)
            for s in sources
        ]
        ingest(repo, runs[1], [fact(key, sources[1], "B", NOW)], {key: 0})
        ingest(repo, runs[0], [fact(key, sources[0], "A", NOW)], {key: 1})
        row = repo.get_fact(key)
        assert (
            row.text,
            row.source_order_state,
            row.last_source_run_sequence,
            row.version,
        ) == ("B", "ambiguous", runs[1].run_sequence, 2)


def test_unknown_timestamp_cannot_erase_known_freshness_fence(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        for version, body, stamp in (
            (0, "A", NOW),
            (1, "B", None),
            (2, "C", NOW - timedelta(days=1)),
        ):
            source = uuid4().hex
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=NOW
            )
            ingest(repo, run, [fact(key, source, body, stamp)], {key: version})
        assert (repo.get_fact(key).text, repo.get_fact(key).version) == ("B", 2)


def test_known_history_fence_does_not_hide_current_equal_time_disagreement(db):
    key = uuid4().hex
    with db[1].begin() as c:
        scope(c)
        repo = repository(c)
        sources = [uuid4().hex for _ in range(3)]
        runs = [
            repo.reserve_run(source_run_id=s, request_checksum="a" * 64, started_at=NOW)
            for s in sources
        ]
        ingest(repo, runs[1], [fact(key, sources[1], "B", NOW)], {key: 0})
        ingest(
            repo,
            runs[0],
            [fact(key, sources[0], "X", NOW + timedelta(days=1))],
            {key: 1},
        )
        ingest(repo, runs[2], [fact(key, sources[2], "A", NOW)], {key: 1})
        row = repo.get_fact(key)
        assert (row.text, row.source_order_state, row.version) == ("B", "ambiguous", 2)
