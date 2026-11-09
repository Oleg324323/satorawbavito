"""Connection and durable intent routes. Registration belongs to bootstrap."""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator
from app.config import get_settings
from app.control_plane.auth import ActorContext
from app.infra.db import get_session_factory
from app.platform.integrations.access import get_marketplace_credential_actor
from app.platform.integrations.credential_store import _load_keyring
from app.routers.cabinet import _credential_status_view
from app.wb_live.connection import WbAccountConnection
from app.wb_live.contracts import WbLiveError
from app.wb_live.repository import WbLiveRepository
from app.wb_live.history_repository import validate_date_from

router = APIRouter(tags=["wb-live"])

def live_repository():
    if not get_settings().wb_live_sync_enabled:
        raise HTTPException(503, detail={"code": "WB_LIVE_UNAVAILABLE"})
    return WbLiveRepository(get_session_factory(), _load_keyring)

def live_connection():
    live_repository()
    return WbAccountConnection(session_factory=get_session_factory(), keyring_loader=_load_keyring)

def error(e):
    code = e.code if isinstance(e, WbLiveError) else "WB_LIVE_UNAVAILABLE"
    status = 403 if code == "WB_ACCESS_DENIED" else (409 if code in {"WB_BINDING_CHANGED", "WB_SYNC_CONFLICT"} else 503)
    return HTTPException(status, detail={"code": code})

class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    wbToken: SecretStr
    displayName: str | None = None

    @model_validator(mode="before")
    @classmethod
    def safe_shape(cls, value):
        if type(value) is not dict or set(value) - {"wbToken", "displayName"}:
            raise ValueError("WB_CONNECT_PAYLOAD_INVALID")
        token = value.get("wbToken")
        if isinstance(token, SecretStr):
            token = token.get_secret_value()
        try:
            valid = type(token) is str and 8 <= len(token.encode("utf-8")) <= 8192 and not any(c.isspace() for c in token)
        except UnicodeError:
            valid = False
        label = value.get("displayName")
        if not valid or (label is not None and (type(label) is not str or not 1 <= len(label.strip()) <= 128 or "\x00" in label)):
            raise ValueError("WB_CONNECT_PAYLOAD_INVALID")
        return value

class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def safe_shape(cls, value):
        if type(value) is not dict or value:
            raise ValueError("WB_SYNC_PAYLOAD_INVALID")
        return value

class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    dateFrom: str

    @model_validator(mode="before")
    @classmethod
    def safe_shape(cls, value):
        if type(value) is not dict or set(value) != {"dateFrom"}:
            raise ValueError("WB_HISTORY_PAYLOAD_INVALID")
        try:
            validate_date_from(value["dateFrom"])
        except WbLiveError:
            raise ValueError("WB_HISTORY_PAYLOAD_INVALID") from None
        return value

@router.get("/api/v1/cabinet/marketplace-accounts")
def accounts(provider: str = Query("wb", pattern="^wb$"), actor: ActorContext = Depends(get_marketplace_credential_actor),
             service=Depends(live_connection)):
    try:
        return {"data": service.list_accounts(actor)}
    except Exception as e:
        raise error(e) from None

@router.post("/api/v1/cabinet/marketplace-accounts/connect/wb")
def connect(payload: ConnectRequest, actor: ActorContext = Depends(get_marketplace_credential_actor), service=Depends(live_connection)):
    try:
        account, metadata = service.connect(actor, token=payload.wbToken.get_secret_value(), display_name=payload.displayName)
        return {"data": {"account": account, "credential": _credential_status_view(account["marketplaceAccountId"], metadata)}}
    except Exception as e:
        raise error(e) from None

@router.get("/api/v2/wb/accounts/{account_id}/sync")
def status(account_id: int, actor: ActorContext = Depends(get_marketplace_credential_actor), repo=Depends(live_repository)):
    try:
        return {"data": repo.status(actor, account_id)}
    except Exception as e:
        raise error(e) from None

@router.post("/api/v2/wb/accounts/{account_id}/sync")
def start(account_id: int, payload: SyncRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
          actor: ActorContext = Depends(get_marketplace_credential_actor), repo=Depends(live_repository)):
    try:
        # Scheduler polls this committed intent; no Redis dependency in acceptance.
        return {"data": repo.create_job(actor, account_id, idempotency_key)}
    except Exception as e:
        raise error(e) from None

@router.post("/api/v2/wb/accounts/{account_id}/history")
def start_history(account_id: int, payload: HistoryRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
                  actor: ActorContext = Depends(get_marketplace_credential_actor), repo=Depends(live_repository)):
    try:
        return {"data": repo.create_history_job(actor, account_id, idempotency_key, date_from=payload.dateFrom)}
    except Exception as e:
        raise error(e) from None
