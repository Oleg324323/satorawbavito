"""Typed, dormant list factory; bootstrap owns allowlist, codec and registration."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.control_plane.auth import ActorContext
from app.notification_list import NotificationListCursorCodec
from app.notification_service import NotificationServiceError, ReviewNotificationService
from app.review_notifications_http import _actor, _error, _wire


class WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("occurredAt", "readAt", "dismissedAt", check_fields=False)
    @classmethod
    def aware_instant(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Aware notification instant required")
        return value

    @field_serializer("occurredAt", "readAt", "dismissedAt", check_fields=False)
    def wire_instant(self, value):
        if value is None:
            return None
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )


class NotificationEventView(WireModel):
    schemaVersion: Literal["notification-event-v1"]
    eventId: UUID
    organizationId: int
    marketplaceAccountId: int
    scope: Literal["account"]
    producer: Literal["reviews"]
    entityId: UUID
    sourceVersion: str = Field(pattern=r"^[1-9][0-9]*$")
    kind: Literal["approval_required", "send_blocked", "send_ambiguous"]
    occurredAt: datetime
    dedupeKey: str
    title: str
    details: str
    severity: Literal["info", "warning"]


class NotificationReceiptValue(WireModel):
    schemaVersion: Literal["notification-in-app-receipt-v1"]
    organizationId: int
    marketplaceAccountId: int
    eventId: UUID
    recipientMembershipId: int
    readAt: datetime | None
    dismissedAt: datetime | None


class NotificationReceiptView(WireModel):
    value: NotificationReceiptValue
    version: str = Field(pattern=r"^[1-9][0-9]*$")


class NotificationItemView(WireModel):
    event: NotificationEventView
    receipt: NotificationReceiptView | None


class NotificationCapabilities(WireModel):
    canRead: bool
    canMarkRead: bool
    canDismiss: bool


class NotificationListResponse(WireModel):
    schemaVersion: Literal["review-notification-list-v1"]
    organizationId: int
    marketplaceAccountId: int
    marketplace: Literal["wb", "avito"]
    recipientMembershipId: int
    eventIds: list[UUID]
    items: list[NotificationItemView]
    nextCursor: str | None
    eventSetVersion: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    capabilities: NotificationCapabilities


def make_review_notification_list_router(
    *, service_dependency, cursor_codec_dependency
):
    if not callable(service_dependency) or not callable(cursor_codec_dependency):
        raise NotificationServiceError("NOTIFICATION_CONFIGURATION_INVALID")
    router = APIRouter(
        prefix="/api/v2/reviews/notifications", tags=["canonical-review-notifications"]
    )

    @router.get("", response_model=NotificationListResponse)
    def list_notifications(
        request: Request,
        response: Response,
        marketplace_account_id: Annotated[int, Query(gt=0, le=2**31 - 1)],
        marketplace: Literal["wb", "avito"],
        actor: Annotated[ActorContext, Depends(_actor)],
        service: Annotated[ReviewNotificationService, Depends(service_dependency)],
        codec: Annotated[NotificationListCursorCodec, Depends(cursor_codec_dependency)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    ):
        response.headers["Cache-Control"] = "no-store"
        allowed = {"marketplace_account_id", "marketplace", "limit", "cursor"}
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise _error("NOTIFICATION_INVALID")
        code = None
        try:
            if (
                type(service) is not ReviewNotificationService
                or type(codec) is not NotificationListCursorCodec
            ):
                raise NotificationServiceError("NOTIFICATION_CONFIGURATION_INVALID")
            result = service.list_visible(
                authenticated_actor=actor,
                marketplace_account_id=marketplace_account_id,
                marketplace=marketplace,
                limit=limit,
                cursor=cursor,
                codec=codec,
            )
            output = NotificationListResponse.model_validate(_wire(result))
        except NotificationServiceError as error:
            code = error.code
        except Exception:  # noqa: BLE001 - no storage/source/auth details at the HTTP boundary.
            code = "NOTIFICATION_UNAVAILABLE"
        if code is not None:
            raise _error(code)
        return output

    return router
