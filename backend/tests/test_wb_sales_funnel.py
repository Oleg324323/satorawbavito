from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import wb_sales_funnel as funnel
from app.platform.period import Period


def wb_product(nm=1, **fields):
    selected = dict(openCount=100, cartCount=20, orderCount=8, orderSum=23434.15,
                    buyoutCount=4, buyoutSum=10000, cancelCount=6)
    selected.update(fields)
    return {"product": {"nmId": nm}, "statistic": {"selected": selected}}


def observation_rows(*args, **kwargs):
    kwargs.setdefault("seller_wide", True)
    return funnel.observation_rows(*args, **kwargs)


def evidence(start, end, **values):
    return {**funnel.normalized_metrics(wb_product(**values)), "revenueSharePopulation": funnel.SHARE_POPULATION,
            "dateFrom": str(start), "dateTo": str(end),
            "fetchedAt": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    funnel.Cache.__table__.create(engine)
    funnel.Product.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(funnel, "get_session_factory", lambda: factory)
    with factory() as session:
        yield session
    engine.dispose()


def catalog(db, count=1, org=1, account=1):
    for nm in range(1, count+1):
        db.add(funnel.Product(organization_id=org, marketplace_account_id=account,
                             external_product_id=str(nm), seller_article=f"sku-{nm}",
                             title=f"Товар {nm}", brand="A" if nm % 2 else "B"))
    db.commit()


def seller_summary(db, scope, period, products, *, now=None, org=1, account=1):
    from app.wb_sales_funnel_totals import SellerTotals
    totals = SellerTotals(period)
    totals.add(products, currency="RUB")
    totals.save(db, org, account, scope, now or datetime.now(timezone.utc))


def test_mapping_distinguishes_missing_zero_and_rubles():
    values = funnel.normalized_metrics(wb_product(openCount=0, cartCount=None, orderCount=False))
    assert values["visits"] == 0
    assert values["carts"] is None and values["orders"] is None
    assert values["orderAmount"] == 2343415
    assert values["impressions"] is None  # Never substitute ad views or opens.
    assert funnel.normalized_metrics(wb_product(orderSum="NaN"))["orderAmount"] is None


def test_saved_page_summary_uses_a_bounded_number_of_catalog_scans(db):
    from sqlalchemy import event
    period = Period(date(2026, 10, 11), date(2026, 10, 14))
    catalog(db, 100)
    scope = funnel.scope_key(1, "synthetic")
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to,
        [wb_product(nm, shareOrderPercent=1) for nm in range(1, 101)], datetime.now(timezone.utc)))
    funnel.project(db, 1, 1, scope, period)
    seller_summary(db, scope, period, [wb_product(nm) for nm in range(1, 101)])
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if "funnel_catalog" in statement:
            statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = funnel.read_page(db, 1, 1, scope, period, page=2, page_size=20)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert len(statements) == 3  # summary, identity/coverage, requested page
    assert result["total"] == result["coveredProducts"] == 100
    assert result["totals"]["visits"] == 10_000
    assert result["totals"]["cartConversion"] == 20
    assert len(result["items"]) == 20


def test_source_period_must_match_request():
    product = wb_product()
    product["statistic"]["selected"]["period"] = {"start": "2026-10-01", "end": "2026-10-06"}
    with pytest.raises(ValueError, match="PERIOD_MISMATCH"):
        observation_rows("synthetic", date(2026, 10, 1), date(2026, 10, 9), [product], datetime.now(timezone.utc))
    row = observation_rows("synthetic", date(2026, 10, 1), date(2026, 10, 6), [product], datetime.now(timezone.utc), currency="BYN")[0]["payload"]
    assert row["orders"] == 8 and row["orderAmount"] is None


@pytest.mark.parametrize("raw,expected", [(3.7, 3.7), (0, 0), (None, None), (True, None), ("NaN", None), ("Infinity", None), (-1, None), (101, None)])
def test_revenue_share_maps_wb_value_without_recalculation(raw, expected):
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    row = evidence(period.date_from, period.date_to, shareOrderPercent=raw)
    assert row["wbRevenueShare"] == expected
    metrics, _ = funnel.aggregate(period, [row])
    assert metrics["revenueShare"] == expected
    assert metrics["revenueShareSourceMapped"]


def test_revenue_share_requires_exact_period_and_never_combines_percentages():
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    parts = [evidence(period.date_from, date(2026, 10, 6), shareOrderPercent=10),
             evidence(date(2026, 10, 7), period.date_to, shareOrderPercent=20)]
    metrics, gaps = funnel.aggregate(period, parts)
    assert not gaps and metrics["revenueShare"] is None
    assert not metrics["revenueShareSourceMapped"]
    exact = evidence(period.date_from, period.date_to, shareOrderPercent=3.7)
    metrics, _ = funnel.aggregate(period, [*parts, exact])
    assert metrics["revenueShare"] == 3.7
    assert funnel.aggregate(Period(date(2026, 10, 2), period.date_to), [exact])[0]["revenueShare"] is None


def test_revenue_share_visible_with_partial_catalog_and_sorted_by_wb_share(db):
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    catalog(db, 3)
    scope = funnel.scope_key(1, "synthetic")
    products = [wb_product(1, orderSum=1000, shareOrderPercent=0), wb_product(2, orderSum=100, shareOrderPercent=3.7)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, products, datetime.now(timezone.utc)))
    funnel.project(db, 1, 1, scope, period)
    result = funnel.read_page(db, 1, 1, scope, period, sort="revenueShare", direction="desc")
    assert not result["complete"]  # Product 3 has no WB observation.
    assert [(item["nmId"], item["metrics"]["revenueShare"]) for item in result["items"]] == [("2", 3.7), ("1", 0), ("3", None)]
    filtered = funnel.read_page(db, 1, 1, scope, period, query="sku-2")
    assert filtered["items"][0]["metrics"]["revenueShare"] == 3.7


@pytest.mark.parametrize("filters,trusted", [
    ({}, True), ({"nmIds": [], "skipDeletedNm": False}, True),
    ({"nmIds": [1]}, False), ({"brandNames": ["A"]}, False),
    ({"subjectIds": [1]}, False), ({"tagIds": [1]}, False),
    ({"skipDeletedNm": True}, False),
])
def test_share_population_requires_unfiltered_wb_request(filters, trusted):
    assert funnel.seller_wide_request(filters) is trusted


def test_newer_batch_share_cannot_override_seller_share():
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    now = datetime.now(timezone.utc)
    seller = observation_rows("synthetic", period.date_from, period.date_to,
                              [wb_product(shareOrderPercent=3.7)], now-timedelta(minutes=2))[0]["payload"]
    batch = funnel.observation_rows("synthetic", period.date_from, period.date_to,
                                   [wb_product(orderCount=9, shareOrderPercent=100)], now)[0]["payload"]
    values, _ = funnel.aggregate(period, [seller, batch], now=now)
    assert values["orders"] == 9  # Count freshness is independent of the share.
    assert values["revenueShare"] == 3.7 and values["revenueShareFresh"]
    assert funnel.aggregate(period, [batch], now=now)[0]["revenueShare"] is None
    seller["fetchedAt"] = (now-timedelta(days=2)).isoformat()
    assert not funnel.aggregate(period, [seller, batch], now=now)[0]["revenueShareFresh"]


def test_old_batch_projection_is_not_exposed_as_seller_share(db):
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    catalog(db)
    scope = funnel.scope_key(1, "synthetic")
    rows = observation_rows(scope, period.date_from, period.date_to, [wb_product(shareOrderPercent=3.7)], datetime.now(timezone.utc))
    funnel.upsert(db, 1, rows)
    funnel.project(db, 1, 1, scope, period)
    projection = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key.contains("_page_")))
    projection.payload = {**projection.payload, "revenueShare": 100, "revenueShareSourceMapped": True,
                          "revenueSharePopulation": None}
    db.commit()
    page = funnel.read_page(db, 1, 1, scope, period, sort="revenueShare")
    assert page["items"][0]["metrics"]["revenueShare"] is None
    assert page["items"][0]["metrics"]["orders"] == 8
    assert not page["complete"]


def test_seller_share_collection_paginates_without_product_filter_and_reopens_from_cache(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    catalog(db, 1001)
    scope = funnel.scope_key(1, "synthetic")
    # Older batch-local observation is not acceptable revenue-share evidence.
    funnel.upsert(db, 1, funnel.observation_rows(scope, period.date_from, period.date_to,
                   [wb_product(shareOrderPercent=100)], datetime.now(timezone.utc)))
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        body = request.jsonBody
        calls.append(body)
        assert body["nmIds"] == [] and not body["skipDeletedNm"]
        assert body["selectedPeriod"] == {"start": "2026-10-01", "end": "2026-10-09"}
        # Last product belongs to the seller, but is not in our local catalog.
        all_products = [wb_product(nm, shareOrderPercent=3.7 if nm == 1 else 0) for nm in range(1, 1003)]
        return {"data": {"products": all_products[body["offset"]:body["offset"]+1000], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert [body["offset"] for body in calls] == [0, 1000]
    page = funnel.read_page(db, 1, 1, scope, period, query="sku-1", sort="revenueShare")
    assert page["items"][0]["metrics"]["revenueShare"] == 3.7
    all_rows = funnel.read_page(db, 1, 1, scope, period)
    assert all_rows["total"] == 1001 and all_rows["complete"]
    assert all_rows["totals"]["orders"] == 1002*8  # Include WB cards outside the local catalog.
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 2


def test_exact_source_percentage_preserves_authoritative_zero_and_rounding():
    period = Period(date(2026, 10, 1), date(2026, 10, 1))
    product = wb_product(**{key: 0 for key in funnel.WB_FIELDS.values()})
    product["statistic"]["selected"]["conversions"] = {"buyoutPercent": 0, "addToCartPercent": 0, "cartToOrderPercent": 0}
    row = observation_rows("synthetic", period.date_from, period.date_to, [product], datetime.now(timezone.utc))[0]["payload"]
    values, gaps = funnel.aggregate(period, [row])
    assert not gaps and values["buyoutPercent"] == 0 and values["cartConversion"] == 0


def test_period_ratios_are_weighted_and_exclude_in_transit_orders():
    period = Period(date(2026, 10, 1), date(2026, 10, 2))
    one = evidence(period.date_from, period.date_from, openCount=100, cartCount=20, buyoutCount=34, cancelCount=89)
    two = evidence(period.date_to, period.date_to, openCount=900, cartCount=90, buyoutCount=0, cancelCount=0)
    result, _ = funnel.aggregate(period, [one, two])
    assert result["cartConversion"] == 11  # Not mean(20%, 10%).
    assert result["buyoutPercent"] == 28  # 34/(34+89); not buyouts/orders.
    assert result["orderAmount"] == 4686830
    result, _ = funnel.aggregate(period, [one])
    assert result["orderConversion"] is None  # Do not call a partial ratio final.


def test_only_missing_interval_is_requested_and_no_overlap_is_summed():
    period = Period(date(2026, 10, 1), date(2026, 10, 10))
    one = evidence(period.date_from, date(2026, 10, 7))
    value, missing = funnel.aggregate(period, [one, one])
    assert value["orders"] == 8
    assert missing == [(date(2026, 10, 8), date(2026, 10, 10))]
    # A broader range cannot be used for a narrower requested report.
    narrow, gaps = funnel.aggregate(Period(date(2026, 10, 2), date(2026, 10, 6)), [one])
    assert narrow["orders"] is None and gaps


def test_zero_activity_is_covered_but_partial_or_stale_metrics_are_not():
    period = Period(date(2026, 10, 1), date(2026, 10, 1))
    zero = evidence(period.date_from, period.date_to, **{key: 0 for key in funnel.WB_FIELDS.values()})
    assert funnel.aggregate(period, [zero])[1] == []
    assert funnel.aggregate(period, [zero])[0]["orders"] == 0
    assert funnel.aggregate(period, [zero])[0]["orderConversion"] is None  # 0/0 undefined.
    zero["cancels"] = None
    assert funnel.aggregate(period, [zero])[1]
    zero["cancels"] = 0
    zero["fetchedAt"] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    values, missing = funnel.aggregate(period, [zero])
    assert values["orders"] == 0 and not values["complete"] and missing


def test_sql_pagination_totals_filters_and_account_isolation(db):
    period = Period(date(2026, 10, 1), date(2026, 10, 2))
    catalog(db, 170)
    catalog(db, 2, org=2, account=2)
    catalog(db, 1, org=1, account=3)
    scope = funnel.scope_key(1, "synthetic")
    products = [wb_product(nm, shareOrderPercent=3.7) for nm in range(1, 171)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, products, datetime.now(timezone.utc)))
    assert funnel.project(db, 1, 1, scope, period) == {}
    seller_summary(db, scope, period, products)
    result = funnel.read_page(db, 1, 1, scope, period, page_size=150, page=2)
    assert result["total"] == 170 and len(result["items"]) == 20
    assert result["totals"]["orders"] == 170*8
    assert result["items"][0]["metrics"]["revenueShare"] == 3.7
    result = funnel.read_page(db, 1, 1, scope, period, brand="A")
    assert result["total"] == 85 and result["totals"]["orders"] == 85*8
    assert result["items"][0]["metrics"]["revenueShare"] == 3.7  # Filtering does not change WB's share.
    result = funnel.read_page(db, 1, 3, funnel.scope_key(3, "synthetic"), period)
    assert result["total"] == 1 and result["items"][0]["metrics"].get("orders") is None


def test_collection_reuses_coverage_and_fetches_only_missing_product_dates(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 1), date(2026, 10, 10))
    catalog(db, 2)
    scope = funnel.scope_key(1, "synthetic")
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, date(2026, 10, 7), [wb_product(1), wb_product(2)], datetime.now(timezone.utc)))
    # Exact-period shares are already available; only count/amount gaps load.
    share_only = [wb_product(nm, **{key: None for key in funnel.WB_FIELDS.values()}, shareOrderPercent=3.7) for nm in (1, 2)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, share_only, datetime.now(timezone.utc)))
    seller_summary(db, scope, period, [wb_product(nm, orderCount=16) for nm in (1, 2)])
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [wb_product(nm) for nm in request.jsonBody["nmIds"]], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 1
    assert calls[0]["selectedPeriod"] == {"start": "2026-10-08", "end": "2026-10-10"}
    assert calls[0]["nmIds"] == [1, 2]
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 1  # Fully covered reopening: no provider read.
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["complete"] and result["totals"]["orders"] == 32


def test_failed_download_preserves_partial_data_and_does_not_cover_gap(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 1), date(2026, 10, 2))
    catalog(db)
    scope = funnel.scope_key(1, "synthetic")
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_from, [wb_product()], datetime.now(timezone.utc)))
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError):
        funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["totals"]["orders"] is None and not result["complete"]
    assert result["items"][0]["metrics"]["orders"] == 8  # Saved card data remains visible.


def test_omitted_products_are_not_zero_or_an_endless_running_job(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 1), date(2026, 10, 8))
    catalog(db, 3)
    scope = funnel.scope_key(1, "synthetic")
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    states = []
    monkeypatch.setattr(store, "save_source_cache", lambda org, key, payload, **kw: states.append(payload))
    # Product 1 is explicitly zero; 2 is omitted; 3 has a missing amount.
    products = [wb_product(1, **{key: 0 for key in funnel.WB_FIELDS.values()}, shareOrderPercent=0),
                wb_product(3, orderSum=None, shareOrderPercent=3.7)]
    calls = []
    def fetch(*args):
        calls.append(args)
        return {"data": {"products": products, "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert states[-1]["status"] == "partial"
    assert states[-1]["missingProducts"] == 2 and states[-1]["omittedProducts"] == 1
    assert "Загрузка завершена" in states[-1]["message"]
    result = funnel.read_page(db, 1, 1, scope, period)
    assert not result["complete"] and result["coveredProducts"] == 1
    metrics = {item["nmId"]: item["metrics"] for item in result["items"]}
    assert metrics["1"]["orders"] == 0
    assert metrics["2"]["orders"] is None
    assert metrics["3"]["orderAmount"] is None
    assert result["refreshRequired"] is False and result["unreportedProducts"] == 1
    assert result["reportComplete"] is False  # Product 3 lacks an actual field.
    assert result["totals"]["cartConversion"] == 20
    assert result["conversionCoverage"]["cartConversion"]["products"] == 2
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 1  # Omitted products are settled until source expiry.
    db.add(funnel.Product(organization_id=1, marketplace_account_id=1, external_product_id="4", title="New"))
    db.commit()
    funnel.project(db, 1, 1, scope, period)
    assert funnel.read_page(db, 1, 1, scope, period)["refreshRequired"] is True


def test_exhausted_report_is_complete_without_inventing_omitted_catalog_metrics(db):
    period = Period(date(2026, 10, 1), date(2026, 10, 7))
    catalog(db, 3)
    scope = funnel.scope_key(1, "synthetic")
    now = datetime.now(timezone.utc)
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, [wb_product(1)], now))
    funnel.project(db, 1, 1, scope, period)
    assert not funnel.read_page(db, 1, 1, scope, period)["reportComplete"]
    funnel.save_enumeration(db, 1, 1, scope, period, {1}, now)
    seller_summary(db, scope, period, [wb_product(1)])
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["reportComplete"] and not result["refreshRequired"]
    assert not result["complete"] and result["coveredProducts"] == 1  # Catalog coverage is still honest.
    assert result["unreportedProducts"] == 2
    assert result["totals"]["visits"] == 100
    assert {item["nmId"]: item["inReport"] for item in result["items"]} == {"1": True, "2": False, "3": False}
    assert all(item["metrics"]["visits"] is None for item in result["items"] if not item["inReport"])


def test_summary_cr_does_not_silently_drop_a_source_product_with_unknown_visits(db):
    period = Period(date(2026, 10, 1), date(2026, 10, 7))
    catalog(db, 3)
    scope = funnel.scope_key(1, "synthetic")
    products = [wb_product(1, openCount=439330, cartCount=23643),
                wb_product(2, openCount=None, cartCount=900)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, products, datetime.now(timezone.utc)))
    funnel.project(db, 1, 1, scope, period)
    seller_summary(db, scope, period, products)
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["totals"]["cartConversion"] is None
    assert result["conversionCoverage"]["cartConversion"] == {"products": 2, "total": 2, "visits": None, "carts": 24543}
    assert not result["complete"] and result["refreshRequired"]


@pytest.mark.parametrize("offsets", [[0], [0, 2], [0, 1, 2]])
def test_complete_raw_pagination_settles_omissions_without_redownload(db, offsets):
    import json
    from hashlib import sha256
    from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, build_wb_analytics_client
    period = Period(date(2026, 10, 1), date(2026, 10, 7))
    catalog(db, 3)
    token, now = "synthetic", datetime.now(timezone.utc)
    scope = funnel.scope_key(1, token)
    host = build_wb_analytics_client("complete", token_override=token).base_url
    for offset in offsets:
        request = WbApiRequest(method="POST", path=funnel.SOURCE, jsonBody={
            "selectedPeriod": {"start": str(period.date_from), "end": str(period.date_to)},
            "nmIds": [], "skipDeletedNm": False, "limit": 1, "offset": offset})
        products = [wb_product(offset+1)] if offset < 2 else []
        response = WbApiResponseEnvelope(request=request, statusCode=200, ok=True,
            data={"data": {"products": products, "currency": "RUB"}}).model_dump(mode="json")
        identity = json.dumps([host, sha256(token.encode()).hexdigest(), request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
        funnel.upsert(db, 1, [{"source_key": "wb_report_http_" + sha256(identity.encode()).hexdigest(),
            "payload": {"response": response}, "fetched_at": now, "range_date_from": None, "range_date_to": None}])
    funnel.import_checkpoints(db, 1, scope, token, period, 1)
    funnel.project(db, 1, 1, scope, period)
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["refreshRequired"] is (offsets != [0, 1, 2])
    if offsets == [0, 1, 2]:
        assert result["unreportedProducts"] == 1 and not result["complete"]
        receipt = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == funnel.enumeration_key(scope, period)))
        receipt.payload = {**receipt.payload, "fetchedAt": (now-timedelta(days=2)).isoformat()}
        db.commit()
        assert funnel.read_page(db, 1, 1, scope, period)["refreshRequired"] is True


def test_settled_missing_products_do_not_enqueue_again(db, monkeypatch):
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    period = Period(date(2026, 10, 1), date(2026, 10, 7))
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, "scope", period))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": False, "total": 3, "refreshRequired": False})
    monkeypatch.setattr(router, "get_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(router, "get_redis_client", lambda: pytest.fail("Settled report must not enqueue"))
    assert router.refresh_funnel(Request({"type": "http"}), period.date_from, period.date_to, None) == {"status": "partial", "settled": True}


def test_collection_fetches_exact_period_share_once_for_fragmented_cache(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 1), date(2026, 10, 9))
    catalog(db)
    scope = funnel.scope_key(1, "synthetic")
    for first, last in ((period.date_from, date(2026, 10, 6)), (date(2026, 10, 7), period.date_to)):
        funnel.upsert(db, 1, observation_rows(scope, first, last, [wb_product(shareOrderPercent=10)], datetime.now(timezone.utc)))
    assert funnel.project(db, 1, 1, scope, period) == {(period.date_from, period.date_to): [1]}
    assert not funnel.read_page(db, 1, 1, scope, period)["complete"]
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [wb_product(shareOrderPercent=3.7)], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 1
    assert calls[0]["selectedPeriod"] == {"start": "2026-10-01", "end": "2026-10-09"}
    result = funnel.read_page(db, 1, 1, scope, period)
    assert result["complete"] and result["items"][0]["metrics"]["revenueShare"] == 3.7
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "test-job")
    assert len(calls) == 1


@pytest.mark.parametrize("seller_wide", [True, False])
def test_credential_scoped_raw_checkpoints_are_reused_but_not_other_accounts(db, seller_wide):
    import json
    from hashlib import sha256
    from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, build_wb_analytics_client
    period = Period(date(2026, 10, 1), date(2026, 10, 2))
    token = "synthetic-current-credential"
    host = build_wb_analytics_client("complete", token_override=token).base_url
    request = WbApiRequest(method="POST", path=funnel.SOURCE, jsonBody={"selectedPeriod": {"start": "2026-10-01", "end": "2026-10-02"}, "nmIds": [] if seller_wide else [1], "skipDeletedNm": False})
    response = WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"products": [wb_product(shareOrderPercent=3.7)], "currency": "RUB"}}).model_dump(mode="json")
    def key(secret):
        identity = json.dumps([host, sha256(secret.encode()).hexdigest(), request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
        return "wb_report_http_" + sha256(identity.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    def checkpoint(secret):
        return {"source_key": key(secret), "payload": {"response": response}, "fetched_at": now,
                "range_date_from": None, "range_date_to": None}
    funnel.upsert(db, 1, [checkpoint("other-account")])
    scope = funnel.scope_key(1, token)
    funnel.import_checkpoints(db, 1, scope, token, period)
    assert not db.scalars(select(funnel.Cache).where(funnel.Cache.source_key.startswith(scope))).all()
    legacy = observation_rows(scope, period.date_from, period.date_to, [wb_product()], now, seller_wide=False)
    legacy[0]["payload"].pop("wbRevenueShare")
    funnel.upsert(db, 1, legacy)
    funnel.upsert(db, 1, [checkpoint(token)])
    funnel.import_checkpoints(db, 1, scope, token, period)
    rows = db.scalars(select(funnel.Cache).where(funnel.Cache.source_key.startswith(scope))).all()
    assert len(rows) == (2 if seller_wide else 1)
    imported = next(row for row in rows if row.payload.get("wbRevenueShare") == 3.7)
    assert imported.payload["orderAmount"] == 2343415
    assert imported.payload["revenueSharePopulation"] == (funnel.SHARE_POPULATION if seller_wide else None)
    projected, _ = funnel.aggregate(period, [row.payload for row in rows])
    assert projected["revenueShare"] == (3.7 if seller_wide else None)


def test_concurrent_refreshes_enqueue_once_and_sorting_only_reads(db, monkeypatch):
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    from app.repricer_tasks import refresh_sales_funnel
    period = Period(date(2026, 10, 1), date(2026, 10, 2))
    scope = funnel.scope_key(1, "synthetic")
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, scope, period))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": False, "total": 1})
    monkeypatch.setattr(router, "touch_demand", lambda *a, **kw: None)
    locks, cache, queued = {}, {}, []
    class Redis:
        def set(self, key, value, **kwargs):
            if key in locks:
                return False
            locks[key] = value
            return True
        def exists(self, key):
            return key in locks
    monkeypatch.setattr(router, "get_redis_client", Redis)
    monkeypatch.setattr(router, "save_source_cache", lambda org, key, payload, **kw: cache.update({key: payload}))
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **kw: cache.get(key))
    monkeypatch.setattr(refresh_sales_funnel, "apply_async", lambda **kw: queued.append(kw))
    request = Request({"type": "http"})
    router.refresh_funnel(request, period.date_from, period.date_to, None)
    router.refresh_funnel(request, period.date_from, period.date_to, None)
    assert len(queued) == 1
    router.get_funnel(request, period.date_from, period.date_to, None, "", "", "visits", "asc", 2, 50)
    assert len(queued) == 1


def test_static_funnel_route_precedes_generic_report_route():
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from app.main import create_app
    async def check():
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.get("/api/wb/reports/sales-funnel?dateFrom=2026-10-01&dateTo=2026-10-09")
            assert response.status_code == 401  # Auth, not report_id Literal 422.
    asyncio.run(check())


def test_rate_limit_reschedules_without_losing_lease_or_partial_evidence(monkeypatch):
    from celery.exceptions import Retry
    from fastapi import HTTPException
    from app import repricer_tasks
    from app.infra import redis_client
    events, saved = [], []
    class Lock:
        def acquire(self, **kw):
            return True
        def extend(self, *a, **kw):
            events.append("extend")
        def release(self):
            events.append("release")
    class Redis:
        def get(self, key):
            return b"owner"
        def lock(self, *a, **kw):
            return Lock()
        def expire(self, key, ttl):
            events.append(("expire", ttl))
        def eval(self, *a):
            events.append("delete")
    monkeypatch.setattr(redis_client, "get_redis_client", Redis)
    monkeypatch.setattr(funnel, "collect", lambda *a, **kw: (_ for _ in ()).throw(HTTPException(429, {"details": {"rateLimit": {"retryAfterSeconds": 1800}}})))
    monkeypatch.setattr(repricer_tasks, "save_source_cache", lambda org, key, payload, **kw: saved.append(payload))
    def retry(**kw):
        events.append(("retry", kw["countdown"]))
        raise Retry()
    monkeypatch.setattr(repricer_tasks.refresh_sales_funnel, "retry", retry)
    with pytest.raises(Retry):
        repricer_tasks.refresh_sales_funnel.run(1, 1, "scope", "2026-10-01", "2026-10-09", "job", "lease", "owner")
    assert saved[-1]["status"] == "queued" and saved[-1]["retryAfterSeconds"] == 1801
    assert ("expire", 1921) in events and "delete" not in events


@pytest.mark.parametrize("start,end,expected", [
    ("2026-10-10", "2026-10-15", ("2026-10-04", "2026-10-09")),
    ("2026-01-01", "2026-01-01", ("2025-12-31", "2025-12-31")),
    ("2024-03-01", "2024-03-02", ("2024-02-28", "2024-02-29")),
])
def test_previous_period_is_inclusive_calendar_arithmetic(start, end, expected):
    previous = funnel.previous_period(Period(date.fromisoformat(start), date.fromisoformat(end)))
    assert (str(previous.date_from), str(previous.date_to)) == expected


def native_product(period, nm=1, **past_fields):
    product = wb_product(nm, shareOrderPercent=3.2)
    product["statistic"]["selected"]["period"] = {"start": str(period.date_from), "end": str(period.date_to)}
    previous = funnel.previous_period(period)
    product["statistic"]["past"] = wb_product(nm, shareOrderPercent=3.1, **past_fields)["statistic"]["selected"]
    product["statistic"]["past"]["period"] = {"start": str(previous.date_from), "end": str(previous.date_to)}
    # Literal provider values intentionally differ from local growth formulas.
    product["statistic"]["comparison"] = {"openCountDynamic": -7, "cartCountDynamic": -3,
        "orderCountDynamic": 25, "orderSumDynamic": 26, "buyoutCountDynamic": 0,
        "buyoutSumDynamic": -150, "shareOrderPercentDynamic": 3,
        "conversions": {"addToCartPercent": 8, "cartToOrderPercent": -14, "buyoutPercent": 17}}
    return product


def test_native_past_values_keep_units_zero_and_seller_population():
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    product = native_product(period, openCount=0, orderSum=12.34, cartCount=None,
                             conversions={"addToCartPercent": 20, "buyoutPercent": 0})
    rows = funnel.past_observation_rows("scope", period, [product], datetime.now(timezone.utc), seller_wide=True)
    row = rows[0]["payload"]
    assert (row["dateFrom"], row["dateTo"]) == ("2026-10-04", "2026-10-09")
    assert row["visits"] == 0 and row["carts"] is None
    assert row["orderAmount"] == 1234 and row["wbcartConversion"] == 20
    assert row["wbRevenueShare"] == 3.1 and row["revenueSharePopulation"] == funnel.SHARE_POPULATION
    foreign = funnel.past_observation_rows("scope", period, [product], datetime.now(timezone.utc), currency="BYN")[0]["payload"]
    assert foreign["orderAmount"] is None and foreign["visits"] == 0
    assert foreign["revenueSharePopulation"] is None


@pytest.mark.parametrize("invalid", [None, {}, {"start": "2026-10-05", "end": "2026-10-09"}])
def test_native_past_retention_clipping_or_missing_period_does_not_poison_current(invalid):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    product = native_product(period)
    product["statistic"]["past"]["period"] = invalid
    assert funnel.past_observation_rows("scope", period, [product], datetime.now(timezone.utc)) == []
    assert observation_rows("scope", period.date_from, period.date_to, [product], datetime.now(timezone.utc))[0]["payload"]["visits"] == 100


def test_native_checkpoint_past_is_reused_without_provider_request(db, monkeypatch):
    import json
    from hashlib import sha256
    from app import repricer_bff
    from app.repricer_cache import store
    from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, build_wb_analytics_client
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    previous = funnel.previous_period(period)
    catalog(db)
    token, now = "synthetic", datetime.now(timezone.utc)
    scope = funnel.scope_key(1, token)
    host = build_wb_analytics_client("complete", token_override=token).base_url
    request = WbApiRequest(method="POST", path=funnel.SOURCE, jsonBody={
        "selectedPeriod": {"start": str(period.date_from), "end": str(period.date_to)},
        "pastPeriod": {"start": str(previous.date_from), "end": str(previous.date_to)}, "nmIds": [], "skipDeletedNm": False,
        "limit": 1000, "offset": 0})
    response = WbApiResponseEnvelope(request=request, statusCode=200, ok=True,
        data={"data": {"products": [native_product(period, openCount=50)], "currency": "RUB"}}).model_dump(mode="json")
    identity = json.dumps([host, sha256(token.encode()).hexdigest(), request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
    funnel.upsert(db, 1, [{"source_key": "wb_report_http_" + sha256(identity.encode()).hexdigest(),
        "payload": {"response": response}, "fetched_at": now, "range_date_from": None, "range_date_to": None}])
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", token))
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", lambda *a: pytest.fail("Valid native past evidence must be reused"))
    for _ in range(2):
        funnel.collect(1, 1, scope, str(previous.date_from), str(previous.date_to), "job")
    page = funnel.read_page(db, 1, 1, scope, previous)
    assert page["complete"] and page["totals"]["visits"] == 50
    for _ in range(2):
        funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "native-job", native_comparison=True)
    comparison = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert comparison["items"][0]["dynamics"]["visits"] == -7
    assert comparison["complete"]


def test_selected_collection_persists_native_past_with_no_second_provider_call(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    previous = funnel.previous_period(period)
    scope = funnel.scope_key(1, "synthetic")
    catalog(db)
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [native_product(period, openCount=50)], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "selected-job")
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert result["complete"] and not result["refreshRequired"]
    assert result["items"][0]["metrics"]["visits"] == 50
    assert result["items"][0]["dynamics"]["visits"] == -7
    assert result["items"][0]["ready"]["visits"]
    funnel.collect(1, 1, scope, str(previous.date_from), str(previous.date_to), "past-job")
    assert len(calls) == 1
    assert calls[0]["pastPeriod"] == {"start": "2026-10-04", "end": "2026-10-09"}


def test_native_comparison_matches_nm_ids_without_previous_projection_and_never_calculates_totals(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    previous = funnel.previous_period(period)
    scope, now = funnel.scope_key(1, "synthetic"), datetime.now(timezone.utc)
    catalog(db, 3)
    products = [native_product(period, 2, openCount=50), native_product(period, 1, openCount=80)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, products, now))
    funnel.upsert(db, 1, funnel.native_rows(scope, period, products, now, seller_wide=True, request_identity="synthetic"))
    funnel.project(db, 1, 1, scope, period)
    funnel.save_enumeration(db, 1, 1, scope, period, {1, 2}, now)
    result = funnel.read_comparison(db, 1, 1, scope, period, [2, 1, 3])
    by_nm = {item["nmId"]: item for item in result["items"]}
    assert by_nm["1"]["metrics"]["revenueShare"] == 3.1
    assert by_nm["2"]["metrics"]["visits"] == 50 and by_nm["2"]["ready"]["visits"]
    assert by_nm["1"]["dynamics"]["visits"] == by_nm["2"]["dynamics"]["visits"] == -7
    assert not by_nm["3"]["ready"]["visits"]
    assert all(value is None for value in result["totals"].values())
    assert funnel.read_page(db, 1, 1, scope, period, query="sku-1")["totals"]["cartConversion"] == 20  # Filtered card calculation is unchanged.
    assert funnel.read_page(db, 1, 1, scope, previous)["coveredProducts"] == 0  # No previous projection is required.
    assert result["refreshRequired"] is False  # Provider-omitted current card 3 is not a zero or endless reload.
    assert not funnel.read_comparison(db, 1, 9, funnel.scope_key(9, "synthetic"), period, [1])["items"]
    foreign_credential = funnel.read_comparison(db, 1, 1, funnel.scope_key(1, "rotated"), period, [1])
    assert not any(foreign_credential["items"][0]["ready"].values())
    assert not funnel.read_comparison(db, 2, 1, scope, period, [1])["items"]


def test_comparison_does_not_promote_partial_or_stale_values_to_complete(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    previous = funnel.previous_period(period)
    scope, now = funnel.scope_key(1, "synthetic"), datetime.now(timezone.utc)
    catalog(db)
    product = native_product(period)
    product["statistic"]["comparison"]["shareOrderPercentDynamic"] = 0
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, [product], now))
    funnel.upsert(db, 1, funnel.native_rows(scope, period, [product], now, seller_wide=True, request_identity="synthetic"))
    funnel.project(db, 1, 1, scope, period)
    projection = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == f"{scope}_page_{period.cache_key}_1"))
    projection.payload = {**projection.payload, "freshFieldCoverage": {**projection.payload["freshFieldCoverage"], "orders": False}}
    db.commit()
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert not result["items"][0]["ready"]["orders"]
    assert result["items"][0]["ready"]["revenueShare"]  # Confirmed zero is still known.
    assert result["items"][0]["ready"]["visits"]
    projection.fetched_at = now - timedelta(days=2)
    db.commit()
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert not any(result["items"][0]["ready"].values())


def test_comparison_router_reads_only_selected_account_and_tracks_native_pair_job(db, monkeypatch):
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    previous = funnel.previous_period(period)
    scope = funnel.scope_key(1, "synthetic")
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, scope, period))
    catalog(db)
    reads, leases = [], []
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **kw: reads.append(key))
    monkeypatch.setattr(router, "touch_demand", lambda request, actor, key: leases.append(key))
    # A GET must not refresh projections or write facts/jobs.
    monkeypatch.setattr(funnel, "upsert", lambda *a, **kw: pytest.fail("Comparison GET must be read-only"))
    monkeypatch.setattr(funnel, "collect", lambda *a, **kw: pytest.fail("Comparison GET must not collect"))
    result = router.get_funnel_comparison(Request({"type": "http"}), period.date_from, period.date_to, None, [1])
    assert result["selectedDateFrom"] == "2026-10-10" and result["dateFrom"] == "2026-10-04"
    assert reads == leases == [funnel.job_key(scope, period, True)]
    assert not result["items"][0]["ready"]["orders"]


def save_native_product(db, period, product, *, seller_wide=True, now=None):
    now = now or datetime.now(timezone.utc)
    scope = funnel.scope_key(1, "synthetic")
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, [product], now))
    funnel.upsert(db, 1, funnel.native_rows(scope, period, [product], now, seller_wide=seller_wide, request_identity="synthetic-provider-response"))
    funnel.project(db, 1, 1, scope, period)
    return scope


def test_all_native_fields_map_literally_and_do_not_use_absolute_conversion_fields(db):
    period = Period(date(2026, 10, 26), date(2026, 10, 28))
    catalog(db)
    product = native_product(period, openCount=2714, cartCount=420)
    product["statistic"]["selected"].update(openCount=2525, cartCount=406,
        conversions={"addToCartPercent": 16, "cartToOrderPercent": 3, "buyoutPercent": 40})
    scope = save_native_product(db, period, product)
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    item = result["items"][0]
    assert item["dynamics"] == {"visits": -7, "carts": -3, "orders": 25, "orderAmount": 26,
        "buyouts": 0, "buyoutAmount": -150, "revenueShare": 3, "cartConversion": 8,
        "orderConversion": -14, "buyoutPercent": 17}
    assert item["selectedValues"]["cartConversion"] == 16
    assert all(item["ready"].values())
    assert all(value is None for value in result["totals"].values())
    assert result["source"] == funnel.NATIVE_SOURCE


@pytest.mark.parametrize("value", [None, True, False, "-7", "NaN", float("nan"), float("inf")])
def test_missing_or_invalid_native_percentage_is_unknown_without_local_fallback(db, value):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    product = native_product(period, openCount=50)
    product["statistic"]["comparison"]["openCountDynamic"] = value
    scope = save_native_product(db, period, product)
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    item = result["items"][0]
    assert item["metrics"]["visits"] == 50
    assert item["dynamics"]["visits"] is None and not item["ready"]["visits"]
    assert item["dynamics"]["carts"] == -3
    assert result["refreshRequired"] is False  # WB actually omitted this field; no endless re-fetch.


def test_native_zero_and_fractional_precision_are_not_reinterpreted(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    product = native_product(period, openCount=0)
    product["statistic"]["comparison"].update(openCountDynamic=0, cartCountDynamic=123.456789)
    scope = save_native_product(db, period, product)
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])["items"][0]
    assert result["selectedValues"]["visits"] == 100 and result["metrics"]["visits"] == 0
    assert result["dynamics"]["visits"] == 0 and result["ready"]["visits"]
    assert result["dynamics"]["carts"] == 123.456789


def test_filtered_native_share_never_becomes_seller_share(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    product = native_product(period)
    scope = save_native_product(db, period, product, seller_wide=False)
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    item = result["items"][0]
    assert item["ready"]["visits"] and item["dynamics"]["visits"] == -7
    assert not item["ready"]["revenueShare"] and item["dynamics"]["revenueShare"] is None
    assert result["refreshRequired"]  # The seller-wide share must come from its own population.


@pytest.mark.parametrize("corruption", ["source", "dateFrom", "previousDateFrom", "nmId", "requestIdentity", "fetchedAt"])
def test_wrong_native_provenance_pair_identity_or_freshness_is_not_exposed(db, corruption):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    scope = save_native_product(db, period, native_product(period))
    record = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == f"{funnel.native_prefix(scope, period)}_1_seller"))
    changes = {"source": "locally_calculated", "dateFrom": "2026-10-11", "previousDateFrom": "2026-10-05",
               "nmId": "2", "requestIdentity": None,
               "fetchedAt": (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()}
    record.payload = {**record.payload, corruption: changes[corruption]}; db.commit()
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert not any(result["items"][0]["ready"].values()) and result["refreshRequired"]


def test_native_delta_is_withheld_when_selected_snapshot_changes(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    scope = save_native_product(db, period, native_product(period))
    funnel.save_enumeration(db, 1, 1, scope, period, {1}, datetime.now(timezone.utc), native=True)
    projection = db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == f"{scope}_page_{period.cache_key}_1"))
    projection.payload = {**projection.payload, "visits": 101}; db.commit()
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert not result["items"][0]["ready"]["visits"] and result["items"][0]["ready"]["orders"]
    assert result["refreshRequired"]  # A prior completed listing cannot certify a different snapshot.


def test_native_cache_does_not_combine_subperiod_dynamics(db):
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    scope = funnel.scope_key(1, "synthetic")
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, [wb_product(shareOrderPercent=1)], datetime.now(timezone.utc)))
    funnel.project(db, 1, 1, scope, period)
    for window in (Period(period.date_from, date(2026, 10, 12)), Period(date(2026, 10, 13), period.date_to)):
        funnel.upsert(db, 1, funnel.native_rows(scope, window, [native_product(window)], datetime.now(timezone.utc), seller_wide=True, request_identity="synthetic"))
    result = funnel.read_comparison(db, 1, 1, scope, period, [1])
    assert not any(result["items"][0]["ready"].values()) and result["refreshRequired"]


def test_native_collection_reuses_combined_checkpoint_without_fetching_previous_period(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db)
    scope = save_native_product(db, period, native_product(period))
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", lambda *a: pytest.fail("Complete native cache must not fetch a second response"))
    for _ in range(2):
        funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "native-job", native_comparison=True)
    assert funnel.read_comparison(db, 1, 1, scope, period, [1])["complete"]


def test_native_only_collection_requests_selected_and_past_together_and_settles_omissions(db, monkeypatch):
    from app import repricer_bff
    from app.repricer_cache import store
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    catalog(db, 2)
    scope, now = funnel.scope_key(1, "synthetic"), datetime.now(timezone.utc)
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, [wb_product(nm, shareOrderPercent=3.2) for nm in (1, 2)], now))
    funnel.project(db, 1, 1, scope, period)
    monkeypatch.setattr(funnel, "resolve_bound_wb_credential", lambda *a, **kw: (1, "ref", "synthetic"))
    monkeypatch.setattr(funnel, "import_checkpoints", lambda *a: None)
    monkeypatch.setattr(store, "save_source_cache", lambda *a, **kw: None)
    calls = []
    def fetch(client, request):
        calls.append(request.jsonBody)
        return {"data": {"products": [native_product(period)], "currency": "RUB"}}
    monkeypatch.setattr(repricer_bff, "_request_or_raise_sales_funnel_products", fetch)
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "native-job", native_comparison=True)
    result = funnel.read_comparison(db, 1, 1, scope, period, [1, 2])
    assert calls[0]["selectedPeriod"] == {"start": "2026-10-10", "end": "2026-10-15"}
    assert calls[0]["pastPeriod"] == {"start": "2026-10-04", "end": "2026-10-09"}
    assert calls[0]["nmIds"] == [] and calls[0]["skipDeletedNm"] is False
    assert not result["refreshRequired"] and not result["complete"]
    assert not next(i for i in result["items"] if i["nmId"] == "2")["ready"]["visits"]
    funnel.collect(1, 1, scope, str(period.date_from), str(period.date_to), "native-job", native_comparison=True)
    assert len(calls) == 1


@pytest.mark.parametrize("selected", [
    {"complete": False, "total": 1, "refreshRequired": True},
    {"complete": True, "total": 1, "refreshRequired": False},
])
@pytest.mark.parametrize("status", ["running", "failed", "paused"])
def test_native_refresh_waits_for_selected_collection(db, monkeypatch, selected, status):
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, "scope", period))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: selected)
    monkeypatch.setattr(router, "get_source_cache", lambda *a, **kw: {"status": status})
    monkeypatch.setattr(router, "read_comparison", lambda *a: pytest.fail("Selected collection must finish before native collection"))
    with pytest.raises(HTTPException) as error:
        router.refresh_funnel(Request({"type": "http"}), period.date_from, period.date_to, None, True)
    assert error.value.status_code == 409


def test_native_refresh_queues_once_for_selected_pair_not_previous_period(db, monkeypatch):
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    from app.repricer_tasks import refresh_sales_funnel
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    scope = funnel.scope_key(1, "synthetic")
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, scope, period))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": True, "total": 1, "refreshRequired": False})
    monkeypatch.setattr(router, "read_comparison", lambda *a: {"complete": False, "total": 1, "refreshRequired": True})
    monkeypatch.setattr(router, "touch_demand", lambda *a, **kw: None)
    locks, cache, queued = {}, {}, []
    class Redis:
        def set(self, key, value, **kw):
            if key in locks:
                return False
            locks[key] = value
            return True
    monkeypatch.setattr(router, "get_redis_client", Redis)
    monkeypatch.setattr(router, "save_source_cache", lambda org, key, payload, **kw: cache.update({key: payload}))
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **kw: cache.get(key))
    monkeypatch.setattr(refresh_sales_funnel, "apply_async", lambda **kw: queued.append(kw))
    for _ in range(2):
        router.refresh_funnel(Request({"type": "http"}), period.date_from, period.date_to, None, True)
    assert len(queued) == 1
    assert queued[0]["args"][3:6] == ("2026-10-10", "2026-10-15", funnel.job_key(scope, period, True))
    assert queued[0]["kwargs"] == {"native_comparison": True}


def test_failed_selected_collection_retries_instead_of_becoming_native_completion(db, monkeypatch):
    from starlette.requests import Request
    from app.routers import wb_sales_funnel as router
    from app.repricer_tasks import refresh_sales_funnel
    period = Period(date(2026, 10, 10), date(2026, 10, 15))
    monkeypatch.setattr(router, "get_session_factory", funnel.get_session_factory)
    monkeypatch.setattr(router, "_scope", lambda *a: (SimpleNamespace(organization_id=1), 1, "scope", period))
    monkeypatch.setattr(router, "read_page", lambda *a, **kw: {"complete": True, "total": 1, "refreshRequired": False})
    monkeypatch.setattr(router, "get_source_cache", lambda *a, **kw: {"status": "failed"})
    monkeypatch.setattr(router, "save_source_cache", lambda *a, **kw: None)
    monkeypatch.setattr(router, "touch_demand", lambda *a, **kw: None)
    monkeypatch.setattr(router, "get_redis_client", lambda: SimpleNamespace(set=lambda *a, **kw: True))
    queued = []
    monkeypatch.setattr(refresh_sales_funnel, "apply_async", lambda **kw: queued.append(kw))
    result = router.refresh_funnel(Request({"type": "http"}), period.date_from, period.date_to, None)
    assert result["status"] == "queued" and len(queued) == 1
    assert queued[0]["args"][5] == funnel.job_key("scope", period)
    assert "kwargs" not in queued[0]
