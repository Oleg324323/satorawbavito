"""Read-only typed projection of the existing cabinet preferences owner.

Trusted services supply authenticated/authorized user and membership identities.
This is not an HTTP authorization boundary or permission to deliver a message.
Caller owns session/tenant context and transaction isolation, and must revalidate
recipient/account/destination before delivery. No ORM identity cache, defaults,
fallback, commit, flush or preference creation is used here.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkUserPreferenceRow, LkUserRow
from app.platform.identity.orm import IamMembershipRow


class PreferencesReadError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NotificationPreferenceFlags:
    email_enabled: bool
    daily_digest: bool
    critical_alerts: bool
    telegram_enabled: bool


def _flags(payload: object) -> NotificationPreferenceFlags:
    invalid = "PREFERENCES_INVALID"
    if type(payload) is not dict or payload.keys() != {"email", "telegram"}:
        raise PreferencesReadError(invalid)
    email, telegram = payload["email"], payload["telegram"]
    if (type(email) is not dict or email.keys() != {"enabled", "dailyDigest", "criticalAlerts"}
            or type(telegram) is not dict or telegram.keys() != {"enabled", "chatId"}):
        raise PreferencesReadError(invalid)
    values = (email["enabled"], email["dailyDigest"], email["criticalAlerts"], telegram["enabled"])
    if not all(type(value) is bool for value in values):
        raise PreferencesReadError(invalid)
    chat_id = telegram["chatId"]
    if chat_id is not None and (not isinstance(chat_id, str) or not chat_id.strip()):
        raise PreferencesReadError(invalid)
    # Legacy destination content is neither returned nor promoted to verified binding.
    return NotificationPreferenceFlags(*values)


def read_notification_preferences(session: Session, *, organization_id: int,
                                  membership_id: int, user_id: str) -> NotificationPreferenceFlags:
    if (type(organization_id) is not int or organization_id <= 0
            or type(membership_id) is not int or membership_id <= 0
            or not isinstance(user_id, str) or not user_id.strip()):
        raise PreferencesReadError("PREFERENCES_INVALID_SCOPE")
    statement = (
        select(LkUserPreferenceRow.notification_settings)
        .select_from(IamMembershipRow)
        .join(LkUserRow, LkUserRow.user_id == IamMembershipRow.user_id)
        .join(LkUserPreferenceRow, LkUserPreferenceRow.user_id == IamMembershipRow.user_id)
        .where(IamMembershipRow.organization_id == organization_id,
               IamMembershipRow.membership_id == membership_id,
               IamMembershipRow.user_id == user_id,
               IamMembershipRow.is_active.is_(True), LkUserRow.is_active.is_(True))
    )
    storage_failed = False
    try:
        with session.no_autoflush:
            row = session.execute(statement).one_or_none()
    except SQLAlchemyError:
        # Raise outside the handler so SQL/parameters do not remain as exception context.
        storage_failed = True
    if storage_failed:
        raise PreferencesReadError("PREFERENCES_STORAGE_UNAVAILABLE")
    if row is None:
        raise PreferencesReadError("PREFERENCES_UNAVAILABLE")
    return _flags(row[0])
