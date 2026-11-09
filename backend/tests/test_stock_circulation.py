import pytest

from app.repricer_bff import stock_circulation_units


def test_circulation_includes_both_transit_directions_without_total_double_count():
    stock = dict(wbStockUnits=121, inWayToClient=299, inWayFromClient=193, totalStockUnits=613)
    assert stock_circulation_units(stock) == 613
    assert stock['wbStockUnits'] == 121


def test_confirmed_zero():
    assert stock_circulation_units(dict(wbStockUnits=0, inWayToClient=0, inWayFromClient=0)) == 0


@pytest.mark.parametrize('value', [None, -1, True, '12', 1.5])
def test_missing_or_invalid_transit_is_not_zero(value):
    assert stock_circulation_units(dict(wbStockUnits=121, inWayToClient=value, inWayFromClient=3)) is None


def test_old_cache_is_not_complete_evidence():
    assert stock_circulation_units({'wbStockUnits': 121}) is None
