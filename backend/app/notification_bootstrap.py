"""One default-off boundary for Review inbox discovery and own receipts."""

from fastapi import HTTPException

from app.config import get_settings
from app.infra.db import get_engine
from app.notification_list import NotificationListCursorCodec
from app.notification_list_http import make_review_notification_list_router
from app.notification_preferences_http import make_notification_preferences_router
from app.notification_preferences_service import NotificationPreferencesService
from app.notification_service import ReviewNotificationService
from app.review_notifications_http import make_review_notifications_router


def _unavailable():
    return HTTPException(
        503,
        detail={"code": "NOTIFICATION_UNAVAILABLE"},
        headers={"Cache-Control": "no-store"},
    )


def notification_service():
    try:
        settings = get_settings()
        if getattr(settings, "canonical_notifications_enabled", False) is not True:
            raise ValueError("disabled")
        return ReviewNotificationService(
            engine=get_engine(),
            allowlist=frozenset(settings.canonical_notification_accounts),
        )
    except Exception:  # noqa: BLE001 - never expose engine/configuration details.
        failure = _unavailable()
    raise failure


def notification_cursor_codec():
    try:
        settings = get_settings()
        if getattr(settings, "canonical_notifications_enabled", False) is not True:
            raise ValueError("disabled")
        return NotificationListCursorCodec(settings.auth_secret.encode("utf-8"))
    except Exception:  # noqa: BLE001 - cursor key/configuration stays private.
        failure = _unavailable()
    raise failure


def notification_preferences_service():
    try:
        settings = get_settings()
        if getattr(settings, "canonical_notifications_enabled", False) is not True:
            raise ValueError("disabled")
        return NotificationPreferencesService(engine=get_engine(), enabled=True)
    except Exception:  # noqa: BLE001 - never expose infrastructure details.
        failure = HTTPException(
            503,
            detail={"code": "PREFERENCES_UNAVAILABLE"},
            headers={"Cache-Control": "no-store"},
        )
    raise failure


def register_notification_routes(app):
    # Both factories use the identical runtime policy; neither grants send rights.
    app.include_router(make_review_notification_list_router(
        service_dependency=notification_service,
        cursor_codec_dependency=notification_cursor_codec,
    ))
    app.include_router(make_review_notifications_router(
        service_dependency=notification_service,
        max_request_bytes=32 * 1024,
        max_visible_ids=100,
    ))
    # Self preferences need live user permissions, not a marketplace allowlist.
    app.include_router(make_notification_preferences_router(
        service_dependency=notification_preferences_service,
        max_request_bytes=16 * 1024,
    ))
