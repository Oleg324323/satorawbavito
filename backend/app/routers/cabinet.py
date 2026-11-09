from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.cabinet.schemas import (
    AuditEventView,
    CabinetMeView,
    IntegrationUpsertRequest,
    IntegrationView,
    MarketplaceCredentialStatusView,
    MarketplaceCredentialWriteRequest,
    SessionBulkRevokeResponse,
    SessionView,
    TeamUserCreateRequest,
    TeamUserPermissionProfileUpdateRequest,
    TeamUserView,
    UserAvitoCredentialsUpsertRequest,
    UserAvitoCredentialsView,
    UserWbTokenUpsertRequest,
    UserWbTokenView,
    UserPreferencesUpdateRequest,
    UserPreferencesView,
)
from app.cabinet.store import (
    build_cabinet_me,
    create_team_user,
    delete_user_avito_credentials,
    delete_user_wb_token,
    get_preferences,
    get_user_avito_credentials,
    get_user_wb_token,
    list_audit_events,
    list_integrations,
    list_team_users,
    list_user_sessions,
    revoke_session,
    revoke_other_sessions,
    upsert_user_avito_credentials,
    upsert_user_wb_token,
    update_team_user_permission_profile,
    update_preferences,
    upsert_integration,
)
from app.config import get_settings
from app.contracts.envelopes import DataEnvelope, PaginatedEnvelope
from app.control_plane.auth import (
    ActorContext,
    actor_from_request,
    has_permission,
    hash_password,
)
from app.infra.db import get_session_factory
from app.platform.integrations.access import get_marketplace_credential_actor
from app.platform.integrations.credential_management import (
    CredentialManagementError,
    CredentialManagementService,
)
from app.platform.integrations.credential_store import (
    CredentialMetadata,
    _load_keyring,
)
from app.platform.integrations.wb_credentials import fetch_wb_seller_id
from app.security.marketplace_credentials import MAX_SECRET_FIELD_BYTES
from app.security import organization_openai


router = APIRouter(tags=["cabinet"])


@router.get("/api/v1/cabinet/openai-key")
def get_openai_key_status(request: Request):
    actor = _require_permission("cabinet:read", request)
    return DataEnvelope(data=organization_openai.key_status(actor.organization_id))


@router.put("/api/v1/cabinet/openai-key")
async def put_openai_key(request: Request):
    actor = _require_permission("integrations:write", request)
    # Parse this sensitive payload ourselves: validation responses must not
    # echo Pydantic input values containing a credential.
    if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 2048:
        raise HTTPException(400, detail="OPENAI_KEY_INVALID")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2048:
            raise HTTPException(400, detail="OPENAI_KEY_INVALID")
    try:
        import json
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"apiKey"}:
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, detail="OPENAI_KEY_INVALID") from None
    try:
        view = await run_in_threadpool(organization_openai.save_key, actor.organization_id, payload["apiKey"])
    except organization_openai.OpenAiKeyError as error:
        raise HTTPException(400 if str(error) == "OPENAI_KEY_INVALID" else 503, detail=str(error)) from None
    return DataEnvelope(data=view)


@router.delete("/api/v1/cabinet/openai-key")
def delete_openai_key(request: Request):
    actor = _require_permission("integrations:write", request)
    return DataEnvelope(data=organization_openai.delete_key(actor.organization_id))


def _enqueue_wb_onboarding_sync(organization_id: int) -> None:
    try:
        from app.repricer_tasks import sync_wb_onboarding_for_org

        sync_wb_onboarding_for_org.delay(organization_id, "complete")
    except Exception:
        return


def _require_permission(permission: str, request: Request):
    actor = actor_from_request(request)
    if not has_permission(actor, permission):
        raise HTTPException(status_code=403, detail=f"NO_ACCESS:{permission}")
    return actor


def _validate_wb_token_for_user_request(wb_token: str) -> None:
    token = wb_token.strip()
    if len(token) < 8:
        raise HTTPException(status_code=400, detail="WB_TOKEN_TOO_SHORT")
    if token != wb_token or any(char.isspace() for char in token):
        raise HTTPException(status_code=400, detail="WB_TOKEN_HAS_WHITESPACE")


def _validate_avito_credentials_for_user_request(client_id: str, client_secret: str) -> None:
    normalized_client_id = client_id.strip()
    normalized_client_secret = client_secret.strip()
    if len(normalized_client_id) < 4:
        raise HTTPException(status_code=400, detail="AVITO_CLIENT_ID_TOO_SHORT")
    if len(normalized_client_secret) < 8:
        raise HTTPException(status_code=400, detail="AVITO_CLIENT_SECRET_TOO_SHORT")
    if (
        normalized_client_id != client_id
        or normalized_client_secret != client_secret
        or any(char.isspace() for char in normalized_client_id)
        or any(char.isspace() for char in normalized_client_secret)
    ):
        raise HTTPException(status_code=400, detail="AVITO_CREDENTIALS_HAVE_WHITESPACE")


def _credential_error(code: str) -> HTTPException:
    if code == "WB_CREDENTIAL_IDENTITY_INVALID":
        return HTTPException(status_code=400, detail={
            "code": code, "message": "WB credential identity could not be verified"})
    if code == "WB_SELLER_IDENTITY_MISMATCH":
        return HTTPException(status_code=409, detail={
            "code": code, "message": "WB seller identity does not match marketplace account"})
    if code == "credential_management_access_denied":
        status_code = 403
    elif code == "credential_account_not_found":
        status_code = 404
    elif code in {
        "credential_account_identity_mismatch",
        "credential_auth_failed",
        "credential_expired",
        "credential_concurrent_update",
    }:
        status_code = 409
    elif code in {
        "credential_configuration_invalid",
        "credential_persistence_failed",
        "credential_key_unavailable",
        "credential_management_context_invalid",
        "credential_management_readback_required",
    }:
        status_code = 503
    elif code == "credential_missing":
        status_code = 404
    else:
        status_code = 400
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": code},
    )


def _credential_management_service() -> CredentialManagementService:
    failed = False
    try:
        service = CredentialManagementService(session_factory=get_session_factory(),
            keyring_loader=_load_keyring, wb_seller_verifier=fetch_wb_seller_id)
    except Exception:  # noqa: BLE001 - never expose configured pool/key paths.
        failed = True
    if failed:
        raise _credential_error("credential_configuration_invalid")
    return service


def _require_user_managed_credential_kind(
    provider: str,
    credential_kind: str,
) -> None:
    if (provider, credential_kind) not in {
        ("wb", "wb_api"),
        ("avito", "avito_oauth_client"),
    }:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CREDENTIAL_KIND_UNSUPPORTED",
                "message": "Credential kind is unsupported for this API",
            },
        )


def _require_bounded_secret(value: str) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        size = MAX_SECRET_FIELD_BYTES + 1
    if size > MAX_SECRET_FIELD_BYTES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CREDENTIAL_PAYLOAD_INVALID",
                "message": "Credential payload is invalid",
            },
        )


def _credential_payload(
    provider: str,
    credential_kind: str,
    payload: MarketplaceCredentialWriteRequest,
) -> dict[str, str]:
    if provider == "wb" and credential_kind == "wb_api":
        if (
            payload.wbToken is None
            or payload.clientId is not None
            or payload.clientSecret is not None
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "CREDENTIAL_PAYLOAD_INVALID",
                    "message": "Credential payload does not match credential kind",
                },
            )
        token = payload.wbToken.get_secret_value()
        _require_bounded_secret(token)
        _validate_wb_token_for_user_request(token)
        return {"token": token}
    if provider == "avito" and credential_kind == "avito_oauth_client":
        if (
            payload.wbToken is not None
            or payload.clientId is None
            or payload.clientSecret is None
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "CREDENTIAL_PAYLOAD_INVALID",
                    "message": "Credential payload does not match credential kind",
                },
            )
        client_id = payload.clientId.get_secret_value()
        client_secret = payload.clientSecret.get_secret_value()
        _require_bounded_secret(client_id)
        _require_bounded_secret(client_secret)
        _validate_avito_credentials_for_user_request(client_id, client_secret)
        return {"clientId": client_id, "clientSecret": client_secret}
    raise HTTPException(
        status_code=400,
        detail={
            "code": "CREDENTIAL_KIND_UNSUPPORTED",
            "message": "Credential kind is unsupported for this API",
        },
    )


def _credential_status_view(
    marketplace_account_id: int,
    metadata: CredentialMetadata | None,
) -> MarketplaceCredentialStatusView:
    if metadata is None:
        return MarketplaceCredentialStatusView(
            marketplaceAccountId=marketplace_account_id,
            status="missing",
        )
    if metadata.revoked_at is not None:
        status = "revoked"
    elif metadata.expires_at is not None and metadata.expires_at <= datetime.now(
        timezone.utc
    ):
        status = "expired"
    else:
        status = "active"
    return MarketplaceCredentialStatusView(
        marketplaceAccountId=marketplace_account_id,
        status=status,
        createdAt=metadata.created_at,
        updatedAt=metadata.updated_at,
        expiresAt=metadata.expires_at,
        revokedAt=metadata.revoked_at,
    )


@router.get("/api/v1/cabinet/me", response_model=DataEnvelope[CabinetMeView])
def get_me(request: Request) -> DataEnvelope[CabinetMeView]:
    actor = _require_permission("cabinet:read", request)
    return DataEnvelope(data=build_cabinet_me(user_id=actor.user_id, current_session_id=actor.session_id))


@router.get("/api/v1/cabinet/team/users", response_model=DataEnvelope[list[TeamUserView]])
def get_team_users(request: Request) -> DataEnvelope[list[TeamUserView]]:
    actor = _require_permission("team:read", request)
    return DataEnvelope(data=list_team_users(actor.organization_id))


@router.post("/api/v1/cabinet/team/users", response_model=DataEnvelope[TeamUserView])
def post_team_user(request: Request, payload: TeamUserCreateRequest) -> DataEnvelope[TeamUserView]:
    actor = _require_permission("team:write", request)
    created = create_team_user(
        organization_id=actor.organization_id,
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.fullName,
        permission_profile=payload.permissionProfile,
        actor_user_id=actor.user_id,
        reason="team onboarding",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=created)


@router.patch("/api/v1/cabinet/team/users/{userId}/permission-profile", response_model=DataEnvelope[TeamUserView])
def patch_team_user_permission_profile(
    request: Request,
    userId: str,
    payload: TeamUserPermissionProfileUpdateRequest,
) -> DataEnvelope[TeamUserView]:
    actor = _require_permission("team:write", request)
    updated = update_team_user_permission_profile(
        organization_id=actor.organization_id,
        user_id=userId,
        permission_profile=payload.permissionProfile,
        actor_user_id=actor.user_id,
        reason=payload.reason,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=updated)


@router.get("/api/v1/cabinet/sessions", response_model=DataEnvelope[list[SessionView]])
def get_sessions(request: Request) -> DataEnvelope[list[SessionView]]:
    actor = _require_permission("sessions:read", request)
    return DataEnvelope(data=list_user_sessions(actor.user_id))


@router.get("/api/v1/cabinet/sessions/current", response_model=DataEnvelope[SessionView | None])
def get_current_session(request: Request) -> DataEnvelope[SessionView | None]:
    actor = _require_permission("sessions:read", request)
    sessions = list_user_sessions(actor.user_id)
    current = next((session for session in sessions if session.sessionId == actor.session_id), None)
    return DataEnvelope(data=current)


@router.post("/api/v1/cabinet/sessions/revoke-others", response_model=DataEnvelope[SessionBulkRevokeResponse])
def post_revoke_other_sessions(request: Request) -> DataEnvelope[SessionBulkRevokeResponse]:
    actor = _require_permission("sessions:read", request)
    revoked = revoke_other_sessions(
        user_id=actor.user_id,
        current_session_id=actor.session_id,
        actor_user_id=actor.user_id,
        organization_id=actor.organization_id,
    )
    return DataEnvelope(data=SessionBulkRevokeResponse(revokedCount=revoked))


@router.post("/api/v1/cabinet/sessions/{sessionId}/revoke", response_model=DataEnvelope[SessionView])
def post_revoke_session(request: Request, sessionId: str) -> DataEnvelope[SessionView]:
    actor = actor_from_request(request)
    own_session = actor.session_id == sessionId
    if not own_session and not has_permission(actor, "sessions:write"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:sessions:write")
    return DataEnvelope(
        data=revoke_session(
            session_id=sessionId,
            actor_user_id=actor.user_id,
            organization_id=actor.organization_id,
            reason="device_logout",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


@router.get("/api/v1/cabinet/integrations", response_model=DataEnvelope[list[IntegrationView]])
def get_integrations(request: Request) -> DataEnvelope[list[IntegrationView]]:
    actor = _require_permission("integrations:read", request)
    return DataEnvelope(data=list_integrations(actor.organization_id))


@router.put("/api/v1/cabinet/integrations/{provider}", response_model=DataEnvelope[IntegrationView])
def put_integration(request: Request, provider: str, payload: IntegrationUpsertRequest) -> DataEnvelope[IntegrationView]:
    actor = _require_permission("integrations:write", request)
    updated = upsert_integration(
        organization_id=actor.organization_id,
        provider=provider,
        status=payload.status,
        external_account_id=payload.externalAccountId,
        token_ref=payload.tokenRef,
        metadata=payload.metadata,
        actor_user_id=actor.user_id,
        reason="integration token/settings updated",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=updated)


@router.get("/api/v1/cabinet/wb-token", response_model=DataEnvelope[UserWbTokenView])
def get_my_wb_token(request: Request) -> DataEnvelope[UserWbTokenView]:
    actor = _require_permission("cabinet:read", request)
    return DataEnvelope(data=get_user_wb_token(actor.user_id))


@router.put("/api/v1/cabinet/wb-token", response_model=DataEnvelope[UserWbTokenView])
def put_my_wb_token(request: Request, payload: UserWbTokenUpsertRequest) -> DataEnvelope[UserWbTokenView]:
    if get_settings().wb_live_sync_enabled:
        raise HTTPException(409, detail={"code": "WB_USE_ACCOUNT_CONNECTION"})
    actor = _require_permission("cabinet:read", request)
    _validate_wb_token_for_user_request(payload.wbToken)
    updated = upsert_user_wb_token(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        wb_token=payload.wbToken,
        reason="user wb token updated",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    settings = get_settings()
    if settings.repricer_wb_sync_enabled and settings.wb_api_mode == "real":
        _enqueue_wb_onboarding_sync(actor.organization_id)
    return DataEnvelope(data=updated)


@router.delete("/api/v1/cabinet/wb-token", response_model=DataEnvelope[UserWbTokenView])
def delete_my_wb_token(request: Request) -> DataEnvelope[UserWbTokenView]:
    if get_settings().wb_live_sync_enabled:
        raise HTTPException(409, detail={"code": "WB_USE_ACCOUNT_CONNECTION"})
    actor = _require_permission("cabinet:read", request)
    cleared = delete_user_wb_token(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        reason="user wb token deleted",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=cleared)


@router.get("/api/v1/cabinet/avito-credentials", response_model=DataEnvelope[UserAvitoCredentialsView])
def get_my_avito_credentials(request: Request) -> DataEnvelope[UserAvitoCredentialsView]:
    actor = _require_permission("cabinet:read", request)
    return DataEnvelope(data=get_user_avito_credentials(actor.user_id))


@router.put("/api/v1/cabinet/avito-credentials", response_model=DataEnvelope[UserAvitoCredentialsView])
def put_my_avito_credentials(
    request: Request,
    payload: UserAvitoCredentialsUpsertRequest,
) -> DataEnvelope[UserAvitoCredentialsView]:
    actor = _require_permission("cabinet:read", request)
    _validate_avito_credentials_for_user_request(payload.clientId, payload.clientSecret)
    updated = upsert_user_avito_credentials(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        client_id=payload.clientId,
        client_secret=payload.clientSecret,
        reason="user avito credentials updated",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=updated)


@router.delete("/api/v1/cabinet/avito-credentials", response_model=DataEnvelope[UserAvitoCredentialsView])
def delete_my_avito_credentials(request: Request) -> DataEnvelope[UserAvitoCredentialsView]:
    actor = _require_permission("cabinet:read", request)
    cleared = delete_user_avito_credentials(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        reason="user avito credentials deleted",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=cleared)


@router.get(
    "/api/v1/cabinet/marketplace-accounts/{marketplaceAccountId}/credentials/{provider}/{credentialKind}",
    response_model=DataEnvelope[MarketplaceCredentialStatusView],
)
def get_marketplace_account_credential_status(
    marketplaceAccountId: int,
    provider: str,
    credentialKind: str,
    actor: ActorContext = Depends(get_marketplace_credential_actor),
    management: CredentialManagementService = Depends(_credential_management_service),
) -> DataEnvelope[MarketplaceCredentialStatusView]:
    _require_user_managed_credential_kind(provider, credentialKind)
    code = None
    try:
        metadata = management.status(authenticated_actor=actor, marketplace_account_id=marketplaceAccountId,
                                     provider=provider, credential_kind=credentialKind)
    except CredentialManagementError as error:
        code = error.code
    if code is not None:
        raise _credential_error(code)
    return DataEnvelope(data=_credential_status_view(marketplaceAccountId, metadata))


@router.put(
    "/api/v1/cabinet/marketplace-accounts/{marketplaceAccountId}/credentials/{provider}/{credentialKind}",
    response_model=DataEnvelope[MarketplaceCredentialStatusView],
)
def put_marketplace_account_credential(
    marketplaceAccountId: int,
    provider: str,
    credentialKind: str,
    payload: MarketplaceCredentialWriteRequest,
    actor: ActorContext = Depends(get_marketplace_credential_actor),
    management: CredentialManagementService = Depends(_credential_management_service),
) -> DataEnvelope[MarketplaceCredentialStatusView]:
    _require_user_managed_credential_kind(provider, credentialKind)
    plaintext = _credential_payload(provider, credentialKind, payload)
    code = None
    try:
        metadata = management.put(authenticated_actor=actor, marketplace_account_id=marketplaceAccountId,
                                  provider=provider, credential_kind=credentialKind, plaintext=plaintext)
    except CredentialManagementError as error:
        code = error.code
    finally:
        plaintext.clear()
    if code is not None:
        raise _credential_error(code)
    return DataEnvelope(
        data=_credential_status_view(marketplaceAccountId, metadata)
    )


@router.delete(
    "/api/v1/cabinet/marketplace-accounts/{marketplaceAccountId}/credentials/{provider}/{credentialKind}",
    response_model=DataEnvelope[MarketplaceCredentialStatusView],
)
def delete_marketplace_account_credential(
    marketplaceAccountId: int,
    provider: str,
    credentialKind: str,
    reasonCode: str = Query(default="operator_revoked"),
    actor: ActorContext = Depends(get_marketplace_credential_actor),
    management: CredentialManagementService = Depends(_credential_management_service),
) -> DataEnvelope[MarketplaceCredentialStatusView]:
    _require_user_managed_credential_kind(provider, credentialKind)
    code = None
    try:
        metadata = management.revoke(authenticated_actor=actor, marketplace_account_id=marketplaceAccountId,
            provider=provider, credential_kind=credentialKind, reason_code=reasonCode)
    except CredentialManagementError as error:
        code = error.code
    if code is not None:
        raise _credential_error(code)
    return DataEnvelope(
        data=_credential_status_view(marketplaceAccountId, metadata)
    )


@router.get("/api/v1/cabinet/audit/events", response_model=PaginatedEnvelope[AuditEventView])
def get_cabinet_audit_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actionPrefix: str | None = None,
    objectType: str | None = None,
    objectId: str | None = None,
    actorUserId: str | None = None,
    createdFrom: date | None = None,
    createdTo: date | None = None,
) -> PaginatedEnvelope[AuditEventView]:
    actor = _require_permission("audit:read", request)
    items, total = list_audit_events(
        actor.organization_id,
        limit=limit,
        offset=offset,
        action_prefix=actionPrefix,
        object_type=objectType,
        object_id=objectId,
        actor_user_id=actorUserId,
        created_from=createdFrom,
        created_to=createdTo,
    )
    return PaginatedEnvelope(items=items, total=total, limit=limit, offset=offset)


@router.get("/api/v1/cabinet/preferences", response_model=DataEnvelope[UserPreferencesView])
def get_user_preferences(request: Request) -> DataEnvelope[UserPreferencesView]:
    actor = _require_permission("preferences:read", request)
    return DataEnvelope(data=get_preferences(actor.user_id))


@router.put("/api/v1/cabinet/preferences", response_model=DataEnvelope[UserPreferencesView])
def put_user_preferences(request: Request, payload: UserPreferencesUpdateRequest) -> DataEnvelope[UserPreferencesView]:
    actor = _require_permission("preferences:write", request)
    updated = update_preferences(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        actor_user_id=actor.user_id,
        notification_settings=payload.notificationSettings,
        export_settings=payload.exportSettings,
        timezone_value=payload.timezone,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return DataEnvelope(data=updated)
