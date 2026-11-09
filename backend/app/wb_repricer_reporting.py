"""Read-only period presentation. No strategy inputs, jobs or marketplace calls."""
from datetime import date, timedelta

VERSION = "wb-repricer-comparison-v1"
CAMPAIGN_ADVERTISING_VERSION = "wb-repricer-campaign-advertising-v1"


def campaign_advertising(cache, start, end):
    """Read the already period-scoped Ads cache, never financial cash postings.

    Legacy successful fullstats snapshots have no sourceComplete flag. Their
    exact bounds, source and fetchedAt identify a finished observation; explicit
    partial/missing coverage always takes precedence. No provider calls here.
    """
    start, end = str(start)[:10], str(end)[:10]
    dated = str(cache.get("dateFrom"))[:10] == start and str(cache.get("dateTo"))[:10] == end
    if not dated or not cache.get("fetchedAt") or not isinstance(cache.get("aggregates"), dict):
        return dict(state="no_data", adSpendKopecks=None, aggregates={})
    aggregates = cache["aggregates"]
    total = (cache.get("totals") or {}).get("adSpendKopecks")
    valid = all(isinstance(row, dict) and type(row.get("adSpendKopecks")) is int for row in aggregates.values())
    if type(total) is not int:
        total = sum(row["adSpendKopecks"] for row in aggregates.values()) if valid else None
    requested = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    partial = (cache.get("coverageState") not in {None, "ok"} or cache.get("sourceComplete") is False
               or bool(cache.get("missingDates")) or (cache.get("coveredDays") is not None and cache["coveredDays"] < requested) or not valid)
    return dict(state="partial" if partial else "ok", adSpendKopecks=total, aggregates=aggregates)


def apply_campaign_advertising(rows, ledger, advertising):
    """Change only the repricer's advertising expense, never its financial ledger.

    Keep settlement cash payouts and their allocation blockers intact. Economic
    profit after campaign spend is a separately named measure. This also avoids
    silently certifying final profit when cabinet fees are still unattributed.
    """
    for row in rows:
        analytics = row.setdefault("analytics", {})
        if analytics.get("campaignAdvertisingVersion") == CAMPAIGN_ADVERTISING_VERSION:
            continue
        nm = str((row.get("meta") or {}).get("nmId"))
        fact = (ledger.get("aggregates") or {}).get(nm, {})
        observation = advertising["aggregates"].get(nm, {})
        spend = observation.get("adSpendKopecks", 0 if advertising["state"] == "ok" else None)
        spend = spend if type(spend) is int else None
        previous = analytics.get("adSpendKopecks")
        financial = fact.get("adSpendKopecks")
        analytics.update(campaignAdvertisingVersion=CAMPAIGN_ADVERTISING_VERSION,
                         adSpendSource="wb_ads_fullstats", adSpendState=advertising["state"],
                         financeAdSpendKopecks=financial, adSpendKopecks=spend,
                         adDataAvailable=spend is not None,
                         adsState=advertising["state"] if spend is not None else "no_data")
        delta = spend - previous if type(spend) is int and type(previous) is int else None
        for field in ("expensesKopecks", "netProfitKopecks", "factNetProfitKopecks"):
            value = analytics.get(field)
            analytics[field] = value + (delta if field == "expensesKopecks" else -delta) if type(value) is int and delta is not None else None
        # Settlement profit contains financial advertising, not whichever legacy
        # expense happened to supply the row's display value.
        delta = spend - financial if type(spend) is int and type(financial) is int else None
        for source, target in (("settlementProfitKopecks", "campaignProfitKopecks"),
                               ("settlementAttributedProfitKopecks", "campaignAttributedProfitKopecks")):
            value = analytics.get(source)
            analytics[target] = value - delta if type(value) is int and delta is not None and advertising["state"] == "ok" else None
        if "marginPct" in analytics:
            profit = analytics["campaignProfitKopecks"]
            if profit is None and analytics.get("settlementBlockers") == ["settlement_account_expenses_unreconciled"]:
                profit = analytics["campaignAttributedProfitKopecks"]
            revenue = analytics.get("revenueKopecks")
            analytics["marginPct"] = round(profit / revenue * 100, 1) if type(profit) is int and type(revenue) is int and revenue else None


def filtered_campaign_advertising(rows):
    facts = [row.get("analytics") or {} for row in rows]
    states = {fact.get("adSpendState", "no_data") for fact in facts}
    amounts = [fact.get("adSpendKopecks") for fact in facts]
    return dict(state="ok" if states <= {"ok"} else "no_data" if states == {"no_data"} else "partial",
                adSpendKopecks=sum(amounts) if all(type(value) is int for value in amounts) else None)


def project_buyout_comparison(rows, start, end):
    """Only the previous-period trade comparison, never an expense/payout ledger.

    The existing loader owns account isolation, raw-source coverage and cursor
    completeness. Reuse its deduplicated operations without pricing, expense,
    storage-allocation or economic calculations that this comparison cannot use.
    """
    from app.repricer_bff import _date_from_any
    from app.wb_api.financial_reports import is_merchandise, money_kopecks

    aggregates, seen = {}, {}
    residual = dict(netSalesUnits=0, revenueKopecks=0)
    for row in rows:
        posted = _date_from_any(row.get("rrDate"))
        if posted is None or not start <= posted <= end:
            continue
        identity = str(row.get("rrdId") or "")
        if not identity:
            raise ValueError("WB_FINANCE_INVALID_ROW_ID")
        if identity in seen:
            if seen[identity] != row:
                raise ValueError("WB_FINANCE_CONFLICTING_ROWS")
            continue
        seen[identity] = row
        if not is_merchandise(row):
            continue
        amount = money_kopecks(row.get("retailAmount"))
        if amount is None:
            raise ValueError("WB_FINANCE_REALIZATION_AMOUNT_MISSING")
        quantity = row.get("quantity")
        if type(quantity) is not int or quantity < 0:
            raise ValueError("WB_FINANCE_INVALID_TRADE_QUANTITY")
        if not _date_from_any(row.get("saleDt")):
            raise ValueError("WB_FINANCE_TRADE_DATE_MISSING")
        nm = int(row.get("nmId") or 0)
        if nm < 0:
            continue  # Match the full ledger's positive-product/zero-residual universe.
        sign = -1 if str(row.get("docTypeName")).strip().casefold() == "возврат" else 1
        if nm == 0:
            residual["netSalesUnits"] += sign * quantity
            residual["revenueKopecks"] += sign * amount
        else:
            item = aggregates.setdefault(str(nm), dict(netSalesUnits=0, realizationRevenueKopecks=0))
            item["netSalesUnits"] += sign * quantity
            item["realizationRevenueKopecks"] += sign * amount
    return dict(aggregates=aggregates, diagnostics=dict(unallocatedRealization=residual),
                comparisonProjectionVersion=VERSION)


def load_buyout_comparison(organization_id, start, end, *, memo=None):
    from app.repricer_page_finance import load_page_finance, page_connection

    key = (organization_id, "repricer_buyout_comparison", page_connection.get(), start, end)
    if memo is not None and key in memo:
        return memo[key]
    # Reuse a valid full projection when already saved. Only a cold comparison
    # needs the trade-only projection; it must not rebuild the expense ledger.
    result = load_page_finance(organization_id, start, end, cached_only=True)
    if result is None:
        result = load_page_finance(organization_id, start, end, projector=project_buyout_comparison)
    if memo is not None:
        memo[key] = result
    return result


def period_coverage(payload, start, end):
    start = date.fromisoformat(str(start)[:10])
    end = date.fromisoformat(str(end)[:10])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    covered = max(0, min(len(days), int(payload.get("coveredDays", len(days) if payload else 0) or 0)))
    state = payload.get("coverageState") or ("ok" if payload else "no_data")
    if not covered:
        state = "no_data"
    missing = [day for day in (payload.get("missingDates") or []) if day in days]
    if not covered:
        missing = days
    elif state == "ok":
        missing = []
    return dict(state=state, coveredDays=covered, requestedDays=len(days), missingDates=missing,
                missingDatesKnown=state == "ok" or len(missing) == len(days) - covered)


def financial_totals(ledger, nm_ids=None):
    from app.repricer_page_finance import report_revenue_kopecks
    complete = ledger.get("sourceComplete") is True
    if not complete and not ledger.get("coveredDays") and not ledger.get("fetchedAt"):
        return None, None
    aggregates = ledger.get("aggregates") or {}
    selected = [row for nm, row in aggregates.items() if nm_ids is None or str(nm) in nm_ids]
    units = sum(int(row.get("netSalesUnits", int(row.get("salesUnits") or 0) - int(row.get("returnsUnits") or 0))) for row in selected)
    amounts = [report_revenue_kopecks(row) for row in selected]
    amount = sum(amounts) if all(type(value) is int for value in amounts) else None
    if nm_ids is None:
        residual = (ledger.get("diagnostics") or {}).get("unallocatedRealization") or {}
        units += int(residual.get("netSalesUnits") or 0)
        if amount is not None:
            amount += int(residual.get("revenueKopecks") or 0)
    return units, amount


def apply_financial_row_state(rows, ledger):
    """Never expose operational quantities as financial observations.

    Only the read projection is changed; execution/planning keeps its own inputs.
    A completed merchandise report can establish a zero trade for an omitted SKU,
    not a zero funnel or a confirmed allocated profit.
    """
    complete = ledger.get("sourceComplete") is True
    known = ledger.get("aggregates") or {}
    for row in rows:
        analytics = row.setdefault("analytics", {})
        nm = str((row.get("meta") or {}).get("nmId"))
        if nm in known:
            analytics["financialTradeState"] = "ok" if complete else "partial"
            continue
        analytics["financialTradeState"] = "ok" if complete else "no_data"
        for key in ("salesUnits", "returnsUnits", "grossSalesUnits", "grossSalesAmountKopecks",
                    "buyoutUnits", "buyoutAmountKopecks", "revenueKopecks", "realizationRevenueKopecks"):
            analytics[key] = 0 if complete else None


def apply_summary_availability(summary, coverage, ledger, nm_ids=None):
    summary["financialCoverageState"] = coverage["finance"]["state"]
    summary["buyoutUnits"], summary["buyoutAmountKopecks"] = financial_totals(ledger, nm_ids)
    if coverage["finance"]["state"] != "ok" and "campaignProfitKopecks" in summary:
        summary["campaignProfitKopecks"] = None
    if coverage["finance"]["state"] == "no_data":
        for key in ("revenueKopecks", "salesUnits", "returnsUnits", "cogsKopecks", "expensesKopecks",
                    "marginKopecks", "taxKopecks", "settlementProfitKopecks", "finalPayoutKopecks"):
            summary[key] = None
        if summary.get("adSpendSource") != "wb_ads_fullstats":
            summary["adSpendKopecks"] = None
    summary["totalBasketsState"] = coverage["baskets"]["state"]
    if coverage["baskets"]["state"] == "no_data":
        summary["totalBaskets"] = None
    return summary


REPRICER_ECONOMICS_VERSION = "wb-stock-campaign-no-deduction-v1"


def apply_repricer_economics(rows, ledger, advertising, contexts):
    """One product profit basis for margin and ABC; preserve the cash ledger.

    Storage is already allocated by current WB stock in the shared projection.
    Campaign spend replaces financial advertising; deductions are excluded by
    the owner's policy. Classify the whole report universe before pagination.
    """
    from app.repricer_bff import cumulative_abc, settlement_payable_confirmed, settlement_profit_metrics, SETTLEMENT_ABC_VERSION
    facts = ledger.get("aggregates") or {}
    ads = advertising.get("aggregates") or {}
    complete = ledger.get("sourceComplete") is True
    allocation_complete = bool(contexts) and all(item.get("campaignAllocationConfirmed") is True for item in contexts.values())
    included_costs = ("logisticsKopecks", "storageKopecks", "acceptanceKopecks", "penaltyKopecks", "loyaltyCostKopecks")
    metrics = {}
    empty_settlements = {}
    for nm in facts.keys() | ads.keys():
        fact = facts.get(nm)
        context = contexts.get(nm, {})
        # A complete ledger establishes no operations for an ads-only product.
        if fact is None and complete:
            fact = dict.fromkeys((*included_costs, "payableKopecks", "additionalPaymentKopecks", "salesUnits", "adSpendKopecks", "deductionKopecks"), 0)
            fact["settlementPayableConfirmed"] = True
            context = dict(settlementCogsKopecks=0, taxKopecks=0, factTaxState="configured",
                campaignAllocationConfirmed=allocation_complete, settlementAllocationConfirmed=False)
            empty_settlements[nm] = settlement_profit_metrics(fact, context)
        fact = fact or {}
        spend = ads.get(nm, {}).get("adSpendKopecks", 0 if advertising["state"] == "ok" else None)
        blockers = []
        if not complete:
            blockers.append("campaign_finance_incomplete")
        if not settlement_payable_confirmed(fact):
            blockers.append("settlement_payable_unconfirmed")
        if any(type(fact.get(field)) is not int for field in (*included_costs, "additionalPaymentKopecks")):
            blockers.append("settlement_components_missing")
        if type(context.get("settlementCogsKopecks")) is not int:
            blockers.append("settlement_dated_cogs_missing")
        if context.get("factTaxState") != "configured" or type(context.get("taxKopecks")) is not int:
            blockers.append("settlement_tax_missing")
        if advertising["state"] != "ok" or type(spend) is not int:
            blockers.append("campaign_advertising_incomplete")
        attributed = None if blockers else (fact["payableKopecks"] - sum(fact[field] for field in included_costs)
            + fact["additionalPaymentKopecks"] - context["settlementCogsKopecks"] - context["taxKopecks"] - spend)
        if context.get("campaignAllocationConfirmed") is not True:
            blockers.append("settlement_account_expenses_unreconciled")
        metrics[nm] = dict(campaignProfitKopecks=None if blockers else attributed,
            campaignAttributedProfitKopecks=attributed, campaignBlockers=blockers)
    sales = cumulative_abc({nm: fact.get("salesUnits") for nm, fact in facts.items()})
    ad_values = [row.get("adSpendKopecks") for row in ads.values()]
    ads_attributed = (advertising["state"] == "ok" and all(type(value) is int for value in ad_values)
                      and sum(ad_values) == advertising.get("adSpendKopecks"))
    profits = cumulative_abc({nm: value["campaignProfitKopecks"] if ads_attributed else None for nm, value in metrics.items()})
    for row in rows:
        nm = str((row.get("meta") or {}).get("nmId"))
        analytics = row.setdefault("analytics", {})
        if analytics.get("repricerEconomicsVersion") == REPRICER_ECONOMICS_VERSION:
            continue
        value = metrics.get(nm, dict(campaignProfitKopecks=None, campaignAttributedProfitKopecks=None,
                                    campaignBlockers=["campaign_product_not_observed"]))
        analytics.update(value, repricerEconomicsVersion=REPRICER_ECONOMICS_VERSION,
                         abcProfitSource="campaignProfitKopecks", abcPolicyVersion=SETTLEMENT_ABC_VERSION)
        if nm in empty_settlements:
            analytics.update(empty_settlements[nm], taxKopecks=0, factTaxState="configured", factTaxReason=None)
        first, second = sales.get(nm), profits.get(nm)
        analytics.update(salesClass=first, profitClass=second,
            abcCode=f"{first or '—'}{second or '—'}",
            abcReason=None if first and second else "Не все исходные данные прибыли товаров загружены")
        deduction = (facts.get(nm) or {}).get("deductionKopecks")
        if type(analytics.get("expensesKopecks")) is int and type(deduction) is int:
            analytics["expensesKopecks"] -= deduction
        if nm in metrics:
            analytics["financeState"] = "ok" if complete else "no_data"
        analytics["netProfitKopecks"] = analytics["factNetProfitKopecks"] = value["campaignProfitKopecks"]
        profit, revenue = value["campaignProfitKopecks"], analytics.get("revenueKopecks")
        if "marginPct" in analytics:
            analytics["marginPct"] = round(profit / revenue * 100, 1) if type(profit) is int and type(revenue) is int and revenue else None
