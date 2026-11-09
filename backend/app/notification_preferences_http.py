"""Dormant strict self-preference router; bootstrap supplies the service and byte budget."""
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.control_plane.auth import ActorContext, actor_from_request
from app.notification_preferences_read import NotificationPreferenceFlags
from app.notification_preferences_service import (
    NotificationPreferencesError,
    NotificationPreferencesService,
    parse_version,
)

_STATUS = {"PREFERENCES_INVALID": 400, "PREFERENCES_AUTHENTICATION_REQUIRED": 401,
           "PREFERENCES_DENIED": 403, "PREFERENCES_CONFLICT": 409, "PREFERENCES_DISABLED": 409,
           "PREFERENCES_UNAVAILABLE": 503, "PREFERENCES_CONFIGURATION_INVALID": 503,
           "PREFERENCES_READBACK_REQUIRED": 503}


def _error(code):
    code = code if code in _STATUS else "PREFERENCES_UNAVAILABLE"
    return HTTPException(_STATUS[code], detail={"code": code}, headers={"Cache-Control": "no-store"})


def _actor(request: Request):
    code = None
    try:
        actor = actor_from_request(request)
    except HTTPException:
        code = "PREFERENCES_AUTHENTICATION_REQUIRED"
    except Exception:  # noqa: BLE001 - hide authentication internals.
        code = "PREFERENCES_UNAVAILABLE"
    if code is not None:
        raise _error(code)
    return actor


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _constant(_):
    raise ValueError()


def _shape(value, *, update):
    version_key = "expectedVersion" if update else "version"
    schema = "notification-preferences-update-v1" if update else "notification-preferences-v1"
    if (type(value) is not dict or value.keys() != {"schemaVersion", version_key, "email", "telegram"}
            or value["schemaVersion"] != schema):
        raise ValueError()
    parse_version(value[version_key])
    email, telegram = value["email"], value["telegram"]
    if (type(email) is not dict or email.keys() != {"enabled", "dailyDigest", "criticalAlerts"}
            or type(telegram) is not dict or telegram.keys() != {"enabled"}):
        raise ValueError()
    flags = (email["enabled"], email["dailyDigest"], email["criticalAlerts"], telegram["enabled"])
    if any(type(flag) is not bool for flag in flags):
        raise ValueError()
    return NotificationPreferenceFlags(*flags)


def make_notification_preferences_router(*, service_dependency, max_request_bytes):
    if (not callable(service_dependency) or type(max_request_bytes) is not int or not 0 < max_request_bytes <= 2**31 - 1):
        raise NotificationPreferencesError("PREFERENCES_CONFIGURATION_INVALID")
    router = APIRouter(prefix="/api/v2/notifications", tags=["notification-preferences"])

    async def call(service, actor, *, value=None):
        writing, code, output = value is not None, None, None
        try:
            if type(service) is not NotificationPreferencesService:
                raise NotificationPreferencesError("PREFERENCES_CONFIGURATION_INVALID")
            if writing:
                result = await run_in_threadpool(service.replace, authenticated_actor=actor,
                    expected_version=value["expectedVersion"], flags=_shape(value, update=True))
            else:
                result = await run_in_threadpool(service.get_current, authenticated_actor=actor)
        except NotificationPreferencesError as error:
            code = error.code
        except Exception:  # noqa: BLE001 - mutation outcome unknown.
            code = "PREFERENCES_READBACK_REQUIRED" if writing else "PREFERENCES_UNAVAILABLE"
        if code is None:
            try:
                _shape(result, update=False)
                output = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            except Exception:  # noqa: BLE001 - serialization failure after commit requires readback.
                code = "PREFERENCES_READBACK_REQUIRED" if writing else "PREFERENCES_UNAVAILABLE"
        if code is not None:
            raise _error(code)
        return Response(output, media_type="application/json", headers={"Cache-Control": "no-store"})

    @router.get("/preferences")
    async def get_current(request: Request, actor: Annotated[ActorContext, Depends(_actor)],
                          service: Annotated[NotificationPreferencesService, Depends(service_dependency)]):
        if request.query_params:
            raise _error("PREFERENCES_INVALID")
        return await call(service, actor)

    @router.put("/preferences")
    async def replace(request: Request, actor: Annotated[ActorContext, Depends(_actor)],
                      service: Annotated[NotificationPreferencesService, Depends(service_dependency)]):
        invalid = False
        try:
            if (request.query_params or request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
                    or request.headers.get("content-encoding", "identity").lower() != "identity"):
                raise ValueError()
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_request_bytes:
                    raise ValueError()
                body.extend(chunk)
            value = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
            _shape(value, update=True)
        except Exception:  # noqa: BLE001 - fixed errors for malformed/oversized/disconnected bodies.
            invalid = True
        if invalid:
            raise _error("PREFERENCES_INVALID")
        return await call(service, actor, value=value)

    return router
