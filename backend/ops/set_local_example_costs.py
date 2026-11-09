"""Explicit user-authorized local example; never used at app startup or in production.

Run from backend with --org and --account. Idempotent, does not overwrite real
costs or change marketplace prices. All samples remain marked as assumptions.
"""
import argparse
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--org', type=int, required=True)
    parser.add_argument('--account', type=int, required=True)
    parser.add_argument('--rubles', type=int, required=True)
    parser.add_argument('--confirm-all-history-example', action='store_true', required=True)
    args = parser.parse_args()
    if args.rubles <= 0:
        raise SystemExit('Example cost must be positive')
    import current_cost_local_preview  # Local DB only; outbound sockets denied.
    from sqlalchemy import select
    from app.infra.db import get_session_factory
    from app.platform.catalog.orm import MarketplaceProductRow
    from app.platform.catalog.service import CatalogService
    from app.platform.integrations.orm import MarketplaceAccountRow
    from app.platform.economics.costs import CostsService
    from app.platform.economics.orm import CatalogCostVersionRow
    from app.platform.identity.orm import IamMembershipRow

    with get_session_factory()() as session:
        account = session.scalar(select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == args.org,
            MarketplaceAccountRow.marketplace_account_id == args.account,
            MarketplaceAccountRow.marketplace == 'wb', MarketplaceAccountRow.status == 'connected'))
        owners = session.scalars(select(IamMembershipRow).where(
            IamMembershipRow.organization_id == args.org, IamMembershipRow.role.in_(['owner', 'admin']),
            IamMembershipRow.is_active.is_(True))).all()
        if account is None or len(owners) != 1:
            raise SystemExit('Expected one scoped WB account and one active owner')
        ids = session.scalars(select(MarketplaceProductRow.external_product_id).where(
            MarketplaceProductRow.organization_id == args.org,
            MarketplaceProductRow.marketplace_account_id == args.account)).all()
        mapping, ambiguous = CatalogService(session, args.org).resolve_wb_product_skus(args.account, [int(i) for i in ids])
        if ambiguous or len(mapping) != len(ids):
            raise SystemExit('Incomplete/ambiguous product mapping; no examples applied')
        sku_ids = sorted(set(mapping.values()))
        existing = session.scalars(select(CatalogCostVersionRow).where(
            CatalogCostVersionRow.organization_id == args.org,
            CatalogCostVersionRow.catalog_sku_id.in_(sku_ids))).all()
        if any(row.source != 'user-example' or row.amount_kopecks != args.rubles * 100 for row in existing):
            raise SystemExit('Existing nonmatching costs preserved; no examples applied')
        service = CostsService(session, args.org)
        for sku in sku_ids:
            service.set_cost(catalog_sku_id=sku, amount_kopecks=args.rubles * 100, value_state='assumed',
                effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc), source='user-example',
                source_reference=f'user-example:2026-10-06:{args.account}:{sku}:{args.rubles}',
                evidence_status='dated', created_by_membership_id=owners[0].membership_id)
        saved = service.get_costs_at(sku_ids, datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert all(row.amount_kopecks == args.rubles * 100 and row.value_state == 'assumed' for row in saved.values())
        print({'products': len(ids), 'skus': len(sku_ids), 'rubles': args.rubles,
               'all_history': True, 'state': 'assumed', 'verified': len(saved)})


if __name__ == '__main__':
    main()
