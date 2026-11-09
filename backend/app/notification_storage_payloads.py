"""Account-scoped in-app bytes only. No Telegram, destinations or new preferences.

These codecs cannot authorize a recipient, resolve source FKs, deduplicate an
event, commit it with its source change, or implement concurrent first-write wins.
"""

from uuid import UUID

from app.notification_contract import (
    NotificationKind,
    NotificationScope,
    NotificationSourceEvent,
    project_notification,
)
from app.reviews.storage_payloads import (
    _finish,
    _integer,
    _object,
    _require,
    _timestamp,
    _uuid,
)


def _scope(value):
    for key in ("organizationId", "marketplaceAccountId"):
        _integer(value[key])
        _require(value[key] <= 2**31 - 1)


def encode_notification_event(payload):
    value = _object(payload, {"schemaVersion", "eventId", "organizationId", "marketplaceAccountId",
        "scope", "producer", "entityId", "sourceVersion", "kind", "occurredAt", "dedupeKey",
        "title", "details", "severity"})
    _require(value["schemaVersion"] == "notification-event-v1" and value["scope"] == "account"
             and value["producer"] == "reviews")
    _scope(value)
    _uuid(value["eventId"])
    _uuid(value["entityId"])
    _integer(value["sourceVersion"])
    _require(type(value["kind"]) is str and value["kind"] in {
        "approval_required", "send_blocked", "send_ambiguous"})
    source = NotificationSourceEvent(value["organizationId"], value["marketplaceAccountId"],
        NotificationScope.account, NotificationKind(value["kind"]), UUID(value["entityId"]),
        value["sourceVersion"], _timestamp(value["occurredAt"]))
    display = project_notification(source)
    _require(value["dedupeKey"] == display.dedupe_key and value["title"] == display.title
             and value["details"] == display.details and value["severity"] == display.severity)
    return _finish(value)


def encode_notification_receipt(payload):
    value = _object(payload, {"schemaVersion", "organizationId", "marketplaceAccountId", "eventId",
                              "recipientMembershipId", "readAt", "dismissedAt"})
    _require(value["schemaVersion"] == "notification-in-app-receipt-v1")
    _scope(value)
    _uuid(value["eventId"])
    _integer(value["recipientMembershipId"])
    _require(value["recipientMembershipId"] <= 2**31 - 1)
    _require(value["readAt"] is not None or value["dismissedAt"] is not None)
    for key in ("readAt", "dismissedAt"):
        if value[key] is not None:
            _timestamp(value[key])
    # No ordering between read/dismiss: either action may happen first, and each
    # column independently retains its first timestamp under the same row lock.
    return _finish(value)


def merge_notification_receipt(existing: dict | None, action: dict):
    """Pure row merge; a repository must serialize it, not SELECT then blind UPSERT.

The action is a server-created receipt-shaped candidate after live authorization.
No candidate timestamp replaces an existing non-null timestamp, even if earlier.
"""
    encode_notification_receipt(action)
    if existing is None:
        return _finish(action)
    encode_notification_receipt(existing)
    for key in ("schemaVersion", "organizationId", "marketplaceAccountId", "eventId", "recipientMembershipId"):
        _require(existing[key] == action[key])
    result = dict(existing)
    for key in ("readAt", "dismissedAt"):
        if result[key] is None:
            result[key] = action[key]
    return encode_notification_receipt(result)


def encode_notification_visible_action(payload):
    """Explicit visible-ID set intent; no high-water/reopen/all-future-events mode."""
    value = _object(payload, {"schemaVersion", "organizationId", "marketplaceAccountId",
                              "recipientMembershipId", "eventIds", "action"})
    _require(value["schemaVersion"] == "notification-visible-action-v1")
    _scope(value)
    _integer(value["recipientMembershipId"])
    _require(value["recipientMembershipId"] <= 2**31 - 1)
    _require(value["action"] in ("read", "dismiss"))
    _require(type(value["eventIds"]) is list and bool(value["eventIds"]))
    for identifier in value["eventIds"]:
        _uuid(identifier)
    _require(len(value["eventIds"]) == len(set(value["eventIds"])))
    # Array order retained for exact intent; service locks the UUIDs in sorted
    # order to avoid inversions while checking EVERY visible ID before any write.
    return _finish(value)
