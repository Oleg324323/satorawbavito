from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import pytest

from app.repricer_cache.store import (
    _source_cache_metadata,
    cached_goods_meta,
    list_source_cache_ranges_by_prefix,
    save_source_cache,
    slim_source_cache_payload,
)
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.cabinet.orm import LkOrganizationRow
from app.repricer_cache import store
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.test_empty_database_migrations import cluster, database  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_cache_boundaries(monkeypatch):
    """Unit tests never resolve configured PostgreSQL or Redis connections."""
    monkeypatch.setattr("app.repricer_cache.store._run_db", lambda fn: None)
    monkeypatch.setattr("app.repricer_cache.store._redis_get_json", lambda *a, **kw: None)
    monkeypatch.setattr("app.repricer_cache.store._redis_set_json", lambda *a, **kw: None)
    monkeypatch.setattr("app.repricer_cache.store._redis_delete", lambda *a, **kw: None)


def test_source_revision_is_scoped_and_ignores_snapshot_and_sync_status(database, monkeypatch):
    _, engine = database
    LkOrganizationRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    observed = datetime(2026, 9, 15, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([LkOrganizationRow(organization_id=org, slug=f"synthetic-{org}", name="Synthetic") for org in (1, 2)])
        session.commit()
        session.add_all([
            WbRepricerSourceCacheRow(organization_id=org, source_key=key, payload={}, fetched_at=observed + timedelta(minutes=offset))
            for org, key, offset in ((1, "baskets_30", 0), (1, "commission_tariffs", 1), (2, "finance_30", 5),
                                     (1, "sku_snapshot_30", 10), (1, "wb_sync_status", 10), (1, "baskets_detail_status", 10))
        ])
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        assert datetime.fromisoformat(store.get_repricer_sources_revision(1)) == observed + timedelta(minutes=1)
        assert datetime.fromisoformat(store.get_repricer_sources_revision(2)) == observed + timedelta(minutes=5)


def test_period_source_revision_uses_only_tenant_overlapping_source_metadata(database, monkeypatch):
    _, engine = database
    LkOrganizationRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    observed = datetime(2026, 9, 15, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add_all([LkOrganizationRow(organization_id=org, slug=f"synthetic-{org}", name="Synthetic") for org in (1, 2)])
        session.commit()
        session.add_all([
            WbRepricerSourceCacheRow(
                organization_id=org, source_key=key, payload={"neverLoaded": True},
                range_date_from=first, range_date_to=last, fetched_at=observed + timedelta(minutes=offset),
            )
            for org, key, first, last, offset in (
                (1, "ads_legacy", None, None, 0),
                (1, "ads_2026-09-02", date(2026, 9, 2), date(2026, 9, 2), 1),
                (1, "ads_later", date(2026, 9, 3), date(2026, 9, 3), 5),
                (2, "ads_other_tenant", date(2026, 9, 1), date(2026, 9, 2), 10),
                (1, "repricer_stats_ads_2", date(2026, 9, 1), date(2026, 9, 2), 20),
            )
        ])
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        queries = []
        def record_query(_conn, _cursor, statement, _params, _context, _many):
            queries.append(statement)
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            actual = store.get_source_cache_range_revision(1, "ads_", date_from=date(2026, 9, 1), date_to=date(2026, 9, 2))
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert datetime.fromisoformat(actual) == observed + timedelta(minutes=1)
        assert len(queries) == 1 and "payload" not in queries[0]


@pytest.mark.parametrize("prefix,slim", [("baskets_", False), ("finance_", True)])
@pytest.mark.parametrize("newest_metadata", [
    {},
    {"range_date_from": date(2027, 1, 1), "range_date_to": date(2027, 1, 2),
     "daily_aggregate_dates": []},
])
def test_covering_cache_keeps_payload_selection_without_whole_jsonb_cast(
    database, monkeypatch, prefix, slim, newest_metadata,
):
    _, engine = database
    LkOrganizationRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    first, last = "2026-09-01", "2026-09-02"
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    payload = {"dateFrom": first, "dateTo": last, "dailyAggregates": {first: {}, "not-a-date": {}},
               "aggregates": {"123": {"cartCount": 2}}, "rows": [{"synthetic": True}]}
    with Session(engine) as session:
        session.add_all([LkOrganizationRow(organization_id=value, slug=f"synthetic-{value}", name="Synthetic")
                         for value in (1, 2)])
        session.commit()
        rows = [
            WbRepricerSourceCacheRow(organization_id=1, source_key=f"{prefix}older_typed", payload=payload,
                                    range_date_from=date.fromisoformat(first), range_date_to=date.fromisoformat(last),
                                    daily_aggregate_dates=[first], fetched_at=now - timedelta(seconds=1)),
            WbRepricerSourceCacheRow(organization_id=1, source_key=f"{prefix}newest", payload=payload,
                                    fetched_at=now, **newest_metadata),
        ]
        invalid = [None, {}, [], [payload], 0, False, "not-an-object",
                   {**payload, "dateFrom": None}, {**payload, "dateFrom": last},
                   {**payload, "dateTo": first},
                   *[{**payload, "dailyAggregates": value} for value in (None, {}, [], [{}], 0, False, "daily")]]
        rows.extend(WbRepricerSourceCacheRow(organization_id=1, source_key=f"{prefix}invalid_{index}",
                                           payload=value, fetched_at=now + timedelta(seconds=index + 1),
                                           range_date_from=date.fromisoformat(first),
                                           range_date_to=date.fromisoformat(last), daily_aggregate_dates=[first])
                    for index, value in enumerate(invalid))
        rows.extend([
            WbRepricerSourceCacheRow(organization_id=2, source_key=f"{prefix}other_tenant", payload=payload,
                                    fetched_at=now + timedelta(days=1)),
            WbRepricerSourceCacheRow(organization_id=1, source_key="ads_other_source", payload=payload,
                                    fetched_at=now + timedelta(days=1)),
        ])
        session.add_all(rows)
        session.commit()
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        try:
            result = store.get_covering_source_cache(
                1, prefix, date_from=date.fromisoformat(first), date_to=date.fromisoformat(last), slim=slim,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        session.delete(rows[1])
        session.commit()
        older = store.get_covering_source_cache(
            1, prefix, date_from=date.fromisoformat(first), date_to=date.fromisoformat(last), slim=slim,
        )
        assert older["sourceKey"] == f"{prefix}older_typed"
        session.delete(rows[0])
        session.commit()
        assert store.get_covering_source_cache(
            1, prefix, date_from=date.fromisoformat(first), date_to=date.fromisoformat(last), slim=slim,
        ) is None

    assert datetime.fromisoformat(result.pop("fetchedAt")) == now
    expected = {**payload, "sourceKey": f"{prefix}newest"}
    if slim:
        expected.pop("rows")
    assert result == expected
    assert len(statements) == 1
    assert "payload::jsonb" not in statements[0]


def test_source_cache_metadata_is_derived_without_mutating_payload():
    payload = {
        "dateFrom": "2026-07-11",
        "dateTo": "2026-08-09",
        "dailyDetailStatus": "partial",
        "dailyDetailError": "часть запросов не загрузилась",
        "dailyDetailRequestsCompleted": 10,
        "dailyDetailRequestsTotal": 92,
        "dailyAggregates": {
            "2026-07-11": {"123": {"baskets": 2}},
            "2026-07-12": {"123": {"baskets": 1}},
        },
    }
    original = deepcopy(payload)

    metadata = _source_cache_metadata("baskets_2026-07-11_2026-08-09", payload)

    assert metadata == {
        "range_date_from": date(2026, 7, 11),
        "range_date_to": date(2026, 8, 9),
        "revenue_basis": None,
        "finance_schema_version": None,
        "daily_detail_status": "partial",
        "daily_detail_error": "часть запросов не загрузилась",
        "daily_detail_deferred_at": None,
        "daily_detail_paused_at": None,
        "daily_detail_failed_at": None,
        "daily_detail_fetched_at": None,
        "daily_detail_partial_at": None,
        "daily_detail_preserved_at": None,
        "daily_detail_preserved_by": None,
        "daily_detail_requests_completed": 10,
        "daily_detail_requests_total": 92,
        "daily_aggregate_dates": ["2026-07-11", "2026-07-12"],
    }
    assert payload == original


def test_finance_metadata_preserves_basis_version_and_payload():
    payload = {
        "dateFrom": "2026-07-11", "dateTo": "2026-08-09",
        "revenueBasis": "synthetic-seller-revenue",
        "financeSchemaVersion": "synthetic-v2",
        "dailyAggregates": {"2026-07-11": {"123": {"revenueKopecks": 0}}},
    }
    original = deepcopy(payload)
    metadata = _source_cache_metadata("finance_2026-07-11_2026-08-09", payload)
    assert metadata["revenue_basis"] == "synthetic-seller-revenue"
    assert metadata["finance_schema_version"] == "synthetic-v2"
    assert payload == original


def test_list_source_cache_ranges_avoids_payload_and_parses_legacy_key(monkeypatch):
    class FakeResult:
        def mappings(self):
            return self

        def __iter__(self):
            records = [
                    {
                        "source_key": "baskets_2026-07-11_2026-08-09",
                        "range_date_from": None,
                        "range_date_to": None,
                        "revenue_basis": None,
                        "finance_schema_version": None,
                        "daily_detail_status": None,
                        "daily_detail_error": None,
                        "daily_detail_deferred_at": None,
                        "daily_detail_paused_at": None,
                        "daily_detail_failed_at": None,
                        "daily_detail_fetched_at": None,
                        "daily_detail_partial_at": None,
                        "daily_detail_preserved_at": None,
                        "daily_detail_preserved_by": None,
                        "daily_detail_requests_completed": None,
                        "daily_detail_requests_total": None,
                        "daily_aggregate_dates": None,
                        "fetched_at": datetime(2026, 8, 10, 16, 10, tzinfo=timezone.utc),
                    }
                ]
            return iter([
                records[0],
                {**records[0], "revenue_basis": "synthetic-seller-revenue",
                 "finance_schema_version": "synthetic-v2"},
                {**records[0], "daily_aggregate_dates": []},
                {**records[0], "daily_aggregate_dates": '["2026-07-11", "2026-07-12"]'},
                {**records[0], "daily_aggregate_dates": 'null'},
                {**records[0], "daily_aggregate_dates": 'not-json'},
            ])

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement).lower()
            assert "payload" not in sql
            assert "jsonb" not in sql
            assert params == {
                "organization_id": 2,
                "source_key_pattern": "baskets_%",
                "limit": 100,
            }
            return FakeResult()

    monkeypatch.setattr("app.repricer_cache.store._run_db", lambda db_fn: db_fn(FakeSession()))

    result = list_source_cache_ranges_by_prefix(2, "baskets_", limit=100)

    assert result[0]["dateFrom"] == "2026-07-11"
    assert result[0]["dateTo"] == "2026-08-09"
    assert result[0]["dailyAggregateDates"] == []
    assert result[0]["dailyAggregatesDays"] is None
    assert result[0]["revenueBasis"] is None
    assert result[0]["financeSchemaVersion"] is None
    assert result[1]["revenueBasis"] == "synthetic-seller-revenue"
    assert result[1]["financeSchemaVersion"] == "synthetic-v2"
    assert result[2]["dailyAggregateDates"] == []
    assert result[2]["dailyAggregatesDays"] == 0
    assert result[3]["dailyAggregateDates"] == ["2026-07-11", "2026-07-12"]
    assert result[3]["dailyAggregatesDays"] == 2
    assert result[4]["dailyAggregatesDays"] is None
    assert result[5]["dailyAggregateDates"] == []


def test_slim_finance_source_cache_drops_raw_rows():
    payload = {
        "aggregates": {"123": {"salesUnits": 2}},
        "rows": [{"nmId": 123, "quantity": 1}],
        "count": 1,
        "rowsCount": 1,
    }
    slim = slim_source_cache_payload("finance_30", payload)
    assert "rows" not in slim
    assert slim["aggregates"]["123"]["salesUnits"] == 2


def test_save_finance_source_cache_omits_raw_rows():
    saved = save_source_cache(
        99,
        "finance_30",
        {
            "aggregates": {"1": {"salesUnits": 1}},
            "rows": [{"nmId": 1}],
            "count": 1,
        },
    )
    assert "rows" not in saved


def test_report_acknowledgement_rejects_failed_database_save():
    from app.routers.wb_reports_bff import save_source_cache as save_report_cache
    with pytest.raises(RuntimeError, match="сохранить данные в БД"):
        save_report_cache(99, "reports_job_stats", {"state": "completed"})


def test_slim_source_cache_preserves_category_fields():
    content = slim_source_cache_payload(
        "content_cards",
        {
            "cards": [
                {
                    "vendorCode": "ФБbt_1133",
                    "nmID": 453200669,
                    "title": "Футболка",
                    "object": "Футболки",
                    "objectID": 192,
                    "subjectID": 192,
                    "brand": "Anomie Studio",
                    "sizes": [{"skus": [123]}],
                }
            ]
        },
    )
    goods = slim_source_cache_payload(
        "goods",
        {
            "goods": [
                {
                    "vendorCode": "ФБbt_1133",
                    "nmID": 453200669,
                    "brand": "Anomie Studio",
                    "subjectName": "Футболки",
                    "subjectID": 192,
                    "sizes": [{"price": 2809}],
                }
            ]
        },
    )

    assert content["cards"][0]["objectID"] == 192
    assert content["cards"][0]["subjectID"] == 192
    assert goods["goods"][0]["subjectID"] == 192
    assert goods["goods"][0]["subjectName"] == "Футболки"


def test_save_source_cache_recovers_from_insert_race(monkeypatch):
    class FakeResult:
        def __init__(self, rowcount: int):
            self.rowcount = rowcount

    class FakeSession:
        def __init__(self):
            self.execute_calls = 0
            self.commits = 0
            self.rollbacks = 0

        def execute(self, statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return FakeResult(0)
            return FakeResult(1)

        def add(self, row):
            assert isinstance(row, WbRepricerSourceCacheRow)

        def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise IntegrityError("insert", {}, Exception("duplicate key"))

        def rollback(self):
            self.rollbacks += 1

    fake_session = FakeSession()

    monkeypatch.setattr("app.repricer_cache.store._run_db", lambda db_fn: db_fn(fake_session))

    saved = save_source_cache(2, "reports_job_stock_2026-06-10_2026-07-10_sku", {"state": "completed"})

    assert saved["state"] == "completed"
    assert fake_session.rollbacks == 1
    assert fake_session.execute_calls == 2


def test_cached_goods_meta_uses_sql_aggregate_without_loading_payload(monkeypatch):
    class FakeResult:
        def mappings(self):
            return self

        def first(self):
            return {
                "pages_cached": 2,
                "total_cached": 1750,
                "next_offset": 2000,
                "page_limit": 1000,
                "latest_fetched_at": datetime(2026, 7, 28, 8, 30, tzinfo=timezone.utc),
            }

    class FakeSession:
        def scalars(self, _statement):
            raise AssertionError("cached_goods_meta must not load goods_payload rows")

        def execute(self, _statement, params=None):
            assert params == {"organization_id": 5}
            return FakeResult()

    monkeypatch.setattr("app.repricer_cache.store._run_db", lambda db_fn: db_fn(FakeSession()))
    monkeypatch.setattr("app.repricer_cache.store._redis_get_json", lambda _key: None)
    monkeypatch.setattr("app.repricer_cache.store._redis_set_json", lambda *_args, **_kwargs: None)

    assert cached_goods_meta(5) == {
        "pagesCached": 2,
        "totalCached": 1750,
        "nextOffset": 2000,
        "pageLimit": 1000,
        "latestFetchedAt": "2026-07-28T08:30:00+00:00",
    }
