"""Real notification discovery against owned synthetic PostgreSQL only."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.control_plane.auth import ActorContext
from app.notification_list_http import make_review_notification_list_router
from app.notification_service import NotificationServiceError
from app.review_notifications_http import _actor
from tests import test_review_notification_send_acceptance as acceptance
from tests.test_notification_list import CODEC

cluster = acceptance.cluster
db = acceptance.db
principal = acceptance.principal
actor = acceptance.actor
notifications = acceptance.notifications
prepared_notification = acceptance.prepared_notification


def listing(service, actor, **changes):
    values = {
        "authenticated_actor": actor,
        "marketplace_account_id": 91101,
        "marketplace": "avito",
        "limit": 1,
        "cursor": None,
        "codec": CODEC,
    }
    values.update(changes)
    return service.list_visible(**values)


def test_discovery_pagination_batched_provenance_own_receipts_and_new_event_conflict(
    db,
    actor,
    notifications,
    prepared_notification,
):
    another = acceptance.local.source(db)
    acceptance.publish(db, actor, acceptance.local.prepared(db, actor, another))
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db[1], "before_cursor_execute", trace)
    try:
        first = listing(notifications, actor)
    finally:
        event.remove(db[1], "before_cursor_execute", trace)
    assert len(first["items"]) == 1 and first["nextCursor"]
    assert (
        first["recipientMembershipId"]
        == acceptance.local.context(db, actor)["actorMembershipId"]
    )
    assert sum("WITH summary AS" in value for value in statements) == 1
    assert not any(
        value.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for value in statements
    )
    second = listing(notifications, actor, cursor=first["nextCursor"])
    assert not set(first["eventIds"]) & set(second["eventIds"])
    assert prepared_notification.event_id in first["eventIds"] + second["eventIds"]
    assert len(set(first["eventIds"] + second["eventIds"])) == 2
    marked = acceptance.mark(notifications, actor, first["eventIds"])
    latest = listing(notifications, actor)
    assert (
        latest["items"][0]["receipt"]["value"]["recipientMembershipId"]
        == marked["recipientMembershipId"]
    )
    assert latest["items"][0]["receipt"]["value"]["readAt"] is not None
    assert latest["eventSetVersion"] == first["eventSetVersion"]
    third = acceptance.local.source(db)
    acceptance.publish(db, actor, acceptance.local.prepared(db, actor, third))
    with pytest.raises(NotificationServiceError, match="NOTIFICATION_CONFLICT"):
        listing(notifications, actor, cursor=first["nextCursor"])


@pytest.mark.parametrize(
    "change", ["other_org", "other_account", "logout", "permissions"]
)
def test_discovery_live_denials(
    db, actor, notifications, prepared_notification, change
):
    values = {}
    if change == "other_org":
        actor = replace(actor, organization_id=91002)
        values = {"marketplace_account_id": 91201, "marketplace": "wb"}
    elif change == "other_account":
        values = {"marketplace_account_id": 91102}
    else:
        with db[0].begin() as c:
            if change == "logout":
                c.execute(
                    text(
                        "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                    ),
                    {"id": actor.session_id},
                )
            else:
                c.execute(
                    text(
                        "UPDATE iam_memberships SET permissions='[]' WHERE user_id=:id"
                    ),
                    {"id": actor.user_id},
                )
    with pytest.raises(NotificationServiceError, match="NOTIFICATION_DENIED"):
        listing(notifications, actor, **values)


def test_real_http_returns_discovered_ids_and_forbids_recipient_override(
    db, actor, notifications, prepared_notification
):
    app = FastAPI()
    app.include_router(
        make_review_notification_list_router(
            service_dependency=lambda: notifications,
            cursor_codec_dependency=lambda: CODEC,
        )
    )
    app.dependency_overrides[_actor] = lambda: actor
    with TestClient(app) as client:
        url = "/api/v2/reviews/notifications?marketplace_account_id=91101&marketplace=avito"
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert prepared_notification.event_id in response.json()["eventIds"]
        assert isinstance(response.json()["items"][0]["event"]["sourceVersion"], str)
        assert client.get(url + "&recipientMembershipId=78").status_code == 400


def test_discovered_receipts_never_include_other_live_recipient(
    db,
    actor,
    notifications,
    prepared_notification,
):
    identifier = prepared_notification.event_id
    own = acceptance.mark(notifications, actor, [identifier])
    login = uuid4().hex
    with db[0].begin() as c:
        c.execute(
            text(
                "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) "
                "VALUES(:id,'local78',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"
            ),
            {"id": login},
        )
    other = ActorContext(
        "local78", "local78", 91001, "admin", frozenset(), session_id=login
    )
    page = listing(notifications, other, limit=100)
    assert page["recipientMembershipId"] == 78 != own["recipientMembershipId"]
    selected = next(
        item for item in page["items"] if item["event"]["eventId"] == identifier
    )
    assert selected["receipt"] is None
    acceptance.mark(notifications, other, [identifier], action="dismiss")
    page = listing(notifications, actor, limit=100)
    selected = next(
        item for item in page["items"] if item["event"]["eventId"] == identifier
    )
    assert (
        selected["receipt"]["value"]["recipientMembershipId"]
        == own["recipientMembershipId"]
    )
    assert selected["receipt"]["value"]["dismissedAt"] is None
