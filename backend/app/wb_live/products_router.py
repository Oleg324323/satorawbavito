"""Read-only WB products route; application registration belongs to coordinator."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import get_db_session
from app.platform.integrations.publication_guard import PublicationGuardError
from app.wb_live.contracts import WbLiveError
from app.wb_live.products_http import ProductsQuery, ProductsResponse
from app.wb_live.products_read import (
    ProductsCursorCodec,
    ProductsReadError,
    read_products_page,
)

router = APIRouter(prefix="/api/v2/wb/accounts", tags=["wb-live-products"])


def _error(status, code):
    return HTTPException(status_code=status, detail={"code": code})


def get_products_actor(request: Request):
    try:
        return actor_from_request(request)
    except HTTPException:
        raise _error(401, "WB_AUTHENTICATION_REQUIRED") from None
    except SQLAlchemyError:
        raise _error(503, "WB_PRODUCTS_UNAVAILABLE") from None


def get_products_codec():
    try:
        return ProductsCursorCodec(get_settings().auth_secret.encode("utf-8"))
    except (ValueError, TypeError, AttributeError):
        raise _error(503, "WB_PRODUCTS_UNAVAILABLE") from None


def get_products_query(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    sort: Literal["nmId", "vendorCode", "title", "brand"] = "nmId",
    direction: Literal["asc", "desc"] = "asc",
    q: Annotated[str, Query(max_length=100)] = "",
    brand: Annotated[str | None, Query(max_length=200)] = None,
):
    if set(request.query_params) - {
        "limit",
        "sort",
        "direction",
        "q",
        "brand",
        "cursor",
    }:
        raise _error(400, "WB_PRODUCTS_QUERY_INVALID")
    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise _error(400, "WB_PRODUCTS_QUERY_INVALID")
    return ProductsQuery(limit=limit, sort=sort, direction=direction, q=q, brand=brand)


@router.get("/{marketplace_account_id}/products", response_model=ProductsResponse)
def get_products(
    marketplace_account_id: Annotated[int, Path(gt=0)],
    query: Annotated[ProductsQuery, Depends(get_products_query)],
    actor: Annotated[ActorContext, Depends(get_products_actor)],
    session: Annotated[Session, Depends(get_db_session)],
    codec: Annotated[ProductsCursorCodec, Depends(get_products_codec)],
    response: Response,
    cursor: Annotated[str | None, Query(max_length=16384)] = None,
):
    response.headers["Cache-Control"] = "no-store"
    if not get_settings().wb_live_sync_enabled:
        raise _error(503, "WB_PRODUCTS_UNAVAILABLE")
    try:
        page = read_products_page(
            session,
            actor=actor,
            account_id=marketplace_account_id,
            query=query,
            codec=codec,
            cursor=cursor,
        )
        return ProductsResponse(data=page)
    except ProductsReadError as exc:
        status = {"WB_PRODUCTS_CURSOR_INVALID": 400, "WB_PRODUCTS_CHANGED": 409}.get(
            exc.code, 503
        )
        raise _error(status, exc.code) from None
    except WbLiveError as exc:
        status = 403 if exc.code == "WB_ACCESS_DENIED" else 503
        raise _error(
            status, "WB_ACCESS_DENIED" if status == 403 else "WB_PRODUCTS_UNAVAILABLE"
        ) from None
    except PublicationGuardError as exc:
        status = (
            503
            if exc.code
            in {"publication_context_invalid", "publication_persistence_failed"}
            else 403
        )
        raise _error(
            status, "WB_ACCESS_DENIED" if status == 403 else "WB_PRODUCTS_UNAVAILABLE"
        ) from None
    except (SQLAlchemyError, ValidationError):
        raise _error(503, "WB_PRODUCTS_UNAVAILABLE") from None
