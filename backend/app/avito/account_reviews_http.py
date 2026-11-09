"""Dormant account metadata preview, not a Review read/write cutover."""

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from app.avito.account_reviews import AccountReviewsError, validate_offset
from app.platform.integrations.access import get_marketplace_credential_actor


def make_account_avito_reviews_router(*, service_dependency):
    router = APIRouter()

    @router.get("/api/v2/avito/accounts/{account_id}/reviews/preview")
    def preview(request: Request, account_id: str):
        code, status = "AVITO_REVIEWS_PREVIEW_UNAVAILABLE", 503
        try:
            actor = get_marketplace_credential_actor(request)
            params = request.query_params
            if (
                set(params) != {"offset"}
                or len(params.getlist("offset")) != 1
                or not account_id.isascii()
                or not account_id.isdecimal()
                or len(account_id) > 10
                or not 0 < int(account_id) < 2**31
                or account_id != str(int(account_id))
            ):
                raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_INVALID_REQUEST")
            raw = params["offset"]
            if (
                len(raw) > 19
                or not raw.isascii()
                or not raw.isdecimal()
                or raw != str(int(raw))
            ):
                raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_INVALID_REQUEST")
            offset = int(raw)
            validate_offset(offset)
            data = service_dependency().preview(
                actor, marketplace_account_id=int(account_id), offset=offset
            )
            return JSONResponse({"data": data}, headers={"Cache-Control": "no-store"})
        except AccountReviewsError as error:
            code = error.code
            status = (
                403
                if code == "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"
                else 422
                if code == "AVITO_REVIEWS_PREVIEW_INVALID_REQUEST"
                else 503
            )
        except HTTPException as error:
            status = 401 if error.status_code == 401 else 403
            code = "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- no sensitive exception data in response.
            code = "AVITO_REVIEWS_PREVIEW_UNAVAILABLE"
        return JSONResponse(
            {"detail": {"code": code}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    return router
