from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.modules.wb_repricing_calculation import (
    CalculationContext,
    CalculationReference,
    LegacyCalculationService,
)


def context(row, algorithm=None, liquidation=None):
    return CalculationContext(
        1,
        2,
        3,
        datetime(2026, 6, 1, 12, tzinfo=UTC),
        tuple(
            CalculationReference(role, "synthetic-v1", "a" * 64)
            for role in (
                "settings",
                "mapping",
                "cost",
                "economics",
                "price",
                "stock",
                "history",
            )
        ),
        row,
        algorithm or {},
        liquidation or {},
        6.0,
        20.0,
    )


@pytest.mark.parametrize("orders", [0, 1, 5, 10, 14, 30])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"pMinKopecks": 70000},
        {"cogsKopecks": 2**53 + 1},
        {
            "cogsKopecks": 40000,
            "wbCommissionPct": 20,
            "minMarginPct": 15,
            "pickPackCostPercent": 3,
            "promoCostPercent": 5,
            "otherExpensePricePct": 2,
            "beautyPriceEnabled": True,
            "beautyPriceTemplate": "x99",
        },
    ],
)
def test_detached_service_preserves_legacy_arithmetic(monkeypatch, orders, settings):
    from app import repricer_execution as old

    row = {
        "meta": {
            "articleId": "synthetic",
            "currentPriceKopecks": 100005,
            "basketNorm": 10,
            "basketsLast7d": 30,
        },
        "settings": settings,
        "analytics": {
            "baskets": orders,
            "ordersUnits": orders,
            "periodDays": 7,
            "hourlyOrders": [{"hour": 15, "ordersUnits": 10}],
            "todayHourlyOrders": [{"hour": 15, "ordersUnits": orders}],
        },
    }
    frozen = context(
        row,
        {"priceStepPct": 3, "basketNormPeriodDays": 7},
        {"synthetic": {"targetPriceKopecks": 70000}},
    )
    service = LegacyCalculationService(frozen)
    monkeypatch.setattr(old, "ALGORITHM_SETTINGS_STATE", dict(frozen.algorithm))
    monkeypatch.setattr(
        old, "LIQUIDATION_ACTIVE", {"synthetic": {"targetPriceKopecks": 70000}}
    )
    monkeypatch.setattr(old, "_utc_now", lambda: frozen.as_of)
    monkeypatch.setattr(
        old,
        "get_repricer_guard_limits",
        lambda: SimpleNamespace(
            per_update_step_pct=6.0,
            per_day_step_pct=20.0,
        ),
    )
    assert service.minimum_price() == old._settings_pmin_kopecks(settings)
    assert service.maximum_price() == old._settings_pmax_kopecks(settings, 100005)
    assert service.basket_candidate() == old._basket_threshold_recommended_price(row)
    for metric in ("orders", "revenue", "margin"):
        assert service.plan_fact_candidate(
            70000, metric=metric
        ) == old._plan_fact_recommended_price(row, 70000, metric=metric)
    for candidate in (80000, 100005, 201300):
        price, notes = old._clamp_candidate_price(
            current=100005, recommended=candidate, min_price=70000, p_max=None
        )
        assert service.clamp_candidate(candidate, 70000, None) == (price, tuple(notes))
        assert service.round_candidate(candidate) == old._apply_price_rounding(
            candidate, current=100005, settings=settings
        )
    price, detail, blockers = old._illiquid_recommended_price(row)
    assert service.liquidation_candidate() == (price, detail, tuple(blockers))
    price, detail, blockers = old._plan_fact_interval_recommended_price(row, 70000)
    assert service.interval_candidate(70000) == (price, detail, tuple(blockers))


def test_context_detaches_all_nested_values_and_partitions_cache():
    row = {"meta": {"currentPriceKopecks": 100000}, "settings": {"pMinKopecks": 70000}}
    algorithm = {"nested": [{"value": 1}]}
    frozen = context(row, algorithm)
    key = frozen.cache_key
    row["settings"]["pMinKopecks"] = 1
    algorithm["nested"][0]["value"] = 2
    assert LegacyCalculationService(frozen).minimum_price() == 70000
    assert frozen.algorithm["nested"][0]["value"] == 1
    assert frozen.cache_key == key
    with pytest.raises(TypeError):
        frozen.row["settings"]["pMinKopecks"] = 5
    for change in (
        {"organization_id": 2},
        {"marketplace_account_id": 3},
        {"catalog_sku_id": 4},
        {"per_day_step_pct": 3.0},
        {"as_of": datetime(2026, 6, 2, tzinfo=UTC)},
        {
            "references": tuple(
                replace(ref, version="synthetic-v2") for ref in frozen.references
            )
        },
    ):
        assert replace(frozen, **change).cache_key != key


@pytest.mark.parametrize(
    "change",
    [
        {"organization_id": True},
        {"marketplace_account_id": 0},
        {"catalog_sku_id": "external"},
        {"as_of": datetime(2026, 6, 1)},  # noqa: DTZ001 -- deliberate invalid input
        {"references": ()},
        {"per_update_step_pct": float("nan")},
        {"row": {"bad": object()}},
        {"algorithm": {"bad": float("inf")}},
    ],
)
def test_context_rejects_unpinned_or_mutable_inputs(change):
    with pytest.raises(ValueError):
        replace(context({}), **change)


def test_calculation_module_has_no_runtime_dependencies():
    import ast
    import inspect

    from app.modules import wb_repricing_calculation

    tree = ast.parse(inspect.getsource(wb_repricing_calculation))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not node.module.startswith(
                ("app.", "sqlalchemy", "fastapi", "celery")
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("now", "utcnow")
