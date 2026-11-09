from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infra.models import Base
from app.cabinet.orm import LkOrganizationRow, LkUserRow
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.economics.costs import CostsService, CostConflictError, CostNotFoundError
from app.platform.economics.schemas import CurrentCostSetRequest


def test_edit_retry_conflict_history_and_tenant_isolation(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cost.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([LkOrganizationRow(organization_id=1, slug="one", name="One"),
            CatalogSkuRow(catalog_sku_id=1, organization_id=1, code="synthetic"),
            LkUserRow(user_id="test", organization_id=1, email="fixture@example.invalid", password_hash="unusable", full_name="Test", permission_profile="owner"),
            IamMembershipRow(membership_id=1, organization_id=1, user_id="test", role="owner")])
        db.commit()
        service = CostsService(db, 1, now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc))
        command = dict(catalog_sku_id=1, amount_kopecks=35050, expected_cost_version_id=None,
            source_reference="edit-one", created_by_user_id="test")
        first = service.set_current_cost(**command)
        assert service.set_current_cost(**command) == first
        with pytest.raises(CostConflictError):
            service.set_current_cost(**(command | {"source_reference": "stale-editor"}))
        db.rollback()
        second = service.set_current_cost(**(command | {"source_reference": "edit-two", "expected_cost_version_id": first.cost_version_id, "amount_kopecks": 0}))
        assert second.amount_kopecks == 0
        assert second.created_by_membership_id == 1
        assert len(service.list_cost_history(1)) == 2
    with Session(engine) as db:
        assert CostsService(db, 1, now=lambda: datetime(2026, 9, 18, tzinfo=timezone.utc)).get_current_cost(1).amount_kopecks == 0
        with pytest.raises(CostNotFoundError):
            CostsService(db, 2).get_current_cost(1)
    barrier = Barrier(2)
    def edit(index):
        with Session(engine) as db:
            barrier.wait()
            try:
                CostsService(db, 1, now=lambda: datetime(2026, 9, 18, tzinfo=timezone.utc)).set_current_cost(
                    **(command | {"source_reference": f"parallel-{index}", "expected_cost_version_id": second.cost_version_id, "amount_kopecks": index}))
                return "saved"
            except CostConflictError:
                db.rollback()
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(edit, (1, 2))) == ["conflict", "saved"]


@pytest.mark.parametrize("amount", [-1, True, 1.5, "350", 9_007_199_254_740_992])
def test_reject_invalid_money(amount):
    with pytest.raises(ValueError):
        CurrentCostSetRequest(amountKopecks=amount, expectedCostVersionId=None, sourceReference="x")
