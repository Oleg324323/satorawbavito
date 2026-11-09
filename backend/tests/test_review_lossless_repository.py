"""Exact0065 mixed-representation consumer acceptance, not schema installation."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.reviews.canonical_contract import review_fact_checksum
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.ingestion_contract import ReviewRepositoryError, snapshot_manifest
from app.reviews.run_binding_storage import encode_review_run_binding
from tests import test_review_facts_repository as existing
from tests.test_orders_schema_candidate import scope

cluster = existing.cluster
db = existing.db


def byte_unit(connection, value, *, corrupt=False, legacy=False):
    fields = (
        "external_product_id",
        "text",
        "source_status",
        "source_schema_version",
        "normalization_version",
    )
    observation = {key: getattr(value, key) if legacy else None for key in fields}
    observation.update(
        {
            key + "_utf8": None
            if legacy or getattr(value, key) is None
            else getattr(value, key).encode("utf-8")
            for key in fields
        }
    )
    observation.update(
        source_created_at=value.source_created_at,
        source_updated_at=value.source_updated_at,
        rating=value.rating,
        answered=value.answered,
        can_answer=value.can_answer,
        observed_at=value.observed_at,
        content_checksum="0" * 64 if corrupt else value.content_checksum,
    )
    schema = existing.schema
    run_id, sequence = schema.run(
        connection,
        **encode_review_run_binding(ReviewBindingDescriptor(
            91001, 91101, "avito", "synthetic-a", None,
        )),
        source_run_id=value.source_run_id if legacy else None,
        source_run_id_utf8=None if legacy else value.source_run_id.encode("utf-8"),
        coverage=json.dumps(existing.COVERAGE) if legacy else None,
        coverage_utf8=None if legacy else json.dumps(existing.COVERAGE).encode("utf-8"),
    )
    rid = schema.identity(
        connection,
        external_review_id=value.identity.external_review_id if legacy else None,
        external_review_id_utf8=None
        if legacy
        else value.identity.external_review_id.encode("utf-8"),
    )
    oid = schema.observation(connection, rid, run_id, **observation)
    assert (
        schema.advance(
            connection,
            rid,
            oid,
            last_source_run_id=run_id,
            last_source_run_sequence=sequence,
        )
        == 1
    )
    schema.item(
        connection, rid, oid, run_id, content_checksum=observation["content_checksum"]
    )
    return {"review_sync_runs_v2": {"sync_run_id": run_id}}


@pytest.mark.parametrize("body", [None, "", "a\0е\u0301🚀"])
def test_decoder_reads_byte_observation_and_exact_nul_identity(db, body):
    key, source = uuid4().hex + "\0🚀", uuid4().hex + "\0run"
    value = existing.fact(key, source, body=body)
    with db[0].begin() as c:
        rows = byte_unit(c, value)
    with db[1].begin() as c:
        scope(c)
        repo = existing.repository(c)
        stored = repo.get_fact(key)
        assert stored is not None
        assert stored.text == body
        assert stored.version == 1
        ref = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
        )
        assert ref.sync_run_id == rows["review_sync_runs_v2"]["sync_run_id"]


def test_decoder_rejects_stored_observation_checksum_mismatch(db):
    key, source = uuid4().hex, uuid4().hex
    with db[0].begin() as c:
        byte_unit(c, existing.fact(key, source), corrupt=True)
    with db[1].begin() as c:
        scope(c)
        with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
            existing.repository(c).get_fact(key)


def test_run_replay_rejects_duplicate_json_keys_admitted_by_sql(db):
    source = uuid4().hex
    encoded = b'{"from":null,"to":null,"streams":[],"pagesObserved":0,"pagesObserved":1,"providerEndReached":false}'
    with db[0].begin() as c:
        existing.schema.run(
            c,
            **encode_review_run_binding(ReviewBindingDescriptor(
                91001, 91101, "avito", "synthetic-a", None,
            )),
            source_run_id=None,
            source_run_id_utf8=source.encode("utf-8"),
            coverage=None,
            coverage_utf8=encoded,
        )
    with db[1].begin() as c:
        scope(c)
        with pytest.raises(ReviewRepositoryError, match="^REVIEW_STORAGE_INVALID$"):
            existing.repository(c).reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
            )


@pytest.mark.parametrize("body", [None, "", "original\0е\u0301🚀"])
def test_writer_roundtrips_all_eight_pairs_without_semantic_hash_changes(db, body):
    key, source = uuid4().hex + "\0🚀", uuid4().hex + "\0run"
    value = replace(
        existing.fact(key, source, body=body),
        external_product_id="product\0е\u0301🚀",
        source_status="status\0🚀",
        source_schema_version="source\0v1",
        normalization_version="normalization\0v1",
        content_checksum="",
    )
    value = replace(value, content_checksum=review_fact_checksum(value))
    coverage = {
        **existing.COVERAGE,
        "streams": [{"name": "reviews\0🚀", "terminalReached": True}],
    }
    with db[1].begin() as c:
        scope(c)
        repo = existing.repository(c)
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
        )
        manifest = repo.ingest(
            run.sync_run_id,
            facts=(value,),
            coverage=coverage,
            completeness="complete",
            completed_at=existing.NOW,
            expected_versions={key: 0},
        )
        snapshot = repo.get_fact(key)
        assert snapshot.text == body
    with db[1].begin() as c:
        scope(c)
        repo = existing.repository(c)
        assert repo.get_fact(key).text == body
        assert (
            repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
            )
            == run
        )
        assert (
            repo.ingest(
                run.sync_run_id,
                facts=(value,),
                coverage=coverage,
                completeness="complete",
                completed_at=existing.NOW,
                expected_versions={key: 0},
            )
            == manifest
        )
    with db[0].connect() as c:
        row = (
            c.execute(
                text("SELECT * FROM review_observations WHERE observation_id=:id"),
                {"id": snapshot.current_observation_id},
            )
            .mappings()
            .one()
        )
        assert row["content_checksum"] == value.content_checksum
        for field in (
            "external_product_id",
            "text",
            "source_status",
            "source_schema_version",
            "normalization_version",
        ):
            assert row[field] is None
            expected = getattr(value, field)
            assert row[field + "_utf8"] == (
                None if expected is None else expected.encode("utf-8")
            )
        stored_run = (
            c.execute(
                text("SELECT * FROM review_sync_runs_v2 WHERE sync_run_id=:id"),
                {"id": run.sync_run_id},
            )
            .mappings()
            .one()
        )
        assert stored_run["source_run_id"] is None
        assert stored_run["source_run_id_utf8"] == source.encode("utf-8")
        assert stored_run["coverage"] is None
        assert json.loads(stored_run["coverage_utf8"]) == coverage
        assert stored_run["manifest_checksum"] == manifest
        stored_fact = (
            c.execute(
                text("SELECT * FROM review_facts WHERE review_id=:id"),
                {"id": snapshot.review_id},
            )
            .mappings()
            .one()
        )
        assert stored_fact["external_review_id"] is None
        assert stored_fact["external_review_id_utf8"] == key.encode("utf-8")


def test_legacy_identity_and_terminal_run_replay_survive_new_byte_observation(db):
    key, source = uuid4().hex, uuid4().hex
    old = existing.fact(key, source, body="legacy")
    manifest = snapshot_manifest((old,), existing.COVERAGE, "complete", {key: 0})[2]
    with db[0].begin() as c:
        rows = byte_unit(c, old, legacy=True)
        old_run = rows["review_sync_runs_v2"]["sync_run_id"]
        c.execute(
            text(
                "UPDATE review_sync_runs_v2 SET status='complete',completeness='complete',"
                "completed_at=:now,observed_count=1,manifest_checksum=:manifest WHERE sync_run_id=:id"
            ),
            {"now": existing.NOW, "manifest": manifest, "id": old_run},
        )
    with db[1].begin() as c:
        scope(c)
        repo = existing.repository(c)
        prior = repo.get_fact(key)
        assert prior.text == "legacy"
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
        )
        assert run.sync_run_id == old_run
        assert existing.ingest(repo, run, [old], {key: 0}) == manifest
        new_source = uuid4().hex + "\0new"
        new_run = repo.reserve_run(
            source_run_id=new_source, request_checksum="b" * 64, started_at=existing.NOW
        )
        existing.ingest(
            repo,
            new_run,
            [existing.fact(key, new_source, body="new\0content")],
            {key: 1},
        )
        current = repo.get_fact(key)
        assert current.review_id == prior.review_id
        assert current.version == 2
        assert current.revision == 2
        assert current.text == "new\0content"
    with db[0].connect() as c:
        identity = c.execute(
            text(
                "SELECT external_review_id,external_review_id_utf8 FROM review_facts WHERE review_id=:id"
            ),
            {"id": prior.review_id},
        ).one()
        assert tuple(identity) == (key, None)
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM review_facts WHERE "
                    "COALESCE(external_review_id_utf8,convert_to(external_review_id,'UTF8'))=:key"
                ),
                {"key": key.encode("utf-8")},
            ).scalar_one()
            == 1
        )
        observations = c.execute(
            text(
                "SELECT text,text_utf8 FROM review_observations WHERE review_id=:id ORDER BY revision"
            ),
            {"id": prior.review_id},
        ).all()
        assert [tuple(row) for row in observations] == [
            ("legacy", None),
            (None, b"new\0content"),
        ]
