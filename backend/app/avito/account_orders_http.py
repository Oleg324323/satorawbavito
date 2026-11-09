"""Dormant status-preview GET, not the canonical Orders queue or a sync action."""

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from app.avito.account_orders import AccountOrderStatusError, validate_preview_request
from app.platform.integrations.access import get_marketplace_credential_actor


def make_account_avito_order_status_router(*, service_dependency):
    router = APIRouter()

    @router.get("/api/v2/avito/accounts/{account_id}/orders/status-preview")
    def preview(request: Request, account_id: str):
        code, status = "AVITO_ORDER_STATUS_UNAVAILABLE", 503
        try:
            actor = get_marketplace_credential_actor(request)
            params = request.query_params
            if (
                set(params) != {"dateFrom", "page"}
                or any(len(params.getlist(key)) != 1 for key in params)
                or not account_id.isascii()
                or not account_id.isdecimal()
                or len(account_id) > 10
                or not 0 < int(account_id) < 2**31
                or account_id != str(int(account_id))
            ):
                raise AccountOrderStatusError("AVITO_ORDER_STATUS_INVALID_REQUEST")
            try:
                start = date.fromisoformat(params["dateFrom"])
                raw_page = params["page"]
                if (
                    start.isoformat() != params["dateFrom"]
                    or len(raw_page) > 19
                    or not raw_page.isascii()
                    or not raw_page.isdecimal()
                ):
                    raise ValueError
                page = int(raw_page)
                if raw_page != str(page):
                    raise ValueError
                validate_preview_request(start, page)
            except ValueError:
                raise AccountOrderStatusError(
                    "AVITO_ORDER_STATUS_INVALID_REQUEST"
                ) from None
            data = service_dependency().preview(
                actor,
                marketplace_account_id=int(account_id),
                date_from=start,
                page=page,
            )
            return JSONResponse({"data": data}, headers={"Cache-Control": "no-store"})
        except AccountOrderStatusError as error:
            code = error.code
            status = (
                403
                if code == "AVITO_ORDER_STATUS_ACCESS_DENIED"
                else 422
                if code == "AVITO_ORDER_STATUS_INVALID_REQUEST"
                else 503
            )
        except HTTPException as error:
            status = 401 if error.status_code == 401 else 403
            code = "AVITO_ORDER_STATUS_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- fixed code only, no sensitive exception data.
            code = "AVITO_ORDER_STATUS_UNAVAILABLE"
        return JSONResponse(
            {"detail": {"code": code}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    return router
