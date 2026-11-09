"""Raw ratings API → existing canonical fact, without publishing authority."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.reviews.canonical_avito_decode import (
    AvitoReviewDecodeError,
    decode_avito_review,
)
from app.reviews.canonical_contract import normalize_avito_review
from app.reviews.lossless_storage import decode_scalar_pair, encode_scalar_pair

OBSERVED = datetime(2026, 9, 10, tzinfo=UTC)


def raw():
    # Native field locations and timestamp from existing test_avito_reviews.py.
    return {
        "id": 92312343,
        "createdAt": 1785230400,
        "score": 2,
        "stage": "fell_through",
        "text": " [TEST] exact\n\x00🙂e\u0301 ",
        "canAnswer": True,
        "usedInScore": True,
        "answer": None,
        "item": {"id": 9007199254740993, "title": "[TEST] private"},
        "sender": {"name": "[TEST] private"},
    }


def decode(payload, *, org=1, account=2, run="run-1", observed=OBSERVED):
    return decode_avito_review(
        payload,
        organization_id=org,
        marketplace_account_id=account,
        source_run_id=run,
        observed_at=observed,
    )


def test_native_field_bridge_preserves_existing_normalization_and_checksum():
    payload = raw()
    before = deepcopy(payload)
    fact = decode(payload)
    # Independently constructed existing accepted normalized shape, not decoder helpers.
    expected = normalize_avito_review(
        {
            "reviewId": "92312343",
            "createdAt": "2026-07-28T09:20:00Z",
            "score": 2,
            "stage": "fell_through",
            "text": " [TEST] exact\n\x00🙂e\u0301 ",
            "canAnswer": True,
            "answer": None,
            "itemId": "9007199254740993",
        },
        1,
        2,
        "run-1",
        OBSERVED,
    )
    assert fact == expected
    assert fact.source_updated_at is None
    assert payload == before
    assert "[TEST] exact" not in repr(fact)
    assert (
        decode_scalar_pair(encode_scalar_pair(fact.text, "text"), "text")
        == payload["text"]
    )


@pytest.mark.parametrize("value", [None, "", "  ", "x\r\ny", "\x00", "é", "e\u0301"])
def test_text_is_exact_null_empty_whitespace_and_unicode_not_sanitized(value):
    payload = raw()
    payload["text"] = value
    assert decode(payload).text == value


def test_missing_optional_fields_are_null_not_legacy_defaults():
    fact = decode({"id": "opaque-review-1", "createdAt": 1785230400, "answer": None})
    assert (
        fact.text,
        fact.rating,
        fact.can_answer,
        fact.external_product_id,
        fact.source_status,
        fact.source_updated_at,
    ) == (None,) * 6
    assert fact.answered is False


def test_answer_presence_is_supported_but_answer_body_is_not_a_canonical_fact():
    payload = raw()
    payload["answer"] = {
        "id": "000-answer",
        "text": "[TEST] source-answer",
        "status": "moderation",
    }
    first = decode(payload)
    payload["answer"]["text"] = "[TEST] changed-source-answer"
    second = decode(payload)
    assert first.answered is True and not hasattr(first, "answer")
    assert first.content_checksum == second.content_checksum
    assert "source-answer" not in repr(first)


def test_semantic_text_changes_hash_but_new_observation_does_not():
    payload = raw()
    first = decode(payload)
    replay = decode(payload, run="run-2", observed=OBSERVED + timedelta(hours=1))
    assert replay.content_checksum == first.content_checksum
    payload["text"] += "changed"
    assert decode(payload).content_checksum != first.content_checksum


def test_scope_is_part_of_existing_content_hash():
    assert (
        len(
            {
                decode(raw(), org=o, account=a).content_checksum
                for o, a in [(1, 2), (1, 3), (2, 2)]
            }
        )
        == 3
    )


@pytest.mark.parametrize("value", ["000123", "opaque-review", "ИД\x00🙂"])
def test_exact_source_identity_is_not_an_internal_account_number(value):
    payload = raw()
    payload["id"] = value
    payload["item"]["id"] = value
    fact = decode(payload)
    assert fact.identity.external_review_id == fact.external_product_id == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("id", 12.0),
        ("id", "2E4"),
        ("score", 0),
        ("score", 6),
        ("score", 2.0),
        ("score", True),
        ("canAnswer", "false"),
        ("text", 123),
        ("text", "\ud800"),
        ("createdAt", "1785230400"),
        ("createdAt", 1785230400.0),
        ("createdAt", True),
        ("createdAt", None),
        ("createdAt", -1),
        ("item", []),
        ("answer", True),
        ("answer", {}),
    ],
)
def test_invalid_native_values_cannot_turn_into_accepted_facts(field, value):
    payload = raw()
    payload[field] = value
    with pytest.raises(AvitoReviewDecodeError) as error:
        decode(payload)
    assert str(error.value) == "AVITO_REVIEW_DECODE_INVALID"
    assert error.value.__context__ is None


def test_missing_answer_evidence_does_not_mean_unanswered():
    payload = raw()
    del payload["answer"]
    with pytest.raises(AvitoReviewDecodeError):
        decode(payload)


@pytest.mark.parametrize(
    "alias", ["accountId", "userId", "sellerId", "updatedAt", "sourceUpdatedAt"]
)
def test_unproven_owner_or_update_alias_is_not_silently_assigned(alias):
    payload = raw()
    payload[alias] = None
    with pytest.raises(AvitoReviewDecodeError):
        decode(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"org": True},
        {"account": 0},
        {"run": ""},
        {"observed": OBSERVED.replace(tzinfo=None)},
    ],
)
def test_caller_metadata_must_satisfy_existing_contract(changes):
    with pytest.raises(AvitoReviewDecodeError):
        decode(raw(), **changes)
