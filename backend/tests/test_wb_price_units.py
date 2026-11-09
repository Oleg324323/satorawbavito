from app.repricer_bff import _resolve_spp_analytics, _seller_spp_pct
from app.wb_api.price_units import wb_goods_price_to_kopecks

def test_wb_goods_price_to_kopecks_converts_rubles():
    assert wb_goods_price_to_kopecks(3761) == 376_100
    assert wb_goods_price_to_kopecks(87.5) == 8_750


def test_wb_goods_price_to_kopecks_keeps_legacy_kopecks_fixtures():
    assert wb_goods_price_to_kopecks(129_000) == 129_000
    assert wb_goods_price_to_kopecks(1_223_20) == 1_223_20


def test_resolve_spp_analytics_from_club_discounted_price():
    good = {"clubDiscount": 4}
    size = {"discountedPrice": 3761, "clubDiscountedPrice": 3253}
    buyer, buyer_with_wallet, spp, wallet = _resolve_spp_analytics(good, size, wb_goods_price_to_kopecks(3761))
    assert buyer is None
    assert buyer_with_wallet is None
    assert spp is None
    assert wallet is None


def test_merge_good_spp_fields_fills_club_discounted_price():
    from app.repricer_bff import _merge_good_spp_fields

    target = {"sizes": [{"discountedPrice": 3761, "price": 8738}]}
    source = {"clubDiscount": 4, "sizes": [{"discountedPrice": 3761, "clubDiscountedPrice": 3253}]}
    _merge_good_spp_fields(target, source)
    assert target["clubDiscount"] == 4
    assert target["sizes"][0]["clubDiscountedPrice"] == 3253


def test_good_missing_spp_fields_ignores_wallet_only_fields():
    from app.repricer_bff import _good_missing_spp_fields

    wallet_only = {"clubDiscount": 0, "sizes": [{"discountedPrice": 3761, "clubDiscountedPrice": 3761}]}
    with_live_buyer = {"sizes": [{"discountedPrice": 3761, "buyerPriceNoWalletKopecks": 325_300}]}

    assert _good_missing_spp_fields(wallet_only) is True
    assert _good_missing_spp_fields(with_live_buyer) is False


def test_resolve_spp_analytics_does_not_derive_wallet_from_club_discount():
    good = {"clubDiscount": 4}
    size = {"discountedPrice": 3761}
    buyer, buyer_with_wallet, spp, wallet = _resolve_spp_analytics(good, size, wb_goods_price_to_kopecks(3761))
    assert wallet is None
    assert buyer is None
    assert buyer_with_wallet is None
    assert spp is None


def test_resolve_spp_analytics_indeepa_formula_from_live_buyer_price():
    good = {"walletPct": 4, "clubDiscount": 9}
    size = {"discountedPrice": 1763, "buyerPriceNoWallet": 1480}
    buyer, buyer_with_wallet, spp, wallet = _resolve_spp_analytics(good, size, wb_goods_price_to_kopecks(1763))
    assert buyer == wb_goods_price_to_kopecks(1480)
    assert buyer_with_wallet == wb_goods_price_to_kopecks(1420)
    assert spp == 16.05
    assert wallet == 4.0


def test_resolve_spp_analytics_prefers_explicit_kopecks_over_legacy_alias():
    good = {"walletPct": 4, "clubDiscount": 9}
    size = {"discountedPrice": 1500, "buyerPriceNoWallet": 95_800, "buyerPriceNoWalletKopecks": 95_800}
    buyer, buyer_with_wallet, spp, wallet = _resolve_spp_analytics(good, size, wb_goods_price_to_kopecks(1500))
    assert buyer == 95_800
    assert buyer_with_wallet == 91_900
    assert spp == 36.13
    assert wallet == 4.0


def test_seller_spp_pct_rejects_buyer_price_above_seller_price():
    assert _seller_spp_pct(1_470, 93_900) is None
