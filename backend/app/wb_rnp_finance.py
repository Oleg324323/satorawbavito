"""RNP's financial columns, without changing its operational funnel/cohorts."""
from datetime import timedelta

from app.repricer_page_finance import report_revenue_kopecks


def apply_financial_buyouts(rows, org, start, end, *, finance_allowed):
    from app.wb_report_finance import load_report_finance
    from app.wb_api.rnp_runtime import _rnp_row_from_sources

    copied = [row.model_copy(deep=True) for row in rows]
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - (end - start)
    current = load_report_finance(org, start, end) if finance_allowed else {}
    previous = load_report_finance(org, previous_start, previous_end) if finance_allowed else {}
    facts = current.get("aggregates", {})
    indexed = {str(row.nmId): row for row in copied}
    for nm, fact in facts.items():
        if str(nm) in indexed or not str(nm).isdigit() or int(nm) <= 0:
            continue
        row = _rnp_row_from_sources({"nmId": int(nm), "sku": fact.get("vendorCode") or str(nm)},
                                    None, "partial", "medium")
        for field in ("openCount", "cartCount", "orderCount", "orderSumKopecks", "buyoutCount",
                      "buyoutSumKopecks", "buyoutPct", "atcrPct", "cartToOrderPct"):
            setattr(row, field, None)
        row.reasons.append("financial_only")
        copied.append(row)
    complete = current.get("sourceComplete") is True
    def delta(now, old):
        return (now - old) / abs(old) * 100 if type(now) is int and type(old) is int and old else None
    for row in copied:
        nm = str(row.nmId)
        row.funnelBuyoutCount, row.funnelBuyoutSumKopecks = row.buyoutCount, row.buyoutSumKopecks
        fact = facts.get(nm)
        observed = fact is not None
        row.buyoutCount = fact.get("netSalesUnits") if observed else 0 if complete else None
        row.buyoutSumKopecks = report_revenue_kopecks(fact) if observed else 0 if complete else None
        row.financialSalesUnits = fact.get("salesUnits") if observed else 0 if complete else None
        row.financialReturnsUnits = fact.get("returnsUnits") if observed else 0 if complete else None
        row.financialBuyoutState = ("forbidden" if not finance_allowed else "confirmed" if complete
                                    else "partial" if observed else "unavailable")
        row.financialBuyoutSource = "finance.rrDate.retailAmount.netSalesUnits" if finance_allowed else None
        old = previous.get("aggregates", {}).get(nm, {})
        compare = complete and previous.get("sourceComplete") is True
        row.buyoutCountDeltaPct = delta(row.buyoutCount, old.get("netSalesUnits", 0)) if compare else None
        row.buyoutSumDeltaPct = delta(row.buyoutSumKopecks, report_revenue_kopecks(old) or 0) if compare else None
    return {"rows": copied, "complete": complete,
            "coverage": {label: {"complete": cache.get("sourceComplete") is True,
                "missingDates": cache.get("missingDates", [] if cache.get("sourceComplete") else
                    [str(left + timedelta(days=i)) for i in range((right-left).days+1)]),
                "fetchedAt": cache.get("fetchedAt"), "dateFrom": str(left), "dateTo": str(right),
                "unallocated": cache.get("unallocatedFinancials", {}),
                "unallocatedRealization": cache.get("unallocatedRealization", {})}
                for label, cache, left, right in (("selected", current, start, end),
                    ("previous", previous, previous_start, previous_end))}}
