from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.advertising.orm import WbAdvertisingFactRow
from app.platform.advertising.service import AdvertisingService
from app.platform.catalog.orm import (
    CatalogSkuRow,
    MarketplaceOfferRow,
    MarketplaceProductRow,
)
from app.platform.economics.costs import CostsService
from app.platform.economics.policies import EconomicsService
from app.platform.finance.service import FinanceService
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
PERIOD = Period(date(2026, 8, 20), date(2026, 8, 21))
GLOBAL_BLOCKERS = {
    "WB_PNL_LOYALTY_NOT_CANONICAL",
}


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                MarketplaceAccountRow(
                    marketplace_account_id=31,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one",
                    status="connected",
                ),
                CatalogSkuRow(
                    catalog_sku_id=11,
                    organization_id=1,
                    code="FBBT_101",
                ),
                MarketplaceProductRow(
                    marketplace_product_id=1011,
                    organization_id=1,
                    marketplace_account_id=31,
                    external_product_id="101",
                    seller_article="FBBT_101",
                ),
            ]
        )
        db.flush()
        db.add(
            MarketplaceOfferRow(
                marketplace_offer_id=2011,
                organization_id=1,
                marketplace_account_id=31,
                marketplace_product_id=1011,
                external_offer_key="FBBT_101",
                catalog_sku_id=11,
            )
        )
        db.commit()
        yield db


def settlement_rows() -> list[dict[str, object]]:
    return [
        {
            "rrdId": 1,
            "reportId": 1001,
            "reportType": 1,
            "nmId": 101,
            "vendorCode": "FBBT_101",
            "docTypeName": "Продажа",
            "sellerOperName": "Продажа",
            "quantity": 2,
            "retailAmount": "1000.00",
            "forPay": "885.00",
            "ppvzSalesCommission": "100.00",
            "deliveryService": "50.00",
            "paidStorage": "20.00",
            "paidAcceptance": "10.00",
            "penalty": "30.00",
            "deduction": "40.00",
            "bonusTypeName": "Другие удержания",
            "paymentSchedule": "25.00",
            "additionalPayment": "100.00",
            "acquiringFee": "15.00",
            "saleDt": "2026-08-20",
            "rrDate": "2026-08-21",
        },
        {
            "rrdId": 2,
            "reportId": 1001,
            "reportType": 1,
            "nmId": 101,
            "vendorCode": "FBBT_101",
            "docTypeName": "Возврат",
            "sellerOperName": "Возврат",
            "quantity": 1,
            "retailAmount": "200.00",
            "forPay": "177.00",
            "ppvzSalesCommission": "20.00",
            "acquiringFee": "3.00",
            "saleDt": "2026-08-21",
            "rrDate": "2026-08-21",
        },
    ]


def settlement_rows_with_loyalty() -> list[dict[str, object]]:
    rows = settlement_rows()
    rows[0].update(
        cashbackAmount="10.00",
        cashbackDiscount="3.50",
        cashbackCommissionChange="-1.25",
    )
    rows[1].update(
        cashbackAmount="-2.00",
        cashbackDiscount="0.50",
        cashbackCommissionChange="0.25",
    )
    return rows


def advertising_payload(
    *, deduction: str = "123.00", nm_id: int | None = 101
) -> dict[str, object]:
    row: dict[str, object] = {
        "rrdId": 99,
        "bonusTypeName": "WB Продвижение",
        "deduction": deduction,
        "rrDate": "2026-08-21",
    }
    if nm_id is not None:
        row["nmId"] = nm_id
    return {
        "rows": [row],
        "dateFrom": PERIOD.date_from.isoformat(),
        "dateTo": PERIOD.date_to.isoformat(),
    }


def raw_advertising_bundle(
    *, nm_id: int = 101, spend_rubles: float = 123.0, residual_rubles: float = 0
) -> dict[str, object]:
    bundle = json.loads(
        (
            Path(__file__).parent / "fixtures/wb_advertising_raw_sanitized.json"
        ).read_text()
    )
    date_from = PERIOD.date_from.isoformat()
    date_to = PERIOD.date_to.isoformat()
    business_day = date_from
    bundle["period"] = {"dateFrom": date_from, "dateTo": date_to}
    response = bundle["fullstats"][0]
    response.update(dateFrom=date_from, dateTo=date_to)
    campaign = response["payload"][0]
    campaign["sum"] = spend_rubles
    campaign["days"] = campaign["days"][:1]
    day = campaign["days"][0]
    day.update(date=f"{business_day}T00:00:00Z", sum=spend_rubles)
    day["apps"] = day["apps"][:1]
    app = day["apps"][0]
    app["sum"] = spend_rubles
    app["nms"] = app["nms"][:1]
    app["nms"][0].update(nmId=nm_id, sum=spend_rubles)
    if residual_rubles:
        residual = response["payload"][1]
        residual["sum"] = residual_rubles
        residual["days"] = residual["days"][:1]
        residual["days"][0].update(
            date=f"{business_day}T00:00:00Z",
            sum=residual_rubles,
            apps=[],
        )
        response["payload"] = response["payload"][:2]
        campaign_ids = "1001,1002"
    else:
        response["payload"] = response["payload"][:1]
        campaign_ids = "1001"
    response["campaignIds"] = [int(value) for value in campaign_ids.split(",")]
    for page in bundle["upd"]:
        page.update(dateFrom=date_from, dateTo=date_to)
        for index, row in enumerate(page["payload"]):
            row["updTime"] = f"{business_day}T12:{index:02d}:00+03:00"
    for item in bundle["manifest"]:
        request = item.get("request", {})
        for key in ("from", "beginDate"):
            if key in request:
                request[key] = date_from
        for key in ("to", "endDate"):
            if key in request:
                request[key] = date_to
        if "ids" in request:
            request["ids"] = campaign_ids
    return bundle


def add_dated_costs(session: Session, *, second_state: str = "configured") -> None:
    service = CostsService(session, organization_id=1)
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=20_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="cost-20",
        evidence_status="dated",
    )
    service.set_cost(
        catalog_sku_id=11,
        amount_kopecks=25_000,
        value_state=second_state,
        effective_from=datetime(2026, 8, 20, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="cost-21",
        evidence_status="dated" if second_state == "configured" else "undated",
    )


def add_dated_economics(
    session: Session,
    *,
    value_state: str = "configured",
    evidence_status: str = "dated",
) -> None:
    EconomicsService(session, organization_id=1).set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1_000,
        value_state=value_state,
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="economics-v1",
        evidence_status=evidence_status,
    )


def test_abc_pnl_base_uses_dated_cogs_and_signed_settlement_formula(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.state == "partial"
    assert page.formula_version == "wb-abc-pnl-finance-rrdate-v4"
    assert page.cost_ledger_revision == 2
    assert page.economics_revision == 1
    assert set(page.blocker_ids) == GLOBAL_BLOCKERS
    assert page.total == len(page.items) == 1
    row = page.items[0]
    assert row.nm_id == 101
    assert row.catalog_sku_id == 11
    assert row.revenue_kopecks == 80_000
    assert row.cogs_kopecks == 15_000
    assert row.cost_value_state == "configured"
    assert row.finance_other_expenses_kopecks == 12_500
    assert row.compensation_kopecks == 0
    assert row.finance_expenses_kopecks == 36_700
    assert row.settlement_profit_kopecks == 28_300
    assert row.economics_value_state == "configured"
    assert row.economics_evidence_status == "dated"
    assert row.tax_kopecks == 4_800
    assert row.other_expenses_kopecks == 0
    assert row.profit_before_ads_and_loyalty_kopecks == 23_500
    assert row.advertising_spend_kopecks == 0
    assert row.profit_before_loyalty_kopecks == 23_500
    assert row.net_profit_kopecks is None
    assert row.profit_class is None
    assert row.abc_code is None
    assert page.summary.revenue_kopecks == 80_000
    assert page.summary.cogs_kopecks == 15_000
    assert page.summary.finance_expenses_kopecks == 36_700
    assert page.summary.settlement_profit_kopecks == 28_300
    assert page.summary.tax_kopecks == 4_800
    assert page.summary.other_expenses_kopecks == 0
    assert page.summary.profit_before_ads_and_loyalty_kopecks == 23_500
    assert page.summary.advertising_spend_kopecks == 0
    assert page.summary.unattributed_advertising_spend_kopecks == 0
    assert page.summary.profit_before_loyalty_kopecks == 23_500
    assert page.summary.net_profit_kopecks is None


def test_finance_promotion_cannot_fill_operational_advertising(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_payload(
        31,
        PERIOD,
        "finance_promotion",
        advertising_payload(),
        source_reference="finance_fixture",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = page.items[0]
    assert row.advertising_spend_kopecks == 0
    assert row.profit_before_loyalty_kopecks == 23_500
    assert page.summary.advertising_spend_kopecks == 0
    assert page.summary.unattributed_advertising_spend_kopecks == 0
    assert page.summary.profit_before_loyalty_kopecks == 23_500
    assert page.advertising_snapshot is None
    assert set(page.blocker_ids) == GLOBAL_BLOCKERS


def test_v3_loyalty_evidence_builds_profit_after_loyalty(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows_with_loyalty(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = page.items[0]
    assert page.formula_version == "wb-abc-pnl-finance-rrdate-v4"
    assert row.cashback_amount_kopecks == 800
    assert row.cashback_discount_kopecks == 400
    assert row.cashback_commission_change_kopecks == -100
    assert row.loyalty_net_cost_kopecks == 700
    assert row.profit_after_loyalty_kopecks == 22_800
    assert page.summary.cashback_amount_kopecks == 800
    assert page.summary.cashback_discount_kopecks == 400
    assert page.summary.cashback_commission_change_kopecks == -100
    assert page.summary.loyalty_net_cost_kopecks == 700
    assert page.summary.profit_after_loyalty_kopecks == 22_800
    assert page.summary.net_profit_kopecks == 22_800
    assert "WB_PNL_LOYALTY_NOT_CANONICAL" not in page.blocker_ids
    assert page.blocker_ids == ("WB_PNL_CLASSIFICATION_NOT_CANONICAL",)
    assert page.advertising_snapshot is None  # Financial evidence is the Finance snapshot.
    assert row.net_profit_kopecks == 22_800
    assert row.profit_class is None
    assert row.abc_code is None


def test_v1_finance_snapshot_keeps_loyalty_unknown(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService
    from app.platform.finance.orm import WbFinanceSyncRunRow

    add_dated_costs(session)
    add_dated_economics(session)
    snapshot = FinanceService(
        session, organization_id=1, now=lambda: NOW
    ).ingest_snapshot(31, PERIOD, settlement_rows_with_loyalty(), observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None
    run.formula_version = "wb-finance-v1"
    session.commit()

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )
    assert page.state == "missing" and page.items == []
    assert page.summary.revenue_kopecks == 0
    assert page.summary.net_profit_kopecks is None
    assert "WB_PNL_LOYALTY_NOT_CANONICAL" in page.blocker_ids


def test_incomplete_payable_blocks_only_affected_sku_and_summary(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    rows = settlement_rows_with_loyalty()
    rows[0]["forPay"] = "885"
    rows[1]["forPay"] = "177"
    common = {
        "nmId": 202,
        "cashbackAmount": "0",
        "cashbackCommissionChange": "0",
        "rrDate": "2026-08-21",
    }
    rows.extend(
        [
            common | {"rrdId": 3, "forPay": "10", "cashbackDiscount": "10"},
            common | {"rrdId": 4, "cashbackDiscount": "0"},
        ]
    )
    FinanceService(session, 1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, rows, observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, 1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )
    complete, incomplete = (
        next(row for row in page.items if row.nm_id == nm) for nm in (101, 202)
    )
    assert complete.profit_after_loyalty_kopecks == 22_800
    assert incomplete.cogs_kopecks == 0
    assert incomplete.settlement_profit_kopecks is None
    assert incomplete.profit_before_ads_and_loyalty_kopecks is None
    assert incomplete.profit_after_loyalty_kopecks is None
    assert incomplete.cashback_discount_kopecks == 1_000
    assert "WB_PNL_PAYABLE_INCOMPLETE" in incomplete.blocker_ids
    assert "WB_PNL_PAYABLE_INCOMPLETE" not in complete.blocker_ids
    assert "WB_PNL_PAYABLE_INCOMPLETE" in page.blocker_ids
    assert page.summary.cogs_kopecks == 15_000
    assert page.summary.settlement_profit_kopecks is None
    assert page.summary.profit_after_loyalty_kopecks is None


def test_incomplete_v2_loyalty_never_becomes_zero(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    rows = settlement_rows_with_loyalty()
    rows[1].pop("cashbackDiscount")
    second_sku = dict(rows[0])
    second_sku.update(
        rrdId=3,
        nmId=202,
        vendorCode="FBBT_202",
        cashbackAmount="1.00",
        cashbackDiscount="0.50",
        cashbackCommissionChange="0.10",
    )
    rows.append(second_sku)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, rows, observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = next(row for row in page.items if row.nm_id == 101)
    assert row.cashback_amount_kopecks == 800
    assert row.cashback_discount_kopecks is None
    assert row.cashback_commission_change_kopecks == -100
    assert row.loyalty_net_cost_kopecks is None
    assert row.profit_after_loyalty_kopecks is None
    assert page.summary.cashback_amount_kopecks == 900
    assert page.summary.cashback_discount_kopecks is None
    assert page.summary.cashback_commission_change_kopecks == -90
    assert page.summary.loyalty_net_cost_kopecks is None
    assert page.summary.profit_after_loyalty_kopecks is None
    assert page.summary.net_profit_kopecks is None
    assert "WB_PNL_LOYALTY_NOT_CANONICAL" in row.blocker_ids
    assert "WB_PNL_LOYALTY_NOT_CANONICAL" in page.blocker_ids
    assert "WB_PNL_CLASSIFICATION_NOT_CANONICAL" not in page.blocker_ids


def test_empty_v2_snapshot_has_zero_loyalty_evidence(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, [], observed_at=NOW
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.state == "empty"
    assert page.items == []
    assert page.summary.cashback_amount_kopecks == 0
    assert page.summary.cashback_discount_kopecks == 0
    assert page.summary.cashback_commission_change_kopecks == 0
    assert page.summary.loyalty_net_cost_kopecks == 0
    assert page.summary.profit_after_loyalty_kopecks is None
    assert "WB_PNL_LOYALTY_NOT_CANONICAL" not in page.blocker_ids


def test_exact_advertising_only_sku_joins_report_union(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    session.add_all(
        [
            CatalogSkuRow(
                catalog_sku_id=12,
                organization_id=1,
                code="FBBT_202",
            ),
            MarketplaceProductRow(
                marketplace_product_id=1012,
                organization_id=1,
                marketplace_account_id=31,
                external_product_id="202",
                seller_article="FBBT_202",
            ),
        ]
    )
    session.flush()
    session.add(
        MarketplaceOfferRow(
            marketplace_offer_id=2012,
            organization_id=1,
            marketplace_account_id=31,
            marketplace_product_id=1012,
            external_offer_key="FBBT_202",
            catalog_sku_id=12,
        )
    )
    session.commit()
    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(nm_id=202),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.total == 1
    finance_row = page.items[0]
    assert finance_row.nm_id == 101 and finance_row.advertising_spend_kopecks == 0
    assert finance_row.profit_before_loyalty_kopecks == 23_500
    assert AdvertisingService(session, 1, now=lambda: NOW).get_pnl_source(31, PERIOD).spend_by_nm[202] == 12_300


def test_aggregate_only_settlement_is_not_operational_advertising(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_finance_aggregate_only(
        31,
        PERIOD,
        981_000,
        source_reference="retained_finance_cache",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.items[0].advertising_spend_kopecks == 0
    assert page.items[0].profit_before_loyalty_kopecks == 23_500
    assert page.summary.advertising_spend_kopecks == 0
    assert page.summary.unattributed_advertising_spend_kopecks == 0
    assert page.summary.profit_before_loyalty_kopecks == 23_500
    assert page.advertising_snapshot is None
    assert set(page.blocker_ids) == GLOBAL_BLOCKERS


def test_unattributed_raw_spend_keeps_summary_but_not_row_profit(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows_with_loyalty(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(residual_rubles=0.5),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = page.items[0]
    assert page.summary.advertising_spend_kopecks == row.advertising_spend_kopecks == 0
    assert page.summary.unattributed_advertising_spend_kopecks == 0
    assert row.profit_before_loyalty_kopecks == 23_500
    assert row.profit_after_loyalty_kopecks == row.net_profit_kopecks == 22_800
    assert "WB_PNL_ADVERTISING_UNATTRIBUTED" not in page.blocker_ids
    assert AdvertisingService(session, 1, now=lambda: NOW).get_pnl_source(31, PERIOD).unattributed_spend_kopecks == 50


def test_advertising_source_failure_propagates_without_guessing(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows_with_loyalty(), observed_at=NOW
    )
    AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31,
        PERIOD,
        raw_advertising_bundle(),
        source_reference="wb_api:raw",
        observed_at=NOW,
    )
    source_row = session.scalar(
        select(WbAdvertisingFactRow).where(
            WbAdvertisingFactRow.nm_id == 101,
            WbAdvertisingFactRow.fact_scope == "source_sku",
        )
    )
    assert source_row is not None
    source_row.spend_kopecks = None
    session.commit()

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = page.items[0]
    assert page.summary.advertising_spend_kopecks == row.advertising_spend_kopecks == 0
    assert page.summary.unattributed_advertising_spend_kopecks == 0
    assert row.profit_before_loyalty_kopecks == 23_500
    assert "WB_ADS_SOURCE_METRIC_INCOMPLETE" not in page.blocker_ids
    assert "WB_ADS_SOURCE_METRIC_INCOMPLETE" in AdvertisingService(session, 1, now=lambda: NOW).get_pnl_source(31, PERIOD).blocker_ids


def test_abc_pnl_resolves_each_business_day_end_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.wb_reports import abc_pnl

    add_dated_costs(session)
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    calls: list[date] = []
    original = abc_pnl._day_end

    def observed(day: date) -> datetime:
        calls.append(day)
        return original(day)

    monkeypatch.setattr(abc_pnl, "_day_end", observed)

    abc_pnl.WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert calls == [date(2026, 8, 20), date(2026, 8, 21)]


def test_assumed_cost_remains_explicit(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session, second_state="assumed")
    add_dated_economics(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )

    assumed = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert assumed.items[0].cogs_kopecks == 15_000
    assert assumed.items[0].cost_value_state == "assumed"
    assert "WB_PNL_COST_ASSUMED" in assumed.items[0].blocker_ids
    assert "WB_PNL_COST_EVIDENCE_UNDATED" in assumed.blocker_ids


def test_missing_or_ambiguous_cost_mapping_never_guesses_cogs(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_economics(session)
    CostsService(session, organization_id=1).set_cost(
        catalog_sku_id=11,
        amount_kopecks=25_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 20, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="late-cost",
        evidence_status="dated",
    )
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    missing = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert missing.items[0].cogs_kopecks is None
    assert missing.items[0].settlement_profit_kopecks is None
    assert missing.summary.cogs_kopecks is None
    assert missing.summary.settlement_profit_kopecks is None
    assert "WB_PNL_COST_MISSING" in missing.blocker_ids

    session.add(CatalogSkuRow(catalog_sku_id=12, organization_id=1, code="ALT_101"))
    session.flush()
    session.add(
        MarketplaceOfferRow(
            marketplace_offer_id=2012,
            organization_id=1,
            marketplace_account_id=31,
            marketplace_product_id=1011,
            external_offer_key="ALT_101",
            catalog_sku_id=12,
        )
    )
    session.commit()
    ambiguous = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert ambiguous.items[0].catalog_sku_id is None
    assert ambiguous.items[0].cogs_kopecks is None
    assert "WB_PNL_COST_MAPPING_AMBIGUOUS" in ambiguous.blocker_ids
    assert ambiguous.items[0].tax_kopecks is None
    assert "WB_PNL_ECONOMICS_MAPPING_AMBIGUOUS" in ambiguous.blocker_ids


def test_unattributed_finance_is_preserved(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    rows = settlement_rows()
    rows.append(
        {
            "rrdId": 3,
            "reportId": 1001,
            "reportType": 1,
            "docTypeName": "Штраф",
            "quantity": 0,
            "retailAmount": "0.00",
            "penalty": "5.00",
            "rrDate": "2026-08-21",
        }
    )
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, rows, observed_at=NOW
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.total == 2
    unattributed = next(row for row in page.items if row.nm_id is None)
    assert unattributed.operation_count == 1
    assert page.summary.operation_count == 3


def test_sales_classes_and_summary_are_independent_of_pagination(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_economics(session)
    session.add_all(
        [
            CatalogSkuRow(
                catalog_sku_id=nm_id - 90,
                organization_id=1,
                code=f"FBBT_{nm_id}",
            )
            for nm_id in range(102, 111)
        ]
    )
    session.add_all(
        [
            MarketplaceProductRow(
                marketplace_product_id=nm_id + 910,
                organization_id=1,
                marketplace_account_id=31,
                external_product_id=str(nm_id),
                seller_article=f"FBBT_{nm_id}",
            )
            for nm_id in range(102, 111)
        ]
    )
    session.flush()
    session.add_all(
        [
            MarketplaceOfferRow(
                marketplace_offer_id=nm_id + 1910,
                organization_id=1,
                marketplace_account_id=31,
                marketplace_product_id=nm_id + 910,
                external_offer_key=f"FBBT_{nm_id}",
                catalog_sku_id=nm_id - 90,
            )
            for nm_id in range(102, 111)
        ]
    )
    session.commit()
    costs = CostsService(session, organization_id=1)
    for nm_id in range(101, 111):
        costs.set_cost(
            catalog_sku_id=nm_id - 90,
            amount_kopecks=0,
            value_state="configured",
            effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
            source="fixture",
            source_reference=f"rank-cost-{nm_id}",
            evidence_status="dated",
        )
    rows = [
        {
            "rrdId": nm_id,
            "reportId": 2001,
            "reportType": 1,
            "nmId": nm_id,
            "vendorCode": f"FBBT_{nm_id}",
            "docTypeName": "Продажа",
            "quantity": 1,
            "retailAmount": f"{(111 - nm_id) * 100}.00",
            "saleDt": "2026-08-20",
            "rrDate": "2026-08-21",
        }
        for nm_id in range(101, 111)
    ]
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, rows, observed_at=NOW
    )
    service = WbAbcPnlService(session, organization_id=1, now=lambda: NOW)

    full = service.get_page(31, PERIOD, limit=100, offset=0)
    page = service.get_page(31, PERIOD, limit=2, offset=2)

    assert [row.sales_class for row in full.items] == [
        "A",
        "A",
        "B",
        "B",
        "B",
        "C",
        "C",
        "C",
        "C",
        "C",
    ]
    assert [row.nm_id for row in page.items] == [103, 104]
    assert page.total == 10
    assert page.summary == full.summary


def test_mid_period_economics_change_uses_daily_basis(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    economics = EconomicsService(session, organization_id=1)
    economics.set_organization_policy(
        tax_basis_points=600,
        other_expense_price_basis_points=500,
        other_expense_per_sale_kopecks=1_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="economics-v1",
        evidence_status="dated",
    )
    economics.set_organization_policy(
        tax_basis_points=750,
        other_expense_price_basis_points=0,
        other_expense_per_sale_kopecks=0,
        value_state="configured",
        effective_from=datetime(2026, 8, 20, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="economics-v2",
        evidence_status="dated",
    )
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    row = page.items[0]
    assert page.economics_revision == 2
    assert row.tax_kopecks == 4_500
    assert row.other_expenses_kopecks == 0
    assert row.profit_before_ads_and_loyalty_kopecks == 23_800


def test_overhead_change_does_not_round_an_unchanged_tax_rate_twice(
    session: Session,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    economics = EconomicsService(session, 1)
    for day, other in [(19, 500), (20, 600)]:
        economics.set_organization_policy(
            tax_basis_points=750,
            other_expense_price_basis_points=other,
            other_expense_per_sale_kopecks=0,
            value_state="configured",
            effective_from=datetime(2026, 8, day, 21, tzinfo=timezone.utc),
            source="fixture",
            source_reference=f"overhead-{day}",
            evidence_status="dated",
        )
    FinanceService(session, 1, now=lambda: NOW).ingest_snapshot(
        31,
        PERIOD,
        [
            row
            | dict(
                docTypeName="Продажа", sellerOperName="Продажа", quantity=1, retailAmount="0.20", forPay="0.20"
            )
            for row in settlement_rows()
        ],
        observed_at=NOW,
    )
    page = WbAbcPnlService(session, 1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )
    assert page.items[0].revenue_kopecks == 40
    assert page.items[0].tax_kopecks == page.summary.tax_kopecks == 3


def test_assumed_or_missing_economics_remains_explicit(session: Session) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
        31, PERIOD, settlement_rows(), observed_at=NOW
    )
    service = WbAbcPnlService(session, organization_id=1, now=lambda: NOW)

    missing = service.get_page(31, PERIOD, limit=100, offset=0)

    assert missing.items[0].settlement_profit_kopecks == 28_300
    assert missing.items[0].tax_kopecks is None
    assert missing.items[0].profit_before_ads_and_loyalty_kopecks is None
    assert "WB_PNL_ECONOMICS_MISSING" in missing.blocker_ids

    add_dated_economics(session, value_state="assumed", evidence_status="undated")
    assumed = service.get_page(31, PERIOD, limit=100, offset=0)

    assert assumed.items[0].economics_value_state == "assumed"
    assert assumed.items[0].tax_kopecks == 4_800
    assert "WB_PNL_ECONOMICS_ASSUMED" in assumed.items[0].blocker_ids
    assert "WB_PNL_ECONOMICS_EVIDENCE_UNDATED" in assumed.blocker_ids


def test_economics_daily_coverage_mismatch_never_computes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    finance = FinanceService(session, organization_id=1, now=lambda: NOW)
    finance.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    source = finance.get_pnl_source(31, PERIOD)
    monkeypatch.setattr(
        FinanceService,
        "get_pnl_source",
        lambda *_args, **_kwargs: replace(source, daily_economics_basis={}),
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.items[0].tax_kopecks is None
    assert "WB_PNL_ECONOMICS_DAILY_COVERAGE_MISMATCH" in page.blocker_ids


def test_cost_daily_coverage_mismatch_never_computes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    add_dated_costs(session)
    add_dated_economics(session)
    finance = FinanceService(session, organization_id=1, now=lambda: NOW)
    finance.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    source = finance.get_pnl_source(31, PERIOD)
    monkeypatch.setattr(
        FinanceService,
        "get_pnl_source",
        lambda *_args, **_kwargs: replace(source, daily_net_units={}),
    )

    page = WbAbcPnlService(session, organization_id=1, now=lambda: NOW).get_page(
        31, PERIOD, limit=100, offset=0
    )

    assert page.items[0].cogs_kopecks is None
    assert "WB_PNL_COST_DAILY_COVERAGE_MISMATCH" in page.blocker_ids


def test_basis_point_rounding_is_signed_half_even() -> None:
    from app.modules.wb_reports.abc_pnl import _round_basis_points

    assert _round_basis_points(1, 5_000) == 0
    assert _round_basis_points(3, 5_000) == 2
    assert _round_basis_points(-1, 5_000) == 0
    assert _round_basis_points(-3, 5_000) == -2


@pytest.mark.parametrize(
    "tax_basis_points,value_state,evidence_status,residual,deduction,confirmed_tax,expected",
    [
        (750, "configured", "dated", 0, 0, False, 24_800_000),
        (750, "assumed", "undated", 0, 0, True, 24_800_000),
        (750, "configured", "dated", 0, 333, False, 24_766_700),
        (750, "configured", "dated", 1_000, 0, False, 24_800_000),
        (750, "configured", "dated", None, 0, False, 24_800_000),
        (600, "configured", "dated", 0, 0, False, 26_300_000),
        (2200, "configured", "dated", 0, 0, False, 10_300_000),
        (750, "assumed", "dated", 0, 0, False, None),
        (750, "configured", "undated", 0, 0, False, None),
        (None, "missing", "dated", 0, 0, False, None),
    ],
)
def test_approved_profit_uses_confirmed_policy_and_account_advertising_once(
    session: Session,
    tax_basis_points: int | None,
    value_state: str,
    evidence_status: str,
    residual: int | None,
    deduction: int,
    confirmed_tax: bool,
    expected: int | None,
) -> None:
    from app.modules.wb_reports.abc_pnl import WbAbcPnlService

    CostsService(session, 1).set_cost(
        catalog_sku_id=11,
        amount_kopecks=40_000_000,
        value_state="configured",
        effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
        source="fixture",
        source_reference="approved-cost",
        evidence_status="dated",
    )
    if tax_basis_points is not None:
        EconomicsService(session, 1).set_organization_policy(
            tax_basis_points=tax_basis_points,
            other_expense_price_basis_points=500,
            other_expense_per_sale_kopecks=0,
            value_state=value_state,
            effective_from=datetime(2026, 8, 19, 21, tzinfo=timezone.utc),
            source="fixture",
            source_reference="approved-tax",
            evidence_status=evidence_status,
            tax_value_state="configured" if confirmed_tax else None,
            tax_evidence_status="dated" if confirmed_tax else None,
        )
    common = dict(
        reportId=1001,
        reportType=1,
        nmId=101,
        vendorCode="FBBT_101", sellerOperName="Продажа", bonusTypeName="Другие удержания",
        cashbackAmount="0",
        cashbackDiscount="0",
        cashbackCommissionChange="0",
        saleDt="2026-08-20",
        rrDate="2026-08-21",
    )
    FinanceService(session, 1, now=lambda: NOW).ingest_snapshot(
        31,
        PERIOD,
        [
            dict(
                common,
                rrdId=1,
                docTypeName="Продажа",
                quantity=1,
                retailAmount="1000000.00",
                forPay="850000.00",
                deliveryService="70000.00",
                paidStorage="10000.00",
                paidAcceptance="5000.00",
                penalty="2000.00",
                deduction=str(deduction),
            ),
            dict(
                common,
                rrdId=2,
                forPay="0",
                bonusTypeName="WB Продвижение",
                deduction="40000.00",
            ),
        ],
        observed_at=NOW,
    )
    if residual is not None:
        AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
            31,
            PERIOD,
            raw_advertising_bundle(spend_rubles=40_000, residual_rubles=residual),
            source_reference="approved-advertising",
            observed_at=NOW,
        )

    service = WbAbcPnlService(session, 1, now=lambda: NOW)
    page = service.get_page(31, PERIOD, limit=1, offset=0)
    row = page.items[0]
    assert page.summary.profit_before_internal_expenses_kopecks == expected
    assert row.profit_before_internal_expenses_kopecks == (
        expected
    )
    assert row.deduction_kopecks == deduction * 100
    assert "WB_MANAGEMENT_OPERATIONS_UNRECONCILED" not in row.blocker_ids
    assert row.internal_expenses_kopecks == 0
    assert page.summary.internal_expenses_kopecks == 0
    assert row.net_profit_kopecks == expected
    assert page.summary.net_profit_kopecks == expected
    assert "WB_PNL_INTERNAL_EXPENSES_MISSING" not in page.blocker_ids
    tax_confirmed = confirmed_tax or (
        tax_basis_points is not None and value_state == "configured" and evidence_status == "dated"
    )
    assert ("WB_PNL_TAX_POLICY_NOT_CONFIRMED_750" in row.blocker_ids) != tax_confirmed
    assert service.get_page(31, PERIOD, limit=1, offset=1).summary == page.summary
