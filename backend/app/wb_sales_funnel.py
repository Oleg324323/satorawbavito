"""Account-scoped, read-only funnel projections in the existing durable cache.

Source: https://dev.wildberries.ru/en/openapi/analytics (sales-funnel/products).
No finance/ads approximations. Intervals are never prorated into smaller dates.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import math
import re

from sqlalchemy import BigInteger, and_, case, cast, func, or_, select
from sqlalchemy.orm import aliased

from app.infra.db import get_session_factory, set_tenant_context
from app.platform.catalog.orm import MarketplaceProductRow as Product
from app.platform.integrations.wb_credentials import resolve_bound_wb_credential
from app.platform.period import Period
from app.repricer_cache.orm import WbRepricerSourceCacheRow as Cache
from app.wb_api.source_freshness import source_is_fresh
from app.wb_sales_funnel_catalog import catalog_state, ensure_catalog

SOURCE = "/api/analytics/v3/sales-funnel/products"
FIELDS = ("visits", "carts", "orders", "orderAmount", "buyouts", "buyoutAmount", "cancels")
WB_FIELDS = dict(zip(FIELDS, ("openCount", "cartCount", "orderCount", "orderSum", "buyoutCount", "buyoutSum", "cancelCount")))
AMOUNTS = {"orderAmount", "buyoutAmount"}
SHARE_POPULATION = "seller_all_products_v1"
RATIO_INPUTS = {"cartConversion": ("carts", "visits"), "orderConversion": ("orders", "carts"),
                "buyoutPercent": ("buyouts", "cancels")}
NATIVE_SOURCE = "wb_sales_funnel_comparison_v1"
NATIVE_FIELDS = {"revenueShare": "shareOrderPercentDynamic", "visits": "openCountDynamic",
                 "carts": "cartCountDynamic", "orders": "orderCountDynamic", "orderAmount": "orderSumDynamic",
                 "buyouts": "buyoutCountDynamic", "buyoutAmount": "buyoutSumDynamic",
                 "cartConversion": "addToCartPercent", "orderConversion": "cartToOrderPercent",
                 "buyoutPercent": "buyoutPercent"}


def previous_period(period):
    return Period(period.date_from - timedelta(days=period.days), period.date_from - timedelta(days=1))


def native_prefix(scope, period):
    return f"{scope}_native_v1_{period.cache_key}_{previous_period(period).cache_key}"


def job_suffix(period, native=False, aggregate=False):
    if aggregate:
        return f"totals_v1_{period.cache_key}_{previous_period(period).cache_key}"
    return f"native_v1_{period.cache_key}_{previous_period(period).cache_key}" if native else period.cache_key


def job_key(scope, period, native=False, aggregate=False):
    return f"{scope}_job_{job_suffix(period, native, aggregate)}"


def native_number(value):
    # No coercion/default-to-zero: WB supplies signed percentage numbers.
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def native_period_matches(product, period):
    statistic = product.get("statistic") or {}
    if not isinstance(statistic, dict):
        return False
    selected, previous = statistic.get("selected"), statistic.get("past")
    if not isinstance(selected, dict) or not isinstance(previous, dict):
        return False
    past = previous_period(period)
    return (selected.get("period") == {"start": str(period.date_from), "end": str(period.date_to)}
            and previous.get("period") == {"start": str(past.date_from), "end": str(past.date_to)})


def native_listing_valid(products, period):
    # Missing provider fields stay unknown. A stated but different period is
    # not a completed comparison for this pair (e.g. retention clipping).
    for product in products:
        statistic = product.get("statistic") or {}
        previous = statistic.get("past") if isinstance(statistic, dict) else None
        if isinstance(previous, dict) and previous.get("period") and not native_period_matches(product, period):
            return False
    return True


def seller_wide_request(body):
    # WB normalizes shareOrderPercent over the requested population, not
    # necessarily the seller's full catalog. Pagination does not narrow it.
    return not any(body.get(key) for key in ("nmIds", "brandNames", "subjectIds", "tagIds", "skipDeletedNm"))


def scope_key(account, token):
    return f"funnel_v1_{account}_{sha256(token.encode()).hexdigest()[:24]}"


def normalized_metrics(product):
    selected = (product.get("statistic") or {}).get("selected") or {}
    result = {}
    for field, source in WB_FIELDS.items():
        raw = selected.get(source)
        value = None
        if raw is not None and not isinstance(raw, bool):
            try:
                number = Decimal(str(raw))
                if number.is_finite() and number >= 0:
                    if field in AMOUNTS:
                        value = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    elif number == number.to_integral_value():
                        value = int(number)
            except (InvalidOperation, ValueError, OverflowError):
                pass
        result[field] = value
    # The connected public endpoint does not expose impressions or CTR.
    result["impressions"] = None
    conversions = selected.get("conversions") or {}
    for key, source in (("cartConversion", "addToCartPercent"), ("orderConversion", "cartToOrderPercent"), ("buyoutPercent", "buyoutPercent")):
        raw = conversions.get(source)
        try:
            number = Decimal(str(raw))
            result["wb" + key] = float(number) if number.is_finite() and number >= 0 and not isinstance(raw, bool) else None
        except InvalidOperation:
            result["wb" + key] = None
    raw_share = selected.get("shareOrderPercent")
    try:
        share = Decimal(str(raw_share))
        result["wbRevenueShare"] = float(share) if not isinstance(raw_share, bool) and share.is_finite() and 0 <= share <= 100 else None
    except InvalidOperation:
        result["wbRevenueShare"] = None
    return result


def days(start, end):
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def intervals(day_set):
    result = []
    for day in sorted(day_set):
        if result and day == result[-1][1] + timedelta(days=1):
            result[-1] = (result[-1][0], day)
        else:
            result.append((day, day))
    return result


def aggregate(period, observations, *, now=None):
    """Select disjoint evidence per metric; stale evidence stays visibly partial."""
    target = days(period.date_from, period.date_to)
    sums, fresh_days, used_days = {}, {}, {}
    for field in FIELDS:
        total, used, fresh = 0, set(), set()
        candidates = sorted(observations, key=lambda row: (
            source_is_fresh(row, now=now),
            (date.fromisoformat(row["dateTo"]) - date.fromisoformat(row["dateFrom"])).days,
            row["fetchedAt"]
        ), reverse=True)
        for row in candidates:
            start, end = date.fromisoformat(row["dateFrom"]), date.fromisoformat(row["dateTo"])
            if start < period.date_from or end > period.date_to or row.get(field) is None:
                continue
            covered = days(start, end)
            if covered & used:
                continue  # Never add overlapping period totals or prorate them.
            used |= covered
            total += row[field]
            if source_is_fresh(row, now=now):
                fresh |= covered
        sums[field] = total if used else None
        fresh_days[field], used_days[field] = fresh, used
    complete = all(fresh_days[field] == target for field in FIELDS)
    missing = set().union(*(target - fresh_days[field] for field in FIELDS))
    sums.update(impressions=None, ctr=None, complete=complete,
                coveredDays=min(len(used_days[field]) for field in FIELDS),
                fieldCoverage={field: used_days[field] == target for field in FIELDS},
                freshFieldCoverage={field: fresh_days[field] == target for field in FIELDS},
                days=period.days)
    # Ratios require matching, complete date coverage for both inputs.
    def ratio(numerator, denominator, precision=2):
        if used_days[numerator] != target or used_days[denominator] != target:
            return None
        return percent(sums[numerator], sums[denominator], precision)
    sums["cartConversion"] = ratio("carts", "visits")
    sums["orderConversion"] = ratio("orders", "carts")
    sums["buyoutPercent"] = (
        percent(sums["buyouts"], sums["buyouts"] + sums["cancels"], 0)
        if used_days["buyouts"] == target and used_days["cancels"] == target else None
    )
    for row in candidates:
        if row["dateFrom"] != period.date_from.isoformat() or row["dateTo"] != period.date_to.isoformat():
            continue
        for key, inputs in (("cartConversion", ("carts", "visits")), ("orderConversion", ("orders", "carts")), ("buyoutPercent", ("buyouts", "cancels"))):
            # Preserve WB's rounding and explicit no-activity 0% for a single
            # exact interval. Never average these percentages across intervals.
            if row.get("wb" + key) is not None and all(sums[field] is not None and sums[field] == row.get(field) and used_days[field] == target for field in inputs):
                sums[key] = row["wb" + key]
        break
    # A WB share belongs to the exact reporting period and seller population.
    # Never recompute it from our catalog, or sum/average subperiod shares.
    share_observation = next((row for row in candidates
        if row["dateFrom"] == period.date_from.isoformat()
        and row["dateTo"] == period.date_to.isoformat()
        and row.get("revenueSharePopulation") == SHARE_POPULATION
        and "wbRevenueShare" in row), None)
    sums["revenueShare"] = share_observation.get("wbRevenueShare") if share_observation is not None else None
    # Distinguish old cache entries from an actual missing field in a WB reply.
    sums["revenueShareSourceMapped"] = share_observation is not None
    sums["revenueSharePopulation"] = SHARE_POPULATION if share_observation is not None else None
    sums["revenueShareFresh"] = share_observation is not None and source_is_fresh(share_observation, now=now)
    return sums, intervals(missing)


def percent(numerator, denominator, precision=2):
    if numerator is None or denominator is None or denominator == 0:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP))


def upsert(session, org, rows):
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    set_tenant_context(session, org)
    for offset in range(0, len(rows), 100):
        stmt = insert(Cache).values([{"organization_id": org, **row} for row in rows[offset:offset+100]])
        stmt = stmt.on_conflict_do_update(index_elements=["organization_id", "source_key"], set_={
            key: getattr(stmt.excluded, key) for key in ("payload", "fetched_at", "range_date_from", "range_date_to")
        }, where=Cache.fetched_at <= stmt.excluded.fetched_at)
        session.execute(stmt)
    session.commit()


def observation_rows(scope, start, end, products, fetched, *, currency="RUB", seller_wide=False):
    result = []
    for product in products:
        source_period = ((product.get("statistic") or {}).get("selected") or {}).get("period")
        if source_period and source_period != {"start": start.isoformat(), "end": end.isoformat()}:
            raise ValueError("WB_FUNNEL_PERIOD_MISMATCH")
        nm = str((product.get("product") or {}).get("nmId") or "")
        if not nm.isdigit():
            continue
        payload = {**normalized_metrics(product), "nmId": nm,
                   "dateFrom": start.isoformat(), "dateTo": end.isoformat(), "fetchedAt": fetched.isoformat()}
        payload["currency"] = currency
        payload["revenueSharePopulation"] = SHARE_POPULATION if seller_wide else None
        if currency != "RUB":
            # Counts remain usable, but an unknown/foreign amount is not ₽.
            payload.update(orderAmount=None, buyoutAmount=None)
        # Keep seller-wide percentages even when a later incremental product
        # request replaces its own counts/amounts with fresher evidence.
        suffix = "_seller" if seller_wide else ""
        result.append({"source_key": f"{scope}_fact_{start}_{end}_{nm}{suffix}", "payload": payload,
                       "range_date_from": start, "range_date_to": end, "fetched_at": fetched})
    return result


def past_observation_rows(scope, period, products, fetched, **kwargs):
    """Use native past VALUES, not rounded/possibly undefined WB deltas.

    WB may truncate old comparison periods to its retention boundary. Such a
    response must not be relabelled as the requested preceding interval.
    """
    previous = previous_period(period)
    expected = {"start": str(previous.date_from), "end": str(previous.date_to)}
    matched = []
    for product in products:
        statistic = product.get("statistic") or {}
        past = statistic.get("past")
        selected_period = (statistic.get("selected") or {}).get("period")
        if (not isinstance(past, dict) or past.get("period") != expected
            or selected_period != {"start": str(period.date_from), "end": str(period.date_to)}):
            continue
        matched.append({"product": product.get("product"), "statistic": {"selected": past}})
    return observation_rows(scope, previous.date_from, previous.date_to, matched, fetched, **kwargs)


def native_rows(scope, period, products, fetched, *, currency="RUB", seller_wide=False, request_identity=None):
    """One exact provider period pair; never derive or aggregate its deltas."""
    previous = previous_period(period)
    rows = []
    for product in products:
        if not native_period_matches(product, period):
            continue
        current_fact = observation_rows(scope, period.date_from, period.date_to, [product], fetched,
                                        currency=currency, seller_wide=seller_wide)
        if not current_fact:
            continue
        past_fact = past_observation_rows(scope, period, [product], fetched, currency=currency, seller_wide=seller_wide)[0]
        selected = aggregate(period, [current_fact[0]["payload"]], now=fetched)[0]
        past = aggregate(previous, [past_fact["payload"]], now=fetched)[0]
        comparison = (product.get("statistic") or {}).get("comparison")
        comparison = comparison if isinstance(comparison, dict) else {}
        conversions = comparison.get("conversions")
        conversions = conversions if isinstance(conversions, dict) else {}
        dynamics = {key: native_number((conversions if key in RATIO_INPUTS else comparison).get(field))
                    for key, field in NATIVE_FIELDS.items()}
        if not seller_wide:
            dynamics["revenueShare"] = None
        nm = current_fact[0]["payload"]["nmId"]
        payload = {"source": NATIVE_SOURCE, "nmId": nm, "dateFrom": str(period.date_from), "dateTo": str(period.date_to),
                   "previousDateFrom": str(previous.date_from), "previousDateTo": str(previous.date_to),
                   "fetchedAt": fetched.isoformat(), "population": SHARE_POPULATION if seller_wide else "filtered",
                   "requestIdentity": request_identity, "dynamics": dynamics,
                   "selectedValues": {key: selected.get(key) for key in NATIVE_FIELDS},
                   "previousValues": {key: past.get(key) for key in NATIVE_FIELDS}}
        rows.append({"source_key": f"{native_prefix(scope, period)}_{nm}_{'seller' if seller_wide else 'batch'}",
                     "payload": payload, "range_date_from": period.date_from, "range_date_to": period.date_to, "fetched_at": fetched})
    return rows


def enumeration_key(scope, period, *, native=False):
    return f"{native_prefix(scope, period)}_enumerated" if native else f"{scope}_enumerated_{period.cache_key}"


def save_enumeration(session, org, account, scope, period, returned, fetched, *, native=False):
    """A finished provider listing is evidence of omission, never a zero fact."""
    checked = session.scalars(select(Product.external_product_id).where(
        Product.organization_id == org, Product.marketplace_account_id == account)).all()
    upsert(session, org, [{"source_key": enumeration_key(scope, period, native=native),
        "range_date_from": period.date_from, "range_date_to": period.date_to, "fetched_at": fetched,
        "payload": {"complete": True, "dateFrom": str(period.date_from), "dateTo": str(period.date_to),
                    "fetchedAt": fetched.isoformat(), "checkedNmIds": list(checked),
                    "returnedNmIds": [str(nm) for nm in sorted(returned)],
                    **({"source": NATIVE_SOURCE, "previousDateFrom": str(previous_period(period).date_from),
                        "previousDateTo": str(previous_period(period).date_to)} if native else {})}}])


def fresh_enumeration(session, org, scope, period, *, native=False):
    payload = session.scalar(select(Cache.payload).where(Cache.organization_id == org,
        Cache.source_key == enumeration_key(scope, period, native=native)))
    if (payload and payload.get("complete") is True
        and payload.get("dateFrom") == str(period.date_from) and payload.get("dateTo") == str(period.date_to)
        and (not native or (payload.get("source") == NATIVE_SOURCE
             and payload.get("previousDateFrom") == str(previous_period(period).date_from)
             and payload.get("previousDateTo") == str(previous_period(period).date_to)))
        and source_is_fresh(payload)):
        return payload
    return None


def import_checkpoints(session, org, scope, token, period, account=None, *, totals_only=False):
    """Reuse original WB payloads, but only after verifying credential + host hash."""
    from app.wb_api.client import build_wb_analytics_client
    host = build_wb_analytics_client("complete", token_override=token).base_url
    token_hash = sha256(token.encode()).hexdigest()
    body = Cache.payload["response"]["request"]["jsonBody"]
    following = Period(period.date_to + timedelta(days=1), period.date_to + timedelta(days=period.days))
    query = select(Cache.cache_id, Cache.source_key, Cache.payload, Cache.fetched_at).where(
        Cache.organization_id == org, Cache.source_key.startswith("wb_report_http_"),
        Cache.payload["response"]["request"]["path"].as_string() == SOURCE,
        or_(and_(body["selectedPeriod"]["start"].as_string() >= str(period.date_from),
                 body["selectedPeriod"]["end"].as_string() <= str(period.date_to)),
            and_(body["selectedPeriod"]["start"].as_string() == str(following.date_from),
                 body["selectedPeriod"]["end"].as_string() == str(following.date_to))),
    )
    last_id = 0
    seller_pages = defaultdict(dict)
    native_pages = defaultdict(dict)
    while True:
        set_tenant_context(session, org)
        checkpoints = session.execute(query.where(Cache.cache_id > last_id).order_by(Cache.cache_id).limit(10)).all()
        if not checkpoints:
            break
        last_id = checkpoints[-1][0]
        pending = []
        for _, key, checkpoint, fetched in checkpoints:
            response = checkpoint.get("response") or {}
            request = response.get("request") or {}
            identity = json.dumps([host, token_hash, request], sort_keys=True, separators=(",", ":"))
            if key != "wb_report_http_" + sha256(identity.encode()).hexdigest() or not response.get("ok"):
                continue
            selected = (request.get("jsonBody") or {}).get("selectedPeriod") or {}
            start, end = date.fromisoformat(selected["start"]), date.fromisoformat(selected["end"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            data = response.get("data") or {}
            body = data.get("data") or data
            products = body.get("products")
            if not isinstance(products, list):
                continue
            options = {"currency": body.get("currency"), "seller_wide": seller_wide_request(request.get("jsonBody") or {})}
            if not totals_only and period.date_from <= start <= end <= period.date_to:
                pending.extend(observation_rows(scope, start, end, products, fetched, **options))
            params = request.get("jsonBody") or {}
            expected_past = previous_period(period)
            requested_past = params.get("pastPeriod")
            exact_pair = (start, end) == (period.date_from, period.date_to) and requested_past in (
                None, {"start": str(expected_past.date_from), "end": str(expected_past.date_to)})
            if exact_pair and not totals_only:
                pending.extend(native_rows(scope, period, products, fetched, request_identity=key, **options))
            native_period = previous_period(Period(start, end))
            if not totals_only and period.date_from <= native_period.date_from <= native_period.date_to <= period.date_to:
                pending.extend(past_observation_rows(scope, Period(start, end), products, fetched, **options))
            limit, offset = params.get("limit"), params.get("offset")
            if (request.get("method") == "POST" and response.get("statusCode") == 200
                and seller_wide_request(params)
                and (start, end) in ((period.date_from, period.date_to), (following.date_from, following.date_to))
                and type(limit) is int and 0 < limit <= 1000 and type(offset) is int and offset >= 0):
                group = json.dumps({key: value for key, value in params.items() if key != "offset"}, sort_keys=True)
                seller_pages[group][offset] = (products, fetched, limit, body.get("currency"))
                if exact_pair and requested_past is not None and native_listing_valid(products, period):
                    native_pages[group][offset] = (products, fetched, limit, body.get("currency"))
        upsert(session, org, pending)
    # Reuse only a contiguous, unique, explicitly terminated sequence. A single
    # full page or a failed/interrupted listing proves no products were omitted.
    listings = [(pages, False, json.loads(group)["selectedPeriod"]) for group, pages in seller_pages.items()]
    listings += [(pages, True, json.loads(group)["selectedPeriod"]) for group, pages in native_pages.items()]
    from app.wb_sales_funnel_totals import SellerTotals
    for pages, native, dates in listings if account is not None else ():
        offset, returned, fetched_pages = 0, set(), []
        # The following report may contain an exact, complete past-period
        # listing too. Its raw request must prove the same full pagination.
        listing_period = Period(date.fromisoformat(dates["start"]), date.fromisoformat(dates["end"]))
        totals = SellerTotals(listing_period)
        while offset in pages:
            products, fetched, limit, currency = pages[offset]
            ids = [(p.get("product") or {}).get("nmId") for p in products]
            if (len(ids) > limit or any(type(nm) is not int or nm <= 0 for nm in ids)
                or len(set(ids)) != len(ids) or returned.intersection(ids)
                or (fetched_pages and fetched < fetched_pages[-1])):
                break
            returned.update(ids); fetched_pages.append(fetched)
            if not native:
                totals.add(products, currency=currency)
            if len(products) < limit:
                if not totals_only and listing_period == period:
                    save_enumeration(session, org, account, scope, period, returned, min(fetched_pages), native=native)
                if not native:
                    totals.save(session, org, account, scope, min(fetched_pages))
                break
            offset += limit


def project(session, org, account, scope, period, *, nm_ids=None):
    """Bound memory to one catalog batch and the selected (max 90-day) interval."""
    missing = defaultdict(list)
    last_id = 0
    while True:
        set_tenant_context(session, org)
        query = select(Product).where(
            Product.organization_id == org, Product.marketplace_account_id == account,
            Product.marketplace_product_id > last_id,
        )
        if nm_ids is not None:
            query = query.where(Product.external_product_id.in_([str(nm) for nm in nm_ids]))
        products = session.scalars(query.order_by(Product.marketplace_product_id).limit(250)).all()
        if not products:
            break
        last_id = products[-1].marketplace_product_id
        ids = {row.external_product_id for row in products}
        observations = defaultdict(list)
        facts = select(Cache.payload).where(
            Cache.organization_id == org, Cache.source_key.startswith(f"{scope}_fact_"),
            Cache.range_date_from >= period.date_from, Cache.range_date_to <= period.date_to,
            Cache.payload["nmId"].as_string().in_(ids),
        )
        for payload in session.scalars(facts.execution_options(yield_per=100)):
            observations[payload["nmId"]].append(payload)
        rows = []
        for product in products:
            nm = product.external_product_id
            values, gaps = aggregate(period, observations[nm])
            if not values["revenueShareFresh"]:
                # Exact-period WB percentages cannot be reconstructed from
                # incremental intervals. Reuse raw checkpoints before fetching.
                gaps = [(period.date_from, period.date_to)]
            for gap in gaps:
                if nm.isdigit():
                    missing[gap].append(int(nm))
            rows.append({"source_key": f"{scope}_page_{period.cache_key}_{nm}",
                         "range_date_from": period.date_from, "range_date_to": period.date_to,
                         "fetched_at": datetime.now(timezone.utc),
                         "payload": {**values, "nmId": nm, "dateTo": period.date_to.isoformat()}})
        upsert(session, org, rows)
    return missing


def read_page(session, org, account, scope, period, *, query="", brand="", sort="orders", direction="desc", page=1, page_size=50):
    set_tenant_context(session, org)
    catalog = catalog_state(session, org, account, scope)
    base = select(Product.external_product_id.label("nmId"), Product.title, Product.seller_article,
                  Product.brand, Product.image_url, Cache.payload, Cache.fetched_at).select_from(Product).outerjoin(Cache, and_(
        Cache.organization_id == org,
        Cache.source_key == f"{scope}_page_{period.cache_key}_" + Product.external_product_id,
    )).where(Product.organization_id == org, Product.marketplace_account_id == account)
    if query:
        term = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        base = base.where(or_(Product.title.ilike(term, escape="\\"), Product.seller_article.ilike(term, escape="\\"), Product.external_product_id.ilike(term, escape="\\")))
    if brand:
        base = base.where(Product.brand == brand)
    # Keep JSON filters above the catalog join; unrelated cache payloads can be megabytes.
    dataset = base.cte("funnel_catalog").prefix_with("MATERIALIZED", dialect="postgresql")
    numeric = lambda key: cast(dataset.c.payload[key].as_string(), BigInteger)
    trusted_share = dataset.c.payload["revenueSharePopulation"].as_string() == SHARE_POPULATION
    paired = and_(*(dataset.c.payload["fieldCoverage"][key].as_boolean().is_(True)
                    for key in ("visits", "carts")))
    # One bounded catalog scan for all summary statistics. Separate SELECTs
    # repeatedly materialized the same joined JSON projection before page 1.
    summary = session.execute(select(
        func.count().label("total"),
        *(func.sum(numeric(key)).label(f"sum_{key}") for key in FIELDS),
        *(func.sum(case((dataset.c.payload["fieldCoverage"][key].as_boolean().is_(True), 1), else_=0)).label(f"coverage_{key}") for key in FIELDS),
        func.sum(case((dataset.c.payload["complete"].as_boolean().is_(True), 1), else_=0)).label("complete"),
        func.sum(case((and_(trusted_share, dataset.c.payload["revenueShareFresh"].as_boolean().is_(True)), 1), else_=0)).label("shares"),
        func.min(dataset.c.fetched_at).label("oldest"),
        func.sum(case((paired, 1), else_=0)).label("paired"),
        func.sum(case((paired, numeric("visits")), else_=None)).label("paired_visits"),
        func.sum(case((paired, numeric("carts")), else_=None)).label("paired_carts"),
    ).select_from(dataset)).mappings().one()
    total, complete, shares_mapped = summary["total"], summary["complete"] or 0, summary["shares"] or 0
    totals = {key: summary[f"sum_{key}"] for key in FIELDS}
    coverage = {key: summary[f"coverage_{key}"] or 0 for key in FIELDS}
    oldest = summary["oldest"]
    fresh = oldest is not None and source_is_fresh({"fetchedAt": oldest.isoformat(), "dateTo": period.date_to.isoformat()})
    totals.update(impressions=None, ctr=None, cartConversion=percent(totals["carts"], totals["visits"]),
                  orderConversion=percent(totals["orders"], totals["carts"]),
                  buyoutPercent=percent(totals["buyouts"], (totals["buyouts"] or 0)+(totals["cancels"] or 0), 0))
    for key, inputs in (("cartConversion", ("carts", "visits")), ("orderConversion", ("orders", "carts")), ("buyoutPercent", ("buyouts", "cancels"))):
        if any(coverage[field] != total for field in inputs):
            totals[key] = None
    # The summary CR can still describe the available cohort. Both counts must
    # cover the exact period for the same products; unmatched rows are excluded.
    pair_count, visits, carts = summary["paired"], summary["paired_visits"], summary["paired_carts"]
    totals["cartConversion"] = percent(carts, visits)
    enumeration = fresh_enumeration(session, org, scope, period)
    ready = and_(dataset.c.payload["complete"].as_boolean().is_(True), trusted_share,
                 dataset.c.payload["revenueShareFresh"].as_boolean().is_(True))
    identities = session.execute(select(dataset.c.nmId, func.coalesce(ready, False))).all()
    unresolved = {nm for nm, is_ready in identities if not is_ready}
    checked = set(enumeration["checkedNmIds"]) if enumeration else set()
    refresh_required = catalog == "missing" or bool(total and (not fresh or unresolved - checked))
    returned = set(enumeration["returnedNmIds"]) if enumeration else set()
    unreported = sum(nm not in returned for nm, _ in identities) if enumeration else 0
    # Completeness of an exhausted provider report differs from coverage of
    # today's catalog. Omitted catalog cards do not make pagination incomplete.
    report_complete = bool(enumeration and not refresh_required and not (unresolved & returned))
    order = cast(dataset.c.nmId, BigInteger) if sort == "nmId" else (numeric(sort) if sort in FIELDS else dataset.c.payload[sort].as_float())
    if sort == "revenueShare":
        order = case((trusted_share, order), else_=None)
    page_query = select(dataset).order_by((order.asc() if direction == "asc" else order.desc()).nulls_last(), dataset.c.nmId).offset((page-1)*page_size).limit(page_size)
    if sort == "sku":
        # Only identities are materialized, not report payloads. Sort the full
        # filtered population BEFORE pagination; the response contract is unchanged.
        alphabet = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
        def sku_key(item):
            text = str(item.seller_article or item.nmId).casefold().replace("ё", "е")
            return tuple((0, int(part)) if part.isdigit() else (1, tuple(
                (0, alphabet.index(char)) if char in alphabet else (1, char) for char in part))
                for part in re.split(r"(\d+)", text))
        identities = session.execute(select(dataset.c.nmId, dataset.c.seller_article).order_by(dataset.c.nmId)).all()
        identities.sort(key=sku_key, reverse=direction == "desc")
        page_ids = [item.nmId for item in identities[(page-1)*page_size:page*page_size]]
        page_query = select(dataset).where(dataset.c.nmId.in_(page_ids)).order_by(
            case({nm: index for index, nm in enumerate(page_ids)}, value=dataset.c.nmId)) if page_ids else select(dataset).where(False)
    items = []
    for row in session.execute(page_query).mappings():
        metrics = dict(row["payload"] or {})
        metrics.setdefault("revenueShare", None)
        if metrics.get("revenueSharePopulation") != SHARE_POPULATION:
            # Legacy projections contain batch-local percentages. Never show
            # them while the existing queue rebuilds seller-wide evidence.
            metrics["revenueShare"] = None
        items.append({"nmId": row["nmId"], "title": row["title"], "sellerArticle": row["seller_article"],
                      "brand": row["brand"], "imageUrl": row["image_url"], "metrics": metrics,
                      "inReport": row["nmId"] in returned if enumeration else None})
    brands = session.scalars(select(Product.brand).where(Product.organization_id == org, Product.marketplace_account_id == account, Product.brand.is_not(None)).distinct().order_by(Product.brand)).all()
    # The diagram describes the seller's report, not only cards imported into
    # Product. Keep the table population/pagination and every row unchanged.
    from app.wb_sales_funnel_totals import read_seller_totals
    seller = read_seller_totals(session, org, account, scope, period) if not (query or brand) else None
    summary_fresh = bool(seller and source_is_fresh(seller))
    products_refresh_required = refresh_required
    if not (query or brand):
        totals = seller["totals"] if seller else dict.fromkeys(totals)
        refresh_required |= not summary_fresh
        pair_count, visits, carts = ((seller["products"], totals["visits"], totals["carts"]) if seller else (0, None, None))
    return {"items": items, "totals": totals, "total": total, "page": page, "pageSize": page_size,
            "complete": (catalog == "empty" or (complete == total and shares_mapped == total and fresh)) and (summary_fresh or bool(query or brand)), "coveredProducts": complete, "brands": brands,
            "catalogState": catalog,
            "refreshRequired": refresh_required, "unreportedProducts": unreported,
            "productsRefreshRequired": products_refresh_required,
            "reportComplete": report_complete,
            "summaryComplete": summary_fresh, "summaryProducts": seller["products"] if seller else 0,
            "conversionCoverage": {"cartConversion": {"products": pair_count or 0, "total": seller["products"] if seller else total,
                "visits": visits, "carts": carts}},
            "dateFrom": period.date_from.isoformat(), "dateTo": period.date_to.isoformat(),
            "unavailable": {"impressions": "Подключённый API WB не передаёт показы карточек; рекламные показы не подставляются.", "ctr": "Нет общего количества показов для расчёта CTR."}}


def metric_ready(metrics, key):
    if metrics.get(key) is None:
        return False
    if key == "revenueShare":
        return metrics.get("revenueSharePopulation") == SHARE_POPULATION and metrics.get("revenueShareFresh") is True
    fields = RATIO_INPUTS.get(key, (key,))
    coverage = metrics.get("freshFieldCoverage", metrics.get("fieldCoverage", {}) if metrics.get("complete") else {})
    return all(coverage.get(field) is True for field in fields)


def read_comparison(session, org, account, scope, period, nm_ids, *, include_missing=False):
    """Native percentages only. Previous projections and local growth formulas
    are deliberately not prerequisites or fallbacks for a provider delta.
    """
    set_tenant_context(session, org)
    past = previous_period(period)
    current_cache, seller_cache, batch_cache = aliased(Cache), aliased(Cache), aliased(Cache)
    query = select(Product.external_product_id, current_cache.payload, current_cache.fetched_at,
                   seller_cache.payload.label("seller"), batch_cache.payload.label("batch")).select_from(Product)
    for cache, prefix, suffix in ((current_cache, f"{scope}_page_{period.cache_key}_", ""),
                                  (seller_cache, native_prefix(scope, period) + "_", "_seller"),
                                  (batch_cache, native_prefix(scope, period) + "_", "_batch")):
        query = query.outerjoin(cache, and_(cache.organization_id == org,
            cache.source_key == prefix + Product.external_product_id + suffix))
    query = query.where(Product.organization_id == org, Product.marketplace_account_id == account)
    receipt = fresh_enumeration(session, org, scope, period, native=True)
    checked = set(receipt["checkedNmIds"]) if receipt else set()
    selected_omissions = fresh_enumeration(session, org, scope, period)
    omitted = (set(selected_omissions["checkedNmIds"]) - set(selected_omissions["returnedNmIds"])) if selected_omissions else set()
    items, missing, total, covered = [], [], 0, 0
    wanted = {str(nm) for nm in nm_ids}
    def valid(record, nm):
        return bool(record and record.get("source") == NATIVE_SOURCE and record.get("requestIdentity")
            and record.get("population") in {SHARE_POPULATION, "filtered"} and record.get("nmId") == nm
            and record.get("dateFrom") == str(period.date_from) and record.get("dateTo") == str(period.date_to)
            and record.get("previousDateFrom") == str(past.date_from) and record.get("previousDateTo") == str(past.date_to)
            and source_is_fresh(record))
    # Stream one bounded catalog scan; return only the requested visible rows.
    for row in session.execute(query.execution_options(yield_per=250)).mappings():
        total += 1
        nm, current = row["external_product_id"], row["payload"] or {}
        current_fresh = row["fetched_at"] is not None and source_is_fresh({"fetchedAt": row["fetched_at"].isoformat(), "dateTo": str(period.date_to)})
        records = [r for r in (row["seller"], row["batch"]) if valid(r, nm)]
        records.sort(key=lambda r: r["fetchedAt"], reverse=True)
        seller = next((r for r in records if r.get("population") == SHARE_POPULATION), None)
        dynamics, selected_values, previous_values, ready = {}, {}, {}, {}
        incoherent = False
        eligible = current_fresh and any(metric_ready(current, key) for key in NATIVE_FIELDS)
        for key in NATIVE_FIELDS:
            candidates = [r for r in records if key != "revenueShare" or r.get("population") == SHARE_POPULATION]
            compatible = next((r for r in candidates if metric_ready(current, key)
                               and current.get(key) == r.get("selectedValues", {}).get(key)), None)
            value = native_number((compatible or {}).get("dynamics", {}).get(key))
            is_ready = current_fresh and compatible is not None and value is not None
            dynamics[key] = value if is_ready else None
            selected_values[key] = (compatible or {}).get("selectedValues", {}).get(key)
            previous_values[key] = (compatible or {}).get("previousValues", {}).get(key)
            ready[key] = is_ready
            if (seller and metric_ready(current, key) and seller.get("selectedValues", {}).get(key) is not None
                and native_number(seller.get("dynamics", {}).get(key)) is not None):
                incoherent |= current.get(key) != seller.get("selectedValues", {}).get(key)
        if eligible and (incoherent or (not seller and nm not in checked and nm not in omitted)):
            missing.append(int(nm))
        covered += all(ready.values())
        if nm in wanted:
            items.append({"nmId": nm, "metrics": previous_values, "dynamics": dynamics,
                          "selectedValues": selected_values, "ready": ready})
    result = {"source": NATIVE_SOURCE, "items": items, "total": total, "complete": covered == total,
              "coveredProducts": covered, "refreshRequired": bool(missing), "reportComplete": not missing,
              "dateFrom": str(past.date_from), "dateTo": str(past.date_to),
              "selectedDateFrom": str(period.date_from), "selectedDateTo": str(period.date_to),
              "totals": {key: None for key in ("visits", "carts", "orders", "buyouts", "cartConversion")},
              "totalsUnavailable": "API WB не передаёт готовую динамику для этих итогов; локальный пересчёт не выполняется."}
    if include_missing:
        result["missingNmIds"] = missing
    return result


def collect(org, account, expected_scope, start, end, job_key, *, heartbeat=lambda: None, native_comparison=False, aggregate_comparison=False):
    from app.repricer_bff import _request_or_raise_sales_funnel_products
    from app.repricer_cache.store import save_source_cache
    from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest, build_wb_analytics_client
    from app.wb_api.report_reads import active_report_read, install_report_read
    period = Period(date.fromisoformat(start), date.fromisoformat(end))
    selected_period = period
    def status(state, **extra):
        heartbeat()
        save_source_cache(org, job_key, {"status": state, **extra}, strict=True)
    status("running")
    with get_session_factory()() as session:
        account, _, token = resolve_bound_wb_credential(session, org, marketplace_account_id=account)
        if scope_key(account, token) != expected_scope:
            raise RuntimeError("WB_ACCOUNT_CHANGED")
        context = install_report_read(org, token, job_key)
        try:
            from app.wb_sales_funnel_totals import SellerTotals, read_seller_totals, read_totals_comparison
            recovered_catalog = False
            if not (native_comparison or aggregate_comparison):
                recovered_catalog = ensure_catalog(session, org, account, expected_scope, token, heartbeat)
            if aggregate_comparison:
                if read_page(session, org, account, expected_scope, period, page_size=1)["refreshRequired"]:
                    raise RuntimeError("WB_FUNNEL_SELECTED_PERIOD_PENDING")
                period = previous_period(period)
            if aggregate_comparison:
                import_checkpoints(session, org, expected_scope, token, period, account, totals_only=True)
            else:
                import_checkpoints(session, org, expected_scope, token, period, account)
            if not (native_comparison or aggregate_comparison):
                # An exhausted provider listing remains exhaustive when local
                # identities are restored. Recheck its membership, not WB, and
                # retain the original observation time (never freshen evidence).
                for native in (False, True):
                    receipt = fresh_enumeration(session, org, expected_scope, period, native=native)
                    if receipt and (recovered_catalog or not receipt["checkedNmIds"]):
                        save_enumeration(session, org, account, expected_scope, period,
                            {int(nm) for nm in receipt["returnedNmIds"]},
                            datetime.fromisoformat(receipt["fetchedAt"]), native=native)
            seller = read_seller_totals(session, org, account, expected_scope, period)
            summary_required = not (seller and source_is_fresh(seller))
            missing = {} if aggregate_comparison else project(session, org, account, expected_scope, period)
            if aggregate_comparison:
                missing = {(period.date_from, period.date_to): []} if summary_required else {}
            elif native_comparison:
                current = read_page(session, org, account, expected_scope, period, page_size=1)
                if current["productsRefreshRequired"]:
                    raise RuntimeError("WB_FUNNEL_SELECTED_PERIOD_PENDING")
                pending = read_comparison(session, org, account, expected_scope, period, [], include_missing=True)["missingNmIds"]
                missing = {(period.date_from, period.date_to): pending} if pending else {}
            elif summary_required:
                # A fully projected local catalog is not proof of seller totals.
                # Reuse checkpoints first; otherwise enumerate one full report.
                ids = session.scalars(select(Product.external_product_id).where(
                    Product.organization_id == org, Product.marketplace_account_id == account)).all()
                missing = {(period.date_from, period.date_to): [int(nm) for nm in ids if nm.isdigit()]}
            enumeration = fresh_enumeration(session, org, expected_scope, period)
            checked = set(enumeration["checkedNmIds"]) if enumeration else set()
            client = RateLimitedWbApiClient(inner=build_wb_analytics_client("complete", token_override=token))
            requests = 0
            native_saved = False
            requested_ids, returned_ids = set(), set()
            for (first, last), nm_ids in sorted(missing.items()):
                seller_wide = (first, last) == (period.date_from, period.date_to)
                if seller_wide and not (native_comparison or aggregate_comparison or summary_required):
                    nm_ids = [nm for nm in nm_ids if str(nm) not in checked]
                    if not nm_ids:
                        continue
                batches = [nm_ids] if seller_wide else [nm_ids[offset:offset+1000] for offset in range(0, len(nm_ids), 1000)]
                for batch in batches:
                    requested_ids.update(batch)
                    wanted = set(batch)
                    page_offset = 0
                    seen = set()
                    listing_native_valid = True
                    listing_started = datetime.now(timezone.utc)
                    totals = SellerTotals(period)
                    while True:
                        past = previous_period(period)
                        request = WbApiRequest(method="POST", path=SOURCE, jsonBody={
                            "selectedPeriod": {"start": first.isoformat(), "end": last.isoformat()},
                            **({"pastPeriod": {"start": str(past.date_from), "end": str(past.date_to)}} if seller_wide and not aggregate_comparison else {}),
                            "nmIds": [] if seller_wide else batch, "skipDeletedNm": False, "limit": 1000, "offset": page_offset,
                        })
                        data = _request_or_raise_sales_funnel_products(client, request)
                        body = data.get("data") or data
                        if not isinstance(body.get("products"), list):
                            raise ValueError("WB_FUNNEL_INVALID_RESPONSE")
                        products = body["products"]
                        valid_native = seller_wide and native_listing_valid(products, period)
                        listing_native_valid &= valid_native
                        if native_comparison and not valid_native:
                            raise ValueError("WB_FUNNEL_COMPARISON_PERIOD_MISMATCH")
                        ids = [(p.get("product") or {}).get("nmId") for p in products]
                        if len(ids) > 1000 or any(type(nm) is not int or nm <= 0 for nm in ids) or len(set(ids)) != len(ids) or seen.intersection(ids):
                            raise ValueError("WB_FUNNEL_PAGINATION_INVALID")
                        seen.update(ids)
                        fetched = datetime.now(timezone.utc)
                        if seller_wide:
                            totals.add(products, currency=body.get("currency"))
                        if seller_wide and not aggregate_comparison:
                            identity = json.dumps([client.inner.base_url, sha256(token.encode()).hexdigest(), request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
                            upsert(session, org, native_rows(expected_scope, period, products, fetched,
                                currency=body.get("currency"), seller_wide=True,
                                request_identity="wb_report_http_" + sha256(identity.encode()).hexdigest()))
                        # Global WB percentages, projected only onto the local
                        # missing products. Never renormalize this subset.
                        products = [] if aggregate_comparison else [p for p in products if native_comparison or p["product"]["nmId"] in wanted]
                        returned_ids.update(p["product"]["nmId"] for p in products)
                        options = {"currency": body.get("currency"), "seller_wide": seller_wide}
                        upsert(session, org, observation_rows(expected_scope, first, last, products, fetched, **options))
                        if seller_wide and not aggregate_comparison:
                            native = past_observation_rows(expected_scope, period, products, fetched, **options)
                            upsert(session, org, native)
                            native_saved = native_saved or bool(native)
                        if not aggregate_comparison:
                            project(session, org, account, expected_scope, period,
                                    nm_ids=[p["product"]["nmId"] for p in products])
                        requests += 1
                        status("running", requests=requests)
                        if len(body["products"]) < 1000:
                            if seller_wide:
                                totals.save(session, org, account, expected_scope, listing_started)
                                if not aggregate_comparison:
                                    save_enumeration(session, org, account, expected_scope, period, seen, listing_started)
                                if listing_native_valid and not aggregate_comparison:
                                    save_enumeration(session, org, account, expected_scope, period, seen, listing_started, native=True)
                            break
                        page_offset += 1000
                        if page_offset > (1000000 if seller_wide else len(batch)):
                            raise ValueError("WB_FUNNEL_PAGINATION_INVALID")
            remaining = {} if aggregate_comparison else project(session, org, account, expected_scope, period)
            # Native past values are reusable by the independent comparison
            # reader/loader. This does not enqueue any additional period.
            if native_saved:
                project(session, org, account, expected_scope, previous_period(period))
            if aggregate_comparison:
                result = read_totals_comparison(session, org, account, expected_scope, selected_period, include_missing=True)
                status("complete" if result["complete"] else "partial", requests=requests,
                       missingPeriods=len(result["missingPeriods"]), settled=not result["refreshRequired"],
                       message=None if result["complete"] else "Часть итогов не имеет сопоставимых данных прошлого периода. Доступные значения сохранены.")
                return
            if native_comparison:
                result = read_comparison(session, org, account, expected_scope, period, [], include_missing=True)
                status("complete" if result["complete"] else "partial", requests=requests,
                       missingProducts=len(result["missingNmIds"]), settled=not result["refreshRequired"],
                       message=None if result["complete"] else "WB не передал часть готовой динамики. Доступные значения сохранены.")
                return
            missing_ids = {nm for ids in remaining.values() for nm in ids}
            omitted = len((requested_ids - returned_ids) & missing_ids)
            enumeration = fresh_enumeration(session, org, expected_scope, period)
            if enumeration:
                omitted = len((set(enumeration["checkedNmIds"]) - set(enumeration["returnedNmIds"]))
                              & {str(nm) for nm in missing_ids})
            message = None
            if missing_ids:
                message = f"Загрузка завершена. Нет полных свежих показателей для {len(missing_ids):,} товаров.".replace(",", " ")
                if omitted:
                    message += f" Из них {omitted:,} отсутствуют в ответе WB за выбранный период.".replace(",", " ")
            status("partial" if remaining else "complete", requests=requests,
                   missingProducts=len(missing_ids), omittedProducts=omitted, message=message)
        finally:
            active_report_read.reset(context)
