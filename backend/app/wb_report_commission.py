"""Reported WB commission evidence for ABC/P&L, separate from payout bridges."""
from app.repricer_bff import _date_from_any
from app.wb_api.financial_reports import money_kopecks

VERSION = "wb-reported-commission-v1"


def attach_reported_commission(result, rows, start, end):
    """Retain signed ppvzSalesCommission without changing other views' math.

    WB returns normally contain a positive commission to reverse. Some receipts
    already carry a negative reversal; preserve that sign instead of reversing
    it a second time. Non-return corrections retain their original sign.
    """
    amounts, daily, unknown, daily_unknown, seen = {}, {}, set(), set(), set()
    for row in rows:
        identity = str(row["rrdId"])
        if identity in seen:
            continue  # the base projector has already rejected conflicting IDs
        seen.add(identity)
        posted = _date_from_any(row.get("rrDate"))
        if posted is not None and not start <= posted <= end:
            continue
        nm = str(int(row.get("nmId") or 0))
        day = _date_from_any(row.get("saleDt")) or posted
        value = money_kopecks(row.get("ppvzSalesCommission"))
        if posted is None or value is None:
            if value != 0:
                unknown.add(nm)
                daily_unknown.add((str(day), nm))
            continue
        if str(row.get("docTypeName") or "").strip().casefold() == "возврат" and value > 0:
            value = -value
        amounts[nm] = amounts.get(nm, 0) + value
        point = (str(day), nm)
        daily[point] = daily.get(point, 0) + value

    def evidence(value, missing):
        return dict(reportedCommissionKopecks=None if missing else value,
                    reportedCommissionComplete=not missing,
                    reportedCommissionSource="ppvzSalesCommission")

    for nm, fact in result["aggregates"].items():
        fact.update(evidence(amounts.get(nm, 0), nm in unknown))
    for day, products in result["dailyAggregates"].items():
        for nm, fact in products.items():
            fact.update(evidence(daily.get((day, nm), 0), (day, nm) in daily_unknown))
    result["reportedAccountCommission"] = evidence(amounts.get("0", 0), "0" in unknown)
    result["reportedCommissionByNm"] = {nm: evidence(amounts.get(nm, 0), nm in unknown)
                                       for nm in amounts.keys() | unknown}
    result["reportedCommissionDaily"] = {}
    for (day, nm), value in daily.items():
        result["reportedCommissionDaily"].setdefault(day, {})[nm] = evidence(value, (day, nm) in daily_unknown)
    result.setdefault("diagnostics", {}).setdefault("rawExpenseTotals", {})["reportedCommissionKopecks"] = (
        None if unknown else sum(amounts.values()))
    result["reportedCommissionVersion"] = VERSION
    return result


def with_commission_products(result, start, end):
    """Add commission-only rows in ABC/P&L, never to RNP or other consumers."""
    missing = {nm: values for nm, values in result.get("reportedCommissionByNm", {}).items()
               if nm.isdigit() and int(nm) > 0 and nm not in result["aggregates"]
               and values.get("reportedCommissionKopecks")}
    if not missing:
        return result
    from copy import deepcopy
    from datetime import datetime
    from app.repricer_bff import aggregate_finance_report_rows
    result = deepcopy(result)
    for nm, values in missing.items():
        seed = aggregate_finance_report_rows([dict(nmId=int(nm), quantity=0,
            docTypeName="", saleDt=str(start), forPay=0)],
            date_from=datetime.combine(start, datetime.min.time()),
            date_to=datetime.combine(end, datetime.min.time()))["aggregates"][nm]
        seed.update(realizationRevenueKopecks=0, realizationGrossSalesKopecks=0,
                    realizationReturnsKopecks=0, settlementPayableConfirmed=True,
                    financeAdSpendAuthoritative=True, **values)
        result["aggregates"][nm] = seed
        for day, products in result["reportedCommissionDaily"].items():
            if nm in products:
                result["dailyAggregates"].setdefault(day, {})[nm] = {**seed, **products[nm]}
    return result
