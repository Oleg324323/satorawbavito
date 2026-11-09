from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.repricer_bff import _merge_good_spp_fields
from app.repricer_sync import _apply_external_spp_prices_to_goods


def good():
    return {"nmID":101,"sizes":[
        {"sizeID":201,"price":500,"discountedPrice":500},
        {"sizeID":202,"price":600,"discountedPrice":600,"techSizeName":"M"},
        {"sizeID":203,"price":700,"discountedPrice":700,"techSizeName":"L"},
    ]}


def test_catalog_price_enrichment_keeps_other_sizes_and_does_not_mutate_source():
    target=good()
    original=deepcopy(target)
    source={"clubDiscount":4,"sizes":[{"sizeID":201,"clubDiscountedPrice":480}]}
    original_source=deepcopy(source)
    _merge_good_spp_fields(target,source)
    assert target["sizes"][0] == {**original["sizes"][0],"clubDiscountedPrice":480}
    assert target["sizes"][1:] == original["sizes"][1:]
    assert source == original_source
    assert target["clubDiscount"] == 4


def test_external_buyer_enrichment_does_not_delete_or_spread_to_other_sizes(monkeypatch):
    observed = datetime(2026, 9, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("app.repricer_sync._utc_now", lambda: observed)
    target=good()
    original=deepcopy(target)
    assert _apply_external_spp_prices_to_goods([target],{101:40000}) == 1
    assert target["sizes"][0] == {
        **original["sizes"][0],"buyerPriceNoWalletKopecks":40000,"buyerPriceNoWallet":400,
        "buyerPriceKopecks":40000,"buyerPrice":400,"clientPrice":400,
        "buyerPriceSource":"41-spp","buyerPriceObservedAt":observed.isoformat(),
        "buyerPriceSellerKopecks":50000,
    }
    assert target["sizes"][1:] == original["sizes"][1:]


@pytest.mark.parametrize("prices", [{}, {102:40000}, {101:0}, {101:60000}])
def test_unmatched_zero_or_above_seller_buyer_price_keeps_all_sizes(prices):
    target=good()
    original=deepcopy(target)
    assert _apply_external_spp_prices_to_goods([target],prices) == 0
    assert target == original


@pytest.mark.parametrize("source", [{}, {"sizes":[]}, {"sizes":[None]}])
def test_missing_enrichment_size_keeps_original_catalog_rows(source):
    target=good()
    original=deepcopy(target)
    _merge_good_spp_fields(target,source)
    assert target == original
