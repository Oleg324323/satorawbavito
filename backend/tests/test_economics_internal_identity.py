from datetime import datetime, timezone

import pytest

from app.platform.economics.costs import CostsService, CostValidationError
from app.platform.economics.policies import EconomicsService, EconomicsValidationError


AT = datetime(2026, 9, 8, tzinfo=timezone.utc)
INVALID = (True, False, 0, -1, 1.0, "1", None)


class NoDatabase:
    def __getattr__(self, name):
        raise AssertionError("invalid internal identity must fail before database access")


@pytest.mark.parametrize("service,error", [(CostsService, CostValidationError), (EconomicsService, EconomicsValidationError)])
@pytest.mark.parametrize("identity", INVALID)
def test_organization_requires_positive_internal_integer_before_db(service, error, identity):
    with pytest.raises(error):
        service(NoDatabase(), organization_id=identity)


@pytest.mark.parametrize("identity", INVALID)
@pytest.mark.parametrize("operation", ["single", "bulk", "dates", "points", "write"])
def test_cost_sku_identity_is_validated_before_lookup_or_deduplication(identity, operation):
    service = CostsService(NoDatabase(), organization_id=1)
    with pytest.raises(CostValidationError):
        if operation == "single":
            service.get_cost_at(identity, AT)
        elif operation == "bulk":
            service.get_costs_at([1, identity], AT)
        elif operation == "dates":
            service.get_costs_on_dates([1, identity], [AT])
        elif operation == "points":
            service.get_costs_for_points([(1, AT), (identity, AT)])
        else:
            service.set_cost(catalog_sku_id=identity, amount_kopecks=100,
                             value_state="configured", effective_from=AT,
                             source="synthetic", source_reference="one", evidence_status="dated")


@pytest.mark.parametrize("identity", INVALID)
@pytest.mark.parametrize("operation", ["points", "write"])
def test_economics_sku_identity_is_validated_before_lookup_or_deduplication(identity, operation):
    service = EconomicsService(NoDatabase(), organization_id=1)
    with pytest.raises(EconomicsValidationError):
        if operation == "points":
            service.get_policies_for_points([(1, AT), (identity, AT)])
        else:
            service.set_sku_override(catalog_sku_id=identity, tax_basis_points=0,
                                     other_expense_price_basis_points=0,
                                     other_expense_per_sale_kopecks=0, value_state="configured",
                                     effective_from=AT, source="synthetic", source_reference="one",
                                     evidence_status="dated")
