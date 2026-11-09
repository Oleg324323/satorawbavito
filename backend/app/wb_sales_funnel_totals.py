"""Seller-wide funnel totals. Never restrict the provider population to Product."""
from decimal import Decimal

from sqlalchemy import select

from app import wb_sales_funnel as funnel

SOURCE = "calculated_wb_sales_funnel_totals_v1"
SNAPSHOT_SOURCE = "wb_sales_funnel_seller_totals_v2"
COUNTS = ("visits", "carts", "orders", "buyouts")
METRICS = (*COUNTS, "cartConversion")


def change(current, previous):
    if current is None or previous is None:
        return None
    if previous == 0:
        return 0.0 if current == 0 else None
    return float((Decimal(current) - Decimal(previous)) * 100 / Decimal(previous))


def snapshot_key(scope, period):
    return f"{scope}_seller_totals_v2_{period.cache_key}"


def ratios(totals):
    return {**totals, "impressions": None, "ctr": None,
        "cartConversion": funnel.percent(totals["carts"], totals["visits"]),
        "orderConversion": funnel.percent(totals["orders"], totals["carts"]),
        "buyoutPercent": funnel.percent(totals["buyouts"],
            totals["buyouts"] + totals["cancels"] if totals["buyouts"] is not None and totals["cancels"] is not None else None, 0)}


class SellerTotals:
    """Bounded accumulator; publish only after unique, terminated pagination.

    Callers validate account credentials, request filters, offsets and IDs. This
    accumulator runs BEFORE the local-catalog filter and never writes card data.
    """
    def __init__(self, period):
        self.period = period
        self.count = 0
        self.selected = dict.fromkeys(funnel.FIELDS, 0)
        self.previous = dict.fromkeys(funnel.FIELDS, 0)
        self.past_valid = True

    def add(self, products, *, currency):
        expected = {"start": str(self.period.date_from), "end": str(self.period.date_to)}
        for product in products:
            selected = (product.get("statistic") or {}).get("selected") or {}
            if selected.get("period") and selected["period"] != expected:
                raise ValueError("WB_FUNNEL_PERIOD_MISMATCH")
            self.count += 1
            self.past_valid &= funnel.native_period_matches(product, self.period)
            for sums, value in ((self.selected, product), (self.previous,
                {"statistic": {"selected": (product.get("statistic") or {}).get("past") or {}}})):
                metrics = funnel.normalized_metrics(value)
                for key in funnel.FIELDS:
                    number = metrics[key] if key not in funnel.AMOUNTS or currency == "RUB" else None
                    sums[key] = sums[key] + number if sums[key] is not None and number is not None else None

    def save(self, session, org, account, scope, fetched):
        periods = [(self.period, self.selected)]
        # An empty selected response does not state a past period. In that case
        # load the preceding listing separately rather than assume a past zero.
        if self.count and self.past_valid:
            periods.append((funnel.previous_period(self.period), self.previous))
        funnel.upsert(session, org, [{"source_key": snapshot_key(scope, period),
            "range_date_from": period.date_from, "range_date_to": period.date_to, "fetched_at": fetched,
            "payload": {"source": SNAPSHOT_SOURCE, "account": account, "population": funnel.SHARE_POPULATION,
                "dateFrom": str(period.date_from), "dateTo": str(period.date_to), "fetchedAt": fetched.isoformat(),
                "completeListing": True, "products": self.count, "totals": ratios(values)}}
            for period, values in periods])


def read_seller_totals(session, org, account, scope, period):
    funnel.set_tenant_context(session, org)
    payload = session.scalar(select(funnel.Cache.payload).where(funnel.Cache.organization_id == org,
        funnel.Cache.source_key == snapshot_key(scope, period)))
    if (payload and payload.get("source") == SNAPSHOT_SOURCE and payload.get("account") == account
        and payload.get("population") == funnel.SHARE_POPULATION and payload.get("completeListing") is True
        and payload.get("dateFrom") == str(period.date_from) and payload.get("dateTo") == str(period.date_to)):
        return payload
    return None


def read_totals_comparison(session, org, account, scope, period, *, include_missing=False):
    past = funnel.previous_period(period)
    selected = read_seller_totals(session, org, account, scope, period)
    previous = read_seller_totals(session, org, account, scope, past)
    current_fresh = bool(selected and funnel.source_is_fresh(selected))
    previous_fresh = bool(previous and funnel.source_is_fresh(previous))
    current_totals = (selected or {}).get("totals", {})
    previous_totals = (previous or {}).get("totals", {})
    # Weighted CR uses full counts, not the mean of product percentages or the
    # rounded displayed CR. All other changes use the same full-cabinet totals.
    def cr(values):
        return (Decimal(values["carts"]) * 100 / values["visits"]
            if values.get("carts") is not None and values.get("visits") else None)
    totals, ready, unavailable = {}, {}, {}
    for key in METRICS:
        current_value = cr(current_totals) if key == "cartConversion" else current_totals.get(key)
        previous_value = cr(previous_totals) if key == "cartConversion" else previous_totals.get(key)
        value = change(current_value, previous_value) if current_fresh and previous_fresh else None
        totals[key], ready[key] = value, value is not None
        unavailable[key] = ("Нет полных свежих итогов выбранного периода." if not current_fresh else
            "Нет полных свежих итогов прошлого периода." if not previous_fresh else
            "WB не передал показатель для всего отчёта либо процент изменения не определён (нулевая база).") if value is None else ""
    missing_periods = [p for p, fresh in ((period, current_fresh), (past, previous_fresh)) if not fresh]
    result = {"source": SOURCE, "population": funnel.SHARE_POPULATION,
        "dateFrom": str(past.date_from), "dateTo": str(past.date_to),
        "selectedDateFrom": str(period.date_from), "selectedDateTo": str(period.date_to),
        "totals": totals, "selectedTotals": {key: current_totals.get(key) for key in METRICS},
        "previousTotals": {key: previous_totals.get(key) for key in METRICS}, "ready": ready, "unavailable": unavailable,
        "coverage": {key: {"products": (selected or {}).get("products", 0),
            "matchedProducts": (previous or {}).get("products", 0)} for key in METRICS},
        "total": (selected or {}).get("products", 0), "complete": all(ready.values()),
        "refreshRequired": bool(missing_periods), "reportComplete": not missing_periods}
    if include_missing:
        result["missingPeriods"] = missing_periods
    return result
