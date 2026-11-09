"""Canonical single-fact read: real authorization/storage, no provider or writes."""

import importlib
import importlib.util
import re
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.control_plane.auth import ActorContext
from tests import test_review_shadow_service as shadow

cluster = shadow.cluster
db = shadow.db
principal = shadow.principal
context = shadow.context


def module():
    assert importlib.util.find_spec("app.reviews.canonical_read"), "Canonical fact read is missing"
    return importlib.import_module("app.reviews.canonical_read")


def client_for(db, context, *, enabled=True):
    read = module()
    app = FastAPI()
    app.include_router(read.router)
    app.dependency_overrides[read.get_read_actor] = lambda: context[0]
    app.dependency_overrides[read.get_read_engine] = lambda: db[1]
    app.dependency_overrides[read.get_read_settings] = lambda: Settings(
        review_shadow_enabled=enabled,
        review_shadow_account_pairs=((91001, 91103), (91001, 91101), (91001, 91201)),
    )
    return TestClient(app)


def prepare(db, context, *, body="Synthetic read"):
    key = uuid4().hex
    ticket = shadow.start(db, context)
    shadow.api().publish_received_review_rows(
        db[1], ticket=ticket, rows=(shadow.row(key, body),), coverage=shadow.COVERAGE
    )
    with db[0].begin() as connection:
        connection.execute(text(
            "UPDATE iam_memberships SET permissions='[\"reviews:read\"]' "
            "WHERE membership_id=:member"
        ), {"member": ticket.principal.membership_id})
    return key, ticket


def query(key, account=91103):
    return {"marketplace_account_id": account, "external_review_id": key}


def test_disabled_read_does_not_need_engine_or_credentials():
    read = module()
    actor = ActorContext("synthetic", "synthetic", 91001, "custom", frozenset(), session_id=str(uuid4()))
    with pytest.raises(read.ReviewReadError, match="^REVIEW_READ_DISABLED$"):
        read.read_canonical_wb_fact(None, actor=actor, settings=Settings(),
                                    marketplace_account_id=91103, external_review_id="synthetic")


@pytest.mark.parametrize("body", [None, "", " Synthetic\0read "])
def test_read_only_permission_preserves_source_without_provider_or_domain_write(db, context, monkeypatch, body):
    from app.platform.integrations import credential_store
    from app.reviews import canonical_sync, canonical_wb_fetch

    key, _ = prepare(db, context, body=body)
    statements = []

    def forbidden(*args, **kwargs):
        pytest.fail("read attempted credential/provider access")

    monkeypatch.setattr(credential_store, "resolve_marketplace_credential_for_fetch", forbidden)
    monkeypatch.setattr(canonical_sync, "fetch_feedbacks", forbidden)
    monkeypatch.setattr(canonical_wb_fetch, "build_wb_feedbacks_client", forbidden)

    def capture(connection, cursor, statement, *args):
        statements.append(statement)

    event.listen(db[1], "before_cursor_execute", capture)
    try:
        with client_for(db, context) as http:
            response = http.get("/api/v2/reviews/wb/fact", params=query(key))
    finally:
        event.remove(db[1], "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["schema_version"] == "canonical-review-fact-v1"
    assert (data["organization_id"], data["marketplace_account_id"], data["external_review_id"]) == (91001, 91103, key)
    assert (data["text"], data["answered"], data["can_answer"], data["revision"]) == (body, False, None, "1")
    assert data["external_product_id"] == "101"
    assert data["source_schema_version"] == "wb-feedback-row-v1"
    assert re.fullmatch("[0-9a-f]{64}", data["content_checksum"])
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", sql, re.IGNORECASE) for sql in statements)
    assert not any("rv_review_" in sql.lower() for sql in statements)
    assert "credential" not in response.text.lower()


@pytest.mark.parametrize("account", [91101, 91201])
def test_foreign_or_wrong_marketplace_denied_before_review_body_query(db, context, account):
    key, _ = prepare(db, context)
    statements = []

    def capture(connection, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(db[1], "before_cursor_execute", capture)
    try:
        with client_for(db, context) as http:
            response = http.get("/api/v2/reviews/wb/fact", params=query(key, account))
    finally:
        event.remove(db[1], "before_cursor_execute", capture)
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "REVIEW_READ_DENIED"}}
    assert not any("review_facts" in sql or "review_observations" in sql for sql in statements)


@pytest.mark.parametrize("change", ["permission", "account_scope", "session"])
def test_live_revocation_denies_despite_existing_actor(db, context, change):
    key, ticket = prepare(db, context)
    with db[0].begin() as connection:
        if change == "session":
            connection.execute(text("UPDATE lk_sessions SET revoked_at=now() WHERE session_id=:id"),
                               {"id": ticket.principal.session_id})
        else:
            assignment = "permissions='[]'" if change == "permission" else "allowed_account_ids='[]'"
            connection.execute(text(f"UPDATE iam_memberships SET {assignment} WHERE membership_id=:id"),
                               {"id": ticket.principal.membership_id})
    with client_for(db, context) as http:
        response = http.get("/api/v2/reviews/wb/fact", params=query(key))
    assert response.status_code == 403
    assert "Synthetic read" not in response.text


def test_completed_account_rebind_returns_conflict_without_old_text(db, context):
    key, _ = prepare(db, context)
    with db[0].begin() as connection:
        connection.execute(text("UPDATE marketplace_accounts SET external_account_id='synthetic-new' WHERE marketplace_account_id=91103"))
    with client_for(db, context) as http:
        response = http.get("/api/v2/reviews/wb/fact", params=query(key))
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "REVIEW_READ_CONFLICT"}}
    assert "Synthetic read" not in response.text


def test_missing_fact_and_invalid_query_never_become_empty_success(db, context):
    key, _ = prepare(db, context)
    with client_for(db, context) as http:
        missing = http.get("/api/v2/reviews/wb/fact", params=query("not-present"))
        invalid = http.get("/api/v2/reviews/wb/fact", params=query(key, -1))
    assert missing.status_code == 404
    assert missing.json() == {"detail": {"code": "REVIEW_READ_NOT_FOUND"}}
    assert invalid.status_code == 422


def test_bigint_snapshot_version_is_lossless_on_http_wire(db, context, monkeypatch):
    key, _ = prepare(db, context)
    version = 2**53 + 1
    read = module()
    original = read.ReviewFactsRepository.get_fact

    def large_snapshot(repository, external_id):
        # Exercise the service/HTTP projection, not billions of persisted CASes.
        # Keep actual DB identity/binding validation; never bypass its version guard.
        return replace(original(repository, external_id), version=version)

    monkeypatch.setattr(read.ReviewFactsRepository, "get_fact", large_snapshot)
    with client_for(db, context) as http:
        response = http.get("/api/v2/reviews/wb/fact", params=query(key))
    assert response.status_code == 200, response.text
    assert response.json()["version"] == str(version)


def test_unavailable_storage_is_safe_error_not_empty_success(db, context):
    key, _ = prepare(db, context)
    read = module()
    with client_for(db, context) as http:
        http.app.dependency_overrides[read.get_read_engine] = lambda: None
        response = http.get("/api/v2/reviews/wb/fact", params=query(key))
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "REVIEW_READ_STORAGE_UNAVAILABLE"}}


def test_real_signed_auth_read_and_missing_or_tampered_bearer(db, context, monkeypatch):
    from app.cabinet import store
    from app.control_plane import auth

    key, _ = prepare(db, context)
    read = module()
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
        auth_secret="synthetic-read-auth-at-least-32-bytes", auth_access_ttl_seconds=120
    ))

    def local_db(operation):
        with Session(db[1]) as session:
            return operation(session)

    monkeypatch.setattr(store, "_run_db", local_db)
    token = auth._issue_access_token(actor=context[0]).access_token
    with client_for(db, context) as http:
        del http.app.dependency_overrides[read.get_read_actor]
        missing = http.get("/api/v2/reviews/wb/fact", params=query(key))
        bad = http.get("/api/v2/reviews/wb/fact", params=query(key), headers={"Authorization": "Bearer synthetic-sensitive"})
        valid = http.get("/api/v2/reviews/wb/fact", params=query(key), headers={"Authorization": "Bearer " + token})
    assert missing.status_code == bad.status_code == 401
    assert "synthetic-sensitive" not in bad.text
    assert valid.status_code == 200, valid.text
