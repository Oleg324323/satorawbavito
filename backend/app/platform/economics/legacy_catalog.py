"""Scoped bulk read of shared catalog facts for legacy WB views."""
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select
from app.infra.db import get_session_factory, set_tenant_context
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.catalog.orm import MarketplaceProductRow
from app.platform.catalog.service import CatalogService
from app.platform.economics.costs import CostsService


def catalog_facts(organization_id: int, *, include_costs: bool = True) -> dict[int, dict]:
    # Legacy views have no account selector. Refuse to mix multiple accounts.
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        accounts = session.scalars(select(MarketplaceAccountRow.marketplace_account_id).where(
            MarketplaceAccountRow.organization_id == organization_id,
            MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all()
        if len(accounts) != 1:
            return {}
        products = session.scalars(select(MarketplaceProductRow).where(
            MarketplaceProductRow.organization_id == organization_id,
            MarketplaceProductRow.marketplace_account_id == accounts[0])).all()
        ids = [int(p.external_product_id) for p in products if p.external_product_id.isdigit()]
        unique = Counter(ids)
        mapping, ambiguous = CatalogService(session, organization_id).resolve_wb_product_skus(accounts[0], ids) if include_costs else ({}, set())
        costs = CostsService(session, organization_id).get_costs_at(list(set(mapping.values())), datetime.now(timezone.utc)) if include_costs else {}
        return {int(p.external_product_id): {
            "photoUrl": p.image_url, "productName": p.title, "sku": p.seller_article,
            "currentCostKopecks": costs[mapping[int(p.external_product_id)]].amount_kopecks
                if int(p.external_product_id) in mapping and int(p.external_product_id) not in ambiguous else None,
        } for p in products if p.external_product_id.isdigit() and unique[int(p.external_product_id)] == 1}
