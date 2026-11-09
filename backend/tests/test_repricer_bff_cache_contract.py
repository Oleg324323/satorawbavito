from dataclasses import replace

import pytest

from app import repricer_bff as repricer_bff_module


@pytest.mark.parametrize("mode,token,demo", [
    ("real", None, False),
    ("real", "synthetic", False),
    ("fake", None, True),
    ("fake", "synthetic", False),
])
def test_cached_sku_uses_demo_only_in_explicit_fake_mode(monkeypatch, mode, token, demo):
    settings = replace(repricer_bff_module.get_settings(), wb_api_mode=mode)
    monkeypatch.setattr(repricer_bff_module, "get_settings", lambda: settings)
    row = repricer_bff_module.list_repricer_skus(
        wb_token=token,
        include_promotions=False,
        include_content=False,
        cached_goods=[{"vendorCode": "CACHE_ONLY_123", "nmID": 123456,
                       "sizes": [{"price": 1199, "discountedPrice": 1199}]}],
        cached_content_cards=[], cached_promotions=[],
        cached_stock_aggregates={}, stocks_cache_loaded=False,
        cached_period_stats={}, cached_finance_aggregates={},
        cached_ads_aggregates={}, cached_baskets_aggregates={},
        baskets_cache_loaded=False, list_view=True,
        allow_commission_tariff_fetch=False,
    )[0]
    analytics = row["analytics"]
    assert analytics["financeState"] == ("fallback" if demo else "no_data")
    assert analytics["basketsState"] == ("fallback" if demo else "no_data")
    assert analytics["commissionCalcMode"] == ("demo_planned" if demo else "no_finance")
    assert analytics["taxKopecks"] is None
    assert analytics["factTaxState"] == "missing"
    if demo:
        assert analytics["ordersUnits"] > 0
        assert analytics["netProfitKopecks"] is not None
        assert analytics["abcCode"] is not None
    else:
        assert analytics["ordersUnits"] == 0
        assert analytics["baskets"] is None
        assert analytics["netProfitKopecks"] is None
        assert analytics["abcCode"] == "——"
        assert analytics["salesClass"] is analytics["profitClass"] is None


@pytest.mark.parametrize("mode", ["fake", "real"])
def test_spp_enrichment_without_explicit_token_does_not_call_wb(monkeypatch, mode):
    settings = replace(repricer_bff_module.get_settings(), wb_api_mode=mode)
    monkeypatch.setattr(repricer_bff_module, "get_settings", lambda: settings)
    monkeypatch.setattr(repricer_bff_module, "build_wb_client", lambda *a, **kw: pytest.fail("WB must not be called without an explicit token"))
    goods = [{"nmID": 123456, "sizes": [{"price": 1199, "discountedPrice": 1199}]}]
    repricer_bff_module._enrich_goods_page_spp_fields("complete", None, goods)
    assert goods == [{"nmID": 123456, "sizes": [{"price": 1199, "discountedPrice": 1199}]}]


def test_cached_sku_list_does_not_fetch_commission_tariffs(monkeypatch):
    repricer_bff_module.COMMISSION_TARIFFS_CACHE.clear()
    monkeypatch.setattr(
        repricer_bff_module,
        "fetch_commission_tariffs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cached list read must not call WB tariffs")),
    )

    rows = repricer_bff_module.list_repricer_skus(
        wb_token="token",
        include_promotions=False,
        include_content=False,
        cached_goods=[
            {
                "vendorCode": "FBBT_42",
                "nmID": 123456,
                "brand": "Ogni",
                "subjectName": "Футболки",
                "sizes": [{"price": 139000, "discountedPrice": 119000}],
            }
        ],
        cached_content_cards=[],
        cached_promotions=[],
        cached_stock_aggregates={},
        cached_period_stats={},
        cached_finance_aggregates={},
        cached_ads_aggregates={},
        cached_baskets_aggregates={},
        list_view=True,
        allow_commission_tariff_fetch=False,
    )

    assert len(rows) == 1
    assert rows[0]["meta"]["articleId"] == "FBBT_42"
    assert rows[0]["analytics"]["commissionState"] == "no_data"
