"""Metadata-only WB/Avito discovery independent of any collector activation."""

from app.config import get_settings
from app.infra.db import get_engine
from app.platform.integrations.account_discovery import (
    AccountDiscoveryError,
    MarketplaceAccountDiscoveryService,
)
from app.platform.integrations.account_discovery_http import (
    make_marketplace_account_discovery_router,
)


def account_discovery_service():
    if get_settings().canonical_account_discovery_enabled is not True:
        raise AccountDiscoveryError("ACCOUNT_DISCOVERY_DISABLED")
    return MarketplaceAccountDiscoveryService(engine=get_engine(), enabled=True)


def register_account_discovery_routes(app):
    app.include_router(make_marketplace_account_discovery_router(
        service_dependency=account_discovery_service,
    ))
