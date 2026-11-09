"""Freeze cross-owner byte parity against existing production serializers only."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.reviews.canonical_contract import ExternalReviewIdentity, ReviewMarketplace
from app.reviews.decision_contract import ReviewDraftRevision, ReviewPolicyVersion
from app.reviews.storage_payloads import (
    StoragePayloadError,
    encode_review_generation,
    encode_review_policy,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/reviews/storage-v1-golden.json").read_text()
)


@pytest.mark.parametrize(
    "name,encode",
    [
        ("policy", encode_review_policy),
        ("fakeGeneration", encode_review_generation),
        ("manualGeneration", encode_review_generation),
    ],
)
def test_exact_serialization_matches_frozen_bytes_and_large_version(name, encode):
    case = FIXTURE[name]
    typed_input = json.loads(case["canonicalUtf8Json"])
    actual = encode(dict(reversed(list(typed_input.items()))))
    assert actual.canonical_bytes == case["canonicalUtf8Json"].encode("utf-8")
    assert actual.checksum == case["sha256"]
    assert (
        typed_input.get("version", typed_input.get("policyVersion"))
        == 9223372036854775808
    )


def test_unicode_control_nul_and_big_revision_keep_exact_decision_binding():
    case = FIXTURE["decisionBinding"]
    raw = json.loads(case["canonicalUtf8Json"])
    owner, policy, draft, source = (
        raw[key] for key in ("owner", "policy", "draft", "source")
    )
    text_bytes = case["text"].encode("utf-8")
    assert text_bytes.hex() == case["textUtf8Hex"]
    assert hashlib.sha256(text_bytes).hexdigest() == case["textSha256"]
    assert b"\0" in text_bytes and b"\n\t" in text_bytes
    identity = ExternalReviewIdentity(*owner)
    version = ReviewPolicyVersion(
        *owner[:2], ReviewMarketplace(owner[2]), UUID(policy[0]), *policy[1:]
    )
    value = ReviewDraftRevision(
        identity,
        UUID(draft[0]),
        draft[1],
        UUID(source[0]),
        source[1],
        version,
        case["text"],
        case["textSha256"],
        datetime.fromisoformat(case["createdAt"]),
    )
    assert value.binding_checksum == case["sha256"]
    assert (
        hashlib.sha256(case["canonicalUtf8Json"].encode("utf-8")).hexdigest()
        == case["sha256"]
    )
    assert value.revision == 9223372036854775808
    assert case["text"] not in repr(value)


@pytest.mark.parametrize("case", FIXTURE["negativeGenerationCases"])
def test_omission_null_and_timestamp_are_not_interchangeable(case):
    payload = json.loads(FIXTURE[case["base"]]["canonicalUtf8Json"])
    payload.update(case.get("changes", {}))
    if "omit" in case:
        del payload[case["omit"]]
    with pytest.raises(StoragePayloadError, match="REVIEW_STORAGE_PAYLOAD_INVALID"):
        encode_review_generation(payload)
