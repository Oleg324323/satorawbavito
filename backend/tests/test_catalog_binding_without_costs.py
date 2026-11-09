from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.infra.models import Base
from app.cabinet.orm import LkOrganizationRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.economics.backfill import LegacyCostSnapshot, apply_cost_backfill
from app.platform.economics.orm import CatalogCostVersionRow


def test_catalog_binding_does_not_import_assumed_type_costs(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'binding.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(LkOrganizationRow(organization_id=1, slug="test", name="test"))
        session.add(MarketplaceAccountRow(marketplace_account_id=1, organization_id=1,
            marketplace="wb", external_account_id="test-seller", status="connected"))
        session.commit()
        snapshot = LegacyCostSnapshot(captured_at=datetime.now(timezone.utc),
            goods=[{"nmID": 123, "vendorCode": "Фут_123", "title": "Футболка"}],
            content_cards=[], runtime_state={}, algorithm_settings={}, costs_excel={})
        for _ in range(2):
            result = apply_cost_backfill(session, organization_id=1,
                marketplace_account_id=1, snapshot=snapshot, include_costs=False)
            assert result.applied_count == 1
            assert result.cost_command_count == 0
        assert session.scalar(select(func.count()).select_from(CatalogSkuRow)) == 1
        assert session.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
