"""Offline HTTP wire/admission controls; actual service acceptance is separate."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.modules.production import AssignmentResult
from app.orders import production_http as http
from app.orders.production_service import (
    ProductionAssignment,
    ProductionCreation,
    ProductionServiceError,
    ProductionWorkItem,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    UserSessionPrincipal,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)
BASE = "/api/v2/production/accounts/2/work-items"
ACTOR = ActorContext(
    "synthetic-user",
    "synthetic-user",
    1,
    "custom",
    frozenset(),
    session_id="synthetic-session",
)
PRINCIPAL = UserSessionPrincipal(1, ACTOR.user_id, 3, ACTOR.session_id)
ACCOUNT = ExpectedAccountBinding(2, "avito", "synthetic-account", None)
CREATE = {"orderItemId": "9007199254740993", "expectedSourceItemVersion": "1"}
ASSIGN = {
    "expectedVersion": "1",
    "catalogSkuId": 4,
    "idempotencyKey": "synthetic-exact-key",
    "reason": "synthetic reason",
}


@pytest.fixture
def client(monkeypatch):
    def no_connection():
        pytest.fail("Offline wire test must not connect to a database")

    engine = create_engine("postgresql+psycopg://", creator=no_connection)
    runtime = http.ProductionHttpRuntime(lambda: Session(engine))
    calls = []

    def discover(session, actor, accounts):
        assert actor == ACTOR and accounts == (2,)
        return PRINCIPAL, (ACCOUNT,)

    def create(session, **values):
        calls.append(values)
        return ProductionCreation(9007199254740993, False)

    def read(session, **values):
        calls.append(values)
        return ProductionWorkItem(
            9007199254740993,
            1,
            2,
            9007199254740994,
            9007199254740995,
            9007199254740996,
            2,
            0,
            2,
            None,
            1,
            NOW,
            NOW,
            None,
        )

    def assign(session, **values):
        calls.append(values)
        return ProductionAssignment(
            AssignmentResult(9007199254740993, 2, 4, 2, 0, 2, 9007199254740996), True
        )

    monkeypatch.setattr(http, "discover_orders_bindings", discover)
    monkeypatch.setattr(http, "create_production_work_item", create)
    monkeypatch.setattr(http, "read_production_work_item", read)
    monkeypatch.setattr(http, "assign_production_work_item", assign)
    app = FastAPI()
    app.include_router(
        http.make_production_router(
            max_request_bytes=1024, runtime_dependency=lambda: runtime
        )
    )
    app.dependency_overrides[http.production_actor] = lambda: ACTOR
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, calls=calls)
    engine.dispose()


def test_exact_bigint_wire_actor_derivation_and_no_store(client):
    created = client.client.post(BASE, json=CREATE)
    assert created.status_code == 200 and created.json() == {
        "schemaVersion": "production-create-v1",
        "workItemId": "9007199254740993",
        "replayed": False,
    }
    assert created.headers["cache-control"] == "no-store"
    assert client.calls[-1] == {
        "principal": PRINCIPAL,
        "account": ACCOUNT,
        "order_item_id": 9007199254740993,
        "expected_source_item_version": 1,
    }
    read = client.client.get(BASE + "/9007199254740993")
    assert read.status_code == 200
    item = read.json()["item"]
    assert (
        item["orderId"] == "9007199254740994"
        and item["sourceItemVersion"] == "9007199254740996"
    )
    assert item["createdAt"] == "2026-09-10T00:00:00.000000Z"
    assert item["catalogSkuId"] is None and item["remainingQuantity"] == 2
    assigned = client.client.post(BASE + "/9007199254740993/assignments", json=ASSIGN)
    assert assigned.status_code == 200
    assert (
        assigned.json()["result"]["version"] == "2"
        and assigned.json()["replayed"] is True
    )
    assert client.calls[-1]["command"].idempotency_key == ASSIGN["idempotencyKey"]


@pytest.mark.parametrize(
    "body",
    [
        dict(CREATE, orderItemId=1),
        dict(CREATE, orderItemId="01"),
        dict(CREATE, orderItemId="9223372036854775808"),
        dict(CREATE, expectedSourceItemVersion=True),
        dict(CREATE, actorMembershipId=3),
        dict(CREATE, organizationId=1),
        dict(CREATE, permissions=["production:create"]),
    ],
)
def test_create_rejects_lossy_or_client_authority_body(client, body):
    result = client.client.post(BASE, json=body)
    assert result.status_code == 400 and not client.calls
    assert result.json() == {"detail": {"code": "PRODUCTION_REQUEST_INVALID"}}


@pytest.mark.parametrize(
    "body",
    [
        dict(ASSIGN, catalogSkuId=True),
        dict(ASSIGN, catalogSkuId=4.0),
        dict(ASSIGN, expectedVersion=1),
        dict(ASSIGN, idempotencyKey=" synthetic"),
        dict(ASSIGN, reason=""),
        dict(ASSIGN, reason="synthetic\x00reason"),
    ],
)
def test_assignment_rejects_coercion_or_nonexact_text(client, body):
    result = client.client.post(BASE + "/1/assignments", json=body)
    assert result.status_code == 400 and not client.calls


@pytest.mark.parametrize(
    "path",
    ["/api/v2/production/accounts/02/work-items/1", BASE + "/01", BASE + "/1?actor=3"],
)
def test_path_and_query_are_exact(client, path):
    assert client.client.get(path).status_code == 400
    assert not client.calls


@pytest.mark.parametrize(
    "body",
    [
        '{"orderItemId":"1","orderItemId":"2","expectedSourceItemVersion":"1"}',
        '{"orderItemId":NaN,"expectedSourceItemVersion":"1"}',
        "[1]",
        "{" + " " * 1024,
    ],
)
def test_duplicate_json_nonfinite_and_byte_budget_reject_before_service(client, body):
    result = client.client.post(
        BASE, content=body, headers={"content-type": "application/json"}
    )
    assert result.status_code == 400 and not client.calls


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("VERSION_CONFLICT", 409, "VERSION_CONFLICT"),
        ("IDEMPOTENCY_CONFLICT", 409, "IDEMPOTENCY_CONFLICT"),
        ("PRODUCTION_DENIED", 403, "PRODUCTION_DENIED"),
        ("PRODUCTION_SOURCE_CHANGED", 409, "PRODUCTION_SOURCE_CHANGED"),
        ("PRODUCTION_STORAGE_UNAVAILABLE", 503, "PRODUCTION_READBACK_REQUIRED"),
        ("synthetic-private-error", 503, "PRODUCTION_READBACK_REQUIRED"),
    ],
)
def test_write_errors_do_not_claim_rollback_or_echo_unknown_details(
    client, monkeypatch, code, status, expected
):
    def fail(*args, **kwargs):
        raise ProductionServiceError(code)

    monkeypatch.setattr(http, "create_production_work_item", fail)
    response = client.client.post(BASE, json=CREATE)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": expected}}
    assert response.headers["cache-control"] == "no-store"


def test_post_commit_serialization_failure_requires_readback(client, monkeypatch):
    monkeypatch.setattr(
        http,
        "create_production_work_item",
        lambda *args, **kwargs: ProductionCreation(2**63, False),
    )
    result = client.client.post(BASE, json=CREATE)
    assert (
        result.status_code == 503
        and result.json()["detail"]["code"] == "PRODUCTION_READBACK_REQUIRED"
    )


def test_factory_default_denies_and_authentication_is_required():
    app = FastAPI()
    app.include_router(http.make_production_router(max_request_bytes=1024))
    with TestClient(app) as client:
        assert client.get(BASE + "/1").status_code == 401
        app.dependency_overrides[http.production_actor] = lambda: ACTOR
        response = client.get(BASE + "/1")
        assert (
            response.status_code == 503
            and response.json()["detail"]["code"] == "PRODUCTION_DISABLED"
        )


def test_openapi_has_exact_request_response_models(client):
    spec = client.client.get("/openapi.json").json()
    path = spec["paths"]["/api/v2/production/accounts/{account_id}/work-items"]
    schema = path["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["orderItemId"]["type"] == "string"
    assert (
        "$ref"
        in path["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    )


def test_bad_factory_cannot_close_callers_existing_root():
    with Session() as session, session.begin():
        runtime = http.ProductionHttpRuntime(lambda: session)
        app = FastAPI()
        app.include_router(
            http.make_production_router(
                max_request_bytes=1024, runtime_dependency=lambda: runtime
            )
        )
        app.dependency_overrides[http.production_actor] = lambda: ACTOR
        with TestClient(app) as client:
            response = client.get(BASE + "/1")
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "PRODUCTION_CONTEXT_INVALID"
        assert session.in_transaction(), (
            "Admission must not close/rollback a borrowed root"
        )
