import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.wb_price_snapshots import (
    PriceSourceValidationError,
    assemble_goods_price_run,
    parse_goods_price_page,
)

NOW=datetime(2026,9,9,tzinfo=UTC)


@pytest.mark.parametrize("exponent", [b"99999999999999999999999", b"-99999999999999999999999"])
def test_decimal_decoder_extreme_exponent_is_a_safe_rejected_page(exponent):
    raw = (b'{"data":{"listGoods":[{"nmID":101,"sizes":[{"sizeID":201,"price":1e'
           + exponent + b'}]}]}}')
    with pytest.raises(PriceSourceValidationError) as caught:
        parse_goods_price_page(raw, organization_id=7, marketplace_account_id=42,
                               offset=0, limit=2, received_at=NOW, request_checksum="a" * 64)
    assert exponent.decode() not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def parse(goods,**kwargs):
    return parse_goods_price_page(json.dumps({"data":{"listGoods":goods}}).encode(),
                                  organization_id=7,marketplace_account_id=42,
                                  offset=0,limit=2,received_at=NOW,request_checksum="a"*64,**kwargs)


def test_rubles_never_use_legacy_magnitude_guess_and_all_sizes_survive():
    page=parse([{"nmID":101,"discount":10,"clubDiscount":4,"sizes":[
        {"sizeID":201,"price":100000,"discountedPrice":90000,"clubDiscountedPrice":86400},
        {"sizeID":202,"price":87.5,"discountedPrice":80},
    ]}])
    product=page.products[0]
    assert product.discount.value == 10
    assert product.club_discount.value == 4
    assert len(product.sizes) == 2
    assert product.sizes[0].list_price.value == 10_000_000
    assert product.sizes[0].club_price.value == 8_640_000
    assert product.sizes[1].list_price.value == 8_750
    assert page.terminal is True
    assert page.source_observed_at is None
    with pytest.raises(FrozenInstanceError):
        page.offset=1


def test_omitted_null_and_zero_remain_distinct_without_buyer_substitution():
    product=parse([{"nmID":101,"sizes":[
        {"sizeID":201,"discountedPrice":None,"clubDiscountedPrice":0,
         "buyerPriceNoWalletKopecks":9999},
    ]}]).products[0]
    size=product.sizes[0]
    assert size.list_price.presence == "missing" and size.list_price.value is None
    assert size.discounted_price.presence == "null" and size.discounted_price.value is None
    assert size.club_price.presence == "value" and size.club_price.value == 0
    assert not hasattr(size,"buyer_price")


def test_missing_size_id_remains_unresolved_not_nm_or_position_identity():
    product=parse([{"nmID":101,"sizes":[{"price":100},{"price":200}]}]).products[0]
    assert [size.source_size_id for size in product.sizes] == [None,None]
    assert product.source_sizes_identified is False
    assert len(product.sizes) == 2


@pytest.mark.parametrize("bad", [True,-1,"100",1.001,float("nan"),float("inf")])
def test_invalid_money_never_turns_into_zero_or_guessed_kopecks(bad):
    with pytest.raises(PriceSourceValidationError):
        parse([{"nmID":101,"sizes":[{"sizeID":201,"price":bad}]}])


@pytest.mark.parametrize("goods", [
    [None], [{"nmID":True,"sizes":[]}], [{"nmID":101}],
    [{"nmID":101,"sizes":[None]}],
    [{"nmID":101,"sizes":[{"sizeID":True}]}],
    [{"nmID":101,"sizes":[]},{"nmID":101,"sizes":[]}],
    [{"nmID":101,"sizes":[{"sizeID":201},{"sizeID":201}]}],
    [{"nmID":101,"sizes":[]},{"nmID":102,"sizes":[]},{"nmID":103,"sizes":[]}],
])
def test_malformed_or_ambiguous_page_is_rejected_as_a_whole(goods):
    with pytest.raises(PriceSourceValidationError):
        parse(goods)


@pytest.mark.parametrize("raw", [b'{"data":{"listGoods":[],"listGoods":[null]}}',
                                 b'{"data":{}}',b'{"data":null}',b'null',b'[]',b''])
def test_invalid_json_wrapper_or_duplicate_keys_cannot_be_empty_terminal(raw):
    with pytest.raises(PriceSourceValidationError):
        parse_goods_price_page(raw,organization_id=7,marketplace_account_id=42,
                               offset=0,limit=2,received_at=NOW,request_checksum="a"*64)


def test_full_page_needs_next_page_and_zero_goods_is_explicit_terminal():
    full=parse([{"nmID":101,"sizes":[]},{"nmID":102,"sizes":[]}])
    assert full.terminal is False
    assert full.next_offset == 2
    empty=parse([])
    assert empty.terminal is True and empty.next_offset is None
    assert len(empty.products) == 0


@pytest.mark.parametrize("field,value", [("organization_id",True),("marketplace_account_id","42"),
                                       ("offset",-1),("limit",0),("received_at",NOW.replace(tzinfo=None))])
def test_transport_scope_and_time_must_be_explicit_and_valid(field,value):
    values={"organization_id":7,"marketplace_account_id":42,"offset":0,"limit":2,"received_at":NOW,"request_checksum":"a"*64}
    with pytest.raises(PriceSourceValidationError):
        parse_goods_price_page(b'{"data":{"listGoods":[]}}',**{**values,field:value})


def test_raw_response_checksum_is_not_rewritten_by_normalization():
    from hashlib import sha256
    raw=b'{"data":{"listGoods":[{"nmID":101,"sizes":[{"sizeID":201,"price":1.20}]}]}}'
    page=parse_goods_price_page(raw,organization_id=7,marketplace_account_id=42,
                               offset=0,limit=2,received_at=NOW,request_checksum="a"*64)
    assert page.raw_checksum == sha256(raw).hexdigest()
    assert page.products[0].sizes[0].list_price.value == 120


def test_run_is_complete_only_with_contiguous_full_pages_then_explicit_terminal():
    first=parse([{"nmID":101,"sizes":[]},{"nmID":102,"sizes":[]}])
    last=replace(parse([]),offset=2,received_at=NOW+timedelta(seconds=1))
    partial=assemble_goods_price_run((first,))
    assert partial.complete is False
    complete=assemble_goods_price_run((first,last))
    assert complete.complete is True
    assert [row.nm_id for row in complete.products] == [101,102]
    assert complete.received_at == last.received_at
    assert complete.pages == (first,last)


@pytest.mark.parametrize("mutation", ["account","org","gap","limit","backwards","after_terminal","request"])
def test_run_rejects_cross_account_gaps_and_incompatible_pages(mutation):
    first=parse([{"nmID":101,"sizes":[]},{"nmID":102,"sizes":[]}])
    last=replace(parse([]),offset=2)
    if mutation=="account":
        last=replace(last,marketplace_account_id=43)
    elif mutation=="org":
        last=replace(last,organization_id=8)
    elif mutation=="gap":
        last=replace(last,offset=3)
    elif mutation=="limit":
        last=replace(last,limit=3)
    elif mutation=="backwards":
        last=replace(last,received_at=NOW-timedelta(seconds=1))
    elif mutation=="request":
        last=replace(last,request_checksum="b"*64)
    else:
        first=parse([])
    with pytest.raises(PriceSourceValidationError):
        assemble_goods_price_run((first,last))


def test_cross_page_duplicate_product_is_not_silently_replaced():
    first=parse([{"nmID":101,"sizes":[]},{"nmID":102,"sizes":[]}])
    duplicate=replace(parse([{"nmID":102,"sizes":[]}]),offset=2)
    with pytest.raises(PriceSourceValidationError):
        assemble_goods_price_run((first,duplicate))


def test_terminal_suffix_cannot_be_a_complete_run_and_empty_input_is_not_success():
    for pages in ((),(replace(parse([]),offset=2),)):
        with pytest.raises(PriceSourceValidationError):
            assemble_goods_price_run(pages)


def test_one_explicit_empty_first_page_is_complete_not_missing():
    run=assemble_goods_price_run((parse([]),))
    assert run.complete and run.products == ()


@pytest.mark.parametrize("payload", [
    {"error":True,"data":{"listGoods":[]}},
    {"error":False,"errorText":"synthetic provider error","data":{"listGoods":[]}},
    {"data":{"error":True,"listGoods":[]}},
])
def test_provider_error_wrapper_never_becomes_complete_empty_snapshot(payload):
    with pytest.raises(PriceSourceValidationError):
        parse_goods_price_page(json.dumps(payload).encode(),organization_id=7,marketplace_account_id=42,
                               offset=0,limit=2,received_at=NOW,request_checksum="a"*64)


@pytest.mark.parametrize("mutation", [{"organization_id":True},{"marketplace_account_id":"42"},
                                      {"request_checksum":""},{"raw_checksum":"A"*64},
                                      {"received_at":NOW.replace(tzinfo=None)},{"products":[]},
                                      {"products":(None,)},{"source_observed_at":NOW}])
def test_hydrated_page_cannot_bypass_strict_transport_contract(mutation):
    page=parse([])
    with pytest.raises(PriceSourceValidationError):
        assemble_goods_price_run((replace(page,**mutation),))


def test_non_rub_currency_is_not_normalized_as_rubles():
    with pytest.raises(PriceSourceValidationError):
        parse([{"nmID":101,"currencyIsoCode":"USD","sizes":[{"sizeID":201,"price":100}]}])


def test_normal_success_marker_and_rub_currency_are_accepted():
    raw=b'{"error":false,"errorText":"","data":{"listGoods":[{"nmID":101,"currencyIsoCode":"RUB","sizes":[]}]}}'
    page=parse_goods_price_page(raw,organization_id=7,marketplace_account_id=42,
                               offset=0,limit=2,received_at=NOW,request_checksum="a"*64)
    assert page.terminal
