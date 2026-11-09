"""Default-off account-scoped HTTP adapter for existing committed override service.

Bootstrap owns the real service/context resolver and account activation policy.
The writer admission callback is NOT an atomic writer fence: it may return True
only after an independently verified legacy-writer cutoff. No source hydration,
legacy-global writes, formula changes, credential reads or price sending here.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
)
from starlette.concurrency import run_in_threadpool

from app.control_plane.auth import actor_from_request
from app.modules.wb_repricing_override_access import OverrideMappingUnresolvedError
from app.modules.wb_repricing_override_service import (
    OverrideConflictError,
    OverridePersistenceError,
    OverrideRevision,
    OverrideScope,
)
from app.modules.wb_repricing_overrides import (
    OverrideChange,
    OverrideCommandValidationError,
    OverrideValues,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
)

InternalId = Annotated[int, Path(gt=0, lt=2**31)]
IntegerText = Annotated[
    str, Field(strict=True, pattern=r"^(0|[1-9][0-9]*)$", max_length=128)
]
DecimalText = Annotated[
    str, Field(strict=True, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=128)
]


class OverrideValuesBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    automation_enabled: StrictBool | None
    allow_negative_margin: StrictBool | None
    night_median_enabled: StrictBool | None
    p_min_kopecks: IntegerText | None
    p_max_kopecks: IntegerText | None
    rrp_kopecks: IntegerText | None
    min_margin_kopecks: IntegerText | None
    max_margin_kopecks: IntegerText | None
    min_margin_pct: DecimalText | None
    max_margin_pct: DecimalText | None
    price_step_pct: DecimalText | None
    price_step_minutes: StrictInt | None
    basket_norm_manual: StrictInt | None
    basket_norm_mode: Literal["auto", "manual", "fallback_by_type"] | None

    def domain(self):
        values = self.model_dump()
        for name, value in values.items():
            if value is not None and name.endswith("_kopecks"):
                values[name] = int(value)
            elif value is not None and name.endswith("_pct"):
                values[name] = Decimal(value)
        return OverrideValues(**values)


class ReplaceOverridesBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    commandId: Annotated[str, Field(strict=True, max_length=36)]
    expectedVersion: IntegerText
    values: OverrideValuesBody


def _safe(operation):
    status, code = 503, "WB_OVERRIDES_UNAVAILABLE"
    try:
        return operation()
    except HTTPException:
        raise
    except (
        OverrideCommandValidationError,
        ValidationError,
        json.JSONDecodeError,
        UnicodeError,
    ):
        status, code = 422, "WB_OVERRIDES_INVALID_REQUEST"
    except OverrideConflictError:
        status, code = 409, "WB_OVERRIDES_CONFLICT"
    except OverrideMappingUnresolvedError:
        status, code = 409, "WB_OVERRIDES_MAPPING_UNRESOLVED"
    except PublicationGuardError as error:
        if error.code in {
            "publication_access_denied",
            "publication_expired",
            "publication_context_invalid",
        }:
            status, code = 403, "WB_OVERRIDES_ACCESS_DENIED"
        elif error.code == "publication_binding_changed":
            status, code = 409, "WB_OVERRIDES_BINDING_CHANGED"
    except Exception:  # noqa: BLE001 -- HTTP boundary must not disclose SQL, payloads or infrastructure details.
        code = "WB_OVERRIDES_UNAVAILABLE"
    raise HTTPException(status_code=status, detail={"code": code})


def _render(revision, scope):
    if type(revision) is not OverrideRevision or (
        revision.change.organization_id,
        revision.change.marketplace_account_id,
        revision.change.catalog_sku_id,
    ) != (scope.organization_id, scope.marketplace_account_id, scope.catalog_sku_id):
        raise OverridePersistenceError()
    if revision.created_at.utcoffset() is None:
        raise OverridePersistenceError()
    return {
        "revision": str(revision.revision),
        "parentRevision": None
        if revision.parent_revision is None
        else str(revision.parent_revision),
        "commandId": revision.change.command_id,
        "actorMembershipId": revision.change.actor_membership_id,
        "createdAt": revision.created_at.astimezone(UTC).isoformat(),
        "requestChecksum": revision.request_checksum,
        "values": json.loads(revision.canonical_request_bytes)["values"],
    }


def _pairs(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise OverrideCommandValidationError()
        values[key] = value
    return values


def create_wb_repricing_overrides_router(
    *,
    service_factory,
    context_resolver,
    enabled_for=None,
    writer_fenced_for=None,
    actor_resolver=actor_from_request,
    clock=None,
):
    """Inject real SkuOverrideService factory and T1's metadata-only context resolver.

    context_resolver(actor, account_id) -> (UserSessionPrincipal, ExpectedAccountBinding).
    Both policy callbacks accept exact (organization_id, marketplace_account_id).
    They default to denial; they never substitute for the service's live guards.
    """
    router = APIRouter(
        prefix="/api/v2/wb/repricing/accounts", tags=["wb-repricing-overrides"]
    )
    clock = clock or (lambda: datetime.now(UTC))

    def context(request, account_id, catalog_sku_id, *, write=False):
        actor = actor_resolver(request)
        if (
            enabled_for is None
            or enabled_for(actor.organization_id, account_id) is not True
        ):
            raise HTTPException(404, detail={"code": "WB_OVERRIDES_DISABLED"})
        if write and (
            writer_fenced_for is None
            or writer_fenced_for(actor.organization_id, account_id) is not True
        ):
            raise HTTPException(409, detail={"code": "WB_OVERRIDES_WRITER_NOT_FENCED"})
        principal, binding = context_resolver(actor, account_id)
        if (
            type(principal) is not UserSessionPrincipal
            or type(binding) is not ExpectedAccountBinding
            or principal.organization_id != actor.organization_id
            or principal.user_id != actor.user_id
            or principal.session_id != actor.session_id
            or binding.marketplace_account_id != account_id
            or binding.provider != "wb"
        ):
            raise PublicationGuardError("publication_context_invalid")
        return (
            OverrideScope(principal.organization_id, account_id, catalog_sku_id),
            principal,
            binding,
        )

    @router.get("/{account_id}/skus/{catalog_sku_id}/overrides")
    def get_current(
        request: Request, account_id: InternalId, catalog_sku_id: InternalId
    ):
        def operation():
            scope, principal, binding = context(request, account_id, catalog_sku_id)
            result = service_factory().get_current(scope, principal, binding)
            return {
                "data": {
                    "marketplaceAccountId": account_id,
                    "catalogSkuId": catalog_sku_id,
                    "version": "0" if result is None else str(result.revision),
                    "revision": None if result is None else _render(result, scope),
                }
            }

        return _safe(operation)

    @router.get("/{account_id}/skus/{catalog_sku_id}/overrides/history")
    def history(
        request: Request,
        account_id: InternalId,
        catalog_sku_id: InternalId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        beforeRevision: Annotated[
            str | None, Query(pattern=r"^[1-9][0-9]*$", max_length=128)
        ] = None,
    ):
        def operation():
            scope, principal, binding = context(request, account_id, catalog_sku_id)
            rows = service_factory().history(
                scope,
                principal,
                binding,
                limit=limit + 1,
                before_revision=None if beforeRevision is None else int(beforeRevision),
            )
            items = [_render(row, scope) for row in rows[:limit]]
            return {
                "data": {
                    "marketplaceAccountId": account_id,
                    "catalogSkuId": catalog_sku_id,
                    "items": items,
                    "nextBeforeRevision": items[-1]["revision"]
                    if len(rows) > limit
                    else None,
                }
            }

        return _safe(operation)

    @router.put("/{account_id}/skus/{catalog_sku_id}/overrides")
    async def replace_overrides(
        request: Request, account_id: InternalId, catalog_sku_id: InternalId
    ):
        scope, principal, binding = await run_in_threadpool(
            lambda: _safe(
                lambda: context(request, account_id, catalog_sku_id, write=True)
            )
        )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, detail={"code": "WB_OVERRIDES_JSON_REQUIRED"})
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16384:
                raise HTTPException(413, detail={"code": "WB_OVERRIDES_BODY_TOO_LARGE"})
            raw.extend(chunk)

        def operation():
            body = ReplaceOverridesBody.model_validate(
                json.loads(raw, object_pairs_hook=_pairs)
            )
            change = OverrideChange(
                scope.organization_id,
                scope.marketplace_account_id,
                scope.catalog_sku_id,
                principal.membership_id,
                body.commandId,
                int(body.expectedVersion),
                body.values.domain(),
            )
            # Re-evaluate rollout admission after reading the body. The service
            # performs its own live authorization through physical COMMIT.
            if enabled_for(scope.organization_id, account_id) is not True:
                raise HTTPException(404, detail={"code": "WB_OVERRIDES_DISABLED"})
            if writer_fenced_for(scope.organization_id, account_id) is not True:
                raise HTTPException(
                    409, detail={"code": "WB_OVERRIDES_WRITER_NOT_FENCED"}
                )
            result = service_factory().replace(change, principal, binding, now=clock())
            return {
                "data": {
                    "marketplaceAccountId": account_id,
                    "catalogSkuId": catalog_sku_id,
                    "version": str(result.revision),
                    "revision": _render(result, scope),
                }
            }

        return await run_in_threadpool(lambda: _safe(operation))

    return router
