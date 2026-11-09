from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.finance.orm import (
    WbFinanceOperationRow,
    WbFinanceSyncRunOperationRow,
    WbFinanceSyncRunRow,
    WbFinanceSyncRunSkuRollupRow,
)
from app.platform.finance.service import FinanceAccountNotFound, FinanceService
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
PERIOD = Period(date(2026, 8, 17), date(2026, 8, 23))
FIXTURE = Path(__file__).parent / "fixtures" / "wb_abc_2026_08_17_23.json"


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
                    marketplace_account_id=32,
                    organization_id=2,
                    marketplace="wb",
                    external_account_id="wb-two",
                    status="connected",
                ),
            ]
        )
        db.commit()
        yield db


def source_row(
    *,
    rrd_id: int,
    amount_kopecks: int,
    report_type: int,
    nm_id: int = 453200669,
    correction: bool = False,
) -> dict[str, object]:
    return {
        "rrdId": rrd_id,
        "reportId": 820347930 if report_type == 1 else 820347933,
        "reportType": report_type,
        "nmId": nm_id,
        "vendorCode": "FBBT_1133",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "isLateCorrection": correction,
        "quantity": 1,
        "retailAmount": f"{amount_kopecks / 100:.2f}",
        "saleDt": "2026-08-20",
        "rrDate": "2026-08-23",
    }


def frozen_rows() -> tuple[dict[str, object], list[dict[str, object]]]:
    fixture = json.loads(FIXTURE.read_text())
    return fixture, [
        source_row(rrd_id=1, amount_kopecks=fixture["mainKopecks"], report_type=1),
        source_row(rrd_id=2, amount_kopecks=fixture["buyoutKopecks"], report_type=2),
        source_row(
            rrd_id=3,
            amount_kopecks=fixture["lateCorrectionKopecks"],
            report_type=1,
            correction=True,
        ),
    ]


def test_frozen_finance_snapshot_is_exact_and_components_do_not_overlap(
    session: Session,
) -> None:
    fixture, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    snapshot = service.ingest_snapshot(
        marketplace_account_id=31,
        period=PERIOD,
        rows=rows,
        observed_at=datetime(2026, 8, 26, 3, 50, tzinfo=timezone.utc),
    )
    page = service.get_page(
        marketplace_account_id=31,
        period=PERIOD,
        limit=100,
        offset=0,
    )

    assert snapshot.operation_count == 3
    assert page.state == "ready"
    assert page.summary.total_revenue_kopecks == fixture["liveKopecks"]
    assert page.summary.main_revenue_kopecks == fixture["mainKopecks"]
    assert page.summary.redemptions_revenue_kopecks == fixture["buyoutKopecks"]
    assert (
        page.summary.late_correction_revenue_kopecks == fixture["lateCorrectionKopecks"]
    )
    assert (
        page.summary.main_revenue_kopecks
        + page.summary.redemptions_revenue_kopecks
        + page.summary.late_correction_revenue_kopecks
        + page.summary.unknown_revenue_kopecks
        == page.summary.total_revenue_kopecks
    )
    assert round(page.summary.total_revenue_kopecks / 100) == fixture["uiRoundedRubles"]
    assert (
        sum(row.revenue_kopecks for row in page.items)
        == page.summary.total_revenue_kopecks
    )


def test_exact_snapshot_reads_its_materialized_sku_rollup(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    snapshot = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    run = session.get(WbFinanceSyncRunRow, snapshot.sync_run_id)

    assert run is not None
    assert run.is_rollup_materialized is True
    assert (
        session.scalar(
            select(func.count()).select_from(WbFinanceSyncRunSkuRollupRow)
        )
        == 1
    )
    monkeypatch.setattr(
        service,
        "_effective_membership",
        lambda *_args: (_ for _ in ()).throw(AssertionError("raw scan")),
    )

    page = service.get_page(31, PERIOD, limit=100, offset=0)

    assert page.summary.operation_count == 3
    assert page.summary.total_revenue_kopecks == fixture["liveKopecks"]


def test_identical_retry_reuses_snapshot_but_changed_payload_creates_a_version(
    session: Session,
) -> None:
    _, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    first = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    retried = service.ingest_snapshot(31, PERIOD, list(reversed(rows)), observed_at=NOW)
    changed = service.ingest_snapshot(
        31,
        PERIOD,
        [
            *rows[:2],
            source_row(
                rrd_id=3, amount_kopecks=120_801, report_type=1, correction=True
            ),
        ],
        observed_at=NOW,
    )

    assert retried.sync_run_id == first.sync_run_id
    assert changed.sync_run_id != first.sync_run_id
    assert session.scalar(select(func.count()).select_from(WbFinanceSyncRunRow)) == 2
    assert session.scalar(select(func.count()).select_from(WbFinanceOperationRow)) == 4
    assert (
        session.scalar(select(func.count()).select_from(WbFinanceSyncRunOperationRow))
        == 5
    )


def test_changed_snapshot_delta_reconstructs_revision_and_deletion(
    session: Session,
) -> None:
    fixture, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    first = service.ingest_snapshot(31, PERIOD, rows, observed_at=NOW)
    revised_rows = [
        *rows[:2],
        source_row(rrd_id=3, amount_kopecks=120_801, report_type=1, correction=True),
    ]
    revised = service.ingest_snapshot(
        31,
        PERIOD,
        revised_rows,
        observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
    )
    revised_page = service.get_page(31, PERIOD, limit=100, offset=0)
    deleted = service.ingest_snapshot(
        31,
        PERIOD,
        revised_rows[:2],
        observed_at=datetime(2026, 9, 1, 12, 2, tzinfo=timezone.utc),
    )

    page = service.get_page(31, PERIOD, limit=100, offset=0)
    changes = session.execute(
        select(
            WbFinanceSyncRunOperationRow.sync_run_id,
            WbFinanceSyncRunOperationRow.is_present,
        ).where(
            WbFinanceSyncRunOperationRow.sync_run_id.in_(
                [first.sync_run_id, revised.sync_run_id, deleted.sync_run_id]
            )
        )
    ).all()

    assert page.snapshot is not None
    assert page.snapshot.sync_run_id == deleted.sync_run_id
    assert revised_page.snapshot is not None
    assert revised_page.snapshot.sync_run_id == revised.sync_run_id
    assert revised_page.summary.total_revenue_kopecks == (
        fixture["mainKopecks"] + fixture["buyoutKopecks"] + 120_801
    )
    assert page.summary.operation_count == 2
    assert page.summary.total_revenue_kopecks == (
        fixture["mainKopecks"] + fixture["buyoutKopecks"]
    )
    assert [present for run_id, present in changes if run_id == first.sync_run_id] == [
        True,
        True,
        True,
    ]
    assert sorted(
        present for run_id, present in changes if run_id == revised.sync_run_id
    ) == [False, True]
    assert [
        present for run_id, present in changes if run_id == deleted.sync_run_id
    ] == [False]


def test_incomplete_snapshot_never_replaces_last_good(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    first = service.ingest_snapshot(31, PERIOD, rows[:2], observed_at=NOW)
    monkeypatch.setattr(
        "app.platform.finance.service._insert_memberships",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        service.ingest_snapshot(
            31,
            PERIOD,
            rows,
            observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
        )

    page = service.get_page(31, PERIOD, limit=100, offset=0)

    assert page.snapshot is not None
    assert page.snapshot.sync_run_id == first.sync_run_id
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunRow).where(
            WbFinanceSyncRunRow.is_materialized.is_(False)
        )
    ) == 1

    monkeypatch.undo()
    resumed = service.ingest_snapshot(
        31,
        PERIOD,
        rows,
        observed_at=datetime(2026, 9, 1, 12, 2, tzinfo=timezone.utc),
    )
    resumed_page = service.get_page(31, PERIOD, limit=100, offset=0)

    assert resumed_page.snapshot is not None
    assert resumed_page.snapshot.sync_run_id == resumed.sync_run_id
    assert resumed.last_observed_at == datetime(
        2026, 9, 1, 12, 2, tzinfo=timezone.utc
    )
    assert resumed_page.summary.operation_count == 3
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunRow).where(
            WbFinanceSyncRunRow.is_materialized.is_(False)
        )
    ) == 0


def test_rollup_failure_never_publishes_the_new_snapshot(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    first = service.ingest_snapshot(31, PERIOD, rows[:2], observed_at=NOW)
    monkeypatch.setattr(
        service,
        "_materialize_rollup",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("rollup interrupted")),
    )

    with pytest.raises(RuntimeError, match="rollup interrupted"):
        service.ingest_snapshot(
            31,
            PERIOD,
            rows,
            observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
        )

    page = service.get_page(31, PERIOD, limit=100, offset=0)
    assert page.snapshot is not None
    assert page.snapshot.sync_run_id == first.sync_run_id
    assert page.summary.operation_count == 2
    assert session.scalar(
        select(func.count()).select_from(WbFinanceSyncRunRow).where(
            WbFinanceSyncRunRow.is_materialized.is_(False)
        )
    ) == 1


def test_exact_period_snapshot_wins_over_a_newer_covering_window(
    session: Session,
) -> None:
    _, rows = frozen_rows()
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    exact = service.ingest_snapshot(31, PERIOD, rows[:1], observed_at=NOW)
    wider_period = Period(PERIOD.date_from, date(2026, 8, 30))
    service.ingest_snapshot(
        31,
        wider_period,
        rows[:2],
        observed_at=datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
    )

    page = service.get_page(31, PERIOD, limit=100, offset=0)
    wider_page = service.get_page(31, wider_period, limit=100, offset=0)

    assert page.snapshot is not None
    assert page.snapshot.sync_run_id == exact.sync_run_id
    assert page.summary.operation_count == 1
    assert wider_page.summary.operation_count == 2


def test_account_scope_and_explicit_source_states(session: Session) -> None:
    service = FinanceService(session, organization_id=1, now=lambda: NOW)

    with pytest.raises(FinanceAccountNotFound):
        service.get_page(32, PERIOD, limit=100, offset=0)

    assert service.get_page(31, PERIOD, limit=100, offset=0).state == "missing"
    assert (
        service.get_page(
            31,
            Period(date(2026, 9, 2), date(2026, 9, 3)),
            limit=100,
            offset=0,
        ).state
        == "future"
    )
    service.ingest_snapshot(31, PERIOD, [], observed_at=NOW)
    assert service.get_page(31, PERIOD, limit=100, offset=0).state == "empty"

    current = Period(date(2026, 9, 1), date(2026, 9, 1))
    service.ingest_snapshot(31, current, [], observed_at=NOW)
    assert service.get_page(31, current, limit=100, offset=0).state == "partial"


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("exact", [False, True])
def test_finance_is_not_complete_until_the_source_window_was_observed_after_close(
    session: Session, empty: bool, exact: bool
) -> None:
    _, rows = frozen_rows()
    rows = [] if empty else rows[:1]
    requested = PERIOD if exact else Period(PERIOD.date_from, date(2026, 8, 22))
    for row in rows:
        row["rrDate"] = str(requested.date_to)
    service = FinanceService(session, organization_id=1, now=lambda: NOW)
    before_close = PERIOD.end_exclusive_at - timedelta(seconds=1)
    first = service.ingest_snapshot(31, PERIOD, rows, observed_at=before_close)

    page = service.get_page(31, requested, limit=100, offset=0)
    source = service.get_pnl_source(31, requested)
    assert page.state == source.state == "partial"
    assert page.summary.operation_count == sum(fact.operation_count for fact in source.facts)
    assert page.summary.operation_count == len(rows)

    replay = service.ingest_snapshot(31, PERIOD, rows, observed_at=PERIOD.end_exclusive_at)
    assert replay.sync_run_id == first.sync_run_id
    page = service.get_page(31, requested, limit=100, offset=0)
    source = service.get_pnl_source(31, requested)
    assert page.state == source.state == ("empty" if empty else "ready")
    assert page.snapshot == source.snapshot == replay
