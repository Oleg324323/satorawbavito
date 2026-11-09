from copy import deepcopy

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow, LkUserRow, LkUserPreferenceRow
from app.platform.identity.orm import IamMembershipRow
from app.notification_preferences_read import PreferencesReadError, read_notification_preferences


def settings():
    return {"email": {"enabled": True, "dailyDigest": False, "criticalAlerts": True},
            "telegram": {"enabled": False, "chatId": "synthetic-private-destination"}}


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [LkOrganizationRow.__table__, LkUserRow.__table__,
              LkUserPreferenceRow.__table__, IamMembershipRow.__table__]
    for table in tables:
        table.create(engine)
    with Session(engine) as session:
        session.add_all([LkOrganizationRow(organization_id=i, slug=f"test-{i}", name="test") for i in (1, 2)])
        session.add_all([LkUserRow(user_id=f"user-{i}", organization_id=i, email=f"test-{i}@example.invalid",
                                  password_hash="synthetic-unusable", full_name="test", permission_profile="viewer")
                         for i in (1, 2)])
        session.add_all([IamMembershipRow(membership_id=i, organization_id=i, user_id=f"user-{i}", role="viewer")
                         for i in (1, 2)])
        session.add(LkUserPreferenceRow(user_id="user-1", notification_settings=settings(), export_settings={}, timezone="UTC"))
        session.commit()
        yield session
    engine.dispose()


def read(db, **changes):
    kwargs = dict(organization_id=1, membership_id=1, user_id="user-1")
    kwargs.update(changes)
    return read_notification_preferences(db, **kwargs)


def test_typed_projection_reads_existing_owner_without_exposing_destination(db):
    result = read(db)
    assert result.email_enabled is True
    assert result.daily_digest is False
    assert result.critical_alerts is True
    assert result.telegram_enabled is False
    assert "synthetic-private" not in repr(result)
    assert not hasattr(result, "chat_id")


@pytest.mark.parametrize("scope", [dict(organization_id=2), dict(membership_id=2), dict(user_id="user-2")])
def test_cross_scope_or_wrong_user_is_unavailable(db, scope):
    with pytest.raises(PreferencesReadError, match="PREFERENCES_UNAVAILABLE"):
        read(db, **scope)


def test_missing_preference_does_not_create_defaults(db):
    before = db.scalar(select(LkUserPreferenceRow.preference_id).where(LkUserPreferenceRow.user_id == "user-2"))
    assert before is None
    with pytest.raises(PreferencesReadError, match="PREFERENCES_UNAVAILABLE"):
        read(db, organization_id=2, membership_id=2, user_id="user-2")
    assert db.scalar(select(LkUserPreferenceRow.preference_id).where(LkUserPreferenceRow.user_id == "user-2")) is None


@pytest.mark.parametrize("model", [IamMembershipRow, LkUserRow])
def test_revocation_is_observed_even_with_prior_loaded_orm_objects(db, model):
    read(db)
    row = db.get(model, 1 if model is IamMembershipRow else "user-1")
    with db.bind.begin() as connection:
        connection.execute(update(model).values(is_active=False))
    assert row.is_active is True  # Identity-map object is deliberately stale.
    with pytest.raises(PreferencesReadError, match="PREFERENCES_UNAVAILABLE"):
        read(db)


@pytest.mark.parametrize("payload", [None, [], {}, {"email": {"enabled": True}},
    dict(settings(), extra="synthetic-private"),
    {"email": {"enabled": 1, "dailyDigest": False, "criticalAlerts": True}, "telegram": {"enabled": False, "chatId": None}},
    {"email": {"enabled": True, "dailyDigest": False, "criticalAlerts": True}, "telegram": {"enabled": "false", "chatId": None}},
    {"email": {"enabled": True, "dailyDigest": False, "criticalAlerts": True}, "telegram": {"enabled": False, "chatId": 123}},
])
def test_legacy_partial_or_malformed_shape_fails_closed(db, payload):
    db.execute(update(LkUserPreferenceRow).values(notification_settings=payload))
    db.commit()
    with pytest.raises(PreferencesReadError) as error:
        read(db)
    assert str(error.value) == "PREFERENCES_INVALID"


def test_read_does_not_autoflush_or_commit_caller_changes(db):
    row = db.get(LkUserRow, "user-1")
    row.full_name = "synthetic-pending-name"
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        read(db)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert statements and set(statements) == {"SELECT"}
    assert row in db.dirty
    db.rollback()
    assert db.get(LkUserRow, "user-1").full_name == "test"


def test_database_failure_is_safe_and_no_alternate_owner_is_used(db):
    LkUserPreferenceRow.__table__.drop(db.bind)
    with pytest.raises(PreferencesReadError) as error:
        read(db)
    assert str(error.value) == "PREFERENCES_STORAGE_UNAVAILABLE"
    assert "SELECT" not in str(error.value)


@pytest.mark.parametrize("changes", [dict(organization_id=True), dict(membership_id=0), dict(user_id="")])
def test_invalid_scope_is_rejected_before_query(db, changes):
    with pytest.raises(PreferencesReadError, match="PREFERENCES_INVALID_SCOPE"):
        read(db, **changes)


def test_preference_update_is_reloaded_without_retaining_mutable_payload(db):
    first = read(db)
    payload = deepcopy(settings())
    payload["telegram"]["enabled"] = True
    db.execute(update(LkUserPreferenceRow).values(notification_settings=payload))
    db.commit()
    second = read(db)
    assert first.telegram_enabled is False and second.telegram_enabled is True
