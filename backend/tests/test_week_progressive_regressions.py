"""Week-only assembly regressions. Synthetic evidence; no WB or live DB."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app import wb_week_finance as finance, wb_week_sources as sources
from app.routers import wb_reports_bff as reports

START, END = date(2026, 10, 12), date(2026, 10, 18)
PRIOR, PRIOR_END = date(2026, 10, 5), date(2026, 10, 11)


@pytest.mark.parametrize("allowed", [True, False])
def test_final_union_gets_existing_pnl_independently_and_drops_obsolete_values(monkeypatch, allowed):
    monkeypatch.setattr(reports, "_catalog_meta_by_nm", lambda org: {})
    def financial(report, *a, **kw):
        report["rows"].append({"nmId": 102})  # expense-only product
        return report
    def operational(report, *a, **kw):
        report["rows"].extend([{"nmId": 103}, {"nmId": None, "isUnallocated": True}])
        return report
    monkeypatch.setattr(finance, "attach", financial)
    monkeypatch.setattr(sources, "attach", operational)
    now = {101: {"profit": 0, "margin": 0}, 102: {"profit": -7904, "margin": None}}
    before = {102: {"profit": -1000, "margin": None}, 103: {"profit": 200, "margin": 10}}
    report = reports._week_financial_columns({"rows": [{"nmId": 101, "profit": {"kopecks": 999}}]},
        7, START, END, allowed, current_pnl=deepcopy(now), previous_pnl=deepcopy(before))
    rows = {row["nmId"]: row for row in report["rows"] if row.get("nmId")}
    assert rows[101]["profit"]["kopecks"] == (0 if allowed else None)
    assert rows[102]["profit"]["kopecks"] == (-7904 if allowed else None)
    assert rows[102]["profit"]["previousKopecks"] == (-1000 if allowed else None)
    assert rows[102]["marginPct"]["percent"] is None
    assert rows[103]["profit"]["kopecks"] is None
    assert rows[103]["profit"]["previousKopecks"] == (200 if allowed else None)
    assert now[102]["profit"] == -7904


@pytest.mark.parametrize("ledger,expected", [({}, None),
    ({"sourceComplete": True, "amounts": {}}, 0),
    ({"sourceComplete": False, "amounts": {"0": {"storageKopecks": 123}}}, 123)])
def test_week_storage_missing_zero_and_known_partial(monkeypatch, ledger, expected):
    monkeypatch.setattr(sources, "source_revision", lambda *a, **kw: "revision")
    monkeypatch.setattr(sources, "load_funnel", lambda *a: dict(metrics={}, complete=False, missingDates=[]))
    monkeypatch.setattr(sources, "load_ads", lambda *a: ({}, False, None))
    monkeypatch.setattr(finance, "load_finance", lambda *a: ledger)
    monkeypatch.setattr(sources.store, "get_source_cache", lambda *a, **kw: {})
    result = sources.attach({"rows": [], "unallocatedFinancials": {"selected": {}, "previous": {}}},
        7, START, END, PRIOR, PRIOR_END, finance_allowed=True, catalog={})
    for period in ("selected", "previous"):
        assert result["unallocatedFinancials"][period]["storageKopecks"] == expected
        assert result["storageAllocation"][period]["unallocatedKopecks"] == expected


@pytest.mark.parametrize('direct,complete,unknown,expected,status', [
    (0, False, False, None, 'missing'),
    (125, False, False, None, 'missing'),
    (0, True, False, 0, 'complete'),
    (0, True, True, None, 'missing'),
])
def test_product_storage_does_not_confirm_zero_before_account_pool_is_resolved(
        monkeypatch, direct, complete, unknown, expected, status):
    monkeypatch.setattr(sources, 'source_revision', lambda *a: 'revision')
    monkeypatch.setattr(sources, 'load_funnel', lambda *a: dict(metrics={}, complete=False, missingDates=[]))
    monkeypatch.setattr(sources, 'load_ads', lambda *a: ({'101': 299994}, True, 0))
    ledger = dict(sourceComplete=True, amounts={'101': {'storageKopecks': direct}, '0': {'storageKopecks': 0 if complete else 1598072}},
        unknown={'101': ['storageKopecks']} if unknown else {})
    monkeypatch.setattr(finance, 'load_finance', lambda *a: ledger)
    monkeypatch.setattr(sources.store, 'get_source_cache', lambda *a, **kw: {})
    monkeypatch.setattr('app.wb_storage.load_stock', lambda org: {'sourceComplete': complete,
        'aggregates': {'101': {'wbStockUnits': 10}, '102': {'wbStockUnits': 30}}})
    result = sources.attach(dict(rows=[{'nmId': 101}],
        columns=[], unallocatedFinancials={'selected': {}, 'previous': {}}),
        7, START, END, PRIOR, PRIOR_END, finance_allowed=True, catalog={})
    row = result['rows'][0]
    assert row['storageKopecks']['kopecks'] == expected
    assert row['storageCoverage']['selected'] == status
    assert row['campaignAdSpendKopecks']['kopecks'] == 299994
    assert any(column['key'] == 'campaignAdSpendKopecks' for column in result['columns'])


def test_revisions_are_period_account_scoped_and_detect_corrections(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE wb_repricer_source_cache (organization_id INTEGER, source_key TEXT, fetched_at DATETIME, range_date_from DATE, range_date_to DATE)")
        for org, key, fetched, left, right in [
            (7, "funnel_v1_products_synthetic_fact_1", "2026-10-24 00:00:00", str(START), str(END)),
            (7, "funnel_v1_products_synthetic_fact_2", "2026-10-25 00:00:00", str(START), str(END)),
            (7, "funnel_v1_products_synthetic_fact_old", "2026-10-23 00:00:00", "2020-01-01", "2020-01-02"),
            (8, "funnel_v1_products_synthetic_fact_1", "2026-10-25 00:00:00", str(START), str(END)),
            (7, "funnel_v1_products_other_fact_1", "2026-10-25 00:00:00", str(START), str(END)),
        ]:
            connection.execute(text("INSERT INTO wb_repricer_source_cache VALUES (:o,:k,:f,:l,:r)"),
                dict(o=org, k=key, f=fetched, l=left, r=right))
    def run(callback):
        with Session(engine) as session:
            return callback(session)
    metadata = [dict(sourceKey="ads_old", dateFrom="2020-01-01", dateTo="2020-01-31", fetchedAt="old"),
                dict(sourceKey="ads_current", dateFrom=str(START), dateTo=str(END), fetchedAt="current")]
    monkeypatch.setattr(sources, "_connection", lambda org: "synthetic")
    monkeypatch.setattr(sources.store, "_run_db", run)
    monkeypatch.setattr(sources.store, "cached_goods_meta", lambda org: {"latestFetchedAt": "catalog"})
    monkeypatch.setattr(sources.store, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw:
        [m for m in metadata if m["sourceKey"].startswith(prefix)])
    revision = lambda: sources.source_revision(7, PRIOR, END)
    initial = revision()
    metadata[0]["fetchedAt"] = "updated-unrelated"
    assert revision() == initial
    with engine.begin() as connection:
        connection.execute(text("UPDATE wb_repricer_source_cache SET fetched_at='2026-10-24 12:00:00' WHERE source_key='funnel_v1_products_synthetic_fact_1' AND organization_id=7"))
    assert revision() != initial  # same count and latest timestamp, corrected old fact
    initial = revision()
    metadata[1]["fetchedAt"] = "corrected-relevant"
    assert revision() != initial
    initial = revision()
    metadata.append(dict(sourceKey="ads_undated", fetchedAt="unknown-bounds"))
    assert revision() != initial
    initial = revision()
    metadata.append(dict(sourceKey="stock_history_2026-10-06", dateFrom="2026-10-06", dateTo="2026-10-06", fetchedAt="stock"))
    assert revision() != initial
    engine.dispose()


def test_loading_cache_cannot_retain_obsolete_cost_tax_or_finance(monkeypatch):
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "economics-1")
    monkeypatch.setattr(reports, "quarterly_tax_revision", lambda org: "tax-1")
    monkeypatch.setattr(sources, "source_revision", lambda *a: "ops-1")
    monkeypatch.setattr(finance, "source_revision", lambda *a: "finance-1")
    cache = dict(taxRevision="economics-1", quarterlyTaxRevision="tax-1",
        fetchedAt=datetime.now(timezone.utc).isoformat(), report=dict(
            cacheVersion=reports.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION, loadingSnapshot=True,
            filters={"dateRange": {"from": str(START), "to": str(END)}},
            financialVersion=finance.VERSION, financialAccess=True,
            operationalSourceRevision="ops-1", financialSourceRevision="finance-1",
            rows=[{"nmId": 101, "sales": {"units": 1}}]))
    usable = lambda: reports._report_payload_cache_is_usable("week-over-week", cache, organization_id=7, allow_stale=True)
    assert usable()
    for module, name, changed in [(reports, "legacy_finance_tax_revision", "economics-2"),
            (reports, "quarterly_tax_revision", "tax-2"), (finance, "source_revision", "finance-2"),
            (sources, "source_revision", "ops-2")]:
        with monkeypatch.context() as patch:
            patch.setattr(module, name, lambda *a, value=changed: value)
            assert not usable()


def test_raw_finance_reuses_projection_until_a_relevant_receipt_changes(monkeypatch):
    from app.cabinet import store as cabinet
    from app import repricer_page_finance as page
    connection = page.connection_key("synthetic")
    metadata = [dict(sourceKey=f"repricer_finance_{connection}_old", dateFrom="2020-01-01", dateTo="2020-01-31", fetchedAt="old"),
                dict(sourceKey=f"repricer_finance_{connection}_current", dateFrom=str(START), dateTo=str(END), fetchedAt="current")]
    calls = []
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda org: "synthetic")
    monkeypatch.setattr(sources.store, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw:
        [m for m in metadata if m["sourceKey"].startswith(prefix)])
    monkeypatch.setattr(page, "load_page_finance", lambda *a, **kw: calls.append(a) or {"sourceComplete": True})
    finance._cache.clear()
    finance.load_finance(7, START, END)
    metadata[0]["fetchedAt"] = "unrelated-correction"
    finance.load_finance(7, START, END)
    assert len(calls) == 1
    metadata[1]["fetchedAt"] = "relevant-correction"
    finance.load_finance(7, START, END)
    assert len(calls) == 2
    finance._cache.clear()


@pytest.mark.parametrize("changed", [False, True])
def test_database_preview_is_bounded_nonblocking_and_never_requests_wb(monkeypatch, changed):
    import asyncio
    from fastapi import BackgroundTasks
    from types import SimpleNamespace
    from app import wb_week_preview as preview
    from app.infra import redis_client
    held, saved, calls, statuses = [False], [], [], []
    class Lease:
        def acquire(self, **kw):
            if held[0]:
                return False
            held[0] = True
            return True
        def owned(self):
            return held[0]
        def release(self):
            held[0] = False
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: SimpleNamespace(lock=lambda *a, **kw: Lease()))
    monkeypatch.setattr(reports, "_report_payload_cache_is_usable", lambda *a, **kw: False)
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "save_source_cache", lambda *a: statuses.append(a[-1]))
    monkeypatch.setattr(reports, "_save_exact_report_payload_cache", lambda **kw: saved.append(kw))
    revisions = iter(["before", "after" if changed else "before"])
    monkeypatch.setattr(preview, "_dependencies", lambda *a: next(revisions))
    def build(**kw):
        assert kw["wb_token"] is None and kw["include_previous"] is True
        calls.append(kw)
        return {"rows": [{"nmId": 101, "sales": {"units": 1}, "profit": {"kopecks": -7904, "previousKopecks": -1000}}]}
    monkeypatch.setattr(reports, "_build_week_over_week_fallback_report", build)
    background = BackgroundTasks()
    args = dict(organization_id=7, user_id="synthetic", start=START, end=END,
        group_by="sku", source="operational", finance_allowed=True, cached={})
    preview.enqueue(background, **args)
    preview.enqueue(background, **args)  # no duplicate while provider worker is busy
    assert len(background.tasks) == 1 and calls == []  # HTTP has not assembled anything
    asyncio.run(background())
    assert len(calls) == 1 and not held[0]
    assert len(saved) == (0 if changed else 1)
    assert statuses[-1]["state"] == ("superseded" if changed else "completed")
    if not changed:
        assert saved[0]["report"]["rows"][0]["profit"]["previousKopecks"] == -1000
