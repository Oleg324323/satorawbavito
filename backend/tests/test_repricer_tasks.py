from datetime import date
from types import SimpleNamespace

import pytest
from celery.exceptions import Ignore, Retry

from app import repricer_tasks
from app.repricer_cache.store import FINANCE_SCHEMA_VERSION


def _mock_working_onboarding_ready(monkeypatch):
    monkeypatch.setattr("app.repricer_tasks.cached_goods_meta", lambda _organization_id: {"totalCached": 3200})

    def cache_ranges(_organization_id, prefix, **_kwargs):
        cache = {
            "dateFrom": "2026-01-01",
            "dateTo": "2026-12-31",
            "dailyAggregatesDays": 365,
        }
        if prefix == "finance_":
            cache.update(revenueBasis="retailAmount", financeSchemaVersion=FINANCE_SCHEMA_VERSION)
        if prefix == "baskets_":
            cache.update(dailyDetailStatus="fetched")
        return [cache]

    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        cache_ranges,
    )


def test_rnp_daily_baskets_ready_rejects_aggregate_only_covering_cache(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "baskets_2026-06-25_2026-07-24",
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ]
        if prefix == "baskets_"
        else [],
    )

    assert repricer_tasks._rnp_daily_baskets_ready(
        2,
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 21),
    ) is False


def test_rnp_daily_baskets_ready_accepts_covering_daily_detail(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "baskets_2026-07-18_2026-07-24",
                "dateFrom": "2026-07-18",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 7,
            }
        ]
        if prefix == "baskets_"
        else [],
    )

    assert repricer_tasks._rnp_daily_baskets_ready(
        2,
        date_from=date(2026, 7, 18),
        date_to=date(2026, 7, 24),
    ) is True


def test_onboarding_readiness_requires_daily_baskets_detail(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "finance_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28", "revenueBasis": "retailAmount", "financeSchemaVersion": FINANCE_SCHEMA_VERSION}],
        "ads_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "baskets_": [
            {
                "dateFrom": "2026-06-29",
                "dateTo": "2026-07-28",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ],
    }

    monkeypatch.setattr("app.repricer_tasks.cached_goods_meta", lambda _organization_id: {"totalCached": 3201})
    monkeypatch.setattr(
        "app.repricer_tasks.get_source_cache",
        lambda _organization_id, key, **_kwargs: {"count": 1} if key in {"content_cards", "stocks"} else {},
    )
    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    profile = next(
        item
        for item in repricer_tasks.onboarding_sync_profiles(as_of=date(2026, 7, 28))
        if item.profile_id == "onboarding-30d"
    )

    assert repricer_tasks._source_ready_for_profile(2, profile, "baskets") is False
    assert repricer_tasks._resume_missing_profile_sources(2, profile) == ("baskets",)
    assert repricer_tasks._working_onboarding_window_ready(2, as_of=date(2026, 7, 28)) is False

    caches_by_prefix["baskets_"][0]["dailyDetailStatus"] = "fetched"
    caches_by_prefix["baskets_"][0]["dailyAggregatesDays"] = 30

    assert repricer_tasks._source_ready_for_profile(2, profile, "baskets") is True
    assert repricer_tasks._resume_missing_profile_sources(2, profile) == ()
    assert repricer_tasks._working_onboarding_window_ready(2, as_of=date(2026, 7, 28)) is True


def test_onboarding_readiness_rejects_shifted_daily_baskets_dates(monkeypatch):
    shifted_dates = [f"2026-06-{day:02d}" for day in range(1, 31)]
    caches_by_prefix = {
        "baskets_": [
            {
                "dateFrom": "2026-06-29",
                "dateTo": "2026-07-28",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 30,
                "dailyAggregateDates": shifted_dates,
            }
        ],
    }
    profile = next(
        item
        for item in repricer_tasks.onboarding_sync_profiles(as_of=date(2026, 7, 28))
        if item.profile_id == "onboarding-30d"
    )

    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    assert repricer_tasks._source_ready_for_profile(2, profile, "baskets") is False


def test_report_snapshot_source_ready_rejects_aggregate_only_covering_cache(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "finance_2026-06-25_2026-07-24",
                "revenueBasis": "retailAmount",
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ]
        if prefix == "finance_"
        else [],
    )

    assert repricer_tasks._report_snapshot_source_ready(
        2,
        "finance",
        date(2026, 7, 8),
        date(2026, 7, 21),
    ) is False


def test_report_snapshot_source_ready_accepts_covering_daily_detail(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "finance_2026-06-25_2026-07-24",
                "revenueBasis": "retailAmount",
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 30,
            }
        ]
        if prefix == "finance_"
        else [],
    )

    assert repricer_tasks._report_snapshot_source_ready(
        2,
        "finance",
        date(2026, 7, 8),
        date(2026, 7, 21),
    ) is True


def test_scheduler_execute_loads_full_cached_goods_list(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_scheduler_enabled=True,
            wb_api_token="env-token",
            wb_api_mode="real",
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr("app.repricer_tasks.list_execution_runs", lambda **_kwargs: [])
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setitem(
        repricer_tasks.repricer_bff_module.ALGORITHM_SETTINGS_STATE,
        "workerAutoApplyPricesEnabled",
        True,
    )
    monkeypatch.setattr(
        repricer_tasks.repricer_bff_module,
        "FRONTEND_STRATEGY_ASSIGNMENTS",
        {"JФЧ1009": {"strategyId": "plan_fact_daily"}},
    )
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {
        "periodDays": 30,
        "periodCacheSuffix": "2026-05-26_2026-06-24",
    })
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.list_cached_goods", lambda _organization_id: [{"vendorCode": "JФЧ1009", "nmID": 123456}])
    monkeypatch.setattr("app.repricer_tasks.get_source_cache", lambda _organization_id, source_key, **_kwargs: {
        "period_stats_2026-05-26_2026-06-24": {"aggregates": {"123456": {"ordersUnits": 130}}, "fetchedAt": "2026-06-24T09:00:00+00:00"},
        "baskets_2026-05-26_2026-06-24": {"aggregates": {"123456": {"cartCount": 0}}, "fetchedAt": "2026-06-24T09:00:00+00:00"},
        "finance_2026-05-26_2026-06-24": {
            "aggregates": {"123456": {"commissionKopecks": 1000}},
            "fetchedAt": "2026-06-24T09:00:00+00:00",
            "revenueBasis": "retailAmount",
            "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
        },
        "ads_2026-05-26_2026-06-24": {"aggregates": {"123456": {"adSpendKopecks": 500}}, "fetchedAt": "2026-06-24T09:00:00+00:00"},
        "stocks": {"aggregates": {"123456": {"wbStockUnits": 15}}, "fetchedAt": "2026-06-24T09:00:00+00:00"},
    }.get(source_key))
    def build_wb_client_stub(*_args, **kwargs):
        captured["token_override"] = kwargs.get("token_override")
        return object()

    monkeypatch.setattr("app.repricer_tasks.build_wb_client", build_wb_client_stub)
    monkeypatch.setattr("app.repricer_tasks.RateLimitedWbApiClient", lambda inner: inner)

    def list_repricer_skus_stub(*_args, **kwargs):
        captured["list_wb_token"] = kwargs.get("wb_token")
        captured["max_items"] = kwargs.get("max_items")
        captured["cached_period_stats"] = kwargs.get("cached_period_stats")
        captured["cached_baskets_aggregates"] = kwargs.get("cached_baskets_aggregates")
        captured["cached_finance_aggregates"] = kwargs.get("cached_finance_aggregates")
        captured["cached_ads_aggregates"] = kwargs.get("cached_ads_aggregates")
        captured["cached_stock_aggregates"] = kwargs.get("cached_stock_aggregates")
        captured["period_days"] = kwargs.get("period_days")
        return []

    def execute_all_assigned_skus_stub(**kwargs):
        captured["execute_wb_token"] = kwargs.get("wb_token")
        captured["execute_options"] = kwargs.get("options")
        return SimpleNamespace(
            runId="exec_test",
            executedCount=0,
            skippedCount=0,
            blockedCount=0,
        )

    monkeypatch.setattr("app.repricer_tasks.list_repricer_skus", list_repricer_skus_stub)
    monkeypatch.setattr("app.repricer_tasks.execute_all_assigned_skus", execute_all_assigned_skus_stub)

    result = repricer_tasks.execute_assigned_for_org.run(10)

    assert result["runId"] == "exec_test"
    assert captured["list_wb_token"] == "profile-token"
    assert captured["token_override"] == "profile-token"
    assert captured["execute_wb_token"] == "profile-token"
    assert captured["max_items"] is None
    assert captured["period_days"] == 30
    assert captured["cached_period_stats"] == {"123456": {"ordersUnits": 130}}
    assert captured["cached_baskets_aggregates"] == {"123456": {"cartCount": 0}}
    assert captured["cached_finance_aggregates"] == {"123456": {"commissionKopecks": 1000}}
    assert captured["cached_ads_aggregates"] == {"123456": {"adSpendKopecks": 500}}
    assert captured["cached_stock_aggregates"] == {"123456": {"wbStockUnits": 15}}
    assert captured["execute_options"].createDrafts is True
    assert captured["execute_options"].autoApprove is True
    assert captured["execute_options"].applyPrices is True


def test_scheduler_execute_skips_when_token_is_blank(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_scheduler_enabled=True,
            wb_api_token="",
            wb_api_mode="real",
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "   ")
    monkeypatch.setattr(
        repricer_tasks.repricer_bff_module,
        "FRONTEND_STRATEGY_ASSIGNMENTS",
        {"JФЧ1009": {"strategyId": "plan_fact_daily"}},
    )
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr("app.repricer_tasks._persist_scheduler_skip", lambda **_kwargs: None)

    result = repricer_tasks.execute_assigned_for_org.run(10)

    assert result == {"organizationId": 10, "skipped": True, "reason": "no_cabinet_wb_token"}


def test_scheduler_execute_does_not_fall_back_to_env_token(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_scheduler_enabled=True,
            wb_api_token="env-token-must-not-be-used",
            wb_api_mode="real",
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: None)
    monkeypatch.setattr(
        repricer_tasks.repricer_bff_module,
        "FRONTEND_STRATEGY_ASSIGNMENTS",
        {"JФЧ1009": {"strategyId": "plan_fact_daily"}},
    )
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks._persist_scheduler_skip", lambda **_kwargs: None)

    result = repricer_tasks.execute_assigned_for_org.run(10)

    assert result == {"organizationId": 10, "skipped": True, "reason": "no_cabinet_wb_token"}


def test_scheduler_execute_skips_without_assignments(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_scheduler_enabled=True,
            wb_api_token="env-token",
            wb_api_mode="real",
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr(
        repricer_tasks.repricer_bff_module,
        "FRONTEND_STRATEGY_ASSIGNMENTS",
        {},
    )
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks._persist_scheduler_skip", lambda **_kwargs: None)

    result = repricer_tasks.execute_assigned_for_org.run(10)

    assert result == {"organizationId": 10, "skipped": True, "reason": "no_strategy_assignments"}


def test_scheduler_all_orgs_only_enqueues_ready_organizations(monkeypatch):
    queued: list[tuple[int, str]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_scheduler_enabled=True,
            wb_api_mode="real",
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.list_organization_ids", lambda: [1, 10, 11])
    monkeypatch.setattr(
        "app.repricer_tasks.load_runtime_state",
        lambda organization_id: {
            1: {"assignments": {"A": {"strategyId": "plan_fact_daily"}}},
            10: {"assignments": {"JФЧ1009": {"strategyId": "plan_fact_daily"}}},
            11: {"assignments": {}},
        }.get(organization_id),
    )
    monkeypatch.setattr(
        "app.repricer_tasks.get_organization_wb_token_secret",
        lambda organization_id: "profile-token" if organization_id == 10 else None,
    )

    def delay_stub(organization_id, scenario):
        queued.append((organization_id, scenario))
        return SimpleNamespace(id=f"task-{organization_id}")

    monkeypatch.setattr(repricer_tasks.execute_assigned_for_org, "delay", delay_stub)

    result = repricer_tasks.execute_assigned_all_orgs.run()

    assert queued == [(10, "complete")]
    assert result["processedOrganizations"] == 1
    assert result["tasks"] == [{"organizationId": 10, "taskId": "task-10"}]
    assert result["skippedOrganizationsCount"] == 2
    assert result["skippedSummary"] == [
        {"reason": "no_cabinet_wb_token", "count": 1},
        {"reason": "no_strategy_assignments", "count": 1},
    ]


def test_scheduler_wb_sync_respects_recent_manual_sync(monkeypatch):
    calls: list[dict[str, object]] = []
    history_events: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_wb_sync_enabled=True,
            repricer_wb_sync_interval_minutes=40,
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        repricer_tasks.repricer_bff_module.ALGORITHM_SETTINGS_STATE,
        "fullSyncIntervalMinutes",
        40,
    )
    monkeypatch.setattr("app.repricer_tasks.datetime", SimpleNamespace(
        now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-06-24T09:12:00+00:00"),
        fromisoformat=repricer_tasks.datetime.fromisoformat,
    ))
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {
        "state": "completed",
        "running": False,
        "trigger": "manual",
        "finishedAt": "2026-06-24T09:10:00+00:00",
        "periodDays": 30,
    })
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr(
        "app.repricer_tasks.list_wb_sync_history",
        lambda *_args, **_kwargs: [
            {
                "type": "sync_finished",
                "syncProfile": profile_id,
                "state": "completed",
                "finishedAt": "2026-06-24T09:10:00+00:00",
            }
            for profile_id in (
                "hourly-operational",
                "sales-funnel-incremental",
                "stock-ads-incremental",
                "finance-recent",
            )
        ],
    )

    def refresh_stub(**kwargs):
        calls.append(kwargs)
        return {"runId": "unexpected", "state": "completed", "steps": []}

    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", refresh_stub)
    monkeypatch.setattr(
        "app.repricer_tasks.record_wb_sync_history_event",
        lambda _organization_id, event: history_events.append(event),
        raising=False,
    )

    result = repricer_tasks.sync_wb_data_for_org.run(10)

    assert calls == []
    assert result["organizationId"] == 10
    assert result["skipped"] is True
    assert result["reason"] == "profile_intervals_not_due"
    assert [profile["syncProfile"] for profile in result["profiles"]] == [
        "hourly-operational",
        "sales-funnel-incremental",
        "stock-ads-incremental",
        "finance-recent",
    ]
    assert result["profiles"][0]["lastRunAt"] == "2026-06-24T09:10:00+00:00"
    assert result["profiles"][0]["nextRunAt"] == "2026-06-24T10:10:00+00:00"
    assert history_events[0]["type"] == "scheduler_profile_decision"
    assert history_events[0]["decision"] == "not_due"
    assert history_events[0]["reason"] == "profile_intervals_not_due"


def test_scheduler_wb_sync_replaces_stale_status_before_running_profiles(monkeypatch):
    calls: list[dict[str, object]] = []
    history_events: list[dict[str, object]] = []
    abandoned: list[dict[str, object]] = []
    sequence: list[str] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_wb_sync_enabled=True,
            repricer_wb_sync_interval_minutes=40,
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        repricer_tasks.repricer_bff_module.ALGORITHM_SETTINGS_STATE,
        "fullSyncIntervalMinutes",
        720,
    )
    monkeypatch.setattr("app.repricer_tasks.datetime", SimpleNamespace(
        now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-09T08:05:50+00:00"),
        fromisoformat=repricer_tasks.datetime.fromisoformat,
    ))
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {
        "state": "stale",
        "running": False,
        "stale": True,
        "runId": "wb_sync_stale",
        "trigger": "scheduler",
        "startedAt": "2026-07-09T07:30:00+00:00",
        "updatedAt": "2026-07-09T07:30:00+00:00",
        "finishedAt": None,
        "currentSource": "goods",
    })
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr("app.repricer_tasks.list_wb_sync_history", lambda *_args, **_kwargs: [])

    def abandon_stub(_organization_id, **kwargs):
        abandoned.append(kwargs)
        sequence.append("abandon")
        return {"state": "failed", "running": False}

    monkeypatch.setattr("app.repricer_tasks.abandon_stale_wb_sync", abandon_stub)

    def refresh_stub(**kwargs):
        calls.append(kwargs)
        sequence.append(str(kwargs.get("sync_profile")))
        return {"runId": f"sync_{kwargs.get('sync_profile')}", "state": "completed", "steps": []}

    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", refresh_stub)
    monkeypatch.setattr(
        "app.repricer_tasks.record_wb_sync_history_event",
        lambda _organization_id, event: history_events.append(event),
        raising=False,
    )

    result = repricer_tasks.sync_wb_data_for_org.run(10)

    assert abandoned[0]["reason"] == "scheduler_replaced_stale"
    assert result["state"] == "completed"
    assert [call["sync_profile"] for call in calls] == [
        "hourly-operational",
        "sales-funnel-incremental",
        "stock-ads-incremental",
        "finance-recent",
    ]
    assert sequence == [
        "abandon",
        "hourly-operational",
        "sales-funnel-incremental",
        "stock-ads-incremental",
        "finance-recent",
    ]
    assert history_events[0]["decision"] == "recover"
    assert history_events[0]["reason"] == "wb_sync_stale_replaced"
    assert history_events[0]["intervalMinutes"] == 720
    assert history_events[0]["lastStatus"]["state"] == "stale"


def test_scheduler_wb_sync_waits_for_onboarding_window(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_wb_sync_enabled=True,
            repricer_wb_sync_interval_minutes=40,
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    monkeypatch.setattr("app.repricer_tasks.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.repricer_tasks.record_wb_sync_history_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", lambda **kwargs: calls.append(kwargs))

    result = repricer_tasks.sync_wb_data_for_org.run(10)

    assert calls == []
    assert result["skipped"] is True
    assert result["reason"] == "onboarding_30d_not_ready"


def test_scheduler_wb_sync_runs_due_periodic_windows(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            repricer_wb_sync_enabled=True,
            repricer_wb_sync_interval_minutes=40,
            repricer_wb_sync_period_days=30,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    monkeypatch.setattr("app.repricer_tasks.list_wb_sync_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.repricer_tasks.NIGHTLY_BASKETS_DETAIL_PAUSE_SECONDS", 0)
    monkeypatch.setattr("app.repricer_tasks._materialize_report_snapshots_for_profile", lambda *_args, **_kwargs: {"skipped": True})
    monkeypatch.setattr("app.repricer_tasks._persist_report_snapshots_sync_step", lambda *_args, **_kwargs: None)
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr(
        "app.repricer_tasks.datetime",
        SimpleNamespace(
            now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-22T09:12:00+00:00"),
            fromisoformat=repricer_tasks.datetime.fromisoformat,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.record_wb_sync_history_event", lambda *_args, **_kwargs: None)

    def refresh_stub(**kwargs):
        calls.append(kwargs)
        return {"runId": f"sync_{kwargs.get('sync_profile')}", "state": "completed", "steps": [], "syncProfile": kwargs.get("sync_profile")}

    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", refresh_stub)

    result = repricer_tasks.sync_wb_data_for_org.run(10)

    assert result["runId"] == "sync_finance-recent"
    assert result["state"] == "completed"
    assert [call["sync_profile"] for call in calls] == [
        "hourly-operational",
        "sales-funnel-incremental",
        "stock-ads-incremental",
        "finance-recent",
    ]
    assert calls[0]["period_days"] == 2
    assert calls[0]["sources"] == ("goods", "period-stats")
    assert calls[0]["sync_profile"] == "hourly-operational"
    assert calls[0]["sync_profile_label"] == "Оперативные заказы и продажи"
    assert calls[0]["window_kind"] == "incremental"
    assert calls[0]["cadence_minutes"] == 60
    assert calls[0]["date_from"].isoformat() == "2026-07-21"
    assert calls[0]["date_to"].isoformat() == "2026-07-22"
    assert calls[1]["sources"] == ("baskets",)
    assert calls[1]["baskets_include_daily_detail"] is False
    assert calls[2]["sources"] == ("stocks", "ads")
    assert calls[3]["sources"] == ("finance",)


def test_onboarding_wb_sync_runs_7_30_day_profiles(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    monkeypatch.setattr(
        "app.repricer_tasks.datetime",
        SimpleNamespace(
            now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-22T09:12:00+00:00"),
            fromisoformat=repricer_tasks.datetime.fromisoformat,
        ),
    )

    def refresh_stub(**kwargs):
        calls.append(kwargs)
        return {"runId": f"sync_{kwargs.get('sync_profile')}", "state": "completed", "steps": []}

    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", refresh_stub)

    result = repricer_tasks.sync_wb_onboarding_for_org.run(10)

    assert result["state"] == "completed"
    assert [call["sync_profile"] for call in calls] == ["onboarding-7d", "onboarding-30d"]
    assert [call["period_days"] for call in calls] == [7, 30]
    assert calls[0]["sources"] == ("goods", "content", "stocks", "period-stats", "finance", "ads", "baskets")
    assert calls[0]["baskets_include_daily_detail"] is True
    assert calls[1]["baskets_include_daily_detail"] is True


def test_nightly_wb_sync_runs_month_reconciliation_once_per_day(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    monkeypatch.setattr("app.repricer_tasks.list_wb_sync_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.repricer_tasks.NIGHTLY_BASKETS_DETAIL_PAUSE_SECONDS", 0)
    monkeypatch.setattr("app.repricer_tasks._materialize_report_snapshots_for_profile", lambda *_args, **_kwargs: {"skipped": True})
    monkeypatch.setattr("app.repricer_tasks._persist_report_snapshots_sync_step", lambda *_args, **_kwargs: None)
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr(
        "app.repricer_tasks.datetime",
        SimpleNamespace(
            now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-22T23:10:00+00:00"),
            fromisoformat=repricer_tasks.datetime.fromisoformat,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.record_wb_sync_history_event", lambda *_args, **_kwargs: None)

    def refresh_stub(**kwargs):
        calls.append(kwargs)
        return {"runId": "sync_nightly", "state": "completed", "steps": []}

    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", refresh_stub)

    result = repricer_tasks.sync_wb_nightly_for_org.run(10)

    assert result["runId"] == "sync_nightly"
    assert calls[0]["sync_profile"] == "nightly-30d-reconcile"
    assert calls[0]["period_days"] == 30
    assert calls[0]["sources"] == ("period-stats", "finance", "ads", "baskets")
    assert calls[0]["baskets_include_daily_detail"] is False
    assert [call["period_days"] for call in calls[1:]] == [15, 15]
    assert [call["sources"] for call in calls[1:]] == [("baskets",), ("baskets",)]
    assert all(call["baskets_include_daily_detail"] is True for call in calls[1:])
    assert result["basketsDetailProfiles"][0]["syncProfile"] == "nightly-30d-baskets-detail-1"


def test_nightly_wb_sync_skips_outside_night_window(monkeypatch):
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr(
        "app.repricer_tasks.datetime",
        SimpleNamespace(
            now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-22T11:10:00+00:00"),
            fromisoformat=repricer_tasks.datetime.fromisoformat,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.record_wb_sync_history_event", lambda _organization_id, event: events.append(event))
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: pytest.fail("daytime nightly must not read token"))

    result = repricer_tasks.sync_wb_nightly_for_org.run(10)

    assert result["skipped"] is True
    assert result["reason"] == "nightly_window_closed"
    assert result["timezone"] == "Europe/Moscow"
    assert events[0]["reason"] == "nightly_window_closed"


def test_nightly_wb_sync_skips_recent_reconciliation(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "profile-token")
    monkeypatch.setattr("app.repricer_tasks.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.flush_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    _mock_working_onboarding_ready(monkeypatch)
    monkeypatch.setattr(
        "app.repricer_tasks.list_wb_sync_history",
        lambda *_args, **_kwargs: [
            {
                "type": "sync_finished",
                "syncProfile": "nightly-30d-reconcile",
                "state": "completed",
                "finishedAt": "2026-07-22T02:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        "app.repricer_tasks.datetime",
        SimpleNamespace(
            now=lambda _tz=None: repricer_tasks.datetime.fromisoformat("2026-07-22T23:10:00+00:00"),
            fromisoformat=repricer_tasks.datetime.fromisoformat,
        ),
    )
    monkeypatch.setattr("app.repricer_tasks.record_wb_sync_history_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", lambda **kwargs: calls.append(kwargs))

    result = repricer_tasks.sync_wb_nightly_for_org.run(10)

    assert calls == []
    assert result["skipped"] is True
    assert result["reason"] == "profile_interval_not_due"


def test_nightly_wb_sync_all_orgs_enqueues_token_ready_organizations(monkeypatch):
    queued: list[tuple[int, str]] = []

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr("app.repricer_tasks.list_organization_ids", lambda: [1, 10, 11])
    monkeypatch.setattr(
        "app.repricer_tasks.get_organization_wb_token_secret",
        lambda organization_id: "profile-token" if organization_id in {10, 11} else None,
    )

    def delay_stub(organization_id, scenario):
        queued.append((organization_id, scenario))
        return SimpleNamespace(id=f"nightly-{organization_id}")

    monkeypatch.setattr(repricer_tasks.sync_wb_nightly_for_org, "delay", delay_stub)

    result = repricer_tasks.sync_wb_nightly_all_orgs.run()

    assert queued == [(10, "complete"), (11, "complete")]
    assert result["processedOrganizations"] == 2
    assert result["tasks"] == [
        {"organizationId": 10, "taskId": "nightly-10"},
        {"organizationId": 11, "taskId": "nightly-11"},
    ]
    assert result["skippedSummary"] == [{"reason": "no_cabinet_wb_token", "count": 1}]


def test_pnl_report_task_waits_for_1c_before_building(monkeypatch):
    from app.routers import wb_reports_bff as reports

    saved_states: list[dict] = []
    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("P&L cache build must not refresh WB"))
    monkeypatch.setattr(reports, "get_cash_flow_for_period", lambda **_kwargs: {"status": "pending", "job_id": "cf_waiting"})
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, _key, payload: saved_states.append(payload))
    monkeypatch.setattr(reports, "build_pnl_report", lambda **_kwargs: pytest.fail("P&L must not build before 1C is ready"))

    with pytest.raises(Retry):
        repricer_tasks.build_report_for_org.run(
            1,
            "finance@example.test",
            "pnl",
            "2026-05-01",
            "2026-05-31",
            "sku",
            "financial",
            True,
            "wb-token",
        )

    assert any(state.get("state") == "waiting_1c" for state in saved_states)


def test_stock_report_task_presyncs_exact_range_before_building(monkeypatch):
    from app.routers import wb_reports_bff as reports

    # Legacy ID retained: the worker now builds from ready cache without presync.
    snapshot_calls: list[dict[str, object]] = []
    report_calls: list[tuple[dict[str, str], object, dict[str, object]]] = []
    saved: dict[str, dict] = {}
    snapshot = SimpleNamespace(source_status="cached", orders=[], sales=[], stocks=[])

    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("stock cache build must not refresh WB"))
    monkeypatch.setattr(reports, "build_cached_wb_reports_sources_snapshot", lambda **kwargs: snapshot_calls.append(kwargs) or snapshot)
    monkeypatch.setattr(
        reports,
        "_build_stock_report_payload",
        lambda date_range, source_snapshot, **kwargs: report_calls.append((date_range, source_snapshot, kwargs)) or {"rows": [{"sku": "NM_1"}]},
    )
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda report, _organization_id: report)
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    result = repricer_tasks.build_report_for_org.run(
        1,
        "viewer",
        "stock",
        "2026-07-10",
        "2026-07-16",
        "warehouse",
        "operational",
        False,
        "wb-token",
    )

    assert result["state"] == "completed"
    assert snapshot_calls == [{"organization_id": 1, "date_from": date(2026, 7, 10), "date_to": date(2026, 7, 16)}]
    assert report_calls == [
        (
            {"preset": "custom", "from": "2026-07-10", "to": "2026-07-16"},
            snapshot,
            {"organization_id": 1, "wb_token": None},
        )
    ]
    assert saved["reports_payload_stock_2026-07-10_2026-07-16_warehouse_operational"]["report"] == {"rows": [{"sku": "NM_1"}]}


def test_ads_report_task_presyncs_exact_range_before_building(monkeypatch):
    from app.routers import wb_reports_bff as reports

    # Legacy ID retained: the worker now builds from ready cache without presync.
    report_calls: list[dict[str, object]] = []
    saved: dict[str, dict] = {}

    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("ads cache build must not refresh WB"))
    monkeypatch.setattr(reports, "_build_ads_report_payload", lambda **kwargs: report_calls.append(kwargs) or {"rows": [{"campaignId": "42"}]})
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda report, _organization_id: report)
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    result = repricer_tasks.build_report_for_org.run(
        1,
        "viewer",
        "ads",
        "2026-07-10",
        "2026-07-16",
        "campaign",
        "operational",
        False,
        "wb-token",
    )

    assert result["state"] == "completed"
    assert len(report_calls) == 1
    assert report_calls[0]["actor"].organization_id == 1
    assert report_calls[0]["actor"].user_id == "viewer"
    assert {key: value for key, value in report_calls[0].items() if key != "actor"} == {
        "date_from": date(2026, 7, 10),
        "date_to": date(2026, 7, 16),
        "date_range": {"preset": "custom", "from": "2026-07-10", "to": "2026-07-16"},
        "wb_token": None,
        "refresh": True,
    }
    assert saved["reports_payload_ads_2026-07-10_2026-07-16_campaign_operational"]["report"] == {"rows": [{"campaignId": "42"}]}


def test_report_source_refresh_task_refreshes_sources_then_builds_report(monkeypatch):
    from app.routers import wb_reports_bff as reports

    refresh_calls: list[dict[str, object]] = []
    build_calls: list[tuple[object, ...]] = []
    saved: dict[str, dict] = {}

    def replace(signature):
        build_calls.append(signature.args)
        assert signature.kwargs["source_refresh"]["state"] == "completed"
        raise Ignore()

    monkeypatch.setattr("app.repricer_tasks.get_user_wb_token_secret", lambda _user_id: None)
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: "org-wb-token")
    monkeypatch.setattr(
        "app.repricer_tasks.refresh_wb_data_sources",
        lambda **kwargs: refresh_calls.append(kwargs) or {"state": "completed", "steps": [{"source": "baskets", "status": "ok"}]},
    )
    monkeypatch.setattr(
        repricer_tasks.refresh_report_sources_for_org,
        "replace",
        replace,
    )
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    with pytest.raises(Ignore):
        repricer_tasks.refresh_report_sources_for_org.run(
            1,
            "viewer",
            "rnp",
            "2026-07-10",
            "2026-07-16",
            "sku",
            "operational",
            True,
            None,
        )
    assert refresh_calls[0]["trigger"] == "reports-rnp-manual-refresh"
    assert refresh_calls[0]["force"] is True
    assert refresh_calls[0]["wb_token"] == "org-wb-token"
    assert refresh_calls[0]["sources"] == ("baskets", "ads")
    assert refresh_calls[0]["baskets_include_daily_detail"] is True
    assert refresh_calls[0]["execute_lock"] is False
    assert build_calls[0][2] == "rnp"
    assert saved["reports_job_rnp_2026-07-10_2026-07-16_sku"]["stage"] == "building_report"


def test_report_source_refresh_task_fails_without_wb_token(monkeypatch):
    from app.routers import wb_reports_bff as reports

    saved: dict[str, dict] = {}

    monkeypatch.setattr("app.repricer_tasks.get_user_wb_token_secret", lambda _user_id: " ")
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.repricer_tasks.refresh_wb_data_sources", lambda **_kwargs: pytest.fail("WB refresh must not run without token"))
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    with pytest.raises(RuntimeError, match="no_cabinet_wb_token"):
        repricer_tasks.refresh_report_sources_for_org.run(
            1,
            "viewer",
            "abc",
            "2026-07-10",
            "2026-07-16",
            "sku",
            "operational",
            True,
            None,
        )

    assert saved["reports_job_abc_2026-07-10_2026-07-16_sku_finance"]["state"] == "failed"
    assert saved["reports_job_abc_2026-07-10_2026-07-16_sku_finance"]["error"] == "no_cabinet_wb_token"


def test_report_source_refresh_task_fails_on_partial_wb_refresh(monkeypatch):
    from app.routers import wb_reports_bff as reports

    saved: dict[str, dict] = {}

    monkeypatch.setattr("app.repricer_tasks.get_user_wb_token_secret", lambda _user_id: "user-token")
    monkeypatch.setattr("app.repricer_tasks.get_organization_wb_token_secret", lambda _organization_id: None)
    monkeypatch.setattr(
        "app.repricer_tasks.refresh_wb_data_sources",
        lambda **_kwargs: {
            "state": "partial",
            "steps": [
                {"source": "period-stats", "status": "ok"},
                {"source": "finance", "status": "error", "error": "429 Too Many Requests"},
            ],
        },
    )
    monkeypatch.setattr(repricer_tasks.build_report_for_org, "run", lambda *_args: pytest.fail("report must not build after partial WB refresh"))
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    with pytest.raises(RuntimeError, match="finance: 429 Too Many Requests"):
        repricer_tasks.refresh_report_sources_for_org.run(
            1,
            "viewer",
            "abc",
            "2026-07-10",
            "2026-07-16",
            "sku",
            "operational",
            True,
            None,
        )

    failed_job = saved["reports_job_abc_2026-07-10_2026-07-16_sku_finance"]
    assert failed_job["state"] == "failed"
    assert failed_job["error"] == "finance: 429 Too Many Requests"


def test_report_source_refresh_plan_for_stock_includes_stock_and_finance_sources():
    plan = repricer_tasks._report_source_refresh_plan("stock")

    assert plan["sources"] == ("stocks", "period-stats", "finance")
    assert plan["baskets_include_daily_detail"] is False


def test_rnp_report_task_presyncs_funnel_sources_before_building(monkeypatch):
    from app.routers import wb_reports_bff as reports

    # Legacy ID retained: the worker now builds from ready cache without presync.
    report_calls: list[dict[str, object]] = []
    saved: dict[str, dict] = {}

    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("RNP cache build must not refresh WB"))
    monkeypatch.setattr(reports, "build_rnp_report", lambda **kwargs: report_calls.append(kwargs) or SimpleNamespace(rows=[]))
    monkeypatch.setattr(reports, "_map_rnp_to_report_response", lambda *_args, **_kwargs: {"rows": [{"sku": "NM_1"}]})
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda report, _organization_id: report)
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    result = repricer_tasks.build_report_for_org.run(
        1,
        "viewer",
        "rnp",
        "2026-07-10",
        "2026-07-16",
        "sku",
        "operational",
        False,
        "wb-token",
    )

    assert result["state"] == "completed"
    assert len(report_calls) == 1
    assert callable(report_calls[0].pop("progress_callback"))
    assert report_calls == [{
        "date_from": date(2026, 7, 10),
        "date_to": date(2026, 7, 16),
        "group_by": "sku",
        "finance_allowed": False,
        "organization_id": 1,
        "wb_token": None,
        "force_refresh": False,
    }]
    assert saved["reports_payload_rnp_2026-07-10_2026-07-16_sku_operational"]["report"] == {"rows": [{"sku": "NM_1"}]}


def test_digest_task_presyncs_report_sources_before_building(monkeypatch):
    from app.routers import wb_reports_bff as reports

    # Legacy ID retained: the worker now builds from ready cache without presync.
    snapshot_calls: list[dict[str, object]] = []
    ads_calls: list[dict[str, object]] = []
    funnel_calls: list[dict[str, object]] = []
    saved: dict[str, dict] = {}
    snapshot = SimpleNamespace(source_status="cached", orders=[], sales=[], stocks=[])
    ads = SimpleNamespace(rows=[])

    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("digest cache build must not refresh WB"))
    monkeypatch.setattr(reports, "build_cached_wb_reports_sources_snapshot", lambda **kwargs: snapshot_calls.append(kwargs) or snapshot)
    monkeypatch.setattr(reports, "_build_digest_ads_snapshot", lambda **kwargs: ads_calls.append(kwargs) or ads)
    monkeypatch.setattr(reports, "_build_digest_funnel_snapshot", lambda **kwargs: funnel_calls.append(kwargs) or {})
    monkeypatch.setattr(reports, "build_plan_fact_report", lambda **_kwargs: SimpleNamespace(rows=[]))
    monkeypatch.setattr(reports, "_build_digest_payload", lambda *_args, **_kwargs: {"rows": [{"sku": "NM_1"}]})
    monkeypatch.setattr(reports, "_apply_digest_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repricer_tasks, "_digest_report_summary", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(reports, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(reports, "get_source_cache", lambda _organization_id, key, **_kwargs: saved.get(key))

    result = repricer_tasks.build_digest_for_org.run(
        1,
        "2026-07-10",
        "2026-07-16",
        False,
        "wb-token",
    )

    assert result["state"] == "completed"
    expected_range = {"organization_id": 1, "date_from": date(2026, 7, 10), "date_to": date(2026, 7, 16)}
    assert snapshot_calls == [expected_range]
    assert ads_calls == [{**expected_range, "wb_token": None}]
    assert callable(funnel_calls[0].pop("progress_callback"))
    assert funnel_calls == [{**expected_range, "wb_token": None}]
    assert saved[f"reports_digest_{reports.DIGEST_REPORT_PAYLOAD_VERSION}_2026-07-10_2026-07-16"]["digest"] == {"rows": [{"sku": "NM_1"}]}
