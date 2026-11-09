"""Financial chart projection: DB only, same posting membership as the table."""
from datetime import timedelta


def financial_daily_series(org, nm_id, start, end):
    from app.wb_report_finance import load_report_finance
    cache = load_report_finance(org, start, end)
    missing = set(cache.get("missingDates", []))
    available = cache.get("source") == "finance_sales_reports_detailed"
    values = {}
    for offset in range((end - start).days + 1):
        day = str(start + timedelta(days=offset))
        covered = available and day not in missing
        fact = cache.get("postingDailyRealization", {}).get(day, {}).get(str(nm_id), {})
        values[day] = {key: fact.get(key, 0) if covered else None
                       for key in ("salesUnits", "returnsUnits", "revenueKopecks")}
        values[day]["grossSalesUnits"] = values[day]["salesUnits"]
        values[day]["salesUnits"] = fact.get("netSalesUnits", 0) if covered else None
    return {"daily": values, "state": "fresh" if cache.get("sourceComplete") else "partial",
            "coverage": {"complete": cache.get("sourceComplete") is True,
                         "dateFrom": str(start), "dateTo": str(end),
                         "quantityBasis": "netSalesUnits", "dateBasis": "rrDate", "moneyBasis": "retailAmount",
                         "missingDates": sorted(missing) if available else list(values)}}
