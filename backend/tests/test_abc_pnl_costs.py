from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.catalog.orm import (
    CatalogSkuRow,
    MarketplaceOfferRow,
    MarketplaceProductRow,
)
from app.platform.catalog.service import CatalogService
from app.platform.economics.costs import CostsService
from app.platform.integrations.orm import MarketplaceAccountRow

AT_20 = datetime(2026, 8, 20, 20, 59, 59, tzinfo=timezone.utc)
AT_21 = datetime(2026, 8, 21, 20, 59, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("nm,sku,ambiguous", [(None, None, False), (123, None, False), (123, 11, True)])
def test_service_only_daily_zero_never_requires_product_cost(nm, sku, ambiguous):
    from types import SimpleNamespace
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    assert WbAbcPnlService._cost_result(SimpleNamespace(nm_id=nm, net_units=0, sales_units=0, returns_units=0),
        catalog_sku_id=sku, ambiguous=ambiguous, daily=[(AT_20, 0)], costs={}) == (0, "configured", None, ())


def test_sale_and_later_return_still_require_dated_cost_when_net_units_zero():
    from types import SimpleNamespace
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    result = WbAbcPnlService._cost_result(SimpleNamespace(nm_id=123, net_units=0, sales_units=1, returns_units=1),
        catalog_sku_id=None, ambiguous=False, daily=[(AT_20, 1), (AT_21, -1)], costs={})
    assert result[0] is None and result[3] == ("WB_PNL_COST_MAPPING_MISSING",)


def test_missing_daily_basis_with_gross_trades_is_not_service_only_zero():
    from types import SimpleNamespace
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    result = WbAbcPnlService._cost_result(SimpleNamespace(nm_id=123, net_units=0, sales_units=1, returns_units=1),
        catalog_sku_id=None, ambiguous=False, daily=[], costs={})
    assert result[0] is None and result[3] == ("WB_PNL_COST_DAILY_COVERAGE_MISMATCH",)


def test_same_undated_legacy_import_preserves_confirmed_cost_in_all_readers(session):
    service = CostsService(session, 1)
    confirmed = service.set_cost(
        catalog_sku_id=11, amount_kopecks=10000, value_state="configured",
        effective_from=AT_20, source="manual", source_reference="confirmed",
        evidence_status="dated",
    )
    service.set_cost(
        catalog_sku_id=11, amount_kopecks=10000, value_state="assumed",
        effective_from=AT_21, source="legacy_sku_override", source_reference="reimport",
        evidence_status="undated",
    )
    assert service.get_cost_at(11, AT_21) == confirmed
    assert service.get_costs_at([11], AT_21)[11] == confirmed
    assert service.get_costs_on_dates([11], [AT_20, AT_21]) == {
        (11, AT_20): confirmed, (11, AT_21): confirmed,
    }
    assert len(service.list_cost_history(11)) == 2
    for day, amount, state in [(1, 20000, "assumed"), (2, 10000, "assumed"), (3, None, "missing")]:
        instant = datetime(2026, 9, day, tzinfo=timezone.utc)
        changed = service.set_cost(
            catalog_sku_id=11, amount_kopecks=amount, value_state=state,
            effective_from=instant, source="legacy_sku_override", source_reference=f"change-{day}",
            evidence_status="undated",
        )
        assert service.get_cost_at(11, instant) == changed


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                MarketplaceAccountRow(
                    marketplace_account_id=31,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=33,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one-extra",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=32,
                    organization_id=2,
                    marketplace="wb",
                    external_account_id="wb-two",
                    status="connected",
                ),
                CatalogSkuRow(catalog_sku_id=11, organization_id=1, code="SKU-11"),
                CatalogSkuRow(catalog_sku_id=12, organization_id=1, code="SKU-12"),
                CatalogSkuRow(catalog_sku_id=21, organization_id=2, code="SKU-21"),
                MarketplaceProductRow(
                    marketplace_product_id=1011,
                    organization_id=1,
                    marketplace_account_id=31,
                    external_product_id="101",
                ),
                MarketplaceProductRow(
                    marketplace_product_id=1021,
                    organization_id=1,
                    marketplace_account_id=31,
                    external_product_id="102",
                ),
                MarketplaceProductRow(
                    marketplace_product_id=1031,
                    organization_id=1,
                    marketplace_account_id=31,
                    external_product_id="103",
                ),
                MarketplaceProductRow(
                    marketplace_product_id=1013,
                    organization_id=1,
                    marketplace_account_id=33,
                    external_product_id="101",
                ),
                MarketplaceProductRow(
                    marketplace_product_id=1012,
                    organization_id=2,
                    marketplace_account_id=32,
                    external_product_id="101",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                MarketplaceOfferRow(
                    marketplace_offer_id=2011,
                    organization_id=1,
                    marketplace_account_id=31,
                    marketplace_product_id=1011,
                    external_offer_key="SKU-11",
                    catalog_sku_id=11,
                ),
                MarketplaceOfferRow(
                    marketplace_offer_id=2021,
                    organization_id=1,
                    marketplace_account_id=31,
                    marketplace_product_id=1021,
                    external_offer_key="SKU-11",
                    catalog_sku_id=11,
                ),
                MarketplaceOfferRow(
                    marketplace_offer_id=2022,
                    organization_id=1,
                    marketplace_account_id=31,
                    marketplace_product_id=1021,
                    external_offer_key="SKU-12",
                    catalog_sku_id=12,
                ),
                MarketplaceOfferRow(
                    marketplace_offer_id=2031,
                    organization_id=1,
                    marketplace_account_id=31,
                    marketplace_product_id=1031,
                    external_offer_key="UNMAPPED",
                    catalog_sku_id=None,
                ),
                MarketplaceOfferRow(
                    marketplace_offer_id=2013,
                    organization_id=1,
                    marketplace_account_id=33,
                    marketplace_product_id=1013,
                    external_offer_key="SKU-12",
                    catalog_sku_id=12,
                ),
                MarketplaceOfferRow(
                    marketplace_offer_id=2012,
                    organization_id=2,
                    marketplace_account_id=32,
                    marketplace_product_id=1012,
                    external_offer_key="SKU-21",
                    catalog_sku_id=21,
                ),
            ]
        )
        db.commit()
        yield db


def test_wb_product_mapping_is_strictly_tenant_and_account_scoped(
    session: Session,
) -> None:
    service = CatalogService(session, organization_id=1)

    mapped, ambiguous = service.resolve_wb_product_skus(31, [101, 102, 103, 999])

    assert mapped == {101: 11}
    assert ambiguous == {102}
    assert service.resolve_wb_product_skus(33, [101]) == ({101: 12}, set())
    assert CatalogService(session, organization_id=2).resolve_wb_product_skus(
        32, [101]
    ) == ({101: 21}, set())


def test_costs_resolve_all_requested_dates_without_losing_state(
    session: Session,
) -> None:
    service = CostsService(session, organization_id=1)
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=20_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="sku-11-v1",
        evidence_status="dated",
    )
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=25_000,
        value_state="assumed",
        effective_from=datetime(2026, 8, 20, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="sku-11-v2",
        evidence_status="undated",
    )
    service.set_cost(
        catalog_sku_id=12,
        amount_kopecks=0,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="sku-12-v1",
        evidence_status="dated",
    )

    costs = service.get_costs_on_dates([11, 12, 999], [AT_20, AT_21])

    assert costs[(11, AT_20)].amount_kopecks == 20_000
    assert costs[(11, AT_20)].value_state == "configured"
    assert costs[(11, AT_21)].amount_kopecks == 25_000
    assert costs[(11, AT_21)].value_state == "assumed"
    assert costs[(11, AT_21)].evidence_status == "undated"
    assert costs[(12, AT_20)].amount_kopecks == 0
    assert costs[(12, AT_20)].value_state == "configured"
    assert costs[(999, AT_20)].amount_kopecks is None
    assert costs[(999, AT_20)].value_state == "missing"
    assert service.revision() == 3


def test_costs_resolve_only_requested_sku_date_points(session: Session) -> None:
    service = CostsService(session, organization_id=1)
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=20_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="point-sku-11",
        evidence_status="dated",
    )
    service.set_cost(
        catalog_sku_id=12,
        amount_kopecks=25_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 20, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="point-sku-12",
        evidence_status="dated",
    )
    points = [(11, AT_20), (12, AT_21), (999, AT_20)]

    costs = service.get_costs_for_points(points)

    assert set(costs) == set(points)
    assert costs[(11, AT_20)].amount_kopecks == 20_000
    assert costs[(12, AT_21)].amount_kopecks == 25_000
    assert costs[(999, AT_20)].value_state == "missing"


def test_unchanged_cost_version_reuses_immutable_projection(session: Session) -> None:
    service = CostsService(session, organization_id=1)
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=20_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="stable-cost",
        evidence_status="dated",
    )

    costs = service.get_costs_for_points([(11, AT_20), (11, AT_21)])

    assert costs[(11, AT_20)] is costs[(11, AT_21)]


def test_bulk_cost_resolution_normalizes_each_instant_once(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.platform.economics import costs as costs_module

    calls: list[datetime] = []
    original = costs_module._aware_utc

    def observed(value: datetime) -> datetime:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(costs_module, "_aware_utc", observed)

    CostsService(session, organization_id=1).get_costs_for_points(
        [(11, AT_20), (12, AT_20), (11, AT_21)]
    )

    assert calls == [AT_20, AT_21]


def test_bulk_cost_resolution_does_not_track_ledger_rows(session: Session) -> None:
    from app.platform.economics.orm import CatalogCostVersionRow

    service = CostsService(session, organization_id=1)
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=20_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="untracked-cost",
        evidence_status="dated",
    )
    session.expunge_all()
    loaded: list[CatalogCostVersionRow] = []

    def track(_session: Session, instance: object) -> None:
        if isinstance(instance, CatalogCostVersionRow):
            loaded.append(instance)

    event.listen(session, "loaded_as_persistent", track)
    try:
        service.get_costs_for_points([(11, AT_20), (11, AT_21)])
    finally:
        event.remove(session, "loaded_as_persistent", track)

    assert loaded == []
