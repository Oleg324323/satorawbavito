"""Approved canonical-only parity; no legacy writes or live provider actions."""

import multiprocessing
import os
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.reviews import canonical_sync, canonical_wb_fetch
from app.reviews.canonical_contract import normalize_wb_review
from app.wb_api.client import FakeWbApiClient
from app.wb_api.feedbacks_runtime import _to_feedback_row
from tests import test_review_canonical_http as http_tests
from tests import test_review_shadow_service as shadow

cluster = http_tests.cluster
db = http_tests.db
principal = http_tests.principal
context = http_tests.context


def raw_item(key, body="Synthetic parity"):
    return {"id": key, "createdDate": "2026-09-09T00:00:00Z", "text": body,
            "productDetails": {"nmId": 101}, "productValuation": 5, "answer": None}


@pytest.mark.parametrize("body,legacy_text", [
    ("Synthetic parity", "Synthetic parity"),
    (None, ""),
    ("  Synthetic\0parity  ", "Synthetic\0parity"),
])
def test_same_received_dto_matches_persistence_and_records_legacy_differences(
    db, context, monkeypatch, body, legacy_text
):
    key, received = uuid4().hex, []
    raw = raw_item(key, body)
    before = deepcopy(raw)
    provider = FakeWbApiClient(
        fixtures={"/api/v1/feedbacks": {"data": {"feedbacks": [raw]}}}
    )
    monkeypatch.setattr(canonical_wb_fetch, "build_wb_feedbacks_client", lambda **kw: provider)
    real_fetch = canonical_sync.fetch_feedbacks

    def capture(**kwargs):
        rows = real_fetch(**kwargs)
        received.extend(rows)
        return rows

    monkeypatch.setattr(canonical_sync, "fetch_feedbacks", capture)
    request = http_tests.payload()
    with http_tests.client_for(db, context) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=request)
    assert response.status_code == 200, response.text
    assert len(provider.requests) == len(received) == 1
    assert raw == before
    normalized = normalize_wb_review(
        received[0], 91001, 91103, "http:" + request["request_id"], shadow.repository_tests.NOW
    )
    legacy_row = _to_feedback_row(raw)
    legacy_fact = normalize_wb_review(
        legacy_row, 91001, 91103, "http:" + request["request_id"], shadow.repository_tests.NOW
    )
    assert legacy_fact.text == legacy_text
    assert normalized.text == body
    for field in ("identity", "external_product_id", "source_created_at", "source_updated_at",
                  "rating", "answered", "can_answer", "source_status", "normalization_version"):
        assert getattr(legacy_fact, field) == getattr(normalized, field)
    assert legacy_fact.source_schema_version == "wb-feedback-row-v1"
    assert normalized.source_schema_version == "wb-feedbacks-api-v1"
    # Different representation/provenance must not be hidden by equalizing hashes.
    assert legacy_fact.content_checksum != normalized.content_checksum
    with db[1].begin() as connection:
        shadow.scope(connection)
        repo = shadow.repository_tests.repository(
            connection, account=91103, provider="wb", external="synthetic-c"
        )
        snapshot = repo.get_fact(key)
        persisted = repo._observation(snapshot.review_id, snapshot.current_observation_id)
        for field in ("external_product_id", "source_created_at", "source_updated_at", "rating",
                      "text", "answered", "can_answer", "source_status", "source_schema_version",
                      "normalization_version", "content_checksum"):
            assert persisted[field] == getattr(normalized, field)
        assert persisted["organization_id"] == 91001
        assert persisted["marketplace_account_id"] == 91103
    assert len(provider.requests) == 1  # Pure comparison never refetches.


def _fresh_process_sync(sender, runtime_url, actor, request, raw, fail):
    """New interpreter/engine/provider; only disposable database state survives."""
    from app.platform.integrations import credential_store
    from app.security.marketplace_credentials import CredentialKeyring

    engine = None
    try:
        engine = create_engine(runtime_url, hide_parameters=True)
        provider = FakeWbApiClient(
            fixtures={"/api/v1/feedbacks": {"data": {"feedbacks": [raw]}}},
            errors={"/api/v1/feedbacks": 503} if fail else {},
        )
        legacy_sql = []

        def capture(connection, cursor, statement, *args):
            if "rv_review_" in statement.lower():
                legacy_sql.append(True)

        event.listen(engine, "before_cursor_execute", capture)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(credential_store, "get_session_factory", lambda: sessionmaker(bind=engine))
            patch.setattr(credential_store, "_load_keyring", lambda: CredentialKeyring(
                current_key_version=1, keys={1: b"t" * 32}
            ))
            patch.setattr(canonical_wb_fetch, "build_wb_feedbacks_client", lambda **kw: provider)
            with http_tests.client_for((None, engine), (actor,)) as http:
                response = http.post("/api/v2/reviews/wb/sync", json=request)
            sender.send({"status": response.status_code, "body": response.json(),
                         "calls": len(provider.requests), "pid": os.getpid(),
                         "legacy_sql": bool(legacy_sql)})
    except Exception as error:  # noqa: BLE001 - parent asserts this safe child failure result.
        # Never send raw SQL/provider errors or credentials through test diagnostics.
        sender.send({"error_type": type(error).__name__})
    finally:
        if engine is not None:
            engine.dispose()
        sender.close()


def run_fresh(runtime_url, actor, request, raw, *, fail=False):
    process_context = multiprocessing.get_context("spawn")
    receiver, sender = process_context.Pipe(duplex=False)
    process = process_context.Process(
        target=_fresh_process_sync, args=(sender, runtime_url, actor, request, raw, fail)
    )
    process.start()
    sender.close()
    try:
        assert receiver.poll(30), "Synthetic child did not return within its bound"
        result = receiver.recv()
        process.join(5)
        assert process.exitcode == 0
        assert "error_type" not in result, result
        assert result["pid"] != os.getpid()
        assert result["legacy_sql"] is False
        return result
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()  # Only this test's exact spawned child on failure.
            process.join(5)
        process.close()


def test_failed_fetch_then_new_process_retry_and_replay_keep_one_fact(db, context):
    request = http_tests.payload()
    key = uuid4().hex
    raw = raw_item(key)
    failed = run_fresh(db[1].url, context[0], request, raw, fail=True)
    assert failed["status"] == 502 and failed["calls"] == 1
    assert failed["body"] == {"detail": {"code": "REVIEW_SYNC_PROVIDER_UNAVAILABLE"}}
    with db[0].connect() as connection:
        run = connection.execute(text(
            "SELECT status,observed_count,sync_run_id FROM review_sync_runs_v2 "
            "WHERE source_run_id_utf8=:source"
        ), {"source": ("http:" + request["request_id"]).encode()}).one()
        assert (run.status, run.observed_count) == ("running", 0)
    assert shadow.current(db, key) is None
    retried = run_fresh(db[1].url, context[0], request, raw)
    replayed = run_fresh(db[1].url, context[0], request, raw)
    assert retried["status"] == replayed["status"] == 200
    assert retried["calls"] == replayed["calls"] == 1
    assert retried["body"] == replayed["body"]
    assert retried["body"]["sync_run_id"] == str(run.sync_run_id)
    current = shadow.current(db, key)
    assert (current.revision, current.text, current.can_answer) == (1, "Synthetic parity", None)
    denied = run_fresh(db[1].url, context[0], {**request, "marketplace_account_id": 91101}, raw)
    assert denied["status"] == 403 and denied["calls"] == 0
    with db[0].connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM review_observations WHERE review_id=:id"
        ), {"id": current.review_id}) == 1
