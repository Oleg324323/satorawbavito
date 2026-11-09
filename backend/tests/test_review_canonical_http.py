"""Canonical-only manual sync; real owned PostgreSQL, provider replaced at boundary."""

import importlib
import importlib.util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings
from tests import test_review_shadow_service as shadow

cluster = shadow.cluster
db = shadow.db
principal = shadow.principal
context = shadow.context


def modules():
    assert importlib.util.find_spec("app.reviews.canonical_router"), (
        "missing canonical-only HTTP"
    )
    return (
        importlib.import_module("app.reviews.canonical_router"),
        importlib.import_module("app.reviews.canonical_sync"),
    )


def client_for(db, context, *, enabled=True):
    router, _ = modules()
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_review_actor] = lambda: context[0]
    app.dependency_overrides[router.get_review_engine] = lambda: db[1]
    app.dependency_overrides[router.get_review_settings] = lambda: Settings(
        review_shadow_enabled=enabled,
        review_shadow_account_pairs=((91001, 91103), (91001, 91101)),
    )
    return TestClient(app)


def payload(account=91103):
    return {
        "marketplace_account_id": account,
        "request_id": str(uuid4()),
        "is_answered": False,
    }


def test_default_off_denies_before_provider(db, context, monkeypatch):
    _, service = modules()
    monkeypatch.setattr(
        service, "fetch_feedbacks", lambda **kw: pytest.fail("disabled fetch")
    )
    with client_for(db, context, enabled=False) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_DISABLED"}}


def test_denied_account_precedes_credential_resolution_and_fetch(
    db, context, monkeypatch
):
    _, service = modules()
    for name in ("fetch_feedbacks", "resolve_marketplace_credential_for_fetch"):
        monkeypatch.setattr(
            service, name, lambda *a, **kw: pytest.fail("denied account side effect")
        )
    with client_for(db, context) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload(91101))
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_DENIED"}}


def test_one_paired_fetch_publishes_only_canonical_rows(db, context, monkeypatch):
    _, service = modules()
    key, calls, statements = uuid4().hex, [], []
    request = payload()

    def fetch(**kwargs):
        calls.append(kwargs)
        assert kwargs["wb_token"] == "SYNTHETIC_REVIEW_SHADOW_TOKEN"
        assert kwargs["is_answered"] is False
        with db[0].connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM review_sync_runs_v2 WHERE status='running' AND source_run_id_utf8=:source"
                    ),
                    {"source": ("http:" + request["request_id"]).encode()},
                )
                == 1
            )
        return [shadow.row(key)]

    def capture(connection, cursor, statement, *args):
        statements.append(statement)

    monkeypatch.setattr(service, "fetch_feedbacks", fetch)
    event.listen(db[1], "before_cursor_execute", capture)
    try:
        with client_for(db, context) as client:
            response = client.post("/api/v2/reviews/wb/sync", json=request)
    finally:
        event.remove(db[1], "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.json()["observed_count"] == 1
    assert response.json()["completeness"] == "partial"
    assert len(calls) == 1
    assert shadow.current(db, key).text == "received\0text"
    assert not any("rv_review_" in statement.lower() for statement in statements)
    assert "received" not in response.text


def test_real_signed_auth_and_safe_missing_tampered_bearer(db, context, monkeypatch):
    router, service = modules()
    from app.cabinet import store
    from app.control_plane import auth

    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            auth_secret="synthetic-auth-key-at-least-32-bytes",
            auth_access_ttl_seconds=120,
        ),
    )

    def local_db(operation):
        with Session(db[1]) as session:
            return operation(session)

    monkeypatch.setattr(store, "_run_db", local_db)
    monkeypatch.setattr(service, "fetch_feedbacks", lambda **kw: [])
    token = auth._issue_access_token(actor=context[0]).access_token
    with client_for(db, context) as client:
        del client.app.dependency_overrides[router.get_review_actor]
        missing = client.post("/api/v2/reviews/wb/sync", json=payload())
        tampered = client.post(
            "/api/v2/reviews/wb/sync",
            json=payload(),
            headers={"Authorization": "Bearer synthetic-sensitive-invalid"},
        )
        response = client.post(
            "/api/v2/reviews/wb/sync",
            json=payload(),
            headers={"Authorization": "Bearer " + token},
        )
    assert missing.status_code == tampered.status_code == 401
    assert "synthetic-sensitive" not in tampered.text
    assert response.status_code == 200, response.text
    assert response.json()["observed_count"] == 0


@pytest.mark.parametrize("change", ["session", "credential"])
def test_revocation_during_fetch_denies_publication(db, context, monkeypatch, change):
    _, service = modules()
    key = uuid4().hex

    def fetch(**kw):
        if change == "credential":
            shadow.store.revoke_marketplace_credential(
                context[2], "wb_api", "operator_revoked"
            )
        else:
            with db[0].begin() as connection:
                connection.execute(
                    text(
                        "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                    ),
                    {"id": context[0].session_id},
                )
        return [shadow.row(key)]

    monkeypatch.setattr(service, "fetch_feedbacks", fetch)
    with client_for(db, context) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 403
    assert shadow.current(db, key) is None


def test_provider_error_is_safe_and_never_publishes(db, context, monkeypatch):
    _, service = modules()

    def fetch(**kw):
        raise service.WbFeedbacksFetchError(
            status_code=502,
            code="synthetic-private",
            message="synthetic-private-payload",
        )

    monkeypatch.setattr(service, "fetch_feedbacks", fetch)
    with client_for(db, context) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 502
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_PROVIDER_UNAVAILABLE"}}


def test_injected_publication_commit_error_rolls_back_with_safe_http_error(
    db, context, monkeypatch
):
    _, service = modules()
    key = uuid4().hex

    def fail_commit(connection):
        raise OperationalError(
            "synthetic-private-SQL", {}, RuntimeError("synthetic-private-payload")
        )

    def fetch(**kw):
        event.listen(db[1], "commit", fail_commit)
        return [shadow.row(key)]

    monkeypatch.setattr(service, "fetch_feedbacks", fetch)
    try:
        with client_for(db, context) as client:
            response = client.post("/api/v2/reviews/wb/sync", json=payload())
    finally:
        event.remove(db[1], "commit", fail_commit)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_STORAGE_UNAVAILABLE"}}
    assert shadow.current(db, key) is None


def test_preflight_account_rebind_before_paired_resolution_denies_fetch(
    db, context, monkeypatch
):
    _, service = modules()
    resolve = service.resolve_marketplace_credential_for_fetch

    def rebound(*args):
        with db[0].begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91103"
                )
            )
        return resolve(*args)

    monkeypatch.setattr(service, "resolve_marketplace_credential_for_fetch", rebound)
    calls = []
    monkeypatch.setattr(service, "fetch_feedbacks", lambda **kw: calls.append(1) or [])
    with client_for(db, context) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("body", [None, "", "  exact\0body  "])
def test_actual_raw_adapter_http_preserves_lossless_source(
    db, context, monkeypatch, body
):
    from app.reviews import canonical_wb_fetch
    from app.wb_api.client import FakeWbApiClient

    key = uuid4().hex + "\0exact"
    raw = {
        "id": key,
        "createdDate": "2026-09-09T00:00:00Z",
        "text": body,
        "productDetails": {"nmId": 101},
        "productValuation": 5,
        "answer": None,
    }
    provider = FakeWbApiClient(
        fixtures={"/api/v1/feedbacks": {"data": {"feedbacks": [raw]}}}
    )

    def client(**kwargs):
        assert kwargs == {"token_override": "SYNTHETIC_REVIEW_SHADOW_TOKEN"}
        return provider

    monkeypatch.setattr(canonical_wb_fetch, "build_wb_feedbacks_client", client)
    with client_for(db, context) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 200, response.text
    persisted = shadow.current(db, key)
    assert persisted.text == body
    with db[0].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT external_review_id_utf8 FROM review_facts WHERE review_id=:id"
                ),
                {"id": persisted.review_id},
            )
            == key.encode()
        )
        assert (
            connection.scalar(
                text(
                    "SELECT source_schema_version_utf8 FROM review_observations WHERE observation_id=:id"
                ),
                {"id": persisted.current_observation_id},
            )
            == b"wb-feedbacks-api-v1"
        )
    assert len(provider.requests) == 1
    assert provider.requests[0].query["isAnswered"] is False


@pytest.mark.parametrize("created", [None, "not-a-date"])
def test_actual_raw_invalid_timestamp_never_becomes_now(
    db, context, monkeypatch, created
):
    from app.reviews import canonical_wb_fetch
    from app.wb_api.client import FakeWbApiClient

    key = uuid4().hex
    raw = {
        "id": key,
        "createdDate": created,
        "text": "private body",
        "productDetails": {"nmId": 101},
        "answer": None,
    }
    provider = FakeWbApiClient(
        fixtures={"/api/v1/feedbacks": {"data": {"feedbacks": [raw]}}}
    )
    monkeypatch.setattr(
        canonical_wb_fetch, "build_wb_feedbacks_client", lambda **kw: provider
    )
    with client_for(db, context) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 400
    assert shadow.current(db, key) is None
    assert "private body" not in response.text


@pytest.mark.parametrize("change", ["permission", "scope"])
def test_removed_live_permission_or_scope_denies_before_fetch(
    db, context, monkeypatch, change
):
    _, service = modules()
    with db[0].begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"
                if change == "permission"
                else "UPDATE iam_memberships SET allowed_account_ids='[91101]' WHERE user_id=:id"
            ),
            {"id": context[0].user_id},
        )
    monkeypatch.setattr(
        service, "fetch_feedbacks", lambda **kw: pytest.fail("permission denied fetch")
    )
    with client_for(db, context) as client:
        response = client.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 403


def test_exact_http_replay_and_changed_request_conflict(db, context, monkeypatch):
    _, service = modules()
    key, calls, request = uuid4().hex, [], payload()
    monkeypatch.setattr(
        service, "fetch_feedbacks", lambda **kw: calls.append(1) or [shadow.row(key)]
    )
    with client_for(db, context) as client:
        first = client.post("/api/v2/reviews/wb/sync", json=request)
        replay = client.post("/api/v2/reviews/wb/sync", json=request)
        changed = client.post("/api/v2/reviews/wb/sync", json={**request, "take": 50})
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert changed.status_code == 409
    assert len(calls) == 2
    assert shadow.current(db, key).revision == 1


def test_engine_configuration_error_has_safe_typed_response(db, context, monkeypatch):
    router, _ = modules()

    def invalid_config():
        raise RuntimeError("synthetic-private-configuration")

    monkeypatch.setattr(router, "get_engine", invalid_config)
    client = client_for(db, context)
    del client.app.dependency_overrides[router.get_review_engine]
    with TestClient(client.app, raise_server_exceptions=False) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_CONFIGURATION_INVALID"}}


def test_request_validation_and_openapi_do_not_call_provider(db, context, monkeypatch):
    _, service = modules()
    monkeypatch.setattr(
        service, "fetch_feedbacks", lambda **kw: pytest.fail("invalid request fetch")
    )
    with client_for(db, context) as http:
        for change in (
            {"marketplace_account_id": True},
            {"is_answered": "false"},
            {"take": 5001},
            {"legacy": True},
        ):
            assert (
                http.post(
                    "/api/v2/reviews/wb/sync", json={**payload(), **change}
                ).status_code
                == 422
            )
        spec = http.get("/openapi.json").json()
    assert (
        spec["components"]["schemas"]["ReviewCanonicalSyncResponse"]["properties"][
            "sync_run_id"
        ]["format"]
        == "uuid"
    )
    assert (
        spec["components"]["schemas"]["ReviewCanonicalSyncRequest"][
            "additionalProperties"
        ]
        is False
    )


def test_credential_storage_outage_is_503_without_fetch(db, context, monkeypatch):
    _, service = modules()

    def outage(*args):
        raise service.CredentialStoreError("credential_persistence_failed")

    monkeypatch.setattr(service, "resolve_marketplace_credential_for_fetch", outage)
    monkeypatch.setattr(
        service, "fetch_feedbacks", lambda **kw: pytest.fail("outage fetch")
    )
    with client_for(db, context) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "REVIEW_SYNC_STORAGE_UNAVAILABLE"}}


def test_canonical_path_never_reads_or_writes_legacy_store(db, context, monkeypatch):
    from app.reviews import service as legacy

    _, service = modules()

    class ForbiddenMemory:
        def __getattribute__(self, name):
            pytest.fail("canonical touched legacy memory")

        def __setattr__(self, name, value):
            pytest.fail("canonical wrote legacy memory")

    monkeypatch.setattr(legacy, "_MEMORY", ForbiddenMemory())
    monkeypatch.setattr(
        legacy, "_run_db", lambda *a, **kw: pytest.fail("canonical called legacy DB")
    )
    key = uuid4().hex
    monkeypatch.setattr(service, "fetch_feedbacks", lambda **kw: [shadow.row(key)])
    with client_for(db, context) as http:
        response = http.post("/api/v2/reviews/wb/sync", json=payload())
    assert response.status_code == 200
    assert shadow.current(db, key).text == "received\0text"
