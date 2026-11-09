"""P1 HTTP → real services → final local API role; synthetic source only.

Authentication dependency is overridden with an existing live DB actor: this
does not prove bearer authentication. Publication/setup uses only the disposable
owner; no production role grants are added by this test. Run with allocated PG.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.orders.production_http import (
    ProductionHttpRuntime,
    make_production_router,
    production_actor,
)
from tests import test_notification_preferences_postgres as preferences
from tests import test_production_service as production
from tests import test_review_local_storage_schema as storage

cluster = preferences.cluster
database = preferences.database
prepared = production.prepared
authority = production.authority
case = production.case


@pytest.fixture
def read_db(database):
    with database.owner.begin() as connection:
        storage.seed(connection)
    # Existing publication evidence fixture remains owner-only. Never grant the
    # final API role publisher authority just to construct this synthetic case.
    return database.owner, database.owner


def test_final_api_role_http_create_read_assign_replay_conflict_and_account_denial(database, case):
    owner, _, principal, item, skus = case
    actor = ActorContext(principal.user_id, principal.user_id, principal.organization_id,
        "custom", frozenset(), session_id=principal.session_id)
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    runtime = ProductionHttpRuntime(lambda: Session(database.runtime))
    app = FastAPI()
    app.include_router(make_production_router(max_request_bytes=4096, runtime_dependency=lambda: runtime))
    app.dependency_overrides[production_actor] = lambda: actor
    base = "/api/v2/production/accounts/91101/work-items"
    with TestClient(app) as client:
        created = client.post(base, json={"orderItemId": str(item.order_item_id),
            "expectedSourceItemVersion": str(item.version)})
        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        creation = created.json()
        assert creation["schemaVersion"] == "production-create-v1" and creation["replayed"] is False
        work = creation["workItemId"]
        assert type(work) is str and work.isdecimal()
        before = client.get(f"{base}/{work}")
        assert before.status_code == 200
        assert before.json()["item"]["version"] == "1"
        assert before.json()["item"]["catalogSkuId"] is None
        recovery_path = (
            f"{base}/by-order-item/{item.order_item_id}"
            f"?expectedSourceItemVersion={item.version}"
        )
        recovered = client.get(recovery_path)
        assert recovered.status_code == 200
        assert recovered.headers["cache-control"] == "no-store"
        assert recovered.json() == before.json()
        command = {"expectedVersion": "1", "catalogSkuId": skus[0],
            "idempotencyKey": "synthetic-http-assignment", "reason": "synthetic manual assignment"}
        assigned = client.post(f"{base}/{work}/assignments", json=command)
        assert assigned.status_code == 200
        saved = assigned.json()
        assert saved["result"]["version"] == "2" and saved["replayed"] is False
        assert saved["result"]["catalogSkuId"] == skus[0]
        current = client.get(f"{base}/{work}")
        assert current.status_code == 200
        assert current.json()["item"]["version"] == "2"
        assert current.json()["item"]["catalogSkuId"] == skus[0]
        assert current.json()["item"]["plannedQuantity"] == 0
        assert current.json()["item"]["remainingQuantity"] == current.json()["item"]["requiredQuantity"]
        recovered_after_assignment = client.get(recovery_path)
        assert recovered_after_assignment.status_code == 200
        assert recovered_after_assignment.json() == current.json()
        replay = client.post(f"{base}/{work}/assignments", json=command)
        assert replay.status_code == 200
        assert replay.json() == {**saved, "replayed": True}
        stale = client.post(f"{base}/{work}/assignments", json={**command, "idempotencyKey": "synthetic-stale"})
        assert stale.status_code == 409
        assert stale.json() == {"detail": {"code": "VERSION_CONFLICT"}}
        denied = client.get(f"/api/v2/production/accounts/91102/work-items/{work}")
        assert denied.status_code == 403
        assert denied.json() == {"detail": {"code": "PRODUCTION_DENIED"}}
    assert production.counts(owner, int(work)) == (1, 1, 1)
    with owner.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM lk_audit_events "
            "WHERE object_type='production_work_item' AND object_id=:work "
            "AND action='production.manually_assigned'"), {"work": work}) == 1
