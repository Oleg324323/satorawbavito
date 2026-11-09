"""Dormant local Review API. Registration is a separate platform integration step.

HTTP versions are canonical decimal strings; persistence codecs use exact Python
integers. Raw request/source/credential descriptors never appear in errors/logs.
"""

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.config import Settings, get_settings
from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import get_engine
from app.reviews.local_history import read_local_review_history
from app.reviews.local_repository import ReviewLocalError
from app.reviews.local_service import execute_local_review, read_local_review_context

_STATUS = {
    "REVIEW_LOCAL_AUTHENTICATION_REQUIRED": 401, "REVIEW_LOCAL_DENIED": 403,
    "REVIEW_LOCAL_INVALID": 400, "REVIEW_LOCAL_NOT_FOUND": 404,
    "REVIEW_LOCAL_DISABLED": 409, "REVIEW_LOCAL_CONFLICT": 409,
    "REVIEW_LOCAL_POLICY_CHANGED": 409, "REVIEW_LOCAL_SOURCE_CHANGED": 409,
    "REVIEW_LOCAL_NOT_ANSWERABLE": 409,
    "REVIEW_LOCAL_CONFIGURATION_INVALID": 503, "REVIEW_LOCAL_STORAGE_UNAVAILABLE": 503,
}
_VERSIONS = frozenset({"version", "policyVersion", "draftRevision", "headVersion", "revision",
                      "expectedHeadVersion", "expectedDraftRevision", "expectedPolicyHeadVersion", "policyHeadVersion",
                      "aggregateVersion", "throughVersion", "nextAfterVersion"})
# Transport budget, not a domain text limit: includes UTF-8 JSON and its envelope.
MAX_LOCAL_REVIEW_COMMAND_BYTES = 64 * 1024


def _json_media(request):
    types = request.headers.getlist("content-type")
    encodings = request.headers.getlist("content-encoding")
    if len(types) != 1 or len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity"):
        return False
    parts = [part.strip().lower() for part in types[0].split(";")]
    return parts[0] == "application/json" and (len(parts) == 1 or (
        len(parts) == 2 and re.fullmatch(r'charset\s*=\s*(?:utf-8|"utf-8")', parts[1]) is not None))


def _error(code):
    if code not in _STATUS:
        code = "REVIEW_LOCAL_STORAGE_UNAVAILABLE"
    return HTTPException(_STATUS[code], detail={"code": code}, headers={"Cache-Control": "no-store"})


def _actor(request: Request):
    try:
        return actor_from_request(request)
    except HTTPException:
        raise _error("REVIEW_LOCAL_AUTHENTICATION_REQUIRED") from None
    except SQLAlchemyError:
        raise _error("REVIEW_LOCAL_STORAGE_UNAVAILABLE") from None


def _engine():
    try:
        return get_engine()
    except (SQLAlchemyError, RuntimeError):
        raise _error("REVIEW_LOCAL_STORAGE_UNAVAILABLE") from None


def _settings():
    try:
        return get_settings()
    except RuntimeError:
        raise _error("REVIEW_LOCAL_CONFIGURATION_INVALID") from None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _constant(_):
    raise ValueError()


def _versions(value, *, decode=False):
    if isinstance(value, list):
        return [_versions(item, decode=decode) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in _VERSIONS and item is not None:
            if decode:
                if type(item) is not str or re.fullmatch(r"0|[1-9][0-9]*", item) is None:
                    raise ValueError()
                result[key] = int(item)
            else:
                if type(item) is not int:
                    raise ValueError()
                result[key] = str(item)
        else:
            result[key] = _versions(item, decode=decode)
    return result


def _response(value):
    # Serialize ourselves: never let JSON encoders coerce Decimal to float.
    from starlette.responses import Response

    return Response(json.dumps(_versions(value), ensure_ascii=True, allow_nan=False,
                               separators=(",", ":")), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


router = APIRouter(prefix="/api/v2/reviews/local", tags=["canonical-reviews-local"])


@router.post("/commands")
async def local_review_command(request: Request,
    actor: Annotated[ActorContext, Depends(_actor)], engine: Annotated[Engine, Depends(_engine)],
    settings: Annotated[Settings, Depends(_settings)]):
    invalid = False
    try:
        if not _json_media(request):
            raise ValueError()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_LOCAL_REVIEW_COMMAND_BYTES:
                raise ValueError()
            body.extend(chunk)
        # Manual decoding avoids default validation responses echoing private input.
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_constant)
        command = _versions(value, decode=True)
    except (ValueError, TypeError, UnicodeError, RecursionError, ClientDisconnect):
        invalid = True
    if invalid:
        raise _error("REVIEW_LOCAL_INVALID")
    code = None
    try:
        result = await run_in_threadpool(execute_local_review, engine, actor=actor, settings=settings, request=command)
        return _response(json.loads(result.canonical_bytes))
    except ReviewLocalError as error:
        code = error.code
    if code is not None:
        raise _error(code)


@router.get("/history")
async def local_review_history(request: Request,
    actor: Annotated[ActorContext, Depends(_actor)], engine: Annotated[Engine, Depends(_engine)],
    settings: Annotated[Settings, Depends(_settings)]):
    try:
        query = _unique(request.query_params.multi_items())
        if not {"marketplace_account_id", "marketplace", "review_id"} <= query.keys() or not query.keys() <= {
            "marketplace_account_id", "marketplace", "review_id", "head_id", "through_version", "after_version", "limit"}:
            raise ValueError()
        for key in ("marketplace_account_id", "through_version", "after_version", "limit"):
            if key in query:
                if re.fullmatch(r"0|[1-9][0-9]*", query[key]) is None:
                    raise ValueError()
                query[key] = int(query[key])
    except (ValueError, TypeError):
        raise _error("REVIEW_LOCAL_INVALID") from None
    try:
        value = await run_in_threadpool(read_local_review_history, engine, actor=actor, settings=settings, **query)
        return _response(value)
    except ReviewLocalError as error:
        raise _error(error.code) from None


@router.get("/context")
async def local_review_context(request: Request,
    actor: Annotated[ActorContext, Depends(_actor)], engine: Annotated[Engine, Depends(_engine)],
    settings: Annotated[Settings, Depends(_settings)]):
    try:
        query = _unique(request.query_params.multi_items())
        if not {"marketplace_account_id", "marketplace"} <= query.keys() or not query.keys() <= {
            "marketplace_account_id", "marketplace", "review_id", "external_review_id"}:
            raise ValueError()
        if re.fullmatch(r"[1-9][0-9]*", query["marketplace_account_id"]) is None:
            raise ValueError()
        query["marketplace_account_id"] = int(query["marketplace_account_id"])
    except (ValueError, TypeError):
        raise _error("REVIEW_LOCAL_INVALID") from None
    try:
        value = await run_in_threadpool(read_local_review_context, engine, actor=actor, settings=settings, **query)
        return _response(value)
    except ReviewLocalError as error:
        raise _error(error.code) from None
