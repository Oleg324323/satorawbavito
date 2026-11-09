"""Default-off review metadata preview, never draft or send authority."""

from app.avito.account_reviews import (
    AccountAvitoReviewsService,
    AccountReviewsError,
    BoundedAvitoReviewsClient,
)
from app.avito.account_reviews_http import make_account_avito_reviews_router
from app.config import get_settings
from app.infra.db import get_engine
from app.platform.integrations.credential_store import _load_keyring


def enabled_for(organization_id, marketplace_account_id):
    settings = get_settings()
    return (
        getattr(settings, "canonical_avito_reviews_preview_enabled", False) is True
        and type(organization_id) is int
        and type(marketplace_account_id) is int
        and (organization_id, marketplace_account_id)
        in getattr(settings, "canonical_avito_reviews_preview_account_pairs", ())
    )


def avito_reviews_preview_service():
    if getattr(get_settings(), "canonical_avito_reviews_preview_enabled", False) is not True:
        raise AccountReviewsError("AVITO_REVIEWS_PREVIEW_DISABLED")
    return AccountAvitoReviewsService(
        engine=get_engine(),
        keyring_loader=_load_keyring,
        client_factory=BoundedAvitoReviewsClient,
        enabled_for=enabled_for,
    )


def register_avito_reviews_preview_routes(app):
    app.include_router(make_account_avito_reviews_router(
        service_dependency=avito_reviews_preview_service,
    ))
