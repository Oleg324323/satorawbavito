"""Synthetic received DTO scenarios; no provider fetch or replacement repository."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.reviews.canonical_contract import normalize_wb_review, normalize_avito_review
from app.reviews.decision_contract import (
    ReviewDecisionError, ReviewPolicyVersion, ReviewSourceEvidence,
    build_review_draft, validate_review_draft_publication,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


@pytest.fixture(params=["wb", "avito"])
def received(request):
    provider = request.param
    payloads = json.loads((Path(__file__).parent / "fixtures" / "reviews" /
                          f"{provider}_reviews_synthetic.json").read_text())["cases"]
    name = "unanswered" if provider == "wb" else "can_answer_true_numeric_id"
    return (normalize_wb_review if provider == "wb" else normalize_avito_review), payloads[name]


def draft_for(fact):
    source = ReviewSourceEvidence(UUID(int=1), fact, "current")
    policy = ReviewPolicyVersion(1, 11, fact.identity.marketplace, UUID(int=2), 1,
                                 "a" * 64, "test-template", "fake-model")
    draft = build_review_draft(source=source, policy=policy, draft_id=UUID(int=3),
                               expected_version=0, text="[TEST] answer", created_at=NOW)
    return source, policy, draft


def test_same_external_id_on_other_account_cannot_publish_draft(received):
    normalize, dto = received
    fact = normalize(dto, 1, 11, "test-first", NOW)
    source, policy, draft = draft_for(fact)
    foreign = normalize(dto, 1, 12, "test-other", NOW)
    assert foreign.identity.external_review_id == fact.identity.external_review_id
    with pytest.raises(ReviewDecisionError, match="REVIEW_SOURCE_CHANGED"):
        validate_review_draft_publication(draft, source=replace(source, fact=foreign),
                                          policy=policy, current_version=0)


def test_changed_received_source_invalidates_generation(received):
    normalize, dto = received
    source, policy, draft = draft_for(normalize(dto, 1, 11, "test-first", NOW))
    changed = normalize(dict(dto, text="[TEST] edited review"), 1, 11, "test-next", NOW)
    with pytest.raises(ReviewDecisionError, match="REVIEW_SOURCE_CHANGED"):
        validate_review_draft_publication(draft, source=replace(source, fact=changed),
                                          policy=policy, current_version=0)


def test_late_generation_cannot_publish_after_current_version_advanced(received):
    normalize, dto = received
    source, policy, draft = draft_for(normalize(dto, 1, 11, "test-first", NOW))
    with pytest.raises(ReviewDecisionError, match="REVIEW_STALE_DRAFT"):
        validate_review_draft_publication(draft, source=source, policy=policy, current_version=1)


def test_reobserved_unchanged_dto_is_same_content_not_new_source_evidence(received):
    normalize, dto = received
    first = normalize(dto, 1, 11, "test-first", NOW)
    later = normalize(dto, 1, 11, "test-later", NOW.replace(hour=13))
    assert first.content_checksum == later.content_checksum
    assert first.observed_at != later.observed_at
    # Whether a current pointer/watermark advances is a future repository test.
