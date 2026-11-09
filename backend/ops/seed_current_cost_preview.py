"""Synthetic fixtures for the isolated preview only. Never connects to WB."""
import current_cost_local_preview  # fixes the local DB path and blocks network
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.infra.db import get_session_factory
from app.cabinet.orm import LkUserRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.economics.costs import CostsService
from app.platform.economics.policies import EconomicsService
from app.repricer_cache.store import save_goods_page, save_source_cache
from app.routers.wb_repricer_bff import _repricer_period_context

def seed():
    now = datetime.now(timezone.utc)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with get_session_factory()() as db:
        user = db.scalar(select(LkUserRow).where(LkUserRow.email == "local-e48ac9@satorna.test"))
        if user is None:
            raise RuntimeError("Dedicated local preview account not found")
        org = user.organization_id
        membership = db.scalar(select(IamMembershipRow).where(IamMembershipRow.organization_id == org, IamMembershipRow.user_id == user.user_id))
        if membership is None:
            db.add(IamMembershipRow(organization_id=org, user_id=user.user_id, role="owner", scope_mode="all"))
        account = db.scalar(select(MarketplaceAccountRow).where(MarketplaceAccountRow.organization_id == org, MarketplaceAccountRow.external_account_id == "SYNTHETIC-PREVIEW"))
        if account is None:
            account = MarketplaceAccountRow(organization_id=org, marketplace="wb", external_account_id="SYNTHETIC-PREVIEW", status="connected")
            db.add(account); db.flush()
        goods, cards, stocks = [], [], {}
        for i, name in enumerate(["Футболка", "Худи", "Лонгслив", "Свитшот", "Шоппер", "Кепка", "Кружка", "Постер"], 1):
            nm, article = 990000000 + i, f"DEMO-{i:03d}"
            sku = db.scalar(select(CatalogSkuRow).where(CatalogSkuRow.organization_id == org, CatalogSkuRow.code == article))
            if sku is None:
                sku = CatalogSkuRow(organization_id=org, code=article); db.add(sku); db.flush()
                product = MarketplaceProductRow(organization_id=org, marketplace_account_id=account.marketplace_account_id, external_product_id=str(nm), seller_article=article, title=f"ДЕМО · {name}", brand="ДЕМО — не данные WB")
                db.add(product); db.flush()
                db.add(MarketplaceOfferRow(organization_id=org, marketplace_account_id=account.marketplace_account_id, marketplace_product_id=product.marketplace_product_id, external_offer_key=article, catalog_sku_id=sku.catalog_sku_id))
            db.commit()
            if CostsService(db, org).get_current_cost(sku.catalog_sku_id).cost_version_id is None:
                CostsService(db, org).set_cost(catalog_sku_id=sku.catalog_sku_id, amount_kopecks=20000 + i * 3000, value_state="configured", effective_from=since, source="synthetic-preview", source_reference=article, evidence_status="dated")
            goods.append({"nmID": nm, "vendorCode": article, "brand": "ДЕМО — не данные WB", "discount": 0, "sizes": [{"sizeID": nm, "techSizeName": "M", "price": 1000+i*100, "discountedPrice": 1000+i*100}]})
            cards.append({"nmID": nm, "vendorCode": article, "title": f"ДЕМО · {name}", "brand": "ДЕМО — не данные WB", "sizes": [{"chrtID":nm,"techSize":"M"}]})
            stocks[str(nm)] = {"stockUnits": i*5, "quantity":i*5}
        EconomicsService(db, org).set_organization_policy(tax_basis_points=750, other_expense_price_basis_points=0, other_expense_per_sale_kopecks=0, value_state="configured", effective_from=since, source="synthetic-preview", source_reference="tax-v1", evidence_status="dated", tax_value_state="configured", tax_evidence_status="dated")
    save_goods_page(organization_id=org, page_offset=0, page_limit=100, goods=goods, wb_request_id="synthetic-preview")
    save_source_cache(org,"content_cards",{"cards":cards})
    save_source_cache(org,"stocks",{"aggregates":stocks})
    yesterday = now.astimezone(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    contexts = [_repricer_period_context(days) for days in (1,7,14,30,90)]
    contexts += [_repricer_period_context(days, yesterday-timedelta(days=days-1), yesterday) for days in (1,7,14,30,90)]
    for start,end,days,suffix in contexts:
        facts, period = {}, {}
        for i in range(1,9):
            units = (9-i)*days
            revenue = units*(100000+i*10000)
            facts[str(990000000+i)] = dict(salesUnits=units,returnsUnits=0,sellerRevenueKopecks=revenue,buyerRevenueKopecks=revenue,payableKopecks=revenue*80//100,commissionKopecks=revenue*20//100,acquiringKopecks=0,logisticsKopecks=units*5000,storageKopecks=units*100,acceptanceKopecks=0,penaltyKopecks=0,deductionKopecks=0,loyaltyCostKopecks=0,adSpendKopecks=units*2000,additionalPaymentKopecks=0,commissionSource="buyerRevenueKopecks-payableKopecks-acquiringKopecks")
            period[str(990000000+i)] = dict(ordersUnits=units+days,salesUnits=units,returnsUnits=0,baskets=units*2)
        meta = {"dateFrom":start.date().isoformat(),"dateTo":end.date().isoformat(),"periodDays":days,"synthetic":True}
        totals = {key: sum(f[key] for f in facts.values()) for key in facts[next(iter(facts))] if key.endswith("Kopecks")}
        save_source_cache(org,f"finance_{suffix}",meta | {"aggregates":facts,"revenueBasis":"retailAmount","financeSchemaVersion":"v4","diagnostics":{"rawExpenseTotals":totals}})
        for key in ("period_stats","baskets"):
            save_source_cache(org,f"{key}_{suffix}",meta | {"aggregates":period})
        save_source_cache(org,f"ads_{suffix}",meta | {"aggregates":{}})
    save_source_cache(org,"wb_sync_status",{"state":"completed","running":False,"dateFrom":min(c[0] for c in contexts).date().isoformat(),"dateTo":max(c[1] for c in contexts).date().isoformat(),"updatedAt":now.isoformat(),"steps":[],"synthetic":True})
    print("Seeded 8 explicitly synthetic products in the dedicated local account")

if __name__ == "__main__":
    seed()
