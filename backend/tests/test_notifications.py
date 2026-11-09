from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cabinet.orm import LkOrganizationRow
from app.main import create_app
from app.notifications import record_wb_sync_notification
from app.repricer_cache import store as cache_store
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def isolated_source_cache(monkeypatch):
    # Exercise the actual legacy SQL repository without an application DB/Redis.
    # SQLite is sufficient for this payload roundtrip, never a PG/RLS/CAS proof.
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    LkOrganizationRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(LkOrganizationRow(organization_id=1, slug="synthetic-notifications", name="Synthetic"))
        session.commit()
    monkeypatch.setattr(cache_store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cache_store, "_redis_get_json", lambda _key: None)
    monkeypatch.setattr(cache_store, "_redis_set_json", lambda *_args: None)
    monkeypatch.setattr(cache_store, "_redis_delete", lambda *_args: None)
    try:
        yield factory
    finally:
        engine.dispose()


def test_notifications_api_returns_and_marks_wb_sync_event_read(isolated_source_cache):
    api = client()
    headers = auth_headers(api, "admin")
    run_id = f"wb_sync_test_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    record_wb_sync_notification(
        1,
        {
            "state": "completed",
            "runId": run_id,
            "finishedAt": datetime.now(UTC).isoformat(),
            "periodDays": 30,
            "steps": [{"source": "goods", "status": "ok", "count": 12}],
        },
    )
    with isolated_source_cache() as session:
        stored = session.scalar(select(WbRepricerSourceCacheRow).where(
            WbRepricerSourceCacheRow.organization_id == 1,
            WbRepricerSourceCacheRow.source_key == "notifications",
        ))
        assert stored.payload["items"][0]["entityId"] == run_id

    listing = api.get("/api/v1/notifications", headers=headers)
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    event = next(item for item in items if item["entityId"] == run_id)
    assert event["title"] == "WB-данные обновлены"
    assert event["readAt"] is None

    marked = api.post(f"/api/v1/notifications/{event['id']}/read", headers=headers)
    assert marked.status_code == 200
    marked_items = marked.json()["data"]["items"]
    marked_event = next(item for item in marked_items if item["id"] == event["id"])
    assert marked_event["readAt"] is not None
    with isolated_source_cache() as session:
        stored = session.scalar(select(WbRepricerSourceCacheRow).where(
            WbRepricerSourceCacheRow.organization_id == 1,
            WbRepricerSourceCacheRow.source_key == "notifications",
        ))
        assert stored.payload["items"][0]["readAt"] == marked_event["readAt"]
