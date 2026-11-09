"""Default-off override read composition; legacy-writer retirement is not assumed."""

from app.config import get_settings, is_wb_sku_overrides_enabled
from app.infra.db import get_engine
from app.modules.wb_repricing_override_service import SkuOverrideService
from app.platform.integrations.sku_override_context import SkuOverrideContextResolver
from app.routers.wb_repricing_overrides import create_wb_repricing_overrides_router


def enabled_for(organization_id, marketplace_account_id):
    return is_wb_sku_overrides_enabled(
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        settings=get_settings(),
    )


def writer_fenced_for(_organization_id, _marketplace_account_id):
    # No config switch can assert parity or retire an existing writer. Change
    # only in the separately verified single-writer cutover implementation.
    return False


def _service():
    return SkuOverrideService(get_engine(), max_request_bytes=16 * 1024)


def _context(actor, account_id):
    return SkuOverrideContextResolver(engine=get_engine())(actor, account_id)


def register_sku_override_routes(app):
    app.include_router(create_wb_repricing_overrides_router(
        service_factory=_service,
        context_resolver=_context,
        enabled_for=enabled_for,
        writer_fenced_for=writer_fenced_for,
    ))
