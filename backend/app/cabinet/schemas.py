from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.contracts.envelopes import UtcDateTime


PermissionProfile = Literal["viewer", "settings_editor", "price_sender", "finance_viewer", "admin", "custom"]
IntegrationProvider = Literal["wb", "avito"]
IntegrationStatus = Literal["disconnected", "connected", "error"]
MarketplaceCredentialStatus = Literal["missing", "active", "expired", "revoked"]


class OrganizationView(BaseModel):
    organizationId: int = Field(ge=1)
    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)
    createdAt: UtcDateTime


class TeamUserView(BaseModel):
    userId: str = Field(min_length=1)
    organizationId: int = Field(ge=1)
    email: str = Field(min_length=3)
    fullName: str = Field(min_length=1)
    permissionProfile: str = Field(min_length=1)
    permissions: list[str]
    isActive: bool
    createdAt: UtcDateTime


class SessionView(BaseModel):
    sessionId: str = Field(min_length=1)
    userId: str = Field(min_length=1)
    issuedAt: UtcDateTime
    expiresAt: UtcDateTime
    lastSeenAt: UtcDateTime
    revokedAt: UtcDateTime | None = None
    revokedReason: str | None = None
    userAgent: str | None = None
    ipAddress: str | None = None


class IntegrationView(BaseModel):
    integrationId: int = Field(ge=1)
    organizationId: int = Field(ge=1)
    provider: IntegrationProvider
    status: IntegrationStatus
    externalAccountId: str | None = None
    tokenRef: str | None = None
    metadata: dict = Field(default_factory=dict)
    updatedByUserId: str | None = None
    updatedAt: UtcDateTime


class AuditEventView(BaseModel):
    eventId: int = Field(ge=1)
    organizationId: int = Field(ge=1)
    actorUserId: str | None = None
    action: str = Field(min_length=1)
    objectType: str = Field(min_length=1)
    objectId: str = Field(min_length=1)
    details: dict | None = None
    beforeState: dict | None = None
    afterState: dict | None = None
    reason: str | None = None
    ipAddress: str | None = None
    userAgent: str | None = None
    createdAt: UtcDateTime


class UserPreferencesView(BaseModel):
    userId: str = Field(min_length=1)
    notificationSettings: dict = Field(default_factory=dict)
    exportSettings: dict = Field(default_factory=dict)
    timezone: str = Field(min_length=1)
    updatedAt: UtcDateTime


class CabinetMeView(BaseModel):
    organization: OrganizationView
    user: TeamUserView
    activeSession: SessionView | None = None
    preferences: UserPreferencesView


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    fullName: str = Field(min_length=1)
    companyName: str = Field(min_length=1)
    wbToken: str | None = Field(default=None, min_length=8)


class RegisterResponse(BaseModel):
    organization: OrganizationView
    user: TeamUserView
    accessToken: str = Field(min_length=1)
    tokenType: str = "bearer"
    expiresIn: int = Field(ge=60)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)


class LoginResponse(BaseModel):
    accessToken: str = Field(min_length=1)
    tokenType: str = "bearer"
    expiresIn: int = Field(ge=60)


class RefreshResponse(BaseModel):
    accessToken: str = Field(min_length=1)
    tokenType: str = "bearer"
    expiresIn: int = Field(ge=60)


class TeamUserCreateRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    fullName: str = Field(min_length=1)
    permissionProfile: PermissionProfile = "viewer"


class TeamUserPermissionProfileUpdateRequest(BaseModel):
    permissionProfile: PermissionProfile
    reason: str | None = None


class IntegrationUpsertRequest(BaseModel):
    status: IntegrationStatus
    externalAccountId: str | None = None
    tokenRef: str | None = None
    metadata: dict = Field(default_factory=dict)


class UserWbTokenView(BaseModel):
    userId: str = Field(min_length=1)
    hasToken: bool
    tokenMasked: str | None = None
    updatedAt: UtcDateTime | None = None


class UserWbTokenUpsertRequest(BaseModel):
    wbToken: str = Field(min_length=8)


class UserAvitoCredentialsView(BaseModel):
    userId: str = Field(min_length=1)
    hasCredentials: bool
    clientIdMasked: str | None = None
    clientSecretMasked: str | None = None
    accessTokenExpiresAt: UtcDateTime | None = None
    updatedAt: UtcDateTime | None = None


class UserAvitoCredentialsUpsertRequest(BaseModel):
    clientId: str = Field(min_length=4)
    clientSecret: str = Field(min_length=8)


class MarketplaceCredentialWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_fields(cls, value: object) -> object:
        if isinstance(value, dict) and any(key not in cls.model_fields for key in value):
            raise ValueError("CREDENTIAL_PAYLOAD_INVALID")
        return value

    wbToken: SecretStr | None = Field(
        default=None,
        min_length=8,
        max_length=8_192,
    )
    clientId: SecretStr | None = Field(
        default=None,
        min_length=4,
        max_length=8_192,
    )
    clientSecret: SecretStr | None = Field(
        default=None,
        min_length=8,
        max_length=8_192,
    )


class MarketplaceCredentialStatusView(BaseModel):
    marketplaceAccountId: int = Field(ge=1)
    status: MarketplaceCredentialStatus
    createdAt: UtcDateTime | None = None
    updatedAt: UtcDateTime | None = None
    expiresAt: UtcDateTime | None = None
    revokedAt: UtcDateTime | None = None


class UserPreferencesUpdateRequest(BaseModel):
    notificationSettings: dict = Field(default_factory=dict)
    exportSettings: dict = Field(default_factory=dict)
    timezone: str = Field(default="UTC", min_length=1)


class SessionRevokeResponse(BaseModel):
    sessionId: str
    revokedAt: datetime


class SessionBulkRevokeResponse(BaseModel):
    revokedCount: int = Field(ge=0)
