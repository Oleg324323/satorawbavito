from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.config import get_settings, validate_security_settings
from app.contracts.envelopes import ErrorEnvelope, ErrorEnvelopeItem
from app.infra.health import readiness_status
from app.orders.router import router as canonical_orders_router
from app.reviews.canonical_read import router as canonical_review_read_router
from app.reviews.canonical_router import router as canonical_reviews_router
from app.reviews.local_http import router as canonical_review_local_router
from app.routers.account_health import router as account_health_router
from app.routers.avito_chats import router as avito_chats_router
from app.routers.avito_listings import router as avito_listings_router
from app.routers.avito_notifications import router as avito_notifications_router
from app.routers.avito_orders import router as avito_orders_router
from app.routers.avito_overview import router as avito_overview_router
from app.routers.avito_repricer import router as avito_repricer_router
from app.routers.avito_reviews import router as avito_reviews_router
from app.routers.avito_stats import router as avito_stats_router
from app.routers.auth import router as auth_router
from app.routers.cabinet import router as cabinet_router
from app.routers.catalog_v2 import router as catalog_v2_router
from app.routers.control_plane import router as control_plane_router
from app.routers.finance_v2 import router as finance_v2_router
from app.routers.notifications import router as notifications_router
from app.routers.one_c_cash_flow import router as one_c_cash_flow_router
from app.routers.source_registry import router as source_registry_router
from app.routers.wb_discovery import router as wb_discovery_router
from app.routers.wb_browser_prices import router as wb_browser_prices_router
from app.routers.wb_19_05 import router as wb_19_05_router
from app.routers.wb_reports_sprint_d import router as wb_reports_sprint_d_router
from app.routers.wb_reports_bff import router as wb_reports_bff_router
from app.routers.wb_sales_funnel import router as wb_sales_funnel_router
from app.routers.wb_reports_v2 import router as wb_reports_v2_router
from app.routers.wb_reviews import router as wb_reviews_router
from app.routers.wb_repricer_bff import router as wb_repricer_bff_router
from app.routers.wb_repricer_sprint_b import router as wb_repricer_sprint_b_router
from app.routers.wb_repricer_sprint_c import router as wb_repricer_sprint_c_router
from app.routers.wb_repricer_wb23 import router as wb_repricer_wb23_router


def create_app() -> FastAPI:
    settings = get_settings()
    validate_security_settings(settings)
    app = FastAPI(
        title=settings.app_name,
        version=settings.contract_version,
        docs_url="/docs" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # The SKU list answers ~1.1 MB of JSON per page; uncompressed that is
    # seconds of transfer on a slow link even though the backend itself
    # responds in well under a second.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=16 * 1024,
        compresslevel=4,
        thread_minimum_size=128 * 1024,
    )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        code = f"HTTP_{exc.status_code}"
        message = str(exc.detail)
        details: dict[str, Any] = {}
        if isinstance(exc.detail, dict):
            raw_code = exc.detail.get("code")
            raw_message = exc.detail.get("message")
            raw_details = exc.detail.get("details")
            if isinstance(raw_code, str) and raw_code.strip():
                code = raw_code.strip()
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
            if isinstance(raw_details, dict):
                details = raw_details
        payload = ErrorEnvelope(
            error=ErrorEnvelopeItem(
                code=code,
                message=message,
                details=details,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {
                key: value
                for key, value in issue.items()
                if key not in {"input", "ctx"}
            }
            for issue in exc.errors()
        ]
        payload = ErrorEnvelope(
            error=ErrorEnvelopeItem(
                code="VALIDATION_ERROR",
                message="Request validation failed",
                details={"issues": issues},
            )
        )
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "environment": settings.environment,
            "contractVersion": settings.contract_version,
            "wbApiMode": settings.wb_api_mode,
            "wbLiveSyncEnabled": settings.wb_live_sync_enabled,
            "realPriceApplyEnabled": settings.real_price_apply_enabled,
            "repricerLocalPriceApplyEnabled": settings.repricer_local_price_apply_enabled,
            "repricerPreserveLocalPriceOverrides": settings.repricer_preserve_local_price_overrides,
            "legacyOneCEnabled": settings.one_c_enabled,
            "apiDocsEnabled": settings.api_docs_enabled,
            "infrastructure": {
                "databaseConfigured": bool(settings.database_url),
                "redisConfigured": bool(settings.redis_url),
                "celeryBrokerConfigured": bool(settings.celery_broker_url),
                "celeryResultBackendConfigured": bool(settings.celery_result_backend),
            },
        }

    @app.get("/health/live")
    def health_live() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        payload = readiness_status()
        return JSONResponse(
            status_code=200 if payload["status"] == "ready" else 503,
            content=payload,
        )

    app.include_router(auth_router)
    app.include_router(avito_chats_router)
    app.include_router(avito_listings_router)
    app.include_router(avito_notifications_router)
    app.include_router(avito_orders_router)
    app.include_router(avito_overview_router)
    app.include_router(avito_repricer_router)
    app.include_router(avito_reviews_router)
    app.include_router(avito_stats_router)
    if getattr(settings, "canonical_avito_stats_enabled", False) is True:
        from app.avito_stats_bootstrap import register_avito_stats_routes

        register_avito_stats_routes(app)
    if getattr(settings, "canonical_avito_order_status_enabled", False) is True:
        from app.avito_order_status_bootstrap import register_avito_order_status_routes

        register_avito_order_status_routes(app)
    if getattr(settings, "canonical_avito_reviews_preview_enabled", False) is True:
        from app.avito_reviews_preview_bootstrap import register_avito_reviews_preview_routes

        register_avito_reviews_preview_routes(app)
    app.include_router(cabinet_router)
    if settings.canonical_account_discovery_enabled:
        from app.account_discovery_bootstrap import register_account_discovery_routes

        register_account_discovery_routes(app)
    if settings.wb_sku_overrides_enabled:
        from app.sku_override_bootstrap import register_sku_override_routes

        register_sku_override_routes(app)
    if getattr(settings, "canonical_notifications_enabled", False) is True:
        from app.notification_bootstrap import register_notification_routes

        register_notification_routes(app)
    if settings.wb_live_sync_enabled:
        from app.wb_live.products_router import router as wb_live_products_router
        from app.wb_live.router import router as wb_live_router

        app.include_router(wb_live_router)
        app.include_router(wb_live_products_router)
    app.include_router(catalog_v2_router)
    app.include_router(canonical_orders_router)
    app.include_router(canonical_reviews_router)
    app.include_router(canonical_review_read_router)
    app.include_router(canonical_review_local_router)
    app.include_router(finance_v2_router)
    app.include_router(wb_reports_v2_router)
    app.include_router(notifications_router)
    if settings.one_c_enabled:
        app.include_router(one_c_cash_flow_router)
    app.include_router(wb_sales_funnel_router)
    app.include_router(wb_reports_sprint_d_router)
    app.include_router(wb_reports_bff_router)
    app.include_router(wb_reviews_router)
    app.include_router(wb_19_05_router)
    app.include_router(account_health_router)
    app.include_router(control_plane_router)
    app.include_router(source_registry_router)
    app.include_router(wb_discovery_router)
    app.include_router(wb_browser_prices_router)
    app.include_router(wb_repricer_bff_router)
    app.include_router(wb_repricer_wb23_router)
    app.include_router(wb_repricer_sprint_b_router)
    app.include_router(wb_repricer_sprint_c_router)
    return app


app = create_app()
