"""Default-off account-scoped Avito read; not a legacy or background-cache cutover."""

from app.avito.account_stats import (
    AccountAvitoStatsService,
    AccountStatsError,
    BoundedAvitoTotalsClient,
)
from app.avito.account_stats_http import make_account_avito_stats_router
from app.config import get_settings
from app.infra.db import get_engine
from app.platform.integrations.credential_store import _load_keyring


def enabled_for(organization_id, marketplace_account_id):
    settings = get_settings()
    return (
        getattr(settings, "canonical_avito_stats_enabled", False) is True
        and type(organization_id) is int
        and type(marketplace_account_id) is int
        and (organization_id, marketplace_account_id)
        in getattr(settings, "canonical_avito_stats_account_pairs", ())
    )


def avito_stats_service():
    if getattr(get_settings(), "canonical_avito_stats_enabled", False) is not True:
        raise AccountStatsError("AVITO_ACCOUNT_STATS_DISABLED")
    return AccountAvitoStatsService(
        engine=get_engine(),
        keyring_loader=_load_keyring,
        client_factory=BoundedAvitoTotalsClient,
        enabled_for=enabled_for,
    )


def register_avito_stats_routes(app):
    app.include_router(make_account_avito_stats_router(
        service_dependency=avito_stats_service,
    ))
