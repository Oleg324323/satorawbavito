"""Unregistered canonical Avito totals route. No legacy credential resolution."""

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from app.avito.account_stats import AccountStatsError, validate_period
from app.platform.integrations.access import get_marketplace_credential_actor


def make_account_avito_stats_router(*, service_dependency):
    router = APIRouter()

    @router.get("/api/v2/avito/accounts/{account_id}/statistics")
    def statistics(request: Request, account_id: str):
        status, code = 503, "AVITO_ACCOUNT_STATS_UNAVAILABLE"
        try:
            actor = get_marketplace_credential_actor(request)
            params = request.query_params
            if (
                set(params) != {"dateFrom", "dateTo"}
                or any(len(params.getlist(key)) != 1 for key in params)
                or not account_id.isascii()
                or not account_id.isdecimal()
                or len(account_id) > 10
                or not 0 < int(account_id) < 2**31
            ):
                raise AccountStatsError("AVITO_ACCOUNT_STATS_INVALID_REQUEST")
            try:
                start, end = (
                    date.fromisoformat(params[key]) for key in ("dateFrom", "dateTo")
                )
                if (
                    start.isoformat() != params["dateFrom"]
                    or end.isoformat() != params["dateTo"]
                ):
                    raise ValueError
                validate_period(start, end)
            except ValueError:
                raise AccountStatsError("AVITO_ACCOUNT_STATS_INVALID_REQUEST") from None
            data = service_dependency().fetch(
                actor,
                marketplace_account_id=int(account_id),
                date_from=start,
                date_to=end,
            )
            return JSONResponse({"data": data}, headers={"Cache-Control": "no-store"})
        except AccountStatsError as error:
            code = error.code
            status = (
                403
                if code == "AVITO_ACCOUNT_STATS_ACCESS_DENIED"
                else 422
                if code == "AVITO_ACCOUNT_STATS_INVALID_REQUEST"
                else 503
            )
        except HTTPException as error:
            status = 401 if error.status_code == 401 else 403
            code = "AVITO_ACCOUNT_STATS_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- fixed codes, never SQL/provider details.
            code = "AVITO_ACCOUNT_STATS_UNAVAILABLE"
        return JSONResponse(
            {"detail": {"code": code}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    return router
