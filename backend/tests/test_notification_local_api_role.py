"""Final launcher API grants, actual services; no bearer HTTP or provider proof.

Uses the existing disposable three-role allocator/grants fixture. Owner-only
synthetic setup publishes the source event; every receipt/preference operation
uses the final API role, without adding producer grants. Run only with PG slot.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.control_plane.auth import ActorContext
from app.notification_list import NotificationListCursorCodec
from app.notification_preferences_service import NotificationPreferencesService
from app.notification_service import ReviewNotificationService
from tests import test_notification_preferences_postgres as preferences
from tests import test_review_local_service as local
from tests import test_review_local_storage_schema as storage
from tests import test_review_notification_send_acceptance as acceptance

cluster = preferences.cluster
database = preferences.database
principal = local.principal
actor = local.actor
prepared_notification = acceptance.prepared_notification


@pytest.fixture
def db(database):
    # Publication requires producer privileges; never broaden the API role to
    # run this setup. The allocator owner is confined to this disposable DB.
    with database.owner.begin() as connection:
        storage.seed(connection)
    return database.owner, database.owner


def test_final_api_role_lists_marks_dismisses_own_receipts_and_replaces_preferences(
    database, db, actor, prepared_notification,
):
    identifier = prepared_notification.event_id
    other_login = uuid4().hex
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    with database.owner.begin() as connection:
        rights = connection.execute(text(
            "SELECT has_table_privilege(:r,'notification_in_app_events','INSERT'),"
            "has_table_privilege(:r,'review_draft_revisions','INSERT')"
        ), {"r": database.roles[0]}).one()
        # Manual review drafts are allowed; notification publication remains producer-only.
        assert tuple(rights) == (False, True)
        connection.execute(text(
            "UPDATE iam_memberships SET permissions='[\"reviews:read\",\"preferences:read\",\"preferences:write\"]' "
            "WHERE user_id IN (:actor,'local78')"
        ), {"actor": actor.user_id})
        connection.execute(text(
            "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) "
            "VALUES(:id,'local78',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"
        ), {"id": other_login})
        for user_id in (actor.user_id, "local78"):
            connection.execute(text(
                "INSERT INTO lk_user_preferences(user_id,notification_settings,export_settings,timezone) "
                "VALUES(:u,CAST(:settings AS json),'{}','UTC')"
            ), {"u": user_id, "settings": json.dumps(preferences.LEGACY)})
    other = ActorContext("local78", "local78", 91001, "admin", frozenset(), session_id=other_login)
    inbox = ReviewNotificationService(engine=database.runtime, allowlist=acceptance.ALLOWLIST)
    codec = NotificationListCursorCodec(b"synthetic-local-api-role-test-key")

    def page(recipient):
        result = inbox.list_visible(authenticated_actor=recipient, marketplace_account_id=91101,
            marketplace="avito", limit=10, cursor=None, codec=codec)
        assert result["eventIds"] == [identifier]
        assert len(result["items"]) == 1
        return result["items"][0]

    assert page(actor)["receipt"] is None
    assert page(other)["receipt"] is None
    first = acceptance.mark(inbox, actor, [identifier])
    member = first["recipientMembershipId"]
    assert member != 78
    assert first["items"][0]["version"] == 1
    assert page(actor)["receipt"] is not None
    assert page(other)["receipt"] is None
    dismissed = acceptance.mark(inbox, actor, [identifier], action="dismiss")
    assert dismissed["items"][0]["version"] == 2
    assert page(other)["receipt"] is None
    acceptance.mark(inbox, other, [identifier])
    rows = acceptance.receipt_rows(db, identifier)
    assert {row.recipient_membership_id for row in rows} == {member, 78}
    own = next(row for row in rows if row.recipient_membership_id == member)
    peer = next(row for row in rows if row.recipient_membership_id == 78)
    assert own.read_at is not None and own.dismissed_at is not None and own.version == 2
    assert peer.read_at is not None and peer.dismissed_at is None and peer.version == 1

    prefs = NotificationPreferencesService(engine=database.runtime, enabled=True)
    peer_before = prefs.get_current(authenticated_actor=other)
    assert prefs.get_current(authenticated_actor=actor)["version"] == "1"
    changed = prefs.replace(authenticated_actor=actor, expected_version="1", flags=preferences.FLAGS)
    assert changed["version"] == "2"
    assert changed["email"] == {"enabled": False, "dailyDigest": True, "criticalAlerts": False}
    assert changed["telegram"] == {"enabled": True}
    assert prefs.get_current(authenticated_actor=actor) == changed
    assert prefs.get_current(authenticated_actor=other) == peer_before
