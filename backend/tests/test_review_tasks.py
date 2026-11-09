from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.infra.celery_app import get_celery_app
from app.reviews.schemas import ReviewFeedbackSyncResponse, ReviewSyncSettingsView, ReviewSyncStatusView


def test_celery_beat_includes_reviews_sync_feedbacks(monkeypatch):
    monkeypatch.setenv("VELLA_REPRICER_SCHEDULER_ENABLED", "true")
    get_celery_app.cache_clear()
    try:
        celery_app = get_celery_app()

        assert "reviews-sync-feedbacks" in celery_app.conf.beat_schedule
        assert celery_app.conf.beat_schedule["reviews-sync-feedbacks"]["task"] == "reviews.sync_all_orgs"
    finally:
        get_celery_app.cache_clear()


def test_reviews_sync_for_org_skips_when_interval_not_due(monkeypatch):
    from app import review_tasks

    future = datetime.now(timezone.utc) + timedelta(minutes=15)
    monkeypatch.setattr(
        review_tasks,
        "get_sync_status",
        lambda organization_id, actor_id=None: ReviewSyncStatusView(
            organizationId=organization_id,
            status="completed",
            enabled=True,
            intervalMinutes=30,
            nextRunAt=future,
            lastRunAt=datetime.now(timezone.utc),
            lastCompletedAt=datetime.now(timezone.utc),
            lastSyncedCount=3,
            lastSourceStatus="fresh",
            lastError=None,
        ),
    )

    result = review_tasks.sync_reviews_for_org(organization_id=1)

    assert result["skipped"] is True
    assert result["reason"] == "review_sync_interval_not_due"


def test_reviews_sync_for_org_runs_when_due(monkeypatch):
    from app import review_tasks

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    calls: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        review_tasks,
        "get_sync_status",
        lambda organization_id, actor_id=None: ReviewSyncStatusView(
            organizationId=organization_id,
            status="completed",
            enabled=True,
            intervalMinutes=30,
            nextRunAt=past,
            lastRunAt=past - timedelta(minutes=30),
            lastCompletedAt=past - timedelta(minutes=30),
            lastSyncedCount=0,
            lastSourceStatus="fresh",
            lastError=None,
        ),
    )
    monkeypatch.setattr(
        review_tasks,
        "get_sync_settings",
        lambda organization_id, actor_id=None: ReviewSyncSettingsView(
            organizationId=organization_id,
            enabled=True,
            intervalMinutes=30,
            tokenType="personal",
            unansweredOnly=True,
            take=500,
            lookbackDays=30,
            updatedByActorId=actor_id,
            createdAt=past,
            updatedAt=past,
            minIntervalMinutes=12,
        ),
    )
    monkeypatch.setattr(review_tasks, "get_organization_wb_token_secret", lambda organization_id: "org-token")
    monkeypatch.setattr(
        review_tasks,
        "sync_feedbacks",
        lambda actor, payload, wb_token=None: calls.append((actor.organization_id, wb_token))
        or ReviewFeedbackSyncResponse(syncedCount=2, sourceStatus="fresh", nextSkip=2, syncedAt=datetime.now(timezone.utc)),
    )

    result = review_tasks.sync_reviews_for_org(organization_id=7)

    assert calls == [(7, "org-token")]
    assert result["syncedCount"] == 2
    assert result["skipped"] is False
