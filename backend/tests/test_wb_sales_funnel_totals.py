from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import wb_sales_funnel as funnel
from app.platform.period import Period
from app.wb_sales_funnel_totals import change, read_totals_comparison, read_seller_totals, snapshot_key
from tests.test_wb_sales_funnel import db, catalog, native_product, wb_product, seller_summary


PERIOD = Period(date(2026, 10, 10), date(2026, 10, 15))
SCOPE = funnel.scope_key(1, "synthetic")


def save_current(db, products):
    funnel.upsert(db, 1, funnel.observation_rows(SCOPE, PERIOD.date_from, PERIOD.date_to, products,
        datetime.now(timezone.utc), seller_wide=True))
    funnel.project(db, 1, 1, SCOPE, PERIOD)
    seller_summary(db, SCOPE, PERIOD, products)


def save_previous(db, products, *, now=None):
    past = funnel.previous_period(PERIOD)
    funnel.upsert(db, 1, funnel.observation_rows(SCOPE, past.date_from, past.date_to, products,
        now or datetime.now(timezone.utc), seller_wide=True))
    funnel.project(db, 1, 1, SCOPE, past)
    seller_summary(db, SCOPE, past, products, now=now)


def read(db, **kw):
    return read_totals_comparison(db, 1, 1, SCOPE, PERIOD, **kw)


def test_totals_use_sums_and_unrounded_weighted_cr_not_product_dynamics(db):
    catalog(db, 2)
    products = [native_product(PERIOD, 1, openCount=30, cartCount=3, orderCount=2, buyoutCount=1),
                native_product(PERIOD, 2, openCount=70, cartCount=7, orderCount=2, buyoutCount=1)]
    products[0]["statistic"]["selected"].update(openCount=30, cartCount=6)
    products[1]["statistic"]["selected"].update(openCount=70, cartCount=6)
    save_current(db, products)
    funnel.upsert(db, 1, funnel.native_rows(SCOPE, PERIOD, products, datetime.now(timezone.utc), seller_wide=True, request_identity="synthetic"))
    before = funnel.read_comparison(db, 1, 1, SCOPE, PERIOD, [1, 2])
    result = read(db)
    assert result["totals"] == {"visits": 0, "carts": 20, "orders": 300, "buyouts": 300, "cartConversion": 20}
    assert result["selectedTotals"]["cartConversion"] == 12
    assert result["previousTotals"]["cartConversion"] == 10
    assert result["complete"] and not result["refreshRequired"]
    assert result["source"] == "calculated_wb_sales_funnel_totals_v1"
    assert (result["dateFrom"], result["dateTo"]) == ("2026-10-04", "2026-10-09")
    assert before == funnel.read_comparison(db, 1, 1, SCOPE, PERIOD, [1, 2])
    assert before["items"][0]["dynamics"]["visits"] == -7
    # There is no previous-period projection: combined native evidence suffices.
    assert not db.scalar(select(funnel.Cache).where(funnel.Cache.source_key.startswith(f"{SCOPE}_page_{funnel.previous_period(PERIOD).cache_key}_")))


def test_cr_change_does_not_compare_rounded_base_percentages(db):
    catalog(db)
    save_current(db, [wb_product(shareOrderPercent=1, openCount=30000, cartCount=1)])
    save_previous(db, [wb_product(shareOrderPercent=1, openCount=60000, cartCount=1)])
    result = read(db)
    assert result["selectedTotals"]["cartConversion"] == 0  # Existing displayed rounding.
    assert result["totals"]["cartConversion"] == 100


@pytest.mark.parametrize("current,previous,expected", [(0, 1, -100), (1, 1, 0), (0, 0, 0), (1, 0, None), (None, 1, None)])
def test_zero_and_unknown_baselines(current, previous, expected):
    assert change(current, previous) == expected


def test_metric_readiness_is_independent_and_missing_is_not_zero(db):
    catalog(db)
    save_current(db, [wb_product(shareOrderPercent=1)])
    save_previous(db, [wb_product(shareOrderPercent=1, cartCount=None, openCount=50)])
    result = read(db)
    assert result["totals"]["visits"] == 100
    assert result["ready"]["orders"] and result["ready"]["buyouts"]
    assert result["totals"]["carts"] is None and result["totals"]["cartConversion"] is None
    assert not result["refreshRequired"]  # Completed response omitted a field; do not loop forever.


def test_each_period_includes_its_full_source_population_not_a_catalog_intersection(db):
    catalog(db, 3)
    save_current(db, [wb_product(1, shareOrderPercent=1), wb_product(2, shareOrderPercent=1)])
    save_previous(db, [wb_product(1, shareOrderPercent=1), wb_product(3, shareOrderPercent=1, openCount=999999)])
    result = read(db, include_missing=True)
    assert result["selectedTotals"]["visits"] == 200
    assert result["coverage"]["visits"] == {"products": 2, "matchedProducts": 2}
    assert result["previousTotals"]["visits"] == 1000099  # Include previous-only source cards too.
    assert result["totals"]["visits"] == pytest.approx((200 / 1000099 - 1) * 100)
    assert not result["refreshRequired"]


def test_partial_current_metric_does_not_become_full_or_block_other_fields(db):
    catalog(db)
    save_current(db, [wb_product(shareOrderPercent=1, orderCount=None)])
    save_previous(db, [wb_product(shareOrderPercent=1)])
    result = read(db)
    assert result["totals"]["orders"] is None
    assert result["totals"]["visits"] == result["totals"]["cartConversion"] == 0


def test_full_previous_listing_does_not_require_every_current_product_to_exist(db):
    catalog(db, 2)
    save_current(db, [wb_product(1, shareOrderPercent=1), wb_product(2, shareOrderPercent=1)])
    save_previous(db, [wb_product(1, shareOrderPercent=1)])
    funnel.save_enumeration(db, 1, 1, SCOPE, funnel.previous_period(PERIOD), {1}, datetime.now(timezone.utc))
    result = read(db)
    assert result["totals"]["visits"] == 100 and not result["refreshRequired"]
    assert result["complete"] and result["reportComplete"]


@pytest.mark.parametrize("corruption", ["source", "account", "dateFrom", "dateTo", "population", "completeListing", "fetchedAt"])
def test_invalid_past_native_evidence_does_not_leak_into_totals(db, corruption):
    catalog(db)
    save_current(db, [native_product(PERIOD)])
    past = funnel.previous_period(PERIOD)
    cache = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == snapshot_key(SCOPE, past)))
    payload = {**cache.payload, corruption: "invalid"}
    if corruption == "fetchedAt":
        payload[corruption] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    cache.payload = payload
    db.commit()
    assert read(db)["totals"]["visits"] is None


def test_scope_and_period_isolation_and_stale_previous_values(db):
    catalog(db)
    save_current(db, [wb_product(shareOrderPercent=1)])
    save_previous(db, [wb_product(shareOrderPercent=1)], now=datetime.now(timezone.utc)-timedelta(days=2))
    assert read(db)["totals"]["visits"] is None
    for org, account, scope, period in [(2, 1, SCOPE, PERIOD), (1, 9, SCOPE, PERIOD),
                                       (1, 1, funnel.scope_key(1, "other"), PERIOD),
                                       (1, 1, SCOPE, Period(date(2026,10,11),date(2026,10,15)))]:
        assert not any(read_totals_comparison(db, org, account, scope, period)["ready"].values())


def test_aggregate_collection_reuses_combined_values_without_provider_call(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    catalog(db)
    product = native_product(PERIOD)
    save_current(db, [product])
    funnel.upsert(db, 1, funnel.native_rows(SCOPE, PERIOD, [product], datetime.now(timezone.utc), seller_wide=True, request_identity="synthetic"))
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", lambda *a: pytest.fail("Combined values are sufficient"))
    before = funnel.read_page(db, 1, 1, SCOPE, PERIOD)["totals"]
    for _ in range(2):
        funnel.collect(1, 1, SCOPE, str(PERIOD.date_from), str(PERIOD.date_to), "totals-job", aggregate_comparison=True)
    assert read(db)["complete"]
    assert funnel.read_page(db, 1, 1, SCOPE, PERIOD)["totals"] == before


def test_missing_previous_uses_one_exact_period_listing_then_reuses_it(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    catalog(db, 2)
    save_current(db, [wb_product(1, shareOrderPercent=1), wb_product(2, shareOrderPercent=1)])
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a, **kw: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [wb_product(1, shareOrderPercent=1)], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    for _ in range(2):
        funnel.collect(1, 1, SCOPE, str(PERIOD.date_from), str(PERIOD.date_to), "totals-job", aggregate_comparison=True)
    assert len(calls) == 1
    assert calls[0]["selectedPeriod"] == {"start": "2026-10-04", "end": "2026-10-09"}
    assert calls[0]["nmIds"] == []
    assert not read(db)["refreshRequired"] and read(db)["totals"]["visits"] == 100


def test_aggregate_refresh_is_selected_pair_scoped_and_deduplicated(db, monkeypatch):
    from app.routers import wb_sales_funnel as router
    from app.repricer_tasks import refresh_sales_funnel
    from starlette.requests import Request
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, SCOPE, PERIOD))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": True, "refreshRequired": False})
    monkeypatch.setattr(router, "read_totals_comparison", lambda *a: {"complete": False, "total": 1, "refreshRequired": True})
    monkeypatch.setattr(router, "touch_demand", lambda *a, **kw: None)
    locks, cache, queued = {}, {}, []
    class Redis:
        def set(self, key, value, **kw):
            if key in locks:
                return False
            locks[key] = value
            return True
    monkeypatch.setattr(router, "get_redis_client", Redis)
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **kw: cache.get(key))
    monkeypatch.setattr(router, "save_source_cache", lambda org, key, payload, **kw: cache.update({key: payload}))
    monkeypatch.setattr(refresh_sales_funnel, "apply_async", lambda **kw: queued.append(kw))
    for _ in range(2):
        router.refresh_funnel(Request({"type": "http"}), PERIOD.date_from, PERIOD.date_to, None, False, True)
    assert len(queued) == 1 and queued[0]["kwargs"] == {"aggregate_comparison": True}
    assert queued[0]["args"][3:6] == ("2026-10-10", "2026-10-15", funnel.job_key(SCOPE, PERIOD, aggregate=True))


@pytest.mark.parametrize("status", ["queued", "running", "failed", "paused"])
def test_aggregate_refresh_waits_for_successful_selected_collection(db, monkeypatch, status):
    from app.routers import wb_sales_funnel as router
    from fastapi import HTTPException
    from starlette.requests import Request
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, SCOPE, PERIOD))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": True, "refreshRequired": False})
    monkeypatch.setattr(router, "get_source_cache", lambda *a, **kw: {"status": status})
    monkeypatch.setattr(router, "read_totals_comparison", lambda *a: pytest.fail("No comparison before selected completion"))
    with pytest.raises(HTTPException) as error:
        router.refresh_funnel(Request({"type": "http"}), PERIOD.date_from, PERIOD.date_to, None, False, True)
    assert error.value.status_code == 409


def test_totals_reader_does_not_scan_or_return_other_accounts_and_does_not_write(db, monkeypatch):
    from app.routers import wb_sales_funnel as router
    from starlette.requests import Request
    catalog(db)
    save_current(db, [native_product(PERIOD)])
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, SCOPE, PERIOD))
    calls = []
    monkeypatch.setattr(router, "_with_job", lambda request, actor, scope, period, result, **kw: calls.append(kw) or result)
    before = db.scalars(select(funnel.Cache.cache_id)).all()
    result = router.get_funnel_totals_comparison(Request({"type": "http"}), PERIOD.date_from, PERIOD.date_to, None)
    assert result["source"] == "calculated_wb_sales_funnel_totals_v1"
    assert db.scalars(select(funnel.Cache.cache_id)).all() == before
    assert calls == [{"aggregate": True}]


def test_full_summary_and_dynamics_include_non_catalog_cards_without_changing_rows(db):
    catalog(db)
    products = [native_product(PERIOD, nm, openCount=50, cartCount=5) for nm in (1, 99)]
    products[0]["statistic"]["selected"].update(openCount=100, cartCount=20)
    products[1]["statistic"]["selected"].update(openCount=900, cartCount=80)
    funnel.upsert(db, 1, funnel.observation_rows(SCOPE, PERIOD.date_from, PERIOD.date_to,
        products[:1], datetime.now(timezone.utc), seller_wide=True))
    funnel.project(db, 1, 1, SCOPE, PERIOD)
    funnel.upsert(db, 1, funnel.native_rows(SCOPE, PERIOD, products, datetime.now(timezone.utc),
        seller_wide=True, request_identity="synthetic"))
    before = funnel.read_page(db, 1, 1, SCOPE, PERIOD)
    native_before = funnel.read_comparison(db, 1, 1, SCOPE, PERIOD, [1])
    assert before["totals"]["visits"] is None  # Never advertise catalog subset as full seller total.
    seller_summary(db, SCOPE, PERIOD, products)
    after = funnel.read_page(db, 1, 1, SCOPE, PERIOD)
    assert after["items"] == before["items"] and after["total"] == 1
    assert after["summaryProducts"] == 2 and after["summaryComplete"]
    assert after["totals"]["visits"] == 1000 and after["totals"]["carts"] == 100
    assert after["totals"]["cartConversion"] == 10
    assert after["items"][0]["metrics"]["visits"] == 100
    assert read(db)["totals"]["visits"] == 900
    assert read(db)["totals"]["cartConversion"] == 0
    assert funnel.read_comparison(db, 1, 1, SCOPE, PERIOD, [1]) == native_before
    assert native_before["items"][0]["dynamics"]["visits"] == -7


def checkpoint(db, offset, products, *, fetched=None, token="synthetic", filters=None):
    import json
    from hashlib import sha256
    from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, build_wb_analytics_client
    request = WbApiRequest(method="POST", path=funnel.SOURCE, jsonBody={
        "selectedPeriod": {"start": str(PERIOD.date_from), "end": str(PERIOD.date_to)},
        "pastPeriod": {"start": str(funnel.previous_period(PERIOD).date_from), "end": str(funnel.previous_period(PERIOD).date_to)},
        "nmIds": [], "skipDeletedNm": False, "limit": 2, "offset": offset, **(filters or {})})
    host = build_wb_analytics_client("complete", token_override=token).base_url
    identity = json.dumps([host, sha256(token.encode()).hexdigest(), request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
    response = WbApiResponseEnvelope(request=request, statusCode=200, ok=True,
        data={"data": {"products": products, "currency": "RUB"}}).model_dump(mode="json")
    funnel.upsert(db, 1, [{"source_key": "wb_report_http_" + sha256(identity.encode()).hexdigest(),
        "payload": {"response": response}, "fetched_at": fetched or datetime.now(timezone.utc),
        "range_date_from": None, "range_date_to": None}])


def test_complete_verified_pagination_bootstraps_global_totals_without_touching_cards(db):
    catalog(db)
    checkpoint(db, 0, [native_product(PERIOD, 1), native_product(PERIOD, 99)])
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    assert read_seller_totals(db, 1, 1, SCOPE, PERIOD) is None  # Full page is not completion.
    checkpoint(db, 2, [native_product(PERIOD, 100)])
    before = funnel.read_page(db, 1, 1, SCOPE, PERIOD)["items"]
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    assert read(db)["selectedTotals"]["visits"] == 300
    assert read(db)["previousTotals"]["visits"] == 300
    assert read(db)["total"] == 3 and read(db)["complete"]
    assert funnel.read_page(db, 1, 1, SCOPE, PERIOD)["items"] == before


def test_new_first_page_cannot_mix_with_old_tail_or_replace_complete_totals(db):
    now = datetime.now(timezone.utc)
    checkpoint(db, 0, [native_product(PERIOD, 1), native_product(PERIOD, 99)], fetched=now-timedelta(minutes=3))
    checkpoint(db, 2, [native_product(PERIOD, 100)], fetched=now-timedelta(minutes=2))
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    original = read(db)
    revised = native_product(PERIOD, 1)
    revised["statistic"]["selected"]["openCount"] = 10000
    checkpoint(db, 0, [revised, native_product(PERIOD, 99)], fetched=now-timedelta(minutes=1))
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    assert read(db) == original
    checkpoint(db, 2, [native_product(PERIOD, 100)], fetched=now)
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    assert read(db)["selectedTotals"]["visits"] == 10200


@pytest.mark.parametrize("token,filters", [("other-token", {}), ("synthetic", {"nmIds": [1]}),
    ("synthetic", {"skipDeletedNm": True}), ("synthetic", {"brandNames": ["A"]})])
def test_wrong_credentials_and_filtered_reports_are_not_seller_totals(db, token, filters):
    checkpoint(db, 0, [native_product(PERIOD)], token=token, filters=filters)
    funnel.import_checkpoints(db, 1, SCOPE, "synthetic", PERIOD, 1, totals_only=True)
    assert read(db)["refreshRequired"] and not any(read(db)["ready"].values())


def test_empty_report_is_confirmed_zero_but_missing_or_clipped_past_is_not(db):
    seller_summary(db, SCOPE, PERIOD, [])
    assert read(db)["selectedTotals"]["visits"] == 0
    assert read(db)["previousTotals"]["visits"] is None
    product = native_product(PERIOD)
    product["statistic"]["past"]["period"]["start"] = "2026-10-05"
    seller_summary(db, SCOPE, PERIOD, [product])
    assert read(db)["previousTotals"]["visits"] is None


def test_missing_aggregate_summary_loads_even_when_local_catalog_is_empty(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a, **kw: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    # A confirmed empty CURRENT catalog can still have historical report rows.
    # Catalog bootstrap itself is covered by the separate recovery regressions.
    from app.wb_sales_funnel_catalog import SOURCE as CATALOG_SOURCE
    now = datetime.now(timezone.utc)
    funnel.upsert(db, 1, [{"source_key": f"{SCOPE}_catalog_v1", "fetched_at": now,
        "payload": {"source": CATALOG_SOURCE, "account": 1, "products": 0,
                    "completeListing": True, "fetchedAt": now.isoformat()}}])
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [native_product(PERIOD, 99)], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    for _ in range(2):
        funnel.collect(1, 1, SCOPE, str(PERIOD.date_from), str(PERIOD.date_to), "job")
    assert len(calls) == 1 and calls[0]["nmIds"] == []
    page = funnel.read_page(db, 1, 1, SCOPE, PERIOD)
    assert page["total"] == 0 and page["summaryProducts"] == 1
    assert page["totals"]["visits"] == 100 and not page["refreshRequired"]
