"""Default-off account status preview; no queue publication or legacy cutover."""

from app.avito.account_orders import (
    AccountAvitoOrderStatusService,
    AccountOrderStatusError,
    BoundedAvitoOrderStatusClient,
)
from app.avito.account_orders_http import make_account_avito_order_status_router
from app.config import get_settings
from app.infra.db import get_engine
from app.platform.integrations.credential_store import _load_keyring


def enabled_for(organization_id, marketplace_account_id):
    settings = get_settings()
    return (
        getattr(settings, "canonical_avito_order_status_enabled", False) is True
        and type(organization_id) is int
        and type(marketplace_account_id) is int
        and (organization_id, marketplace_account_id)
        in getattr(settings, "canonical_avito_order_status_account_pairs", ())
    )


def avito_order_status_service():
    if getattr(get_settings(), "canonical_avito_order_status_enabled", False) is not True:
        raise AccountOrderStatusError("AVITO_ORDER_STATUS_DISABLED")
    return AccountAvitoOrderStatusService(
        engine=get_engine(),
        keyring_loader=_load_keyring,
        client_factory=BoundedAvitoOrderStatusClient,
        enabled_for=enabled_for,
    )


def register_avito_order_status_routes(app):
    app.include_router(make_account_avito_order_status_router(
        service_dependency=avito_order_status_service,
    ))
