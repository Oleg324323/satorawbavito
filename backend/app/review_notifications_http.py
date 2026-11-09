"""Dormant router factory for exact visible Review notification IDs only.

No global engine/settings/registration or inferred notification discovery policy.
Bootstrap must provide a trusted service dependency and explicit request budgets.
"""

import json
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.control_plane.auth import ActorContext, actor_from_request
from app.notification_service import NotificationServiceError, ReviewNotificationService

_STATUS = {"NOTIFICATION_INVALID": 400, "NOTIFICATION_AUTHENTICATION_REQUIRED": 401,
    "NOTIFICATION_DENIED": 403, "NOTIFICATION_NOT_FOUND": 404, "NOTIFICATION_CONFLICT": 409,
    "NOTIFICATION_DISABLED": 409, "NOTIFICATION_READBACK_REQUIRED": 503,
    "NOTIFICATION_UNAVAILABLE": 503, "NOTIFICATION_CONFIGURATION_INVALID": 503}


def _error(code):
    code = code if code in _STATUS else "NOTIFICATION_UNAVAILABLE"
    return HTTPException(_STATUS[code], detail={"code": code}, headers={"Cache-Control": "no-store"})


def _actor(request: Request):
    code = None
    try:
        actor = actor_from_request(request)
    except HTTPException:
        code = "NOTIFICATION_AUTHENTICATION_REQUIRED"
    except Exception:  # noqa: BLE001 - HTTP boundary must not expose authentication internals.
        code = "NOTIFICATION_UNAVAILABLE"
    if code is not None:
        raise _error(code)
    return actor


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(_):
    raise ValueError()


def _wire(value):
    if type(value) is list:
        return [_wire(item) for item in value]
    if type(value) is not dict:
        return value
    result = {}
    for key, item in value.items():
        if key in {"sourceVersion", "version"}:
            if type(item) is not int or item <= 0:
                raise ValueError()
            result[key] = str(item)
        else:
            result[key] = _wire(item)
    return result


def make_review_notifications_router(*, service_dependency, max_request_bytes: int, max_visible_ids: int):
    """No defaults: deployment budgets are not domain policy or permission grants."""
    if (not callable(service_dependency) or any(type(n) is not int or not 0 < n <= 2**31 - 1
                                               for n in (max_request_bytes, max_visible_ids))):
        raise NotificationServiceError("NOTIFICATION_CONFIGURATION_INVALID")
    router = APIRouter(prefix="/api/v2/reviews/notifications", tags=["canonical-review-notifications"])

    def validate(scope, ids):
        if (type(scope) is not dict or set(scope) != {"marketplaceAccountId", "marketplace"}
                or type(scope["marketplaceAccountId"]) is not int or not 0 < scope["marketplaceAccountId"] <= 2**31 - 1
                or type(scope["marketplace"]) is not str or scope["marketplace"] not in {"wb", "avito"}
                or type(ids) is not list or not 0 < len(ids) <= max_visible_ids
                or any(type(item) is not str or len(item) != 36 for item in ids)):
            raise ValueError()
        if len(set(ids)) != len(ids) or any(str(UUID(item)) != item or UUID(item).int == 0 for item in ids):
            raise ValueError()

    async def call(service, method, actor, scope, ids, **extra):
        code, output = None, None
        try:
            if type(service) is not ReviewNotificationService:
                raise NotificationServiceError("NOTIFICATION_CONFIGURATION_INVALID")
            result = await run_in_threadpool(getattr(service, method), authenticated_actor=actor,
                marketplace_account_id=scope["marketplaceAccountId"], marketplace=scope["marketplace"], event_ids=ids, **extra)
            output = json.dumps(_wire(result), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        except NotificationServiceError as error:
            code = error.code
        except Exception:  # noqa: BLE001 - an unknown post-commit result requires readback.
            # A serialization failure after receipt commit is not a rolled-back write.
            code = "NOTIFICATION_READBACK_REQUIRED" if method == "mark_visible" else "NOTIFICATION_UNAVAILABLE"
        if code is not None:
            raise _error(code)
        return Response(output, media_type="application/json", headers={"Cache-Control": "no-store"})

    def query(request):
        values, ids = {}, []
        for key, value in request.query_params.multi_items():
            if key == "event_id":
                ids.append(value)
            elif key not in {"marketplace_account_id", "marketplace"} or key in values:
                raise ValueError()
            else:
                values[key] = value
        if set(values) != {"marketplace_account_id", "marketplace"} or re.fullmatch(r"[1-9][0-9]{0,9}", values["marketplace_account_id"]) is None:
            raise ValueError()
        scope = {"marketplaceAccountId": int(values["marketplace_account_id"]), "marketplace": values["marketplace"]}
        validate(scope, ids)
        return scope, ids

    async def read(request, actor, service, method):
        invalid = False
        try:
            scope, ids = query(request)
        except (ValueError, TypeError, UnicodeError):
            invalid = True
        if invalid:
            raise _error("NOTIFICATION_INVALID")
        return await call(service, method, actor, scope, ids)

    @router.get("/visible")
    async def visible(request: Request, actor: Annotated[ActorContext, Depends(_actor)],
                      service: Annotated[ReviewNotificationService, Depends(service_dependency)]):
        return await read(request, actor, service, "read_visible")

    @router.get("/capabilities")
    async def capabilities(request: Request, actor: Annotated[ActorContext, Depends(_actor)],
                           service: Annotated[ReviewNotificationService, Depends(service_dependency)]):
        return await read(request, actor, service, "capabilities")

    @router.post("/receipts")
    async def receipts(request: Request, actor: Annotated[ActorContext, Depends(_actor)],
                       service: Annotated[ReviewNotificationService, Depends(service_dependency)]):
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
            if (type(value) is not dict or set(value) != {"schemaVersion", "organizationId", "marketplaceAccountId", "marketplace", "eventIds", "action"}
                    or value["schemaVersion"] != "review-notification-action-v1"
                    or type(value["organizationId"]) is not int or value["organizationId"] != actor.organization_id
                    or type(value["action"]) is not str or value["action"] not in {"read", "dismiss"}):
                raise ValueError()
            scope = {"marketplaceAccountId": value["marketplaceAccountId"], "marketplace": value["marketplace"]}
            validate(scope, value["eventIds"])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            invalid = True
        if invalid:
            raise _error("NOTIFICATION_INVALID")
        return await call(service, "mark_visible", actor, scope, value["eventIds"], action=value["action"])

    return router
