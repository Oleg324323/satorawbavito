"""Daily-cache unions must retain the same finance-basis guard as whole ranges."""

from datetime import date

import pytest

from app import repricer_tasks
from app.repricer_cache.store import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION


@pytest.mark.parametrize(
    "left,right,require_current,expected",
    [
        ({}, {}, True, False),
        (
            {"revenueBasis": "retailAmount", "financeSchemaVersion": "v2"},
            {},
            True,
            False,
        ),
        (
            {"revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION},
            {},
            True,
            False,
        ),
        (
            {"revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION},
            {"revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION},
            True,
            True,
        ),
        (
            {"revenueBasis": "sellerPayout", "financeSchemaVersion": "v3"},
            {"revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION},
            True,
            False,
        ),
        ({}, {}, False, True),
    ],
)
def test_partitioned_finance_days_require_current_basis_unless_baseline_explicitly_allows_legacy(
    monkeypatch, left, right, require_current, expected
):
    caches = [
        {
            "dateFrom": "2026-07-01",
            "dateTo": "2026-07-01",
            "dailyAggregateDates": ["2026-07-01"],
            **left,
        },
        {
            "dateFrom": "2026-07-02",
            "dateTo": "2026-07-02",
            "dailyAggregateDates": ["2026-07-02"],
            **right,
        },
    ]
    monkeypatch.setattr(
        repricer_tasks,
        "list_source_cache_ranges_by_prefix",
        lambda *args, **kwargs: caches,
    )
    assert (
        repricer_tasks._range_source_ready(
            7,
            "finance",
            date(2026, 7, 1),
            date(2026, 7, 2),
            require_current_finance_basis=require_current,
        )
        is expected
    )


def test_daily_readiness_rejects_previous_withdrawal_semantics(monkeypatch):
    caches = [{"dateFrom": day, "dateTo": day, "dailyAggregateDates": [day],
               "revenueBasis": "retailAmount", "financeSchemaVersion": "v3",
               "aggregates": {"101": {"paymentScheduleKopecks": 1000, "additionalPaymentKopecks": 1000}}}
              for day in ("2026-07-01", "2026-07-02")]
    monkeypatch.setattr(repricer_tasks, "list_source_cache_ranges_by_prefix", lambda *a, **kw: caches)
    assert not repricer_tasks._range_source_ready(7, "finance", date(2026, 7, 1), date(2026, 7, 2), require_current_finance_basis=True)
