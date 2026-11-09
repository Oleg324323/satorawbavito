from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.notification_contract import (
    NotificationContractError, NotificationKind, NotificationScope,
    NotificationSourceEvent, project_notification,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def event(**kwargs):
    values = dict(organization_id=1, marketplace_account_id=11,
                  scope=NotificationScope.account, kind=NotificationKind.approval_required,
                  entity_id=UUID(int=3), source_version=2, occurred_at=NOW)
    values.update(kwargs)
    return NotificationSourceEvent(**values)


def test_safe_display_is_fixed_copy_not_customer_or_provider_payload():
    output = project_notification(event())
    assert output.title == "Ответ на отзыв требует подтверждения"
    assert output.details == "Проверьте текущую версию черновика в разделе отзывов."
    assert output.severity == "info"
    assert output.entity_id == UUID(int=3)
    assert output.organization_id == 1 and output.marketplace_account_id == 11
    with pytest.raises(FrozenInstanceError):
        output.title = "customer text"


@pytest.mark.parametrize("raw_field", ["text", "details", "provider_error", "prompt", "token", "route", "chat_id"])
def test_free_form_payload_is_not_accepted(raw_field):
    sentinel = "synthetic-private-payload"
    with pytest.raises(TypeError) as error:
        event(**{raw_field: sentinel})
    assert sentinel not in str(error.value)


@pytest.mark.parametrize("change", [
    dict(organization_id=2), dict(marketplace_account_id=12),
    dict(kind=NotificationKind.send_ambiguous), dict(entity_id=UUID(int=4)),
    dict(source_version=3),
])
def test_dedupe_identity_cannot_cross_owner_entity_kind_or_revision(change):
    assert project_notification(event()).dedupe_key != project_notification(event(**change)).dedupe_key


def test_replay_ignores_delivery_time_and_uses_a_stable_digest():
    first = project_notification(event())
    later = project_notification(event(occurred_at=NOW.replace(hour=13)))
    assert first.dedupe_key == later.dedupe_key
    assert len(first.dedupe_key) == 64 and int(first.dedupe_key, 16) >= 0
    assert first.producer == "reviews"


def test_org_wide_event_is_explicit_and_not_a_lost_review_account():
    output = project_notification(event(scope=NotificationScope.organization,
                                         marketplace_account_id=None,
                                         kind=NotificationKind.system_attention))
    assert output.scope == NotificationScope.organization
    assert output.marketplace_account_id is None
    assert output.producer == "platform"
    with pytest.raises(NotificationContractError):
        event(scope=NotificationScope.organization, marketplace_account_id=None)


@pytest.mark.parametrize("changes", [
    dict(organization_id=True), dict(organization_id=0),
    dict(marketplace_account_id=None), dict(marketplace_account_id=False),
    dict(scope=NotificationScope.organization), dict(scope="account"),
    dict(source_version=0), dict(source_version=True), dict(source_version=1.5),
    dict(entity_id="synthetic-private-id"), dict(entity_id=UUID(int=0)),
    dict(kind="synthetic-provider-error"), dict(occurred_at=NOW.replace(tzinfo=None)),
])
def test_malformed_identity_and_unknown_event_kind_fail_closed(changes):
    with pytest.raises(NotificationContractError) as error:
        event(**changes)
    assert error.value.code == "NOTIFICATION_INPUT_INVALID"
    assert "synthetic" not in str(error.value)


def test_ambiguous_send_copy_does_not_instruct_blind_retry():
    output = project_notification(event(kind=NotificationKind.send_ambiguous))
    assert output.severity == "warning"
    assert output.title == "Результат отправки ответа требует проверки"
    assert output.details == "Повторная отправка заблокирована до проверки результата."


def test_blocked_send_has_a_different_identity_and_safe_message():
    blocked = project_notification(event(kind=NotificationKind.send_blocked))
    assert blocked.severity == "warning"
    assert blocked.title == "Отправка ответа заблокирована"
    assert blocked.details == "Проверьте актуальность источника, черновика и разрешений."


def test_event_is_immutable_and_projection_has_no_shared_mutable_payload():
    first = event()
    with pytest.raises(FrozenInstanceError):
        first.source_version = 9
    second = replace(first, source_version=3)
    assert project_notification(first).source_version == 2
    assert project_notification(second).source_version == 3
