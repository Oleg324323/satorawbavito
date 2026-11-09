from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.finance.orm import (
    WbFinanceSyncRunRow,
    WbFinanceSyncRunSkuDailyPnlRollupRow,
    WbFinanceSyncRunSkuPnlRollupRow,
)
from app.platform.finance.service import FinanceService
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
PERIOD = Period(date(2026, 8, 20), date(2026, 8, 21))


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
            ]
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
            "ppvzSalesCommission": "100.00",
            "deliveryService": "50.00",
            "paidStorage": "20.00",
            "paidAcceptance": "10.00",
            "penalty": "30.00",
            "deduction": "40.00",
            "paymentSchedule": "25.00",
            "additionalPayment": "100.00",
            "acquiringFee": "15.00",
            "saleDt": "2026-08-20",
            "rrDate": "2026-08-20",
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
            "ppvzSalesCommission": "20.00",
            "acquiringFee": "3.00",
            "saleDt": "2026-08-21",
            "rrDate": "2026-08-21",
        },
    ]


def test_pnl_source_materializes_signed_settlement_components(session: Session) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    snapshot = service.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "ready"
    assert source.snapshot == snapshot
    assert source.daily_net_units == {
        (101, date(2026, 8, 20)): 2,
        (101, date(2026, 8, 21)): -1,
    }
    assert len(source.facts) == 1
    fact = source.facts[0]
    assert fact.nm_id == 101
    assert fact.revenue_kopecks == 80_000
    assert fact.sales_revenue_kopecks == 100_000
    assert fact.returns_revenue_kopecks == 20_000
    assert fact.sales_units == 2
    assert fact.returns_units == 1
    assert fact.net_units == 1
    assert fact.commission_kopecks == 8_000
    assert fact.logistics_kopecks == 5_000
    assert fact.storage_kopecks == 2_000
    assert fact.acceptance_kopecks == 1_000
    assert fact.penalty_kopecks == 3_000
    assert fact.deduction_kopecks == 4_000
    assert fact.additional_payment_kopecks == -12_500
    assert fact.acquiring_kopecks == 1_200
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunSkuPnlRollupRow)
    ) == 1
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None
    assert run.is_pnl_rollup_materialized is True


def test_exact_pnl_fallback_preserves_all_report_membership(session: Session) -> None:
    rows = settlement_rows() + [
        {"rrdId": 3, "nmId": 101, "saleDt": "2026-08-10", "rrDate": "2026-08-20", "penalty": "10"},
        {"rrdId": 4, "nmId": 101, "rrDate": "2026-08-21", "penalty": "2"},
    ]
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    materialized = service.get_pnl_source(31, PERIOD)
    assert materialized.facts[0].operation_count == 4
    assert materialized.facts[0].penalty_kopecks == 4_200

    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None and run.is_daily_pnl_rollup_materialized
    run.is_pnl_rollup_materialized = False
    session.commit()

    assert service.get_pnl_source(31, PERIOD) == materialized


def test_pnl_source_materializes_complete_loyalty_money(session: Session) -> None:
    rows = settlement_rows()
    rows[0].update(
        forPay="885.00",
        cashbackAmount="10.00",
        cashbackDiscount="3.00",
        cashbackCommissionChange="2.00",
    )
    rows[1].update(
        forPay="177.00",
        cashbackAmount="-1.00",
        cashbackDiscount="0.50",
        cashbackCommissionChange="-0.25",
    )
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    fact = service.get_pnl_source(31, PERIOD).facts[0]

    assert fact.cashback_amount_kopecks == 900
    assert fact.cashback_discount_kopecks == 350
    assert fact.cashback_commission_change_kopecks == 175
    assert fact.loyalty_net_cost_kopecks == 1_075


def test_missing_loyalty_component_stays_unknown_in_exact_and_custom_rollups(
    session: Session,
) -> None:
    rows = settlement_rows()
    rows[0].update(
        forPay="885.00",
        cashbackAmount="10.00",
        cashbackDiscount="3.00",
        cashbackCommissionChange="2.00",
    )
    rows[1].update(
        forPay="177.00",
        cashbackAmount="0",
        cashbackCommissionChange="0",
    )
    covering = Period(date(2026, 8, 19), date(2026, 8, 22))
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    service.ingest_snapshot(31, covering, rows, observed_at=NOW)

    exact = service.get_pnl_source(31, covering).facts[0]
    custom = service.get_pnl_source(31, PERIOD).facts[0]
    first_day = service.get_pnl_source(
        31, Period(date(2026, 8, 20), date(2026, 8, 20))
    ).facts[0]
    second_day = service.get_pnl_source(
        31, Period(date(2026, 8, 21), date(2026, 8, 21))
    ).facts[0]

    assert (
        exact.cashback_amount_kopecks,
        exact.cashback_discount_kopecks,
        exact.cashback_commission_change_kopecks,
        exact.loyalty_net_cost_kopecks,
    ) == (1_000, None, 200, None)
    assert (
        custom.cashback_amount_kopecks,
        custom.cashback_discount_kopecks,
        custom.cashback_commission_change_kopecks,
        custom.loyalty_net_cost_kopecks,
    ) == (1_000, None, 200, None)
    assert (
        first_day.cashback_amount_kopecks,
        first_day.cashback_discount_kopecks,
        first_day.cashback_commission_change_kopecks,
        first_day.loyalty_net_cost_kopecks,
    ) == (1_000, 300, 200, 1_200)
    assert (
        second_day.cashback_amount_kopecks,
        second_day.cashback_discount_kopecks,
        second_day.cashback_commission_change_kopecks,
        second_day.loyalty_net_cost_kopecks,
    ) == (0, None, 0, None)


def test_backfill_publishes_each_missing_pnl_rollup(session: Session) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None
    session.execute(delete(WbFinanceSyncRunSkuPnlRollupRow))
    run.is_pnl_rollup_materialized = False
    session.commit()

    assert service.backfill_pnl_rollups(31) == 1

    session.refresh(run)
    assert run.is_pnl_rollup_materialized is True
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunSkuPnlRollupRow)
    ) == 1
    assert service.backfill_pnl_rollups(31) == 0


def test_custom_pnl_period_filters_a_covering_snapshot(session: Session) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    service.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)

    source = service.get_pnl_source(
        31,
        Period(date(2026, 8, 20), date(2026, 8, 20)),
    )

    assert source.state == "ready"
    assert source.facts[0].revenue_kopecks == 100_000
    assert source.facts[0].sales_units == 2
    assert source.facts[0].returns_units == 0
    assert source.daily_net_units == {(101, date(2026, 8, 20)): 2}


def test_pnl_source_retains_unattributed_finance(session: Session) -> None:
    rows = settlement_rows()
    rows.append(
        {
            "rrdId": 3,
            "reportId": 1001,
            "reportType": 1,
            "docTypeName": "",
            "sellerOperName": "Штраф без артикула",
            "penalty": "5.00",
            "saleDt": "2026-08-20",
            "rrDate": "2026-08-21",
        }
    )
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    source = service.get_pnl_source(31, PERIOD)

    unattributed = next(fact for fact in source.facts if fact.nm_id is None)
    assert unattributed.operation_count == 1
    assert unattributed.penalty_kopecks == 500


def test_interrupted_pnl_rollup_does_not_replace_previous_snapshot(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    first = service.ingest_snapshot(31, PERIOD, settlement_rows()[:1], observed_at=NOW)
    monkeypatch.setattr(
        service,
        "_materialize_pnl_rollup",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("pnl interrupted")),
    )

    with pytest.raises(RuntimeError, match="pnl interrupted"):
        service.ingest_snapshot(
            31,
            PERIOD,
            settlement_rows(),
            observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
        )

    source = service.get_pnl_source(31, PERIOD)
    assert source.snapshot is not None
    assert source.snapshot.sync_run_id == first.sync_run_id
    assert source.facts[0].revenue_kopecks == 100_000


def test_exact_and_custom_pnl_read_daily_materialization(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None
    assert run.is_daily_pnl_rollup_materialized is True
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunSkuDailyPnlRollupRow)
    ) == 2
    daily_reads: list[Period] = []
    read_daily = service._materialized_daily_bases

    def observed_daily_read(
        run: WbFinanceSyncRunRow, period: Period, **kwargs
    ) -> tuple[dict[tuple[int | None, date], int], dict]:
        daily_reads.append(period)
        return read_daily(run, period, **kwargs)

    monkeypatch.setattr(service, "_materialized_daily_bases", observed_daily_read)

    exact = service.get_pnl_source(31, PERIOD)
    # Subperiod membership is posting-date based; business-day rollups are
    # unsuitable for that filter. Exact reports still reuse materialization.
    custom = service.get_pnl_source(
        31, Period(date(2026, 8, 20), date(2026, 8, 20))
    )

    assert exact.daily_net_units == {
        (101, date(2026, 8, 20)): 2,
        (101, date(2026, 8, 21)): -1,
    }
    assert {
        key: (basis.revenue_kopecks, basis.sales_units)
        for key, basis in exact.daily_economics_basis.items()
    } == {
        (101, date(2026, 8, 20)): (100_000, 2),
        (101, date(2026, 8, 21)): (-20_000, 0),
    }
    assert custom.facts[0].revenue_kopecks == 100_000
    assert custom.daily_net_units == {(101, date(2026, 8, 20)): 2}
    assert {
        key: (basis.revenue_kopecks, basis.sales_units)
        for key, basis in custom.daily_economics_basis.items()
    } == {(101, date(2026, 8, 20)): (100_000, 2)}
    assert daily_reads == [PERIOD]  # subperiods filter immutable posting evidence


def test_daily_pnl_backfill_is_resumable(session: Session) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, settlement_rows(), observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)
    assert run is not None
    session.execute(delete(WbFinanceSyncRunSkuDailyPnlRollupRow))
    run.is_daily_pnl_rollup_materialized = False
    session.commit()

    assert service.backfill_daily_pnl_rollups(31) == 1

    session.refresh(run)
    assert run.is_daily_pnl_rollup_materialized is True
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunSkuDailyPnlRollupRow)
    ) == 2
    assert service.backfill_daily_pnl_rollups(31) == 0


def test_interrupted_daily_pnl_rollup_keeps_previous_snapshot(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    first = service.ingest_snapshot(31, PERIOD, settlement_rows()[:1], observed_at=NOW)
    monkeypatch.setattr(
        service,
        "_materialize_daily_pnl_rollup",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("daily pnl interrupted")),
    )

    with pytest.raises(RuntimeError, match="daily pnl interrupted"):
        service.ingest_snapshot(
            31,
            PERIOD,
            settlement_rows(),
            observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
        )

    source = service.get_pnl_source(31, PERIOD)
    assert source.snapshot is not None
    assert source.snapshot.sync_run_id == first.sync_run_id
    assert source.facts[0].revenue_kopecks == 100_000
