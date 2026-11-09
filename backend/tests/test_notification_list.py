"""Offline query/codec/HTTP tests, not a substitute for runtime-role checks."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.notification_list import (
    NotificationListCursorCodec,
    NotificationPosition,
    list_statement,
    read_notification_page,
)
from app.notification_list_http import make_review_notification_list_router
from app.notification_repository import NotificationStorageError
from app.notification_service import NotificationServiceError, ReviewNotificationService
from app.review_notifications_http import _actor

CODEC = NotificationListCursorCodec(b"synthetic-notification-cursor-test-key")
SCOPE = [91001, "synthetic-user", "synthetic-login", 77, 91101, "avito"]
POSITION = NotificationPosition(2, datetime(2026, 9, 10, tzinfo=UTC), UUID(int=1))


def test_cursor_exact_roundtrip_and_redacted_repr():
    token = CODEC.issue(scope=SCOPE, limit=1, position=POSITION)
    assert CODEC.parse(token, scope=SCOPE, limit=1) == POSITION
    assert "synthetic" not in repr(CODEC)


@pytest.mark.parametrize("index", range(6))
def test_cursor_binds_all_actor_recipient_account_scope(index):
    token = CODEC.issue(scope=SCOPE, limit=1, position=POSITION)
    scope = list(SCOPE)
    scope[index] = "different-synthetic"
    with pytest.raises(NotificationStorageError, match="NOTIFICATION_STORAGE_INVALID"):
        CODEC.parse(token, scope=scope, limit=1)


@pytest.mark.parametrize("token", ["", ".", "x" * 4097, "synthetic-private"])
def test_bad_cursor_does_not_echo_input(token):
    with pytest.raises(NotificationStorageError) as error:
        CODEC.parse(token, scope=SCOPE, limit=1)
    assert str(error.value) == "NOTIFICATION_STORAGE_INVALID"


def test_changed_page_size_or_signature_rejected():
    token = CODEC.issue(scope=SCOPE, limit=1, position=POSITION)
    for bad, limit in [(token, 2), (token + "a", 1)]:
        with pytest.raises(NotificationStorageError):
            CODEC.parse(bad, scope=SCOPE, limit=limit)


def result(items=None):
    return {
        "schemaVersion": "review-notification-list-v1",
        "organizationId": 91001,
        "marketplaceAccountId": 91101,
        "marketplace": "avito",
        "recipientMembershipId": 77,
        "eventIds": [],
        "items": items or [],
        "nextCursor": None,
        "eventSetVersion": "0",
        "capabilities": {"canRead": True, "canMarkRead": True, "canDismiss": True},
    }


@pytest.fixture
def http_client():
    service = object.__new__(ReviewNotificationService)
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return result()

    service.list_visible = read
    app = FastAPI()
    app.include_router(
        make_review_notification_list_router(
            service_dependency=lambda: service, cursor_codec_dependency=lambda: CODEC
        )
    )
    app.dependency_overrides[_actor] = lambda: SimpleNamespace(organization_id=91001)
    with TestClient(app) as client:
        yield client, service, calls


def test_http_discovers_without_recipient_or_event_ids_and_has_openapi(http_client):
    client, _, calls = http_client
    response = client.get(
        "/api/v2/reviews/notifications?marketplace_account_id=91101&marketplace=avito"
    )
    assert response.status_code == 200 and response.json() == result()
    assert response.headers["cache-control"] == "no-store"
    assert calls[0]["limit"] == 50 and "recipient_membership_id" not in calls[0]
    spec = client.get("/openapi.json").json()
    assert "NotificationListResponse" in spec["components"]["schemas"]


@pytest.mark.parametrize(
    "extra,status",
    [
        ("recipientMembershipId=78", 400),
        ("event_id=x", 400),
        ("limit=101", 422),
        ("limit=0", 422),
        ("limit=1&limit=2", 400),
        ("cursor=", 422),
    ],
)
def test_unknown_recipient_override_and_bounds_rejected(http_client, extra, status):
    client, _, calls = http_client
    response = client.get(
        "/api/v2/reviews/notifications?marketplace_account_id=91101&marketplace=avito&"
        + extra
    )
    assert response.status_code == status and not calls


@pytest.mark.parametrize(
    "code,status",
    [
        ("NOTIFICATION_DENIED", 403),
        ("NOTIFICATION_DISABLED", 409),
        ("NOTIFICATION_CONFLICT", 409),
        ("NOTIFICATION_UNAVAILABLE", 503),
    ],
)
def test_http_safe_service_error(http_client, code, status):
    client, service, _ = http_client

    def fail(**kwargs):
        raise NotificationServiceError(code)

    service.list_visible = fail
    response = client.get(
        "/api/v2/reviews/notifications?marketplace_account_id=91101&marketplace=avito"
    )
    assert response.status_code == status and response.json() == {
        "detail": {"code": code}
    }


def test_query_scope_and_stable_database_tuple_order():
    statement = str(list_statement(POSITION))
    assert "r.recipient_membership_id=:member" in statement
    assert (
        "e.organization_id=:org" in statement
        and "e.marketplace_account_id=:account" in statement
    )
    assert "(e.occurred_at,e.event_id)<(:after_at,:after_id)" in statement
    assert "ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT :limit" in statement


def test_new_append_invalidates_snapshot_count_before_event_decode(monkeypatch):
    class Connection:
        def execute(self, *args):
            return self

        def mappings(self):
            return self

        def all(self):
            return [{"event_count": 3, "event_id": None}]

    repository = SimpleNamespace(
        _connected=True,
        connection=Connection(),
        binding=SimpleNamespace(marketplace_account_id=91101, marketplace="avito"),
    )
    actor = SimpleNamespace(
        organization_id=91001, user_id="synthetic-user", session_id="synthetic-login"
    )
    cursor = CODEC.issue(scope=SCOPE, limit=1, position=POSITION)
    with pytest.raises(NotificationStorageError, match="NOTIFICATION_STORAGE_CONFLICT"):
        read_notification_page(
            repository, actor=actor, member=77, limit=1, cursor=cursor, codec=CODEC
        )


def test_numeric_versions_stay_lossless_on_wire():
    from pathlib import Path

    from app.notification_list_http import NotificationListResponse
    from app.review_notifications_http import _wire

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/reviews/send-in-app-storage-v1-golden.json"
        ).read_text()
    )
    event = json.loads(fixture["vectors"]["notificationEvent"]["canonicalUtf8Json"])
    event["sourceVersion"] = 2**53 + 1
    value = result([{"event": event, "receipt": None}])
    value["eventIds"] = [event["eventId"]]
    model = NotificationListResponse.model_validate(_wire(value))
    assert model.items[0].event.sourceVersion == "9007199254740993"


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-09-10T00:00:00Z", "2026-09-10T00:00:00.000000Z"),
        ("2026-09-10T03:00:00.123456+03:00", "2026-09-10T00:00:00.123456Z"),
    ],
)
@pytest.mark.parametrize("receipt_field", ["readAt", "dismissedAt"])
def test_actual_http_preserves_six_digit_utc_instants(
    http_client, instant, expected, receipt_field
):
    from pathlib import Path

    client, service, _ = http_client
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures/reviews/send-in-app-storage-v1-golden.json"
        ).read_text()
    )
    event = json.loads(fixture["vectors"]["notificationEvent"]["canonicalUtf8Json"])
    event["occurredAt"] = instant
    receipt = {
        "value": {
            "schemaVersion": "notification-in-app-receipt-v1",
            "organizationId": event["organizationId"],
            "marketplaceAccountId": event["marketplaceAccountId"],
            "eventId": event["eventId"],
            "recipientMembershipId": 77,
            "readAt": None,
            "dismissedAt": None,
        },
        "version": 1,
    }
    receipt["value"][receipt_field] = instant
    service.list_visible = lambda **kwargs: result(
        [{"event": event, "receipt": receipt}]
    )
    response = client.get(
        "/api/v2/reviews/notifications?marketplace_account_id=91101&marketplace=avito"
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert (
        item["event"]["occurredAt"]
        == item["receipt"]["value"][receipt_field]
        == expected
    )
    assert (
        item["receipt"]["value"][
            "dismissedAt" if receipt_field == "readAt" else "readAt"
        ]
        is None
    )


def test_list_wire_rejects_naive_instants():
    from pydantic import ValidationError

    from app.notification_list_http import NotificationReceiptValue

    with pytest.raises(ValidationError, match="Aware notification instant required"):
        NotificationReceiptValue(
            schemaVersion="notification-in-app-receipt-v1",
            organizationId=1,
            marketplaceAccountId=1,
            eventId=UUID(int=1),
            recipientMembershipId=1,
            readAt="2026-09-10T00:00:00",
            dismissedAt=None,
        )
