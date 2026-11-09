from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.reviews.schemas import ReviewAiGenerationResult
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    return TestClient(create_app())


def _mock_openai_review_reply(monkeypatch):
    def fake_generate_openai_review_reply(*, feedback, rating, moderation, **_kwargs):
        return ReviewAiGenerationResult(
            replyText=f"Спасибо за отзыв о {feedback.productName}.",
            riskLevel="medium" if moderation.state != "clean" or rating < 4 else "low",
            requiresApproval=moderation.state != "clean" or rating < 4,
            reasons=list(moderation.reasons),
            matchedStopTopics=[],
            model="test-openai",
            generationSource="openai",
        )

    monkeypatch.setattr("app.reviews.service.generate_openai_review_reply", fake_generate_openai_review_reply)


def _configure_synthetic_review_prompt(api, headers):
    # Real generation requires an explicit prompt; do not weaken its 409 guard.
    configured = api.put(
        "/api/v1/wb-reviews/sync-settings", headers=headers,
        json={"enabled": False, "aiPrompt": "Synthetic local review test instruction"},
    )
    assert configured.status_code == 200
    assert configured.json()["data"]["aiPrompt"] == "Synthetic local review test instruction"
    assert configured.json()["data"]["enabled"] is False
    assert configured.json()["data"]["reviewSettings"]["automationMode"] == "draft-first"
    assert configured.json()["data"]["reviewSettings"]["lowStarsAction"] == "alert-block"


def test_reviews_settings_bundle_is_available_for_editor():
    api = client()
    headers = auth_headers(api, "settings_editor")

    response = api.get("/api/v1/wb-reviews/settings", headers=headers)

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["templates"]
    assert payload["stopTopics"]
    assert payload["moderationRules"]


def test_reviews_sync_settings_default_and_update_clamps_base_token_interval():
    api = client()
    headers = auth_headers(api, "settings_editor")

    default_response = api.get("/api/v1/wb-reviews/sync-settings", headers=headers)
    assert default_response.status_code == 200
    default_settings = default_response.json()["data"]
    assert default_settings["enabled"] is True
    assert default_settings["intervalMinutes"] == 30
    assert default_settings["tokenType"] == "personal"

    updated = api.put(
        "/api/v1/wb-reviews/sync-settings",
        headers=headers,
        json={
            "enabled": True,
            "intervalMinutes": 5,
            "tokenType": "base",
            "unansweredOnly": True,
            "take": 200,
            "lookbackDays": 14,
        },
    )

    assert updated.status_code == 200
    settings = updated.json()["data"]
    assert settings["intervalMinutes"] == 12
    assert settings["tokenType"] == "base"
    assert settings["unansweredOnly"] is True
    assert settings["take"] == 200
    assert settings["lookbackDays"] == 14


def test_reviews_sync_status_reports_idle_before_first_sync():
    api = client()
    headers = auth_headers(api, "settings_editor")

    response = api.get("/api/v1/wb-reviews/sync-status", headers=headers)

    assert response.status_code == 200
    status = response.json()["data"]
    assert status["status"] == "idle"
    assert status["lastRunAt"] is None
    assert status["lastSyncedCount"] == 0
    assert status["nextRunAt"] is not None


def test_feedbacks_runtime_does_not_mask_wb_api_errors(monkeypatch):
    from app.wb_api.client import FakeWbApiClient
    from app.wb_api.feedbacks_runtime import WbFeedbacksFetchError, fetch_feedbacks

    monkeypatch.setattr(
        "app.wb_api.feedbacks_runtime.build_wb_feedbacks_client",
        lambda scenario="complete", token_override=None: FakeWbApiClient(errors={"/api/v1/feedbacks": 403}),
    )

    with pytest.raises(WbFeedbacksFetchError) as exc:
        fetch_feedbacks(scenario="complete")

    assert exc.value.status_code == 403
    assert exc.value.code == "forbidden_scope"


def test_feedbacks_runtime_uses_cabinet_token_override(monkeypatch):
    from app.wb_api.client import FakeWbApiClient
    from app.wb_api.feedbacks_runtime import fetch_feedbacks

    captured = {}

    def build_client(*, scenario="complete", token_override=None):
        captured["scenario"] = scenario
        captured["token_override"] = token_override
        return FakeWbApiClient(fixtures={"/api/v1/feedbacks": {"data": {"feedbacks": []}}})

    monkeypatch.setattr("app.wb_api.feedbacks_runtime.build_wb_feedbacks_client", build_client)

    assert fetch_feedbacks(scenario="complete", wb_token="cabinet-token") == []
    assert captured == {"scenario": "complete", "token_override": "cabinet-token"}


def test_reviews_settings_are_editable_through_backend():
    api = client()
    headers = auth_headers(api, "settings_editor")

    created = api.post(
        "/api/v1/wb-reviews/templates",
        headers=headers,
        json={
            "brandVoiceId": "wb-default",
            "title": "Custom empathy reply",
            "bodyTemplate": "Спасибо за отзыв о {productName}. Мы внимательно изучили вашу обратную связь.",
            "ratingFrom": 1,
            "ratingTo": 3,
            "keywords": ["обратн"],
            "keywordMode": "any",
            "isActive": True,
        },
    )
    assert created.status_code == 200
    template = created.json()["data"]

    updated = api.put(
        f"/api/v1/wb-reviews/templates/{template['templateId']}",
        headers=headers,
        json={
            "brandVoiceId": "wb-default",
            "title": "Custom empathy reply v2",
            "bodyTemplate": "Спасибо за отзыв о {productName}. Мы уже передали замечание команде качества.",
            "ratingFrom": 1,
            "ratingTo": 3,
            "keywords": ["замеч"],
            "keywordMode": "any",
            "isActive": True,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["data"]["title"] == "Custom empathy reply v2"


def test_reviews_sync_generate_approve_and_send_workflow(monkeypatch):
    _mock_openai_review_reply(monkeypatch)
    monkeypatch.setattr("app.routers.wb_reviews.get_user_wb_token_secret", lambda _user_id: "cabinet-token")
    api = client()
    editor_headers = auth_headers(api, "settings_editor")
    admin_headers = auth_headers(api, "admin")
    _configure_synthetic_review_prompt(api, editor_headers)

    sync = api.post(
        "/api/v1/wb-reviews/sync",
        headers=editor_headers,
        json={"scenario": "complete", "take": 100, "skip": 0, "order": "dateDesc"},
    )
    assert sync.status_code == 200
    assert sync.json()["data"]["syncedCount"] >= 2

    feedbacks = api.get("/api/v1/wb-reviews/feedbacks", headers=editor_headers)
    assert feedbacks.status_code == 200
    feedback_ids = {row["feedbackId"] for row in feedbacks.json()["items"]}
    assert "low-rating-review-001" in feedback_ids

    draft_response = api.post(
        "/api/v1/wb-reviews/drafts/generate",
        headers=editor_headers,
        json={
            "feedbackId": "low-rating-review-001",
            "brandVoiceId": "wb-default",
            "cacheMetrics": {"cachedTokens": 144, "hitRatePct": 62.5},
            "promptInstruction": "Вариант промпта: отзыв 3*2*1* (выкуп)\nИнструкция: попросить написать в чат",
            "regenerate": True,
        },
    )
    assert draft_response.status_code == 200
    draft = draft_response.json()["data"]
    assert draft["approvalState"] == "required"
    assert draft["externalSendAllowed"] is False
    # Default lowStarsAction=alert-block keeps a two-star draft blocked.
    assert draft["sendState"] == "blocked"
    assert draft["promptTraceId"] is not None

    prompt_trace = api.get(
        f"/api/v1/wb-reviews/drafts/{draft['draftId']}/prompt-trace",
        headers=editor_headers,
    )
    assert prompt_trace.status_code == 200
    assert prompt_trace.json()["data"]["cacheMetrics"]["cachedTokens"] == 144
    assert "отзыв 3*2*1*" in prompt_trace.json()["data"]["metadata"]["promptInstruction"]

    blocked_send = api.post(
        f"/api/v1/wb-reviews/drafts/{draft['draftId']}/send",
        headers=admin_headers,
        json={"dryRun": False, "reason": "attempt before approval"},
    )
    assert blocked_send.status_code == 200
    assert blocked_send.json()["data"]["status"] == "blocked"

    approved = api.post(
        f"/api/v1/wb-reviews/drafts/{draft['draftId']}/approve",
        headers=admin_headers,
        json={"approvalRef": "APR-1001", "reason": "support reviewed"},
    )
    assert approved.status_code == 200
    assert approved.json()["data"]["approvalState"] == "approved"
    assert approved.json()["data"]["externalSendAllowed"] is False
    assert approved.json()["data"]["sendState"] == "blocked"

    approval_snapshot = api.get("/api/v1/wb-reviews/low-rating-review-001/approval", params={"rating": 2})
    assert approval_snapshot.status_code == 200
    assert approval_snapshot.json()["approvalState"] == "approved"

    dry_run_send = api.post(
        f"/api/v1/wb-reviews/drafts/{draft['draftId']}/send",
        headers=admin_headers,
        json={"dryRun": True, "reason": "uat smoke"},
    )
    assert dry_run_send.status_code == 200
    send_job = dry_run_send.json()["data"]
    assert send_job["status"] == "blocked"
    assert send_job["externalRequestPath"] is None
    assert "disabled" in send_job["resultMessage"].lower()

    fetched_job = api.get(
        f"/api/v1/wb-reviews/send-jobs/{send_job['sendJobId']}",
        headers=editor_headers,
    )
    assert fetched_job.status_code == 200
    assert "disabled" in fetched_job.json()["data"]["resultMessage"].lower()


def test_safe_review_generation_stays_draft_only_without_live_send(monkeypatch):
    _mock_openai_review_reply(monkeypatch)
    monkeypatch.setattr("app.routers.wb_reviews.get_user_wb_token_secret", lambda _user_id: "cabinet-token")
    api = client()
    editor_headers = auth_headers(api, "settings_editor")
    _configure_synthetic_review_prompt(api, editor_headers)

    sync = api.post(
        "/api/v1/wb-reviews/sync",
        headers=editor_headers,
        json={"scenario": "complete", "take": 100, "skip": 0, "order": "dateDesc"},
    )
    assert sync.status_code == 200

    draft_response = api.post(
        "/api/v1/wb-reviews/drafts/generate",
        headers=editor_headers,
        json={
            "feedbackId": "safe-review-001",
            "brandVoiceId": "wb-default",
            "regenerate": True,
        },
    )

    assert draft_response.status_code == 200
    draft = draft_response.json()["data"]
    # Default draft-first still requires review, including a clean five-star reply.
    assert draft["approvalState"] == "required"
    assert draft["externalSendAllowed"] is False
    assert draft["sendState"] == "draft_only"


def test_reviews_send_requires_reviews_send_permission(monkeypatch):
    _mock_openai_review_reply(monkeypatch)
    monkeypatch.setattr("app.routers.wb_reviews.get_user_wb_token_secret", lambda _user_id: "cabinet-token")
    api = client()
    editor_headers = auth_headers(api, "settings_editor")
    admin_headers = auth_headers(api, "admin")
    _configure_synthetic_review_prompt(api, editor_headers)

    api.post(
        "/api/v1/wb-reviews/sync",
        headers=editor_headers,
        json={"scenario": "complete", "take": 100, "skip": 0, "order": "dateDesc"},
    )
    draft_response = api.post(
        "/api/v1/wb-reviews/drafts/generate",
        headers=editor_headers,
        json={"feedbackId": "low-rating-review-001", "brandVoiceId": "wb-default", "regenerate": True},
    )
    assert draft_response.status_code == 200, draft_response.text
    draft_id = draft_response.json()["data"]["draftId"]
    api.post(
        f"/api/v1/wb-reviews/drafts/{draft_id}/approve",
        headers=admin_headers,
        json={"approvalRef": "APR-2002"},
    )

    response = api.post(
        f"/api/v1/wb-reviews/drafts/{draft_id}/send",
        headers=editor_headers,
        json={"dryRun": True},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "NO_ACCESS:reviews:send"


def test_missing_ai_prompt_blocks_before_generation(monkeypatch):
    def forbidden_generation(**_kwargs):
        pytest.fail("missing prompt reached generation")

    monkeypatch.setattr("app.reviews.service.generate_openai_review_reply", forbidden_generation)
    monkeypatch.setattr("app.routers.wb_reviews.get_user_wb_token_secret", lambda _user_id: "synthetic-token")
    api = client()
    headers = auth_headers(api, "settings_editor")
    configured = api.put("/api/v1/wb-reviews/sync-settings", headers=headers,
                         json={"enabled": False, "aiPrompt": ""})
    assert configured.status_code == 200
    sync = api.post("/api/v1/wb-reviews/sync", headers=headers,
                    json={"scenario": "complete", "take": 100, "skip": 0, "order": "dateDesc"})
    assert sync.status_code == 200
    response = api.post("/api/v1/wb-reviews/drafts/generate", headers=headers,
                        json={"feedbackId": "safe-review-001", "brandVoiceId": "wb-default"})
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "REVIEW_AI_PROMPT_REQUIRED"
