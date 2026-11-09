from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import app.wb_reports_sprint_d as sprint_d


def test_spp_history_carries_daily_price_and_source(monkeypatch):
    daily = {
        "2026-10-02": {"443894764": {"sppPct": 20, "avgPriceWithSppKopecks": 99_000}},
        "2026-10-05": {"443894764": {"sppPct": 25.5, "avgPriceWithSppKopecks": 95_000}},
    }
    monkeypatch.setattr(sprint_d, "_period_cache", lambda *args, **kwargs: {"dailyAggregates": daily})
    monkeypatch.setattr(sprint_d, "list_cached_goods", lambda organization_id: [])

    result = sprint_d.abc_spp_by_nm(1, date(2026, 9, 22), date(2026, 10, 5))["443894764"]

    assert result["sppPct"] == 25.5
    assert result["sppObservedOn"] == "2026-10-05"
    assert len(result["sppHistory"]) == 14
    by_day = {point["date"]: point for point in result["sppHistory"]}
    assert by_day["2026-10-02"] == {"date": "2026-10-02", "pct": 20, "buyerPriceKopecks": 99_000, "source": "orders"}
    assert by_day["2026-10-03"] == {"date": "2026-10-03", "pct": None, "buyerPriceKopecks": None, "source": None}


def test_latest_spp_source_matches_the_observation_not_an_older_extension_price(monkeypatch):
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Europe/Moscow")).date()
    yesterday = today - timedelta(days=1)
    monkeypatch.setattr(sprint_d, "_period_cache", lambda *args, **kwargs: {
        "dailyAggregates": {str(today): {"443894764": {"sppPct": 15, "avgPriceWithSppKopecks": 85_000}}}})
    monkeypatch.setattr(sprint_d, "list_cached_goods", lambda org: [{
        "nmID": 443894764, "buyerPriceObservedAt": (now - timedelta(days=1)).isoformat()}])
    monkeypatch.setattr(sprint_d, "_good_seller_price_kopecks", lambda good: 100_000)
    monkeypatch.setattr(sprint_d, "_good_buyer_price_no_wallet_kopecks", lambda good: 80_000)

    result = sprint_d.abc_spp_by_nm(1, today - timedelta(days=6), today)["443894764"]

    assert (result["sppPct"], result["sppObservedOn"], result["sppSource"]) == (
        15, str(today), "supplier.orders.spp")
    assert next(point for point in result["sppHistory"] if point["date"] == str(yesterday))["source"] == "extension"
