from datetime import date, datetime, timedelta, timezone


def test_onboarding_profiles_stage_from_7_to_30_days():
    from app.wb_sync_plan import onboarding_sync_profiles

    profiles = onboarding_sync_profiles(as_of=date(2026, 7, 22))

    assert [profile.profile_id for profile in profiles] == [
        "onboarding-7d",
        "onboarding-30d",
    ]
    assert [(profile.date_from, profile.date_to) for profile in profiles] == [
        (date(2026, 7, 16), date(2026, 7, 22)),
        (date(2026, 6, 23), date(2026, 7, 22)),
    ]
    assert profiles[0].sources == (
        "goods",
        "content",
        "stocks",
        "period-stats",
        "finance",
        "ads",
        "baskets",
    )
    assert profiles[0].baskets_daily_detail is True
    assert profiles[1].baskets_daily_detail is True


def test_periodic_profiles_use_short_refresh_windows():
    from app.wb_sync_plan import periodic_sync_profiles

    profiles = {profile.profile_id: profile for profile in periodic_sync_profiles(as_of=date(2026, 7, 22))}

    assert profiles["hourly-operational"].period_days == 2
    assert profiles["hourly-operational"].sources == ("goods", "period-stats")
    assert profiles["sales-funnel-incremental"].period_days == 2
    assert profiles["sales-funnel-incremental"].sources == ("baskets",)
    assert profiles["stock-ads-incremental"].period_days == 3
    assert profiles["stock-ads-incremental"].sources == ("stocks", "ads")
    assert profiles["finance-recent"].period_days == 7
    assert profiles["finance-recent"].sources == ("finance",)


def test_historical_sync_as_of_uses_last_closed_day_for_cold_windows():
    from app.wb_sync_plan import historical_sync_as_of, onboarding_sync_profiles

    as_of = historical_sync_as_of(datetime(2026, 8, 5, 4, 30, tzinfo=timezone.utc))
    profiles = {profile.profile_id: profile for profile in onboarding_sync_profiles(as_of=as_of)}

    assert as_of == date(2026, 8, 4)
    assert profiles["onboarding-7d"].date_from == date(2026, 7, 29)
    assert profiles["onboarding-7d"].date_to == date(2026, 8, 4)
    assert profiles["onboarding-30d"].date_from == date(2026, 7, 6)
    assert profiles["onboarding-30d"].date_to == date(2026, 8, 4)


def test_nightly_profile_reconciles_month_with_split_baskets_detail():
    from app.wb_sync_plan import nightly_baskets_detail_profiles, nightly_reconciliation_profile

    profile = nightly_reconciliation_profile(as_of=date(2026, 7, 22))
    detail_profiles = nightly_baskets_detail_profiles(as_of=date(2026, 7, 22))

    assert profile.profile_id == "nightly-30d-reconcile"
    assert profile.period_days == 30
    assert profile.sources == ("period-stats", "finance", "ads", "baskets")
    assert profile.baskets_daily_detail is False
    assert [item.period_days for item in detail_profiles] == [15, 15]
    assert [item.sources for item in detail_profiles] == [("baskets",), ("baskets",)]
    assert all(item.baskets_daily_detail for item in detail_profiles)
    assert detail_profiles[0].date_from == date(2026, 6, 23)
    assert detail_profiles[0].date_to == date(2026, 7, 7)
    assert detail_profiles[1].date_from == date(2026, 7, 8)
    assert detail_profiles[1].date_to == date(2026, 7, 22)


def test_sales_funnel_estimate_explains_month_long_cold_sync():
    from app.wb_sync_plan import estimate_sales_funnel

    estimate = estimate_sales_funnel(sku_count=3000, period_days=30, include_daily_detail=True)

    assert estimate.request_count == 93
    assert estimate.estimated_seconds == 1674
    assert estimate.explanation == "3 SKU batches * (1 aggregate + 30 daily detail windows)"

    aggregate_only = estimate_sales_funnel(sku_count=3000, period_days=2, include_daily_detail=False)
    assert aggregate_only.request_count == 3
    assert aggregate_only.estimated_seconds == 54


def test_begin_wb_sync_persists_profile_metadata(monkeypatch):
    from app import repricer_sync

    saved: dict[str, object] = {}
    monkeypatch.setattr(repricer_sync, "get_wb_sync_status", lambda _organization_id: {"state": "idle"})
    monkeypatch.setattr(repricer_sync, "_save_status", lambda _organization_id, payload: saved.setdefault("status", payload))
    monkeypatch.setattr(repricer_sync, "record_wb_sync_history_event", lambda *_args, **_kwargs: None)

    status = repricer_sync.begin_wb_sync(
        1,
        trigger="manual",
        period_days=2,
        force=False,
        sources=("period-stats",),
        token_fingerprint="token-1",
        sync_profile="hourly-operational",
        sync_profile_label="Оперативные заказы и продажи",
        window_kind="incremental",
        cadence_minutes=60,
        baskets_daily_detail=False,
        estimated_requests=0,
        estimated_seconds=0,
        estimate_explanation="No Sales Funnel requests",
    )

    assert status["syncProfile"] == "hourly-operational"
    assert status["syncProfileLabel"] == "Оперативные заказы и продажи"
    assert status["windowKind"] == "incremental"
    assert status["cadenceMinutes"] == 60
    assert status["basketsDailyDetail"] is False
    assert status["estimatedSeconds"] == 0
    assert saved["status"] == status


def test_wb_sync_status_exposes_profile_metadata(monkeypatch):
    from app import repricer_sync

    monkeypatch.setattr(
        repricer_sync,
        "get_source_cache",
        lambda *_args, **_kwargs: {
            "state": "completed",
            "running": False,
            "runId": "sync_1",
            "syncProfile": "sales-funnel-incremental",
            "syncProfileLabel": "Воронка продаж сегодня и вчера",
            "windowKind": "incremental",
            "cadenceMinutes": 120,
            "basketsDailyDetail": False,
            "estimatedRequests": 2,
            "estimatedSeconds": 36,
            "estimateExplanation": "1 SKU batches * (1 aggregate)",
        },
    )

    status = repricer_sync.get_wb_sync_status(1)

    assert status["syncProfile"] == "sales-funnel-incremental"
    assert status["syncProfileLabel"] == "Воронка продаж сегодня и вчера"
    assert status["cadenceMinutes"] == 120
    assert status["estimatedSeconds"] == 36


def test_queued_wb_sync_preserves_last_covered_range(monkeypatch):
    from app import repricer_sync

    saved: dict[str, object] = {}
    monkeypatch.setattr(
        repricer_sync,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "running": False,
            "periodDays": 31,
            "dateFrom": "2026-06-15",
            "dateTo": "2026-07-15",
            "periodCacheSuffix": "2026-06-15_2026-07-15",
            "finishedAt": "2026-07-15T22:00:00+00:00",
        },
    )
    monkeypatch.setattr(repricer_sync, "_save_status", lambda _organization_id, payload: saved.setdefault("status", payload))
    monkeypatch.setattr(repricer_sync, "record_wb_sync_history_event", lambda *_args, **_kwargs: None)

    status = repricer_sync.queue_wb_sync(
        1,
        trigger="manual-onboarding",
        task_id="task-1",
        sync_profile="onboarding-full",
        sync_profile_label="Полная холодная загрузка",
        window_kind="onboarding",
        sources=("goods", "finance", "baskets"),
    )

    assert status["state"] == "queued"
    assert status["dateFrom"] == "2026-06-15"
    assert status["dateTo"] == "2026-07-15"
    assert status["periodDays"] == 31
    assert status["periodCacheSuffix"] == "2026-06-15_2026-07-15"
    assert saved["status"] == status


def test_abandon_stale_wb_sync_marks_running_steps_failed(monkeypatch):
    from app import repricer_sync

    saved: dict[str, object] = {}
    events: list[dict[str, object]] = []
    stale_status = {
        "state": "stale",
        "stale": True,
        "running": False,
        "runId": "stale-run",
        "syncProfile": "nightly-30d-reconcile",
        "steps": [
            {"source": "finance", "status": "running", "progressPercent": 5},
            {"source": "ads", "status": "ok", "progressPercent": 100},
        ],
        "startedAt": "2026-07-23T07:00:00+00:00",
        "updatedAt": "2026-07-23T07:10:00+00:00",
    }
    monkeypatch.setattr(repricer_sync, "_save_status", lambda _organization_id, payload: saved.setdefault("status", payload))
    monkeypatch.setattr(repricer_sync, "record_wb_sync_history_event", lambda _organization_id, event: events.append(event))

    status = repricer_sync.abandon_stale_wb_sync(1, reason="manual_onboarding_replaced_stale", status=stale_status)

    assert status["state"] == "failed"
    assert status["running"] is False
    assert status["steps"][0]["status"] == "error"
    assert status["steps"][0]["error"] == "WB sync heartbeat expired"
    assert saved["status"] == status
    assert events[0]["type"] == "sync_abandoned"
    assert events[0]["reason"] == "manual_onboarding_replaced_stale"


def test_long_running_wb_sync_uses_estimated_seconds_before_stale(monkeypatch):
    from app import repricer_sync

    started = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)
    status_payload = {
        "state": "running",
        "running": True,
        "runId": "long-run",
        "estimatedSeconds": 2400,
        "steps": [{"source": "baskets", "status": "running", "progressPercent": 83}],
        "startedAt": started.isoformat(),
        "updatedAt": started.isoformat(),
    }
    monkeypatch.setattr(repricer_sync, "get_source_cache", lambda *_args, **_kwargs: status_payload)
    monkeypatch.setattr(repricer_sync, "_utc_now", lambda: started + timedelta(minutes=35))

    status = repricer_sync.get_wb_sync_status(1)

    assert status["running"] is True
    assert status["stale"] is False
