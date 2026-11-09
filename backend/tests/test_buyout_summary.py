from app.routers.wb_repricer_bff import _buyout_summary


def test_buyouts_use_confirmed_sale_units_and_amount_not_orders_or_net_revenue():
    rows = [{"analytics": {"financeState": "ok", "ordersUnits": 90, "salesUnits": 4,
        "returnsUnits": 1, "revenueKopecks": 700, "buyoutUnits": 4, "buyoutAmountKopecks": 1001}},
        {"analytics": {"financeState": "ok", "buyoutUnits": 2, "buyoutAmountKopecks": 599}}]
    assert _buyout_summary(rows) == (6, 1600)
    assert _buyout_summary(rows[:1]) == (4, 1001)


def test_missing_not_zero_and_zero_is_confirmed():
    assert _buyout_summary([{"analytics": {"financeState": "no_data"}}]) == (None, None)
    assert _buyout_summary([{"analytics": {"financeState": "ok", "buyoutUnits": 1}}]) == (None, None)
    assert _buyout_summary([{"analytics": {"financeState": "ok", "buyoutUnits": 0, "buyoutAmountKopecks": 0}}]) == (0, 0)
