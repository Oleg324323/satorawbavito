"""Synthetic observations for cache-only legacy RNP HTTP contracts."""

from datetime import timedelta


def install_rnp_cache(monkeypatch, date_from, date_to, *, bff=False):
    days = (date_to - date_from).days + 1
    rows = {
        "baskets": {"openCount": 100, "cartCount": 20, "orderCount": 10,
                    "orderSumKopecks": 100_000, "buyoutCount": 7,
                    "buyoutSumKopecks": 70_000},
        "ads": {"adSpendKopecks": 1_000, "adImpressions": 40, "adClicks": 8,
                "adCartAdds": 4, "adOrders": 2, "adSalesKopecks": 20_000},
    }
    caches = {}
    for source, row in rows.items():
        key = f"{source}_{date_from.isoformat()}_{date_to.isoformat()}"
        caches[key] = {
            "sourceKey": key, "dateFrom": date_from.isoformat(),
            "aggregateComplete": True,
            "dateTo": date_to.isoformat(), "dailyAggregatesDays": days,
            "dailyAggregates": {
                (date_from + timedelta(days=i)).isoformat(): {"101": dict(row)}
                for i in range(days)
            },
            "aggregates": {"101": {name: value * days for name, value in row.items()}},
        }

    def read(_org, key, **_kwargs):
        return caches.get(key)

    def ranges(_org, prefix, **_kwargs):
        return [value for key, value in caches.items() if key.startswith(prefix)]

    for module in ["app.wb_api.rnp_runtime"] + (["app.routers.wb_reports_bff"] if bff else []):
        monkeypatch.setattr(f"{module}.get_source_cache", read)
        monkeypatch.setattr(f"{module}.list_source_cache_ranges_by_prefix", ranges)
        monkeypatch.setattr(f"{module}.save_source_cache", lambda *_args, **_kwargs: None)
    if bff:
        monkeypatch.setattr("app.routers.wb_reports_bff._report_missing_source_ranges", lambda *a, **kw: [])
        # This test targets cache parsing/HTTP shape, not settings rule decoration.
        monkeypatch.setattr("app.routers.wb_reports_bff._apply_report_rules_to_payload", lambda payload, *_args, **_kwargs: payload)
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda org, start, end: {
        "sourceComplete": True, "aggregates": {"101": {"netSalesUnits": 3, "realizationRevenueKopecks": 30000}},
        "dateFrom": str(start), "dateTo": str(end), "missingDates": []})
