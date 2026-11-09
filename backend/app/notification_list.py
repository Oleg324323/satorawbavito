"""Bounded discovery of existing account Review events, not a universal inbox."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text

from app.notification_repository import NotificationStorageError
from app.reviews.canonical_repository import (
    FACT,
    OBS,
    ReviewFactsRepository,
    ReviewOwner,
)


def _require(condition):
    if not condition:
        raise NotificationStorageError("NOTIFICATION_STORAGE_INVALID")


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _encode(decoded) != value:
        raise ValueError
    return decoded


def _stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class NotificationPosition:
    count: int
    occurred_at: datetime
    event_id: UUID


class NotificationListCursorCodec:
    def __init__(self, key: bytes):
        _require(type(key) is bytes and len(key) >= 32)
        self._key = key

    def __repr__(self):
        return "<NotificationListCursorCodec redacted>"

    def __reduce_ex__(self, protocol):
        raise TypeError("Cursor key is private")

    def issue(self, *, scope, limit, position):
        payload = _encode(
            _json(
                {
                    "v": 1,
                    "scope": scope,
                    "limit": limit,
                    "count": position.count,
                    "at": _stamp(position.occurred_at),
                    "id": str(position.event_id),
                }
            )
        )
        mac = hmac.digest(
            self._key,
            b"satorna.review-notification-list.v1\x00" + payload.encode("ascii"),
            hashlib.sha256,
        )
        return payload + "." + _encode(mac)

    def parse(self, token, *, scope, limit):
        try:
            _require(type(token) is str and 0 < len(token) <= 4096)
            payload, signature = token.split(".")
            expected = hmac.digest(
                self._key,
                b"satorna.review-notification-list.v1\x00" + payload.encode("ascii"),
                hashlib.sha256,
            )
            _require(hmac.compare_digest(_decode(signature), expected))
            raw = _decode(payload)
            value = json.loads(raw)
            _require(
                type(value) is dict
                and set(value) == {"v", "scope", "limit", "count", "at", "id"}
            )
            _require(
                _json(value) == raw and type(value["v"]) is int and value["v"] == 1
            )
            _require(
                _json(value["scope"]) == _json(scope)
                and type(value["limit"]) is int
                and value["limit"] == limit
            )
            _require(type(value["count"]) is int and 0 < value["count"] <= 2**63 - 1)
            _require(type(value["at"]) is str and type(value["id"]) is str)
            at, identifier = datetime.fromisoformat(value["at"]), UUID(value["id"])
            _require(at.utcoffset() is not None and _stamp(at) == value["at"])
            _require(str(identifier) == value["id"] and identifier.int != 0)
            return NotificationPosition(value["count"], at, identifier)
        except (
            ValueError,
            TypeError,
            UnicodeError,
            binascii.Error,
            KeyError,
            RecursionError,
        ):
            raise NotificationStorageError("NOTIFICATION_STORAGE_INVALID") from None


def list_statement(position):
    after = (
        ""
        if position is None
        else "AND (e.occurred_at,e.event_id)<(:after_at,:after_id)"
    )
    return text(f"""
      WITH summary AS (
        SELECT count(*) AS event_count FROM notification_in_app_events
        WHERE organization_id=:org AND marketplace_account_id=:account AND marketplace=:marketplace
      )
      SELECT summary.event_count,e.*,r.recipient_membership_id,r.read_at,r.dismissed_at,r.version AS receipt_version
      FROM summary LEFT JOIN LATERAL (
        SELECT e.* FROM notification_in_app_events e
        WHERE e.organization_id=:org AND e.marketplace_account_id=:account AND e.marketplace=:marketplace
        {after} ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT :limit
      ) e ON true LEFT JOIN notification_in_app_receipts r
        ON r.organization_id=:org AND r.marketplace_account_id=:account AND r.marketplace=:marketplace
        AND r.event_id=e.event_id AND r.recipient_membership_id=:member
      ORDER BY e.occurred_at DESC,e.event_id DESC
    """)


def _validate_bindings(repository, rows):
    """Batch the same current source-binding checks used by exact-ID visibility."""
    ids = {row["source_review_id"] for row in rows}
    if not ids:
        return
    facts = (
        repository.connection.execute(
            select(FACT).where(*repository._scope(FACT), FACT.c.review_id.in_(ids))
        )
        .mappings()
        .all()
    )
    if {row["review_id"] for row in facts} != ids:
        raise NotificationStorageError("NOTIFICATION_STORAGE_UNAVAILABLE")
    pointers = set()
    run_ids = set()
    expected = set()
    for fact in facts:
        _require(fact["current_observation_id"] is not None)
        current = {fact["current_observation_id"]}
        if fact["ambiguous_observation_id"] is not None:
            current.add(fact["ambiguous_observation_id"])
        pointers.update(current)
        expected.update((fact["review_id"], identifier) for identifier in current)
        run_ids.add(fact["last_source_run_id"])
    observations = repository.connection.execute(
        select(OBS.c.review_id, OBS.c.observation_id, OBS.c.source_run_id).where(
            *repository._scope(OBS), OBS.c.observation_id.in_(pointers)
        )
    ).all()
    if {(row.review_id, row.observation_id) for row in observations} != expected:
        raise NotificationStorageError("NOTIFICATION_STORAGE_CONFLICT")
    run_ids.update(row.source_run_id for row in observations)
    binding = repository.binding
    facts_repository = ReviewFactsRepository(
        repository.connection,
        ReviewOwner(
            binding.organization_id,
            binding.marketplace_account_id,
            binding.marketplace,
            binding.external_account_id,
            binding.credential_ref,
        ),
        command_savepoints=False,
    )
    facts_repository._require_run_ids(run_ids)


def read_notification_page(repository, *, actor, member, limit, cursor, codec):
    _require(repository._connected)
    _require(type(member) is int and 0 < member <= 2**31 - 1)
    _require(type(limit) is int and 1 <= limit <= 100)
    _require(type(codec) is NotificationListCursorCodec)
    scope = [
        actor.organization_id,
        actor.user_id,
        actor.session_id,
        member,
        repository.binding.marketplace_account_id,
        repository.binding.marketplace,
    ]
    position = (
        codec.parse(cursor, scope=scope, limit=limit) if cursor is not None else None
    )
    params = {
        "org": actor.organization_id,
        "account": repository.binding.marketplace_account_id,
        "marketplace": repository.binding.marketplace,
        "member": member,
        "limit": limit + 1,
    }
    if position is not None:
        params.update(after_at=position.occurred_at, after_id=position.event_id)
    records = (
        repository.connection.execute(list_statement(position), params).mappings().all()
    )
    count = records[0]["event_count"]
    if position is not None and count != position.count:
        raise NotificationStorageError("NOTIFICATION_STORAGE_CONFLICT")
    rows = [row for row in records if row["event_id"] is not None]
    selected = rows[:limit]
    _validate_bindings(repository, selected)
    items = []
    for row in selected:
        event = repository._decode_event(row, current_binding=False)
        receipt = None
        if row["recipient_membership_id"] is not None:
            receipt = repository._receipt(dict(row, version=row["receipt_version"]))
        items.append({"event": event, "receipt": receipt})
    next_cursor = None
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = codec.issue(
            scope=scope,
            limit=limit,
            position=NotificationPosition(count, last["occurred_at"], last["event_id"]),
        )
    return {
        "schemaVersion": "review-notification-list-v1",
        "organizationId": actor.organization_id,
        "marketplaceAccountId": repository.binding.marketplace_account_id,
        "marketplace": repository.binding.marketplace,
        "recipientMembershipId": member,
        "eventIds": [str(row["event_id"]) for row in selected],
        "items": items,
        "nextCursor": next_cursor,
        "eventSetVersion": str(count),
        "capabilities": {"canRead": True, "canMarkRead": True, "canDismiss": True},
    }
