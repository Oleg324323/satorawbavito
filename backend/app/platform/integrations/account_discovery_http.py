"""Unregistered read-only cabinet account listing; exact safe metadata only."""

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from app.platform.integrations.access import get_marketplace_credential_actor
from app.platform.integrations.account_discovery import AccountDiscoveryError


def make_marketplace_account_discovery_router(*, service_dependency):
    router = APIRouter()

    @router.get("/api/v2/cabinet/marketplace-accounts")
    def list_accounts(request: Request):
        status, code = 503, "ACCOUNT_DISCOVERY_UNAVAILABLE"
        try:
            actor = get_marketplace_credential_actor(request)
            params = request.query_params
            if set(params) - {"provider"} or len(params.getlist("provider")) > 1:
                raise AccountDiscoveryError("ACCOUNT_DISCOVERY_INVALID_REQUEST")
            provider = params.get("provider")
            if provider is not None and provider not in ("wb", "avito"):
                raise AccountDiscoveryError("ACCOUNT_DISCOVERY_INVALID_REQUEST")
            rows = service_dependency().list_accounts(actor, provider=provider)
            return JSONResponse({"data": rows}, headers={"Cache-Control": "no-store"})
        except AccountDiscoveryError as error:
            code = error.code
            status = (
                403
                if code == "ACCOUNT_DISCOVERY_ACCESS_DENIED"
                else 422
                if code == "ACCOUNT_DISCOVERY_INVALID_REQUEST"
                else 503
            )
        except HTTPException as error:
            status = 401 if error.status_code == 401 else 403
            code = "ACCOUNT_DISCOVERY_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- do not return raw infrastructure/provider details.
            code = "ACCOUNT_DISCOVERY_UNAVAILABLE"
        return JSONResponse(
            {"detail": {"code": code}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    return router
