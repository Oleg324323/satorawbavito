"""Versioned self preferences in the existing cabinet owner, with live commit fences.

No transport, account allowlist, preference creation, fallback or global engine.
Deployment must upgrade every legacy writer before enabling this service.
"""
import json
import re

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.control_plane.auth import ActorContext
from app.infra.db import set_tenant_context
from app.notification_preferences_read import (
    NotificationPreferenceFlags,
    PreferencesReadError,
    _flags,
)
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.publication_guard import (
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)

MAX_VERSION = 2**63 - 1


class NotificationPreferencesError(ValueError):
    def __init__(self, code):
        allowed = {"PREFERENCES_INVALID", "PREFERENCES_DISABLED", "PREFERENCES_DENIED",
                   "PREFERENCES_CONFLICT", "PREFERENCES_UNAVAILABLE",
                   "PREFERENCES_CONFIGURATION_INVALID", "PREFERENCES_READBACK_REQUIRED"}
        self.code = code if code in allowed else "PREFERENCES_UNAVAILABLE"
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise NotificationPreferencesError(code)


def parse_version(value):
    _require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None,
             "PREFERENCES_INVALID")
    parsed = int(value)
    _require(parsed <= MAX_VERSION, "PREFERENCES_INVALID")
    return parsed


def _valid_flags(flags):
    return type(flags) is NotificationPreferenceFlags and all(type(value) is bool for value in (
        flags.email_enabled, flags.daily_digest, flags.critical_alerts, flags.telegram_enabled))


def preferences_view(version, flags):
    _require(type(version) is int and 0 < version <= MAX_VERSION and _valid_flags(flags), "PREFERENCES_UNAVAILABLE")
    return {"schemaVersion": "notification-preferences-v1", "version": str(version),
            "email": {"enabled": flags.email_enabled, "dailyDigest": flags.daily_digest,
                      "criticalAlerts": flags.critical_alerts}, "telegram": {"enabled": flags.telegram_enabled}}


class NotificationPreferencesService:
    def __init__(self, *, engine: Engine, enabled=False):
        _require(isinstance(engine, Engine) and engine.dialect.name == "postgresql" and type(enabled) is bool,
                 "PREFERENCES_CONFIGURATION_INVALID")
        self._engine, self._enabled = engine, enabled

    def get_current(self, *, authenticated_actor):
        return self._execute(authenticated_actor)

    def replace(self, *, authenticated_actor, expected_version, flags):
        version = parse_version(expected_version)
        _require(_valid_flags(flags), "PREFERENCES_INVALID")
        return self._execute(authenticated_actor, expected_version=version, flags=flags)

    def _execute(self, actor, *, expected_version=None, flags=None):
        _require(self._enabled, "PREFERENCES_DISABLED")
        _require(type(actor) is ActorContext and type(actor.organization_id) is int
                 and 0 < actor.organization_id <= 2**31 - 1
                 and type(actor.user_id) is str and bool(actor.user_id.strip())
                 and type(actor.session_id) is str and bool(actor.session_id.strip()), "PREFERENCES_DENIED")
        writing = expected_version is not None
        session, code, result, committing = None, None, None, False
        try:
            session = Session(self._engine, autoflush=False)
            session.begin()
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id, IamMembershipRow.is_active.is_(True)))
            _require(member is not None, "PREFERENCES_DENIED")
            guard = acquire_publication_guard(session,
                principal=UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id),
                required_permissions=frozenset({"preferences:write" if writing else "preferences:read"}),
                accounts=(), authorities=())
            lock = "FOR UPDATE" if writing else "FOR SHARE"
            row = session.execute(text("SELECT notification_settings, notification_version "
                "FROM lk_user_preferences WHERE user_id=:user " + lock), {"user": actor.user_id}).one_or_none()
            _require(row is not None, "PREFERENCES_UNAVAILABLE")
            current_flags = _flags(row.notification_settings)
            result = preferences_view(row.notification_version, current_flags)
            if writing:
                _require(row.notification_version == expected_version, "PREFERENCES_CONFLICT")
                _require(expected_version < MAX_VERSION, "PREFERENCES_UNAVAILABLE")
                result = preferences_view(expected_version + 1, flags)
                payload = {"email": result["email"], "telegram": {
                    "enabled": flags.telegram_enabled, "chatId": row.notification_settings["telegram"]["chatId"]}}
                guard.revalidate_before_write()
                updated = session.execute(text("UPDATE lk_user_preferences SET notification_settings=CAST(:payload AS json), "
                    "notification_version=notification_version+1, updated_at=clock_timestamp() "
                    "WHERE user_id=:user AND notification_version=:expected RETURNING notification_version"),
                    {"payload": json.dumps(payload), "user": actor.user_id, "expected": expected_version}).scalar_one_or_none()
                _require(updated == expected_version + 1, "PREFERENCES_CONFLICT")
                session.add(LkAuditEventRow(organization_id=actor.organization_id, actor_user_id=actor.user_id,
                    action="notifications.preferences.replace", object_type="lk_preferences", object_id=actor.user_id,
                    details={"oldVersion": str(expected_version), "newVersion": str(updated)}))
                session.flush()
            guard.revalidate_before_write()
            committing = True
            session.commit()  # Existing listener revalidates the same physical transaction.
        except NotificationPreferencesError as error:
            code = error.code
        except PublicationGuardError as error:
            code = "PREFERENCES_UNAVAILABLE" if error.code in {
                "publication_context_invalid", "publication_persistence_failed"} else "PREFERENCES_DENIED"
        except PreferencesReadError:
            code = "PREFERENCES_UNAVAILABLE"
        except Exception:  # noqa: BLE001 - no SQL/payload context or uncertain success.
            code = "PREFERENCES_READBACK_REQUIRED" if writing and committing else "PREFERENCES_UNAVAILABLE"
        finally:
            if session is not None:
                failed = False
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - close must still be attempted.
                    failed = True
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - a cleanup error cannot imply rollback.
                    failed = True
                if failed:
                    code = "PREFERENCES_READBACK_REQUIRED" if writing and committing else "PREFERENCES_UNAVAILABLE"
        if code is not None:
            raise NotificationPreferencesError(code)
        return result
