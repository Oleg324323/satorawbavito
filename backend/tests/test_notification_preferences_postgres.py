"""Disposable PostgreSQL proof, run only with the coordinator's heavy slot."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.cabinet.orm import (
    LkOrganizationRow,
    LkSessionRow,
    LkUserPreferenceRow,
    LkUserRow,
)
from app.control_plane.auth import ActorContext
from app.notification_preferences_read import NotificationPreferenceFlags
from app.platform.identity.orm import IamMembershipRow
from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
FLAGS = NotificationPreferenceFlags(False, True, False, True)
LEGACY = {"email": {"enabled": True, "dailyDigest": False, "criticalAlerts": True},
          "telegram": {"enabled": False, "chatId": "private-destination-marker"}}


@pytest.fixture(scope="module")
def database(cluster):
    roles = tuple("prefs_" + uuid4().hex for _ in range(3))
    with candidate.disposable_database(cluster, roles) as db:
        result = candidate.migrate(db.url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        owner = create_engine(db.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=roles[0]), hide_parameters=True)
        try:
            sql = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))
            for old, new in (("satorna_wb_live", db.name), ("wb_live_owner", db.owner),
                             ("wb_live_api", roles[0]), ("wb_live_worker", roles[1]), ("wb_live_dispatch", roles[2])):
                sql = sql.replace(old, new)
            with owner.begin() as c:
                c.execute(text(sql))
            yield SimpleNamespace(owner=owner, runtime=runtime, db=db, roles=roles)
        finally:
            runtime.dispose()
            owner.dispose()


@pytest.fixture
def state(database):
    org = uuid4().int % 100000000 + 100
    user, session_id = "prefs-user-" + uuid4().hex, "prefs-session-" + uuid4().hex
    with Session(database.owner) as session, session.begin():
        session.add(LkOrganizationRow(organization_id=org, slug="prefs-" + uuid4().hex, name="Synthetic"))
        session.flush()
        session.add(LkUserRow(user_id=user, organization_id=org, email=user + "@example.invalid",
                              password_hash="unusable", full_name="Synthetic", permission_profile="custom"))
        session.flush()
        session.add(IamMembershipRow(membership_id=org, organization_id=org, user_id=user, role="custom",
                                    permissions=["preferences:read", "preferences:write"], scope_mode="all", is_active=True))
        session.add(LkSessionRow(session_id=session_id, user_id=user, issued_at=datetime.now(UTC),
                                last_seen_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(hours=1)))
        session.add(LkUserPreferenceRow(user_id=user, notification_settings=LEGACY,
                                       export_settings={"custom": [1, {"preserved": True}]}, timezone="Europe/Moscow"))
    return SimpleNamespace(**vars(database), actor=ActorContext(user, user, org, "custom", frozenset(), session_id=session_id))


def service(state):
    from app.notification_preferences_service import NotificationPreferencesService
    return NotificationPreferencesService(engine=state.runtime, enabled=True)


def read_row(state):
    with state.owner.connect() as c:
        return c.execute(text("SELECT notification_settings, notification_version, export_settings, timezone "
                              "FROM lk_user_preferences WHERE user_id=:u"), {"u": state.actor.user_id}).one()


def test_legacy_changes_advance_version_and_refuse_rewind(state):
    assert read_row(state).notification_version == 1
    with state.owner.begin() as c:
        c.execute(text("UPDATE lk_user_preferences SET notification_settings=CAST(:p AS json) WHERE user_id=:u"),
                  {"p": json.dumps({**LEGACY, "telegram": {"enabled": True, "chatId": None}}), "u": state.actor.user_id})
    assert read_row(state).notification_version == 2
    with pytest.raises(IntegrityError), state.owner.begin() as c:
        c.execute(text("UPDATE lk_user_preferences SET notification_version=1 WHERE user_id=:u"), {"u": state.actor.user_id})
    assert read_row(state).notification_version == 2


def test_used_schema_downgrade_refuses_to_erase_versions(state):
    with state.owner.begin() as c:
        initial_revision = c.scalar(text("SELECT version_num FROM alembic_version"))
        c.execute(text("UPDATE lk_user_preferences SET notification_version=notification_version+1 WHERE user_id=:u"),
                  {"u": state.actor.user_id})
    result = candidate.migrate(state.db.url, "downgrade", "20260910_0080")
    assert result.returncode != 0
    assert "preferences_downgrade_would_lose_versions" in result.stderr
    with state.owner.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == initial_revision
    assert read_row(state).notification_version == 2


def test_replace_preserves_private_preferences_and_atomic_safe_audit(state):
    svc = service(state)
    assert svc.get_current(authenticated_actor=state.actor)["version"] == "1"
    result = svc.replace(authenticated_actor=state.actor, expected_version="1", flags=FLAGS)
    assert result == {"schemaVersion": "notification-preferences-v1", "version": "2",
                      "email": {"enabled": False, "dailyDigest": True, "criticalAlerts": False}, "telegram": {"enabled": True}}
    row = read_row(state)
    assert row.notification_settings["telegram"]["chatId"] == "private-destination-marker"
    assert row.export_settings == {"custom": [1, {"preserved": True}]}
    assert row.timezone == "Europe/Moscow"
    with state.owner.connect() as c:
        audit = c.execute(text("SELECT actor_user_id,details,before_state,after_state FROM lk_audit_events "
                               "WHERE organization_id=:o AND action='notifications.preferences.replace'"),
                          {"o": state.actor.organization_id}).one()
    assert audit.actor_user_id == state.actor.user_id
    assert audit.details == {"oldVersion": "1", "newVersion": "2"}
    assert audit.before_state is None and audit.after_state is None
    assert "private-destination-marker" not in repr(audit)


def test_two_same_version_replacements_commit_once(state):
    from app.notification_preferences_service import NotificationPreferencesError
    barrier = Barrier(2)
    def attempt():
        barrier.wait(timeout=10)
        try:
            return service(state).replace(authenticated_actor=state.actor, expected_version="1", flags=FLAGS)["version"]
        except NotificationPreferencesError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["2", "PREFERENCES_CONFLICT"]
    assert read_row(state).notification_version == 2


@pytest.mark.parametrize("changed", ["organization", "session", "membership", "permission"])
def test_current_database_authority_required_despite_actor_cache(state, changed):
    from app.notification_preferences_service import NotificationPreferencesError
    actor = replace(state.actor, permissions=frozenset({"preferences:read", "preferences:write"}))
    if changed == "organization":
        actor = replace(actor, organization_id=actor.organization_id + 1)
    else:
        with state.owner.begin() as c:
            sql = {"session": "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE user_id=:u",
                   "membership": "UPDATE iam_memberships SET is_active=false WHERE user_id=:u",
                   "permission": "UPDATE iam_memberships SET permissions='[]' WHERE user_id=:u"}[changed]
            c.execute(text(sql), {"u": actor.user_id})
    with pytest.raises(NotificationPreferencesError, match="^PREFERENCES_DENIED$"):
        service(state).get_current(authenticated_actor=actor)


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_preferences_do_not_create_fallback(state, corrupt):
    from app.notification_preferences_service import NotificationPreferencesError
    with state.owner.begin() as c:
        c.execute(text("UPDATE lk_user_preferences SET notification_settings='{}' WHERE user_id=:u" if corrupt
                       else "DELETE FROM lk_user_preferences WHERE user_id=:u"), {"u": state.actor.user_id})
    with pytest.raises(NotificationPreferencesError, match="^PREFERENCES_UNAVAILABLE$"):
        service(state).get_current(authenticated_actor=state.actor)


def test_audit_failure_rolls_back_preference_replace(state):
    from app.notification_preferences_service import NotificationPreferencesError
    def fail(c, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO lk_audit_events"):
            raise RuntimeError("audit unavailable")
    event.listen(state.runtime, "before_cursor_execute", fail)
    try:
        with pytest.raises(NotificationPreferencesError, match="^PREFERENCES_UNAVAILABLE$"):
            service(state).replace(authenticated_actor=state.actor, expected_version="1", flags=FLAGS)
    finally:
        event.remove(state.runtime, "before_cursor_execute", fail)
    assert read_row(state).notification_version == 1


def test_unknown_commit_requires_readback_even_when_write_committed(state, monkeypatch):
    import app.notification_preferences_service as module
    real_commit = Session.commit
    class UnknownCommitSession(Session):
        def commit(self):
            real_commit(self)
            raise RuntimeError("unknown transport result")
    monkeypatch.setattr(module, "Session", UnknownCommitSession)
    with pytest.raises(module.NotificationPreferencesError, match="^PREFERENCES_READBACK_REQUIRED$"):
        service(state).replace(authenticated_actor=state.actor, expected_version="1", flags=FLAGS)
    assert read_row(state).notification_version == 2


def test_local_api_notification_rights_are_read_and_receipt_only(database):
    with database.owner.connect() as c:
        row = c.execute(text("SELECT has_table_privilege(:r,'notification_in_app_events','SELECT'),"
            "has_table_privilege(:r,'notification_in_app_events','INSERT'),"
            "has_table_privilege(:r,'notification_in_app_receipts','INSERT'),"
            "has_column_privilege(:r,'notification_in_app_receipts','read_at','UPDATE')"), {"r": database.roles[0]}).one()
    assert tuple(row) == (True, False, True, True)
    with database.runtime.connect() as c:
        encoded = c.scalar(text("SELECT notification_in_app_receipt_bytes(ROW(1,2,'wb',CAST(:id AS uuid),"
            "3,TIMESTAMPTZ '2026-09-10 10:00:00+00',NULL,1)::notification_in_app_receipts)"), {"id": str(uuid4())})
    assert json.loads(bytes(encoded))["recipientMembershipId"] == 3


def test_activated_legacy_writer_fences_notifications_but_allows_other_preferences(state, monkeypatch):
    from fastapi import HTTPException

    from app.cabinet import store
    from app.config import Settings
    monkeypatch.setattr(store, "get_settings", lambda: Settings(canonical_notifications_enabled=True))
    monkeypatch.setattr(store, "get_engine", lambda: state.runtime)
    monkeypatch.setattr(store, "get_session_factory", lambda: sessionmaker(bind=state.runtime))
    kwargs = {"user_id": state.actor.user_id, "organization_id": state.actor.organization_id,
              "actor_user_id": state.actor.user_id, "export_settings": {"kept": True}, "timezone_value": "UTC"}
    with pytest.raises(HTTPException) as error:
        store.update_preferences(**kwargs, notification_settings={**LEGACY, "telegram": {"enabled": True, "chatId": None}})
    assert error.value.status_code == 409
    assert read_row(state).notification_version == 1
    store.update_preferences(**kwargs, notification_settings=LEGACY)
    row = read_row(state)
    assert row.export_settings == {"kept": True} and row.timezone == "UTC"
    assert row.notification_version == 1


def test_activated_legacy_writer_cannot_fall_back_after_database_failure(state, monkeypatch):
    from fastapi import HTTPException

    from app.cabinet import store
    from app.config import Settings
    monkeypatch.setattr(store, "get_settings", lambda: Settings(canonical_notifications_enabled=True))
    monkeypatch.setattr(store, "get_engine", lambda: state.runtime)
    monkeypatch.setattr(store, "_run_db", lambda callback: None)
    with pytest.raises(HTTPException) as error:
        store.update_preferences(user_id=state.actor.user_id, organization_id=state.actor.organization_id,
            actor_user_id=state.actor.user_id, notification_settings=LEGACY, export_settings={}, timezone_value="UTC")
    assert error.value.status_code == 503
    assert state.actor.user_id not in store._MEMORY.preferences
