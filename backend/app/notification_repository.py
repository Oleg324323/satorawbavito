"""0074 transaction participant, NOT an authenticated service or HTTP endpoint.

Caller acquires live user/member/session/account guards before construction and
owns the physical root through commit. Account serialization precedes domain
reads/receipt locks. There is no cache, provider, fallback or internal commit.
"""

import json
from datetime import timezone
from uuid import UUID, uuid4

from sqlalchemy import bindparam, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.platform.integrations.orm import MarketplaceAccountRow
from app.notification_contract import NotificationKind, NotificationScope, NotificationSourceEvent, project_notification
from app.notification_storage_payloads import (
    encode_notification_event,
    encode_notification_receipt,
    encode_notification_visible_action,
)
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.local_repository import FACT
from app.reviews.send_tables import EVENT, RECEIPT


class NotificationStorageError(ValueError):
    def __init__(self, code="NOTIFICATION_STORAGE_CONFLICT"):
        allowed = {"NOTIFICATION_STORAGE_CONFLICT", "NOTIFICATION_STORAGE_INVALID",
                   "NOTIFICATION_STORAGE_UNAVAILABLE", "NOTIFICATION_STORAGE_NOT_FOUND"}
        self.code = code if code in allowed else "NOTIFICATION_STORAGE_UNAVAILABLE"
        super().__init__(self.code)


def _require(value, code="NOTIFICATION_STORAGE_CONFLICT"):
    if not value:
        raise NotificationStorageError(code)


def _stamp(value):
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class NotificationRepository:
    def __init__(self, connection, binding: ReviewBindingDescriptor):
        _require(type(binding) is ReviewBindingDescriptor, "NOTIFICATION_STORAGE_INVALID")
        binding.__post_init__()
        _require(connection.in_transaction() and connection.dialect.name == "postgresql",
                 "NOTIFICATION_STORAGE_UNAVAILABLE")
        self.connection, self.binding = connection, binding
        self.owner = {"organization_id": binding.organization_id,
                      "marketplace_account_id": binding.marketplace_account_id, "marketplace": binding.marketplace}
        context = connection.execute(text("SELECT current_setting('transaction_isolation'), "
            "current_setting('app.organization_id',true), current_setting('app.marketplace_account_id',true)")).one()
        _require(tuple(context) == ("read committed", str(binding.organization_id), str(binding.marketplace_account_id)),
                 "NOTIFICATION_STORAGE_INVALID")
        account = connection.execute(select(MarketplaceAccountRow.external_account_id,
            MarketplaceAccountRow.credential_ref, MarketplaceAccountRow.status).where(
                MarketplaceAccountRow.organization_id == binding.organization_id,
                MarketplaceAccountRow.marketplace_account_id == binding.marketplace_account_id,
                MarketplaceAccountRow.marketplace == binding.marketplace).with_for_update()).one_or_none()
        _require(account is not None and tuple(account)[:2] == (binding.external_account_id, binding.credential_ref))
        self._connected = account.status == "connected"

    def _scope(self, table, **keys):
        return [table.c[key] == bindparam(None, value, type_=table.c[key].type)
                for key, value in dict(self.owner, **keys).items()]

    def _decode_event(self, row, *, current_binding=True):
        raw = bytes(row["event_payload"])
        try:
            value = json.loads(raw)
            encoded = encode_notification_event(value)
        except (ValueError, TypeError, RecursionError):
            raise NotificationStorageError("NOTIFICATION_STORAGE_UNAVAILABLE") from None
        _require(encoded.canonical_bytes == raw and encoded.checksum == row["event_checksum"],
                 "NOTIFICATION_STORAGE_UNAVAILABLE")
        _require(value["organizationId"] == self.binding.organization_id
                 and value["marketplaceAccountId"] == self.binding.marketplace_account_id
                 and value["eventId"] == str(row["event_id"]), "NOTIFICATION_STORAGE_UNAVAILABLE")
        # Closing may append a safe historical event after disconnection/rebind;
        # that must not erase the durable outcome. Visibility is a separate gate.
        if not current_binding:
            return value
        # Account rebinding must not make an old source identity current again.
        facts = ReviewFactsRepository(self.connection, ReviewOwner(
            self.binding.organization_id, self.binding.marketplace_account_id, self.binding.marketplace,
            self.binding.external_account_id, self.binding.credential_ref), command_savepoints=False)
        fact = self.connection.execute(select(FACT).where(
            *self._scope(FACT, review_id=row["source_review_id"]))).mappings().one_or_none()
        _require(fact is not None, "NOTIFICATION_STORAGE_UNAVAILABLE")
        facts._require_identity_binding(fact)
        return value

    def publish(self, *, review_id: UUID, entity_id: UUID, source_version: int,
                kind: str, source_audit_event_id: UUID | None = None):
        """Call inside the NEW source mutation root; never retrofit old writers.

        Dedupe retains original UUID/time/bytes. SQL proves source FK/version,
        while the service owns source-change + event atomicity and authorization.
        """
        now = self.connection.scalar(select(func.clock_timestamp()))
        source = NotificationSourceEvent(self.binding.organization_id, self.binding.marketplace_account_id,
            NotificationScope.account, NotificationKind(kind), entity_id, source_version, now)
        display = project_notification(source)
        refs = {"source_review_id": review_id,
                "source_draft_id": entity_id if kind == "approval_required" else None,
                "source_command_id": None if kind == "approval_required" else entity_id,
                "source_audit_event_id": source_audit_event_id}
        _require((kind == "approval_required") == (source_audit_event_id is None), "NOTIFICATION_STORAGE_INVALID")
        old = self.connection.execute(select(EVENT).where(
            *self._scope(EVENT, dedupe_key=display.dedupe_key))).mappings().one_or_none()
        if old is not None:
            _require(all(old[key] == value for key, value in refs.items()))
            return self._decode_event(old, current_binding=False)
        value = {"schemaVersion": "notification-event-v1", "eventId": str(uuid4()),
                 "organizationId": self.binding.organization_id, "marketplaceAccountId": self.binding.marketplace_account_id,
                 "scope": "account", "producer": "reviews", "entityId": str(entity_id), "sourceVersion": source_version,
                 "kind": kind, "occurredAt": _stamp(now), "dedupeKey": display.dedupe_key,
                 "title": display.title, "details": display.details, "severity": display.severity}
        encoded = encode_notification_event(value)
        row = self.connection.execute(insert(EVENT).values(**self.owner, **refs,
            event_id=UUID(value["eventId"]), scope="account", producer="reviews", entity_id=entity_id,
            source_version=source_version, kind=kind, occurred_at=now, dedupe_key=display.dedupe_key,
            title=display.title, details=display.details, severity=display.severity,
            event_payload=encoded.canonical_bytes, event_checksum=encoded.checksum).returning(EVENT)).mappings().one()
        return self._decode_event(row, current_binding=False)

    def _visible(self, event_ids):
        _require(self._connected)
        rows = {}
        # Immutable events have SELECT/INSERT only; do not request FOR UPDATE on
        # them. The already held canonical account lock serializes this batch.
        for identifier in sorted(event_ids, key=lambda value: value.int):
            row = self.connection.execute(select(EVENT).where(
                *self._scope(EVENT, event_id=identifier))).mappings().one_or_none()
            _require(row is not None, "NOTIFICATION_STORAGE_NOT_FOUND")
            rows[identifier] = self._decode_event(row)
        return rows

    def read_visible(self, *, event_ids: list[str], recipient_membership_id: int):
        # Reuse closed visible-ID validation, without performing its action.
        self._intent(event_ids, recipient_membership_id, "read")
        ids = [UUID(value) for value in event_ids]
        events = self._visible(ids)
        result = []
        for identifier in ids:
            receipt = self.connection.execute(select(RECEIPT).where(*self._scope(RECEIPT,
                event_id=identifier, recipient_membership_id=recipient_membership_id))).mappings().one_or_none()
            result.append({"event": events[identifier], "receipt": None if receipt is None else self._receipt(receipt)})
        return result

    def _intent(self, event_ids, member, action):
        # Membership must be derived by caller's live guard, never request body.
        encode_notification_visible_action({"schemaVersion": "notification-visible-action-v1",
            "organizationId": self.binding.organization_id, "marketplaceAccountId": self.binding.marketplace_account_id,
            "recipientMembershipId": member, "eventIds": event_ids, "action": action})

    def _receipt(self, row):
        value = {"schemaVersion": "notification-in-app-receipt-v1", "organizationId": self.binding.organization_id,
            "marketplaceAccountId": self.binding.marketplace_account_id, "eventId": str(row["event_id"]),
            "recipientMembershipId": row["recipient_membership_id"],
            "readAt": _stamp(row["read_at"]), "dismissedAt": _stamp(row["dismissed_at"])}
        encode_notification_receipt(value)
        # Version is metadata, never an additional canonical receipt byte field.
        return {"value": value, "version": int(row["version"])}

    def mark_visible(self, *, event_ids: list[str], recipient_membership_id: int, action: str):
        self._intent(event_ids, recipient_membership_id, action)
        ids = [UUID(value) for value in event_ids]
        self._visible(ids)  # ALL visibility/binding checks before the first write.
        results = {}
        for identifier in sorted(ids, key=lambda value: value.int):
            self.connection.execute(select(RECEIPT.c.event_id).where(*self._scope(RECEIPT,
                event_id=identifier, recipient_membership_id=recipient_membership_id)).with_for_update()).all()
        column = "read_at" if action == "read" else "dismissed_at"
        for identifier in sorted(ids, key=lambda value: value.int):
            statement = pg_insert(RECEIPT).values(**self.owner, event_id=identifier,
                recipient_membership_id=recipient_membership_id, version=1,
                read_at=func.clock_timestamp() if action == "read" else None,
                dismissed_at=func.clock_timestamp() if action == "dismiss" else None)
            statement = statement.on_conflict_do_update(
                index_elements=[RECEIPT.c.organization_id, RECEIPT.c.event_id, RECEIPT.c.recipient_membership_id],
                set_={column: func.clock_timestamp()},
                where=(RECEIPT.c.marketplace_account_id == self.binding.marketplace_account_id)
                    & (RECEIPT.c.marketplace == self.binding.marketplace))
            row = self.connection.execute(statement.returning(RECEIPT)).mappings().one()
            results[identifier] = self._receipt(row)
        return [results[identifier] for identifier in ids]
