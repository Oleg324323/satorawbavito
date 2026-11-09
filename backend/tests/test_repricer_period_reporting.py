"""Date-switch/reporting regressions. Synthetic sources; no marketplace writes."""
from copy import deepcopy
from datetime import date
import pytest
from app.wb_repricer_reporting import period_coverage, apply_financial_row_state, apply_summary_availability, financial_totals

START, END = date(2026, 10, 5), date(2026, 10, 11)


def test_missing_coverage_enumerates_inclusive_dates_without_certifying_zero():
    missing = period_coverage({}, START, END)
    assert missing == dict(state="no_data", coveredDays=0, requestedDays=7,
        missingDates=[f"2026-10-{day:02d}" for day in range(5, 12)], missingDatesKnown=True)
    partial = period_coverage(dict(coverageState="partial", coveredDays=2), START, END)
    assert partial["missingDatesKnown"] is False
    assert partial["state"] == "partial"


@pytest.mark.parametrize("complete,expected", [(False, None), (True, 0)])
def test_financial_fields_never_fall_back_to_operational_values(complete, expected):
    rows = [dict(meta=dict(nmId=101, currentPriceKopecks=200000),
                 analytics=dict(salesUnits=999, grossSalesUnits=999, ordersUnits=55, plannedMarginKopecks=1234))]
    original = deepcopy(rows)
    apply_financial_row_state(rows, dict(sourceComplete=complete, aggregates={}))
    assert rows[0]["analytics"]["grossSalesUnits"] == expected
    assert rows[0]["analytics"]["buyoutUnits"] == expected
    for key in ("ordersUnits", "plannedMarginKopecks"):
        assert rows[0]["analytics"][key] == original[0]["analytics"][key]
    assert rows[0]["meta"] == original[0]["meta"]


def test_summary_missing_vs_complete_empty_finance_and_baskets():
    ledger = dict(sourceComplete=False, aggregates={})
    coverage = {key: period_coverage({}, START, END) for key in ("finance", "baskets")}
    summary = apply_summary_availability(dict(revenueKopecks=0, totalBaskets=0), coverage, ledger)
    assert summary["buyoutUnits"] is None
    assert summary["salesUnits"] is None
    assert summary["totalBaskets"] is None
    complete = dict(sourceComplete=True, aggregates={})
    coverage = {key: period_coverage(dict(coverageState="ok", coveredDays=7), START, END) for key in coverage}
    summary = apply_summary_availability(dict(revenueKopecks=0, totalBaskets=0), coverage, complete)
    assert summary["buyoutUnits"] == summary["buyoutAmountKopecks"] == summary["totalBaskets"] == 0


def test_signed_financial_comparison_is_scope_correct_and_keeps_account_residual():
    ledger = dict(sourceComplete=True, aggregates={
        "101": dict(netSalesUnits=-1, realizationRevenueKopecks=-10001),
        "102": dict(netSalesUnits=2, realizationRevenueKopecks=24000)},
        diagnostics=dict(unallocatedRealization=dict(netSalesUnits=1, revenueKopecks=8000)))
    assert financial_totals(ledger) == (2, 21999)
    assert financial_totals(ledger, {"101"}) == (-1, -10001)


def test_financial_comparison_uses_previous_equal_period_and_filtered_identifiers(monkeypatch):
    from app.routers import wb_repricer_bff as router
    from app.repricer_page_finance import page_projection
    calls = []
    def load(*args, **kwargs):
        calls.append(args)
        return dict(sourceComplete=True, coverageState="ok", coveredDays=7,
            aggregates={"101": dict(netSalesUnits=4, realizationRevenueKopecks=4000),
                        "102": dict(netSalesUnits=1000, realizationRevenueKopecks=1000000)})
    monkeypatch.setattr("app.wb_repricer_reporting.load_buyout_comparison", load)
    marker = page_projection.set(True)
    try:
        current = dict(sourceComplete=True, coveredDays=7, aggregates={"101": dict(netSalesUnits=2, realizationRevenueKopecks=2000)})
        coverage = {key: period_coverage(dict(coverageState="ok", coveredDays=7), START, END) for key in ("finance", "baskets")}
        result = router._apply_period_reporting({}, 2, START, END, ledger=current, coverage=coverage, nm_ids={"101"})
        assert result["buyoutUnits"] == 2
        assert result["buyoutComparison"]["units"] == 4
        assert result["buyoutComparison"]["dateFrom"] == "2026-09-28"
        assert result["buyoutComparison"]["dateTo"] == "2026-10-04"
        assert calls[0] == (2, date(2026, 9, 28), date(2026, 10, 4))
    finally:
        page_projection.reset(marker)


def test_unrelated_raw_period_does_not_invalidate_financial_projection(monkeypatch):
    from app.repricer_page_finance import _overlapping_metadata
    entries = [dict(sourceKey="before", dateFrom="2026-10-01", dateTo="2026-10-04"),
               dict(sourceKey="overlap", dateFrom="2026-10-04", dateTo="2026-10-05"),
               dict(sourceKey="later", dateFrom="2026-10-12", dateTo="2026-10-20"),
               dict(sourceKey="unknown")]
    assert [item["sourceKey"] for item in _overlapping_metadata(entries, START, END)] == ["overlap", "unknown"]


def test_snapshot_revision_uses_overlapping_sources_and_global_planning_dependencies(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.repricer_cache import store
    from app.repricer_cache.orm import WbRepricerSourceCacheRow as Row
    engine = create_engine("sqlite://")
    Row.__table__.create(engine)
    observed = datetime(2026, 10, 20, tzinfo=timezone.utc)
    with Session(engine) as session:
        for org, key, left, right, minute in [
            (1, "finance_selected", START, END, 1),
            (1, "finance_unrelated", date(2026, 10, 12), date(2026, 10, 15), 5),
            (1, "repricer_finance_progress_test", START, END, 6),
            (2, "finance_other_account", START, END, 10),
        ]:
            session.add(Row(organization_id=org, source_key=key, payload={}, range_date_from=left,
                range_date_to=right, fetched_at=observed + timedelta(minutes=minute)))
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        revision = store.get_repricer_sources_revision(1, date_from=START, date_to=END)
        assert datetime.fromisoformat(revision) == (observed + timedelta(minutes=1)).replace(tzinfo=None)
        session.add(Row(organization_id=1, source_key="commission_tariffs", payload={}, fetched_at=observed + timedelta(minutes=20)))
        session.commit()
        assert store.get_repricer_sources_revision(1, date_from=START, date_to=END) != revision
    engine.dispose()


@pytest.mark.parametrize("source", ["finance", "periodStats", "baskets"])
def test_snapshot_expires_when_a_required_observation_becomes_stale(monkeypatch, source):
    from app.routers import wb_repricer_bff as router
    from app.wb_api import source_freshness
    snapshot = dict(version=router.SKU_LIST_SNAPSHOT_VERSION, stateRevision="settings", taxRevision="tax",
        sourceRevision="source", goodsRevision="goods", dateFrom=str(START), dateTo=str(END), items=[],
        cache={f"{source}FreshnessState": "fresh", f"{source}OldestFetchedAt": "2026-10-20T10:00:00+00:00"})
    monkeypatch.setattr(router, "get_source_cache", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(router, "_snapshot_source_revision", lambda *args: "source")
    monkeypatch.setattr(router, "_page_finance_active", lambda: False)
    monkeypatch.setattr(router, "cached_goods_meta", lambda *args: {"latestFetchedAt": "goods"})
    monkeypatch.setattr(router, "get_source_cache_fetched_at", lambda *args: None)
    monkeypatch.setattr(source_freshness, "source_is_fresh", lambda payload: True)
    options = dict(period_suffix=f"{START}_{END}", include_promotions=False, include_content=False,
                   state_revision="settings", tax_revision="tax")
    assert router._load_repricer_sku_snapshot(1, "complete", **options) is snapshot
    monkeypatch.setattr(source_freshness, "source_is_fresh", lambda payload: False)
    assert router._load_repricer_sku_snapshot(1, "complete", **options) is None
