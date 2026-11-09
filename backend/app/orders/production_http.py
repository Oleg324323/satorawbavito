"""Unregistered Production HTTP factory; no implicit runtime or capability grants."""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.control_plane.auth import ActorContext, actor_from_request
from app.modules.production import AssignmentCommand
from app.orders.production_service import (
    ProductionServiceError,
    assign_production_work_item,
    create_production_work_item,
    read_production_work_item,
    read_production_work_item_by_source,
)
from app.orders.router import discover_orders_bindings
from app.platform.integrations.publication_guard import PublicationGuardError

DecimalId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
Quantity = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CreateRequest(Wire):
    orderItemId: DecimalId
    expectedSourceItemVersion: DecimalId


class AssignRequest(Wire):
    expectedVersion: DecimalId
    catalogSkuId: PositiveInt
    idempotencyKey: str
    reason: str


class CreateResponse(Wire):
    schemaVersion: Literal["production-create-v1"] = "production-create-v1"
    workItemId: DecimalId
    replayed: bool


class AssignmentValue(Wire):
    workItemId: DecimalId
    version: DecimalId
    catalogSkuId: PositiveInt
    requiredQuantity: PositiveInt
    plannedQuantity: Quantity
    remainingQuantity: Quantity
    sourceItemVersion: DecimalId


class AssignmentResponse(Wire):
    schemaVersion: Literal["production-assignment-v1"] = "production-assignment-v1"
    result: AssignmentValue
    replayed: bool


class WorkItemValue(Wire):
    workItemId: DecimalId
    organizationId: PositiveInt
    marketplaceAccountId: PositiveInt
    orderId: DecimalId
    orderItemId: DecimalId
    sourceItemVersion: DecimalId
    requiredQuantity: PositiveInt
    plannedQuantity: Quantity
    remainingQuantity: Quantity
    catalogSkuId: PositiveInt | None
    version: DecimalId
    createdAt: str
    updatedAt: str
    currentAssignmentReceiptId: DecimalId | None


class WorkItemResponse(Wire):
    schemaVersion: Literal["production-work-item-v1"] = "production-work-item-v1"
    item: WorkItemValue


@dataclass(frozen=True, slots=True)
class ProductionHttpRuntime:
    session_factory: Callable[[], Session]

    def __post_init__(self):
        if not callable(self.session_factory):
            raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")


_STATUS = {
    "PRODUCTION_REQUEST_INVALID": 400,
    "PRODUCTION_AUTHENTICATION_REQUIRED": 401,
    "PRODUCTION_DENIED": 403,
    "PRODUCTION_CATALOG_DENIED": 403,
    "PRODUCTION_NOT_FOUND": 404,
    "PRODUCTION_SOURCE_INVALID": 409,
    "PRODUCTION_SOURCE_CHANGED": 409,
    "PRODUCTION_RECEIPT_INVALID": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "VERSION_CONFLICT": 409,
    "PRODUCTION_DISABLED": 503,
    "PRODUCTION_CONTEXT_INVALID": 503,
    "PRODUCTION_STORAGE_UNAVAILABLE": 503,
    "PRODUCTION_READBACK_REQUIRED": 503,
}


def _error(code):
    code = code if code in _STATUS else "PRODUCTION_STORAGE_UNAVAILABLE"
    return HTTPException(
        _STATUS[code], detail={"code": code}, headers={"Cache-Control": "no-store"}
    )


def _disabled():
    raise _error("PRODUCTION_DISABLED")


def production_actor(request: Request):
    code = None
    try:
        result = actor_from_request(request)
    except HTTPException:
        code = "PRODUCTION_AUTHENTICATION_REQUIRED"
    except Exception:  # noqa: BLE001 - no authentication internals at HTTP boundary.
        code = "PRODUCTION_STORAGE_UNAVAILABLE"
    if code:
        raise _error(code)
    return result


def _id(value, maximum=2**63 - 1):
    if (
        type(value) is not str
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
        or int(value) > maximum
    ):
        raise ValueError("invalid identifier")
    return int(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite number")


def _wire(value):
    result = {}
    decimal = {
        "work_item_id",
        "order_id",
        "order_item_id",
        "source_item_version",
        "version",
        "current_assignment_receipt_id",
    }
    for name, item in asdict(value).items():
        pieces = name.split("_")
        key = pieces[0] + "".join(piece.title() for piece in pieces[1:])
        if name in decimal and item is not None:
            if type(item) is not int or not 0 < item <= 2**63 - 1:
                raise ValueError("invalid stored integer")
            item = str(item)
        elif isinstance(item, datetime):
            if item.utcoffset() is None:
                raise ValueError("invalid stored timestamp")
            item = (
                item.astimezone(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        result[key] = item
    return result


def make_production_router(*, max_request_bytes: int, runtime_dependency=_disabled):
    if (
        type(max_request_bytes) is not int
        or not 0 < max_request_bytes <= 2**31 - 1
        or not callable(runtime_dependency)
    ):
        raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")
    router = APIRouter(
        prefix="/api/v2/production/accounts/{account_id}/work-items",
        tags=["production"],
    )

    async def body(request, model):
        invalid = False
        try:
            if (
                request.query_params
                or request.headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/json"
                or request.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise ValueError()
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > max_request_bytes:
                    raise ValueError()
                data.extend(chunk)
            value = json.loads(
                bytes(data).decode("utf-8"),
                object_pairs_hook=_unique,
                parse_constant=_constant,
            )
            result = model.model_validate(value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            invalid = True
        if invalid:
            raise _error("PRODUCTION_REQUEST_INVALID")
        return result

    def execute(runtime, actor, account_id, operation, **arguments):
        if type(runtime) is not ProductionHttpRuntime or not isinstance(
            actor, ActorContext
        ):
            raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")
        session = runtime.session_factory()
        # Reject a borrowed root before entering a context that would close it.
        if (
            not isinstance(session, Session)
            or session.in_transaction()
            or not session.is_active
        ):
            raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")
        with session:
            if (
                not isinstance(session.get_bind(), Engine)
                or session.get_bind().dialect.name != "postgresql"
            ):
                raise ProductionServiceError("PRODUCTION_CONTEXT_INVALID")
            principal, bindings = discover_orders_bindings(
                session, actor, (account_id,)
            )
            return operation(
                session, principal=principal, account=bindings[0], **arguments
            )

    async def call(
        runtime, actor, account_id, operation, response_model, *, write, **arguments
    ):
        code = None
        try:
            result = await run_in_threadpool(
                execute, runtime, actor, account_id, operation, **arguments
            )
            if response_model is CreateResponse:
                response = CreateResponse(**_wire(result))
            elif response_model is AssignmentResponse:
                response = AssignmentResponse(
                    result=AssignmentValue(**_wire(result.result)),
                    replayed=result.replayed,
                )
            else:
                response = WorkItemResponse(item=WorkItemValue(**_wire(result)))
            content = response.model_dump_json()
        except ProductionServiceError as error:
            code = error.code
            if write and (
                code == "PRODUCTION_STORAGE_UNAVAILABLE" or code not in _STATUS
            ):
                code = "PRODUCTION_READBACK_REQUIRED"
        except PublicationGuardError as error:
            code = (
                "PRODUCTION_STORAGE_UNAVAILABLE"
                if error.code
                in {"publication_persistence_failed", "publication_context_invalid"}
                else "PRODUCTION_DENIED"
            )
        except Exception:  # noqa: BLE001 - unknown post-commit errors cannot imply rollback.
            code = (
                "PRODUCTION_READBACK_REQUIRED"
                if write
                else "PRODUCTION_STORAGE_UNAVAILABLE"
            )
        if code:
            raise _error(code)
        return Response(
            content,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    def path(request, account_id, work_item_id=None):
        invalid = False
        try:
            if request.query_params:
                raise ValueError()
            account = _id(account_id, 2**31 - 1)
            work = None if work_item_id is None else _id(work_item_id)
        except ValueError:
            invalid = True
        if invalid:
            raise _error("PRODUCTION_REQUEST_INVALID")
        return account, work

    @router.post(
        "",
        response_model=CreateResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": CreateRequest.model_json_schema()}
                },
            }
        },
    )
    async def create(
        request: Request,
        account_id: str,
        actor: Annotated[ActorContext, Depends(production_actor)],
        runtime: Annotated[ProductionHttpRuntime, Depends(runtime_dependency)],
    ):
        account, _ = path(request, account_id)
        value = await body(request, CreateRequest)
        invalid = False
        try:
            order_id, version = (
                _id(value.orderItemId),
                _id(value.expectedSourceItemVersion),
            )
        except ValueError:
            invalid = True
        if invalid:
            raise _error("PRODUCTION_REQUEST_INVALID")
        return await call(
            runtime,
            actor,
            account,
            create_production_work_item,
            CreateResponse,
            write=True,
            order_item_id=order_id,
            expected_source_item_version=version,
        )

    @router.get("/by-order-item/{order_item_id}", response_model=WorkItemResponse)
    async def read_by_source(
        request: Request,
        account_id: str,
        order_item_id: str,
        actor: Annotated[ActorContext, Depends(production_actor)],
        runtime: Annotated[ProductionHttpRuntime, Depends(runtime_dependency)],
    ):
        invalid = False
        try:
            pairs = list(request.query_params.multi_items())
            if len(pairs) != 1 or pairs[0][0] != "expectedSourceItemVersion":
                raise ValueError()
            account = _id(account_id, 2**31 - 1)
            item = _id(order_item_id)
            version = _id(pairs[0][1])
        except ValueError:
            invalid = True
        if invalid:
            raise _error("PRODUCTION_REQUEST_INVALID")
        return await call(
            runtime,
            actor,
            account,
            read_production_work_item_by_source,
            WorkItemResponse,
            write=False,
            order_item_id=item,
            expected_source_item_version=version,
        )

    @router.get("/{work_item_id}", response_model=WorkItemResponse)
    async def read(
        request: Request,
        account_id: str,
        work_item_id: str,
        actor: Annotated[ActorContext, Depends(production_actor)],
        runtime: Annotated[ProductionHttpRuntime, Depends(runtime_dependency)],
    ):
        account, work = path(request, account_id, work_item_id)
        return await call(
            runtime,
            actor,
            account,
            read_production_work_item,
            WorkItemResponse,
            write=False,
            work_item_id=work,
        )

    @router.post(
        "/{work_item_id}/assignments",
        response_model=AssignmentResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": AssignRequest.model_json_schema()}
                },
            }
        },
    )
    async def assign(
        request: Request,
        account_id: str,
        work_item_id: str,
        actor: Annotated[ActorContext, Depends(production_actor)],
        runtime: Annotated[ProductionHttpRuntime, Depends(runtime_dependency)],
    ):
        account, work = path(request, account_id, work_item_id)
        value = await body(request, AssignRequest)
        invalid = False
        try:
            command = AssignmentCommand(
                work,
                _id(value.expectedVersion),
                value.idempotencyKey,
                value.catalogSkuId,
                value.reason,
            )
        except ValueError:
            invalid = True
        if invalid:
            raise _error("PRODUCTION_REQUEST_INVALID")
        return await call(
            runtime,
            actor,
            account,
            assign_production_work_item,
            AssignmentResponse,
            write=True,
            command=command,
        )

    return router
