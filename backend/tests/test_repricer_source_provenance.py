"""WB strategy source reuse, using disposable SQLite and synthetic receipts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import repricer_bff as bff, repricer_execution as execution
from app import repricer_page_finance as finance, repricer_strategy_sources as sources
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_sync import _period_cache_suffix, wb_token_fingerprint
from app.routers import wb_repricer_bff as router
from app.wb_api.financial_reports import CORE_FIELDS, MONEY_FIELDS
from tests import test_repricer_safety_regressions as base
from tests.test_repricer_safety_regressions import isolated

NOW = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
NM, ARTICLE = 443894764, "Фбbt_3036"
END = NOW.date() - timedelta(days=1)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def scenario(isolated, monkeypatch):
    from app.infra import db
    from app.infra.models import Base
    from app.platform.economics import legacy_catalog, legacy_tax
    with isolated() as session:
        Base.metadata.create_all(session.get_bind())
    for module in (db, legacy_catalog, legacy_tax):
        monkeypatch.setattr(module, "get_session_factory", lambda: isolated)
    monkeypatch.setattr(store, "datetime", Clock)
    monkeypatch.setattr(bff, "datetime", Clock)
    monkeypatch.setattr(base, "NOW", NOW)
    monkeypatch.setattr(execution, "_utc_now", lambda: NOW)
    provider = base.Provider()
    good = deepcopy(provider.goods[101])
    good.update(nmID=NM, vendorCode=ARTICLE)
    provider.goods[NM] = good
    row = base.healthy_row(provider, NM)
    row["meta"]["articleId"] = ARTICLE
    row["settings"].update(nightMedianEnabled=False, ordersPlanQty=7, ordersPlanDays=7)
    row["strategy"].update(id="plan_fact_period", typedStrategyId=None, assignmentSource="manual")
    tokens = {1: provider.token}
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: tokens.get(org))
    bff.ALGORITHM_SETTINGS_STATE.update(nightMedianEnabled=False, planFactFactPeriodDays=7)
    bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE] = {"strategyId": "plan_fact_period", "source": "manual", "config": {}}
    bff.SKU_SETTINGS_OVERRIDES[ARTICLE] = deepcopy(row["settings"])
    with isolated() as session:
        cached = session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id == 1))
        cached.goods_payload = [good]
        cached.fetched_at = NOW
        session.commit()
    store.save_source_cache(1, "content_cards", {"cards": [{"nmID": NM, "vendorCode": ARTICLE,
        "subjectID": 1, "subjectName": "Футболки", "title": "Synthetic target"}]})
    store.save_source_cache(1, "commission_tariffs", {"index": {"id:1": {
        "baseCommissionPct": 20, "sourceField": "kgvpMarketplace", "fetchedAt": NOW.isoformat()}}})
    store.save_source_cache(1, "stocks", {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
        "fetchedAt": NOW.isoformat(), "dateTo": str(NOW.date()), "aggregates": {str(NM): {"wbStockUnits": 50}}})
    queued = []
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: queued.append(args[2]))
    return provider, row, tokens, queued


def save_baskets(token, *, days=7, count=7, daily=False, org=1, at=NOW):
    first = END - timedelta(days=days - 1)
    stamp = {"strategyTokenFingerprint": wb_token_fingerprint(token), "fetchedAt": at.isoformat(),
        "dateFrom": str(first), "dateTo": str(END), "coverageState": "ok", "aggregateComplete": True}
    fact = {"orderCount": count, "cartCount": count * 2, "buyoutPct": 80,
        "buyoutSource": "sales_funnel.conversions.buyoutPercent"}
    if daily:
        for offset in range(days):
            day = str(first + timedelta(days=offset))
            store.save_source_cache(org, f"baskets_{day}_{day}", {**stamp, "dateFrom": day, "dateTo": day,
                "dailyDetailStatus": "complete", "dailyAggregates": {day: {str(NM): fact}}, "aggregates": {str(NM): fact}})
    else:
        suffix = _period_cache_suffix(days, date_from=first, date_to=END)
        store.save_source_cache(org, "baskets_" + suffix, {**stamp, "aggregates": {str(NM): fact}})


def save_finance(token, *, org=1, at=NOW, first=None, last=END, identity=1, **changes):
    first = first or END - timedelta(days=6)
    operation = {key: 0 for key in CORE_FIELDS | MONEY_FIELDS}
    operation.update(rrdId=identity, nmId=NM, quantity=1, retailAmount=1500, retailPriceWithDisc=1720,
        forPay=1400, ppvzSalesCommission=280, currency="RUB", saleDt=str(last), rrDate=str(last),
        docTypeName="Продажа", sellerOperName="Продажа", supplierArticle=ARTICLE)
    operation.update(changes)
    payload = finance.save_operations(org, token, first, last, [operation])
    key = f"repricer_finance_{finance.connection_key(token)}_{first}_{last}"
    store.save_source_cache(org, key, {**payload, "fetchedAt": at.isoformat()})
    return key


def prepare(row, token, *, metric="orders", org=1):
    bff.ALGORITHM_SETTINGS_STATE["planFactMetric"] = metric
    return sources.prepare_strategy_rows([row], org=org, token=token,
        algorithm=bff.ALGORITHM_SETTINGS_STATE, default_days=7, now=NOW, queue_missing=True)[0]


@pytest.mark.parametrize("metric", ["revenue", "margin"])
def test_finance_projection_reaches_real_row_enrichment_and_calculation(scenario, metric):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    save_finance(provider.token)
    for _ in range(2):  # Includes the persisted projection hit.
        prepared = prepare(row, provider.token, metric=metric)
        analytics = prepared["analytics"]
        assert analytics["financeState"] == "ok"
        assert analytics["revenueKopecks"] == 150000
        assert analytics["plannedMarginState"] == "ok" and analytics["marginKopecks"] is not None
        decision = execution._execute_single_sku(prepared, client=provider, actor_id="synthetic", actor_role="admin",
            options=execution.StrategyExecuteOptions(createDrafts=False, applyPrices=False), organization_id=1)
        assert not {"financeState_no_data", "revenueKopecks_required", "marginKopecks_required"} & set(decision.blockedReasons)
    assert not queued and not provider.calls


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("daily", [False, True])
def test_whole_and_daily_windows_reach_the_same_orders_decision(scenario, count, daily):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=count if daily else count * 3, daily=daily)
    prepared = prepare(row, provider.token)
    assert prepared["analytics"]["ordersUnits"] == count * 3
    assert prepared["analytics"]["baskets"] == count * 6
    assert prepared["analytics"]["basketsState"] == "ok"
    control = deepcopy(prepared)
    control["analytics"].update(ordersUnits=count * 3, periodDays=3)
    assert execution._plan_fact_recommended_price(prepared, 100000) == execution._plan_fact_recommended_price(control, 100000)
    assert not queued and not provider.calls


def daily_key(day=END):
    return f"baskets_{day}_{day}"


@pytest.mark.parametrize("invalid", ["wrong_token", "missing_binding", "wrong_org", "stale", "future", "invalid_time",
    "partial_day", "partial_row", "failed_source", "aggregate_incomplete", "missing_day", "null_count"])
def test_invalid_required_daily_evidence_is_not_certified(scenario, invalid):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=2, daily=True)
    key = daily_key()
    receipt = store.get_source_cache(1, key, slim=False)
    if invalid == "wrong_token": receipt["strategyTokenFingerprint"] = wb_token_fingerprint("foreign-synthetic")
    if invalid == "missing_binding": receipt.pop("strategyTokenFingerprint")
    if invalid == "wrong_org": receipt["organizationId"] = 2
    if invalid == "stale": receipt["fetchedAt"] = (NOW - timedelta(hours=2)).isoformat()
    if invalid == "future": receipt["fetchedAt"] = (NOW + timedelta(minutes=1)).isoformat()
    if invalid == "invalid_time": receipt["fetchedAt"] = "invalid"
    if invalid == "partial_day": receipt["chunks"] = [{"type": "daily", "date": str(END), "status": "pending"}]
    if invalid == "partial_row": receipt["dailyAggregates"][str(END)][str(NM)]["coverageState"] = "partial"
    if invalid == "failed_source": receipt["coverageState"] = "failed"
    if invalid == "aggregate_incomplete": receipt["aggregateComplete"] = False
    if invalid == "missing_day": receipt["dailyAggregates"] = {}
    if invalid == "null_count": receipt["dailyAggregates"][str(END)][str(NM)]["orderCount"] = None
    store.save_source_cache(1, key, receipt)
    prepared = prepare(row, provider.token)
    assert prepared["analytics"]["basketsState"] != "ok" or prepared["analytics"]["ordersUnits"] is None
    assert not provider.calls
    if invalid not in {"null_count", "partial_row"}: assert queued


@pytest.mark.parametrize("invalid", ["foreign", "unbound", "partial", "future"])
def test_newer_invalid_overlap_does_not_shadow_valid_daily_observations(scenario, invalid):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=2, daily=True, at=NOW-timedelta(minutes=10))
    bad = store.get_source_cache(1, daily_key(), slim=False)
    bad["fetchedAt"] = (NOW-timedelta(minutes=1)).isoformat()
    bad["dailyAggregates"][str(END)][str(NM)]["orderCount"] = 999
    if invalid == "foreign": bad["strategyTokenFingerprint"] = wb_token_fingerprint("foreign-synthetic")
    if invalid == "unbound": bad.pop("strategyTokenFingerprint")
    if invalid == "partial": bad["chunks"] = [{"type": "daily", "date": str(END), "status": "pending"}]
    if invalid == "future": bad["fetchedAt"] = (NOW+timedelta(minutes=1)).isoformat()
    store.save_source_cache(1, daily_key()+"_invalid", bad)
    prepared = prepare(row, provider.token)
    assert prepared["analytics"]["ordersUnits"] == 6 and not queued
    evidence = next(x for x in prepared["analytics"]["strategySourceEvidence"] if x["source"] == "baskets")
    assert evidence["fetchedAt"] == (NOW-timedelta(minutes=10)).isoformat()


def test_unused_old_overlap_does_not_poison_new_complete_observation(scenario):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=2, daily=True, at=NOW-timedelta(hours=2))
    save_baskets(provider.token, days=3, count=6)
    prepared = prepare(row, provider.token)
    assert prepared["analytics"]["ordersUnits"] == 6 and not queued


def test_generic_stitching_keeps_totals_without_certifying_mixed_binding(scenario):
    provider, _, _, _ = scenario
    save_baskets(provider.token, days=3, count=2, daily=True)
    receipt = store.get_source_cache(1, daily_key(), slim=False)
    receipt["strategyTokenFingerprint"] = wb_token_fingerprint("other-synthetic")
    store.save_source_cache(1, daily_key(), receipt)
    result = router._stitched_period_cache_from_days(1, "baskets", END-timedelta(days=2), END)
    assert result["aggregates"][str(NM)]["orderCount"] == 6
    assert "strategyTokenFingerprint" not in result


@pytest.mark.parametrize("invalid", ["binding_org", "binding_connection", "fingerprint", "oldest_stale", "oldest_invalid", "future", "schema"])
def test_cached_financial_projection_must_retain_valid_source_evidence(scenario, invalid):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    save_finance(provider.token)
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "ok"
    metas = store.list_source_cache_ranges_by_prefix(1, "page_finance_projection_", limit=100)
    assert len(metas) == 1
    key = metas[0]["sourceKey"]
    cache = store.get_source_cache(1, key, slim=False)
    result = cache["result"]
    if invalid == "binding_org": result["strategySourceBinding"]["organizationId"] = 2
    if invalid == "binding_connection": result["strategySourceBinding"]["connectionKey"] = finance.connection_key("other-synthetic")
    if invalid == "fingerprint": result["strategyTokenFingerprint"] = wb_token_fingerprint("other-synthetic")
    if invalid == "oldest_stale": result["oldestFetchedAt"] = (NOW-timedelta(hours=2)).isoformat()
    if invalid == "oldest_invalid": result["oldestFetchedAt"] = None
    if invalid == "future": result["fetchedAt"] = (NOW+timedelta(minutes=1)).isoformat()
    if invalid == "schema": result["financeSchemaVersion"] = "v3"
    store.save_source_cache(1, key, cache)
    prepared = prepare(row, provider.token, metric="revenue")
    assert prepared["analytics"]["financeState"] == "no_data" and queued
    assert not provider.calls


def test_token_rotation_and_delayed_previous_connection_remain_blocked(scenario):
    provider, row, tokens, queued = scenario
    old_token = provider.token
    save_baskets(old_token)
    save_finance(old_token)
    assert prepare(row, old_token, metric="revenue")["analytics"]["financeState"] == "ok"
    tokens[1] = "rotated-synthetic"
    save_baskets(tokens[1])
    assert prepare(row, tokens[1], metric="revenue")["analytics"]["financeState"] == "no_data"
    marker = finance.page_connection.set(finance.connection_key(old_token))
    try:
        assert prepare(row, tokens[1], metric="revenue")["analytics"]["financeState"] == "no_data"
    finally:
        finance.page_connection.reset(marker)
    queued.clear()
    save_finance(tokens[1])
    # Stock is independently bound; replace its synthetic observation as a new read would.
    stock = store.get_source_cache(1, "stocks", slim=False)
    stock["strategyTokenFingerprint"] = wb_token_fingerprint(tokens[1])
    store.save_source_cache(1, "stocks", stock)
    assert prepare(row, tokens[1], metric="revenue")["analytics"]["financeState"] == "ok"
    assert not queued and not provider.calls


def test_legacy_projection_cache_is_rebuilt_from_saved_receipts_without_deleting_it(scenario):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    save_finance(provider.token)
    first = END-timedelta(days=6)
    from app.wb_statistics_finance import project_statistics_operations
    projection = f"{project_statistics_operations.__name__}_{finance.VERSION}"
    old_key = f"page_finance_projection_{finance.VERSION}_{finance.connection_key(provider.token)}_{first}_{END}_{projection}_0"
    store.save_source_cache(1, old_key, {"sourceRevision": "old-contract", "result": {"sourceComplete": True}})
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "ok"
    assert store.get_source_cache(1, old_key)["sourceRevision"] == "old-contract"
    assert not queued and not provider.calls


def test_finance_uses_oldest_required_receipt_not_newest_write_time(scenario):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    for offset in range(7):
        day = END - timedelta(days=offset)
        save_finance(provider.token, first=day, last=day, identity=offset+1,
            at=NOW-timedelta(hours=2) if offset == 6 else NOW)
    result = finance.load_page_finance(1, END-timedelta(days=6), END)
    assert result["sourceComplete"] and result["fetchedAt"] == NOW.isoformat()
    assert result["oldestFetchedAt"] == (NOW-timedelta(hours=2)).isoformat()
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "no_data"
    assert queued and not provider.calls


def test_foreign_organization_receipt_cannot_fill_current_finance(scenario):
    provider, row, tokens, queued = scenario
    tokens[2] = provider.token
    save_baskets(provider.token)
    save_finance(provider.token, org=2)
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "no_data"
    assert queued and not provider.calls


def test_trusted_request_connection_is_not_relabelled_as_organization_default(scenario):
    provider, row, _, queued = scenario
    token = "synthetic-selected-request-connection"
    save_baskets(token)
    save_finance(token)
    stock = store.get_source_cache(1, "stocks", slim=False)
    stock["strategyTokenFingerprint"] = wb_token_fingerprint(token)
    store.save_source_cache(1, "stocks", stock)
    marker = finance.page_connection.set(finance.connection_key(token))
    try:
        projected = finance.load_page_finance(1, END-timedelta(days=6), END)
        assert "strategyTokenFingerprint" not in projected
        assert projected["strategySourceBinding"]["connectionKey"] == finance.connection_key(token)
        assert prepare(row, token, metric="revenue")["analytics"]["financeState"] == "ok"
    finally:
        finance.page_connection.reset(marker)
    assert not queued and not provider.calls


def test_stitched_missing_product_or_cart_measure_remains_unknown(scenario):
    provider, row, _, _ = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=2, daily=True)
    key = daily_key()
    receipt = store.get_source_cache(1, key, slim=False)
    receipt["dailyAggregates"][str(END)][str(NM)]["cartCount"] = None
    store.save_source_cache(1, key, receipt)
    prepared = prepare(row, provider.token)
    assert prepared["analytics"]["ordersUnits"] == 6 and prepared["analytics"]["baskets"] is None
    receipt["dailyAggregates"][str(END)] = {}
    store.save_source_cache(1, key, receipt)
    assert prepare(row, provider.token)["analytics"]["ordersUnits"] is None


@pytest.mark.parametrize("invalid", ["partial", "incompatible"])
def test_partial_or_incompatible_financial_receipts_remain_unusable(scenario, invalid):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    key = save_finance(provider.token)
    receipt = store.get_source_cache(1, key, slim=False)
    if invalid == "partial": receipt["sourceComplete"] = False
    if invalid == "incompatible": receipt["availableFields"].remove("retailAmount")
    store.save_source_cache(1, key, receipt)
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "no_data"
    assert queued and not provider.calls


def test_projection_does_not_stamp_current_token_onto_a_foreign_receipt(scenario, monkeypatch):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    key = save_finance("foreign-synthetic")
    original = store.list_source_cache_ranges_by_prefix
    foreign = original(1, "repricer_finance_", limit=100)
    assert [x["sourceKey"] for x in foreign] == [key]
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix",
        lambda org, prefix, **kw: foreign if prefix.startswith("repricer_finance_") else original(org, prefix, **kw))
    result = finance.load_page_finance(1, END-timedelta(days=6), END)
    assert result["sourceComplete"]
    assert "strategyTokenFingerprint" not in result and "strategySourceBinding" not in result
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "no_data"
    assert queued and not provider.calls


def test_known_zero_financial_revenue_is_not_missing(scenario):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    save_finance(provider.token, retailAmount=0)
    prepared = prepare(row, provider.token, metric="revenue")
    assert prepared["analytics"]["financeState"] == "ok"
    assert prepared["analytics"]["revenueKopecks"] == 0
    assert not queued and not provider.calls


@pytest.mark.parametrize("field", ["oldestFetchedAt", "oldestSourceFetchedAt"])
def test_financial_projection_cannot_hide_contradictory_raw_observation(scenario, field):
    provider, row, _, queued = scenario
    save_baskets(provider.token)
    key = save_finance(provider.token)
    receipt = store.get_source_cache(1, key, slim=False)
    receipt[field] = (NOW + timedelta(minutes=1)).isoformat()
    store.save_source_cache(1, key, receipt)
    assert prepare(row, provider.token, metric="revenue")["analytics"]["financeState"] == "no_data"
    assert queued and not provider.calls


def refresh_basket_aggregate(monkeypatch, token, *, include_daily=False):
    from app import repricer_sync as sync
    from app.wb_api import source_freshness
    monkeypatch.setattr(source_freshness, "datetime", Clock)
    monkeypatch.setattr(sync, "_utc_now", lambda: NOW)
    monkeypatch.setattr(sync, "_release_warmup_goods_by_baskets", lambda *_a: 0)
    monkeypatch.setattr(sync, "fetch_baskets_aggregates", lambda *_a, **_kw: {
        "aggregates": {str(NM): {"orderCount": 7, "cartCount": 14}},
        "dateFrom": str(END-timedelta(days=6)), "dateTo": str(END),
        "fetchedAt": NOW.isoformat(), "coverageState": "ok", "count": 1})
    result = sync.refresh_wb_data_sources(organization_id=1, wb_token=token,
        date_from=END-timedelta(days=6), date_to=END, sources=["baskets"],
        baskets_include_daily_detail=include_daily, execute_lock=False, _parallelize=False)
    assert result["state"] == "completed"
    return store.get_source_cache(1, f"baskets_{END-timedelta(days=6)}_{END}", slim=False)


def complete_daily_seed_chunks():
    daily, chunks = {}, []
    for offset in range(3):
        day = END-timedelta(days=offset)
        key = daily_key(day)
        cache = store.get_source_cache(1, key, slim=False)
        cache["chunks"] = [{"type": "daily", "date": str(day), "chunkIndex": 0, "status": "done"}]
        store.save_source_cache(1, key, cache)
        daily.update(cache["dailyAggregates"])
        chunks.extend(cache["chunks"])
    store.save_source_cache(1, f"baskets_{END-timedelta(days=6)}_{END}", {**cache,
        "dateFrom": str(END-timedelta(days=6)), "dateTo": str(END), "dailyDetailStatus": "fetched",
        "dailyAggregates": daily, "chunks": chunks})


@pytest.mark.parametrize("invalid", ["rotated_token", "unbound"])
def test_aggregate_refresh_cannot_rebind_foreign_daily_seeds(scenario, monkeypatch, invalid):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets("foreign-synthetic" if invalid == "rotated_token" else provider.token,
        days=3, count=2, daily=True)
    complete_daily_seed_chunks()
    if invalid == "unbound":
        for key in [daily_key(END-timedelta(days=offset)) for offset in range(3)] + [f"baskets_{END-timedelta(days=6)}_{END}"]:
            cache = store.get_source_cache(1, key, slim=False)
            cache.pop("strategyTokenFingerprint")
            store.save_source_cache(1, key, cache)
    aggregate = refresh_basket_aggregate(monkeypatch, provider.token)
    assert not aggregate["dailyAggregates"]
    assert aggregate["aggregates"][str(NM)]["orderCount"] == 7
    assert prepare(row, provider.token)["analytics"]["ordersUnits"] is None
    assert queued and not provider.calls


def test_repeated_aggregate_refresh_preserves_original_daily_age(scenario, monkeypatch):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    observed = NOW-timedelta(minutes=20)
    save_baskets(provider.token, days=3, count=2, daily=True, at=observed)
    complete_daily_seed_chunks()
    for _ in range(2):
        aggregate = refresh_basket_aggregate(monkeypatch, provider.token)
        assert aggregate["dailyAggregates"], aggregate
        prepared = prepare(row, provider.token)
        assert prepared["analytics"]["ordersUnits"] == 6
        evidence = next(x for x in prepared["analytics"]["strategySourceEvidence"] if x["source"] == "baskets")
        assert evidence["fetchedAt"] == observed.isoformat()
    assert not queued and not provider.calls


@pytest.mark.parametrize("invalid", ["foreign", "unbound", "stale", "future", "missing", "legacy_preserved"])
def test_daily_evidence_remains_required_after_an_aggregate_refresh(scenario, invalid):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=3, count=2, daily=True)
    key = daily_key()
    cache = store.get_source_cache(1, key, slim=False)
    evidence = {"strategyTokenFingerprint": wb_token_fingerprint(provider.token), "fetchedAt": NOW.isoformat()}
    if invalid == "foreign": evidence["strategyTokenFingerprint"] = wb_token_fingerprint("foreign-synthetic")
    if invalid == "unbound": evidence.pop("strategyTokenFingerprint")
    if invalid == "stale": evidence["fetchedAt"] = (NOW-timedelta(hours=2)).isoformat()
    if invalid == "future": evidence["fetchedAt"] = (NOW+timedelta(minutes=1)).isoformat()
    cache["dailySourceEvidence"] = {} if invalid == "missing" else {str(END): evidence}
    if invalid == "legacy_preserved":
        cache.pop("dailySourceEvidence")
        cache["dailyDetailPreservedBy"] = "aggregate_refresh"
    store.save_source_cache(1, key, cache)
    generic = router._stitched_period_cache_from_days(1, "baskets", END-timedelta(days=2), END)
    assert generic["aggregates"][str(NM)]["orderCount"] == 6
    assert prepare(row, provider.token)["analytics"]["ordersUnits"] is None
    assert queued and not provider.calls


def test_unused_stale_daily_evidence_does_not_block_fresh_requested_days(scenario):
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    save_baskets(provider.token, days=7, count=2, daily=True)
    daily, evidence = {}, {}
    for offset in range(7):
        day = END-timedelta(days=offset)
        daily.update(store.get_source_cache(1, daily_key(day), slim=False)["dailyAggregates"])
        evidence[str(day)] = {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
            "fetchedAt": (NOW-timedelta(hours=2) if offset >= 3 else NOW).isoformat()}
    save_baskets(provider.token, days=7, count=14)
    key = f"baskets_{END-timedelta(days=6)}_{END}"
    cache = store.get_source_cache(1, key, slim=False)
    cache.update(dailyAggregates=daily, dailySourceEvidence=evidence, dailyDetailStatus="fetched")
    store.save_source_cache(1, key, cache)
    assert prepare(row, provider.token)["analytics"]["ordersUnits"] == 6
    assert not queued and not provider.calls


def test_fresh_daily_callbacks_create_reusable_source_evidence(scenario, monkeypatch):
    from app import repricer_sync as sync
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"

    def controlled_daily_provider(*_args, **kwargs):
        daily, chunks = {}, []
        for offset in range(7):
            day = str(END-timedelta(days=offset))
            rows = {str(NM): {"orderCount": 2, "cartCount": 4}}
            daily[day] = rows
            chunks.append({"type": "daily", "date": day, "chunkIndex": 0, "status": "done"})
            progress = {"requestsCompleted": offset+1, "requestsTotal": 7, "status": "done"}
            kwargs["chunk_completed_callback"](day, 0, rows, progress)
            kwargs["day_completed_callback"](day, rows, progress)
        return {"dailyAggregates": daily, "chunks": chunks, "requestsCompleted": 7, "requestsTotal": 7}

    monkeypatch.setattr(sync, "fetch_baskets_daily_detail", controlled_daily_provider)
    aggregate = refresh_basket_aggregate(monkeypatch, provider.token, include_daily=True)
    assert set(aggregate["dailySourceEvidence"]) == set(aggregate["dailyAggregates"])
    assert {item["fetchedAt"] for item in aggregate["dailySourceEvidence"].values()} == {NOW.isoformat()}
    assert prepare(row, provider.token)["analytics"]["ordersUnits"] == 6
    assert not queued and not provider.calls


@pytest.mark.parametrize("path", ["seed", "stitch", "covering"])
def test_original_daily_observation_time_controls_newest_valid_precedence(scenario, monkeypatch, path):
    from app import repricer_sync as sync
    from app.wb_api import source_freshness
    provider, row, _, queued = scenario
    row["strategy"]["id"] = "plan_fact_daily"
    monkeypatch.setattr(sync, "_utc_now", lambda: NOW)
    monkeypatch.setattr(source_freshness, "datetime", Clock)
    save_baskets(provider.token, days=3, count=2, daily=True)
    old = store.get_source_cache(1, daily_key(), slim=False)
    old["dailySourceEvidence"] = {str(END): {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
        "fetchedAt": (NOW-timedelta(minutes=20)).isoformat()}}
    store.save_source_cache(1, daily_key(), old)
    newer = deepcopy(old)
    newer.pop("dailySourceEvidence")
    newer["fetchedAt"] = (NOW-timedelta(minutes=10)).isoformat()
    newer["dailyAggregates"][str(END)][str(NM)]["orderCount"] = 3
    store.save_source_cache(1, daily_key()+"_new_observation", newer)
    if path == "seed":
        evidence = {}
        daily, _ = sync._baskets_daily_seed_from_recent_cache(1,
            range_start=datetime.combine(END-timedelta(days=2), datetime.min.time()),
            range_end=datetime.combine(END, datetime.min.time()),
            strategy_fingerprint=wb_token_fingerprint(provider.token), observation_evidence=evidence)
        assert sum(rows[str(NM)]["orderCount"] for rows in daily.values()) == 7
        assert evidence[str(END)]["fetchedAt"] == newer["fetchedAt"]
    elif path == "stitch":
        result = router._stitched_period_cache_from_days(1, "baskets", END-timedelta(days=2), END,
            strategy_fingerprint=wb_token_fingerprint(provider.token), strategy_now=NOW)
        assert result["aggregates"][str(NM)]["orderCount"] == 7
    else:
        save_baskets(provider.token, days=7, count=7)
        key = f"baskets_{END-timedelta(days=6)}_{END}"
        covering = store.get_source_cache(1, key, slim=False)
        daily = {str(END-timedelta(days=i)): store.get_source_cache(1, daily_key(END-timedelta(days=i)), slim=False)["dailyAggregates"][str(END-timedelta(days=i))] for i in range(3)}
        covering.update(dailyAggregates=daily, dailyDetailStatus="fetched",
            dailySourceEvidence={day: {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
                "fetchedAt": (NOW-timedelta(minutes=20)).isoformat()} for day in daily})
        store.save_source_cache(1, key, covering)
        assert prepare(row, provider.token)["analytics"]["ordersUnits"] == 7
        assert not queued
    assert not provider.calls
