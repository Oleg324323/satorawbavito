"""Safe notification identity/projection only; no event, receipt or delivery store.

Producers must commit a source change plus event/outbox in one transaction and
derive these internal references from authorized domain state, never free-form
provider responses. The future repository owns dedupe and the original event's
timestamp; this function does not perform an upsert or grant recipient access.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class NotificationContractError(ValueError):
    code = "NOTIFICATION_INPUT_INVALID"

    def __init__(self) -> None:
        super().__init__(self.code)


class NotificationScope(str, Enum):
    account = "account"
    organization = "organization"


class NotificationKind(str, Enum):
    approval_required = "approval_required"
    send_blocked = "send_blocked"
    send_ambiguous = "send_ambiguous"
    system_attention = "system_attention"


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True, slots=True)
class NotificationSourceEvent:
    organization_id: int
    marketplace_account_id: int | None
    scope: NotificationScope
    kind: NotificationKind
    entity_id: UUID
    source_version: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        valid = (
            _positive_int(self.organization_id)
            and isinstance(self.scope, NotificationScope)
            and isinstance(self.kind, NotificationKind)
            and isinstance(self.entity_id, UUID) and self.entity_id.int != 0
            and _positive_int(self.source_version)
            and isinstance(self.occurred_at, datetime)
            and self.occurred_at.tzinfo is not None and self.occurred_at.utcoffset() is not None
        )
        if not valid:
            raise NotificationContractError()
        if self.scope is NotificationScope.account:
            if not _positive_int(self.marketplace_account_id):
                raise NotificationContractError()
        elif self.marketplace_account_id is not None or self.kind is not NotificationKind.system_attention:
            # A lost account on a Reviews event can never become org-wide.
            raise NotificationContractError()


@dataclass(frozen=True, slots=True)
class SafeNotificationDisplay:
    organization_id: int
    marketplace_account_id: int | None
    scope: NotificationScope
    entity_id: UUID
    source_version: int
    kind: NotificationKind
    producer: str
    dedupe_key: str
    occurred_at: datetime
    title: str
    details: str
    severity: str


def project_notification(event: NotificationSourceEvent) -> SafeNotificationDisplay:
    if not isinstance(event, NotificationSourceEvent):
        raise NotificationContractError()
    # No caller-controlled customer content, URL, recipient, secret, raw error,
    # product title or provider status is interpolated into these messages.
    match event.kind:
        case NotificationKind.approval_required:
            title = "Ответ на отзыв требует подтверждения"
            details = "Проверьте текущую версию черновика в разделе отзывов."
            severity = "info"
        case NotificationKind.send_blocked:
            title = "Отправка ответа заблокирована"
            details = "Проверьте актуальность источника, черновика и разрешений."
            severity = "warning"
        case NotificationKind.send_ambiguous:
            title = "Результат отправки ответа требует проверки"
            details = "Повторная отправка заблокирована до проверки результата."
            severity = "warning"
        case NotificationKind.system_attention:
            title = "Системное событие требует внимания"
            details = "Проверьте состояние интеграций в приложении."
            severity = "warning"
        case _:
            raise NotificationContractError()
    producer = "platform" if event.kind is NotificationKind.system_attention else "reviews"
    identity = {
        "contract": "notification-event-v1",
        "organizationId": event.organization_id,
        "marketplaceAccountId": event.marketplace_account_id,
        "scope": event.scope.value,
        "producer": producer,
        "entityId": str(event.entity_id),
        "sourceVersion": event.source_version,
        "kind": event.kind.value,
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return SafeNotificationDisplay(event.organization_id, event.marketplace_account_id,
                                   event.scope, event.entity_id, event.source_version,
                                   event.kind, producer, key, event.occurred_at,
                                   title, details, severity)
