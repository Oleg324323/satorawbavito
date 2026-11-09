"""Owner-approved arithmetic on synthetic inputs; no DB/provider actions."""

import pytest

from app.modules.wb_reports import abc_pnl


def inputs():
    return dict(
        sales_kopecks=100_000_000,
        commission_kopecks=15_000_000,
        logistics_kopecks=7_000_000,
        storage_kopecks=1_000_000,
        acceptance_kopecks=500_000,
        advertising_kopecks=4_000_000,
        penalty_kopecks=200_000,
        cogs_kopecks=40_000_000,
        tax_basis_points=750,
        internal_expenses_kopecks=5_000_000,
        sales_basis_confirmed=True,
        unmapped_components={},
    )


def test_owner_example_is_248000_before_internal_and_198000_net():
    result = abc_pnl.calculate_management_profit(**inputs())
    assert result.tax_kopecks == 7_500_000
    assert result.profit_before_internal_kopecks == 24_800_000
    assert result.net_profit_kopecks == 19_800_000
    assert result.blocker_ids == ()


def test_confirmed_22_percent_is_deducted_from_sales_not_added_or_extracted():
    zero_expenses = {key: 0 for key in inputs() if key.endswith("_kopecks")}
    result = abc_pnl.calculate_management_profit(**(inputs() | zero_expenses | {
        "sales_kopecks": 100_000_000, "tax_basis_points": 2200,
    }))
    assert result.tax_kopecks == 22_000_000
    assert result.net_profit_kopecks == 78_000_000
    resolved = abc_pnl.calculate_management_profit(**(inputs() | zero_expenses | {
        "sales_kopecks": 100_000_000, "tax_basis_points": None,
        "resolved_tax_kopecks": result.tax_kopecks, "resolved_tax_confirmed": True,
    }))
    assert resolved.net_profit_kopecks == result.net_profit_kopecks


@pytest.mark.parametrize(
    "internal,net", [(None, None), (0, 24_800_000), (30_000_000, -5_200_000)]
)
def test_internal_unknown_zero_and_loss_are_distinct(internal, net):
    result = abc_pnl.calculate_management_profit(
        **(inputs() | {"internal_expenses_kopecks": internal})
    )
    assert result.profit_before_internal_kopecks == 24_800_000
    assert result.net_profit_kopecks == net
    assert ("WB_MANAGEMENT_INTERNAL_EXPENSES_MISSING" in result.blocker_ids) == (
        internal is None
    )


@pytest.mark.parametrize(
    "field",
    [
        "sales_kopecks",
        "commission_kopecks",
        "logistics_kopecks",
        "storage_kopecks",
        "acceptance_kopecks",
        "advertising_kopecks",
        "penalty_kopecks",
        "cogs_kopecks",
        "tax_basis_points",
    ],
)
def test_missing_component_never_produces_profit(field):
    result = abc_pnl.calculate_management_profit(**(inputs() | {field: None}))
    assert result.profit_before_internal_kopecks is None
    assert result.net_profit_kopecks is None
    assert result.blocker_ids


@pytest.mark.parametrize("sales,tax", [(20, 2), (60, 4), (-20, -2), (-60, -4), (0, 0)])
def test_signed_tax_reuses_half_even_rounding_without_double_returns(sales, tax):
    values = {key: 0 for key in inputs() if key.endswith("_kopecks")}
    result = abc_pnl.calculate_management_profit(
        **(inputs() | values | {"sales_kopecks": sales})
    )
    assert result.tax_kopecks == tax
    assert result.profit_before_internal_kopecks == sales - tax


@pytest.mark.parametrize(
    "change",
    [
        {"sales_basis_confirmed": False},
        {"unmapped_components": None},
        {"unmapped_components": {"acquiring": 100}},
        {"unmapped_components": {"loyalty": None}},
    ],
)
def test_unconfirmed_basis_or_unreconciled_operations_block_acceptance(change):
    result = abc_pnl.calculate_management_profit(**(inputs() | change))
    assert result.profit_before_internal_kopecks is None
    assert result.net_profit_kopecks is None
    assert result.blocker_ids


@pytest.mark.parametrize("value", [True, 7.5, "750", -1, 10001])
def test_invalid_basis_points_are_rejected(value):
    with pytest.raises(ValueError):
        abc_pnl.calculate_management_profit(**(inputs() | {"tax_basis_points": value}))


def test_separate_calls_do_not_reuse_another_scopes_cost_or_rate():
    first = abc_pnl.calculate_management_profit(**inputs())
    second = abc_pnl.calculate_management_profit(
        **(inputs() | {"tax_basis_points": None, "internal_expenses_kopecks": None})
    )
    assert second.net_profit_kopecks is None
    assert first.net_profit_kopecks == 19_800_000


def test_summary_preserves_tax_rounded_on_resolved_sku_bases():
    values = {key: 0 for key in inputs() if key.endswith("_kopecks")}
    first = abc_pnl.calculate_management_profit(
        **(inputs() | values | {"sales_kopecks": 20})
    )
    second = abc_pnl.calculate_management_profit(
        **(inputs() | values | {"sales_kopecks": 20})
    )
    summary = abc_pnl.calculate_management_profit(
        **(
            inputs()
            | values
            | {
                "sales_kopecks": 40,
                "resolved_tax_kopecks": first.tax_kopecks + second.tax_kopecks,
            }
        )
    )
    assert summary.tax_kopecks == 4
    assert (
        summary.profit_before_internal_kopecks
        == first.profit_before_internal_kopecks + second.profit_before_internal_kopecks
    )


@pytest.mark.parametrize(
    "field", ["sales_kopecks", "advertising_kopecks", "internal_expenses_kopecks"]
)
@pytest.mark.parametrize("value", [True, 1.25, "100"])
def test_money_rejects_booleans_floats_and_strings(field, value):
    with pytest.raises(ValueError):
        abc_pnl.calculate_management_profit(**(inputs() | {field: value}))
