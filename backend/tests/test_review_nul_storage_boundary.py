"""Lossless0065 roundtrip supersedes the historical0063 rollback limitation."""

from uuid import uuid4

from sqlalchemy import text

from tests import test_review_facts_repository as repository_tests
from tests.test_orders_schema_candidate import scope

cluster = repository_tests.cluster
db = repository_tests.db


def test_nul_text_is_durable_without_stripping_or_partial_run(db):
    source, key = uuid4().hex, uuid4().hex
    normalized = repository_tests.fact(key, source, body="synthetic\0review")
    assert normalized.text == "synthetic\0review"
    with db[1].begin() as connection:
        scope(connection)
        repo = repository_tests.repository(connection)
        run = repo.reserve_run(
            source_run_id=source,
            request_checksum="a" * 64,
            started_at=repository_tests.NOW,
        )
        repository_tests.ingest(repo, run, [normalized], {key: 0})
        assert repo.get_fact(key).text == "synthetic\0review"
        assert (
            connection.execute(
                text("SELECT status FROM review_sync_runs_v2 WHERE sync_run_id=:run"),
                {"run": run.sync_run_id},
            ).scalar_one()
            == "complete"
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_sync_run_items WHERE sync_run_id=:run"
                ),
                {"run": run.sync_run_id},
            ).scalar_one()
            == 1
        )
    with db[1].begin() as connection:
        scope(connection)
        assert (
            repository_tests.repository(connection).get_fact(key).text
            == "synthetic\0review"
        )
