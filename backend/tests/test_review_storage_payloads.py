import hashlib
from dataclasses import FrozenInstanceError

import pytest

from app.reviews.storage_payloads import (
    StoragePayloadError,
    encode_review_generation,
    encode_review_policy,
    encode_review_send,
)

ID = "00000000-0000-4000-8000-000000000001"


def policy():
    return {"schemaVersion": "review-policy-v1", "organizationId": 1, "marketplaceAccountId": 11,
                "marketplace": "wb", "policyId": ID, "version": 1, "approvalMode": "manual",
                "templateVersion": "test-template", "modelVersion": "fake-model"}


def generation():
    return {"schemaVersion": "review-generation-v1", "generationId": ID,
                "sourceObservationId": ID, "sourceChecksum": "a" * 64, "policyId": ID, "policyVersion": 1,
                "policyChecksum": "b" * 64, "templateVersion": "test-template", "modelVersion": "fake-model",
                "mode": "fake", "actorMembershipId": 1, "startedAt": "2026-09-09T00:00:00.000000Z",
                "completedAt": "2026-09-09T00:00:01.000000Z"}


def send():
    return {"schemaVersion": "review-send-request-v1", "operationKind": "review.answer.create.v1",
                "organizationId": 1, "marketplaceAccountId": 11, "marketplace": "avito",
                "externalReviewId": "test-review-001", "draftId": ID, "draftRevision": 1, "decisionId": ID,
                "bindingChecksum": "c" * 64, "textChecksum": "d" * 64}


def test_policy_bytes_match_hand_written_contract_and_digest():
    expected = (b'{"approvalMode":"manual","marketplace":"wb","marketplaceAccountId":11,'
                b'"modelVersion":"fake-model","organizationId":1,'
                b'"policyId":"00000000-0000-4000-8000-000000000001",'
                b'"schemaVersion":"review-policy-v1","templateVersion":"test-template","version":1}')
    result = encode_review_policy(policy())
    assert result.canonical_bytes == expected
    assert result.checksum == hashlib.sha256(expected).hexdigest()
    with pytest.raises(FrozenInstanceError):
        result.checksum = "x"


@pytest.mark.parametrize("builder,encode", [(policy, encode_review_policy),
                                           (generation, encode_review_generation), (send, encode_review_send)])
def test_dict_order_is_irrelevant_and_input_is_not_mutated(builder, encode):
    payload = builder()
    before = dict(payload)
    first = encode(payload)
    assert first == encode(dict(reversed(list(payload.items()))))
    assert payload == before
    assert "00000000" not in repr(first)


def test_generation_bytes_preserve_complete_provenance_for_replay():
    expected = (
        '{"actorMembershipId":1,"completedAt":"2026-09-09T00:00:01.000000Z",'
        '"generationId":"00000000-0000-4000-8000-000000000001","mode":"fake",'
        '"modelVersion":"fake-model","policyChecksum":"' + 'b' * 64 + '",'
        '"policyId":"00000000-0000-4000-8000-000000000001","policyVersion":1,'
        '"schemaVersion":"review-generation-v1","sourceChecksum":"' + 'a' * 64 + '",'
        '"sourceObservationId":"00000000-0000-4000-8000-000000000001",'
        '"startedAt":"2026-09-09T00:00:00.000000Z","templateVersion":"test-template"}'
    ).encode("utf-8")
    actual = encode_review_generation(generation())
    assert actual.canonical_bytes == expected
    assert actual.checksum == hashlib.sha256(expected).hexdigest()


def test_send_bytes_preserve_complete_binding_for_replay():
    expected = (
        '{"bindingChecksum":"' + 'c' * 64 + '",'
        '"decisionId":"00000000-0000-4000-8000-000000000001",'
        '"draftId":"00000000-0000-4000-8000-000000000001","draftRevision":1,'
        '"externalReviewId":"test-review-001","marketplace":"avito",'
        '"marketplaceAccountId":11,"operationKind":"review.answer.create.v1",'
        '"organizationId":1,"schemaVersion":"review-send-request-v1",'
        '"textChecksum":"' + 'd' * 64 + '"}'
    ).encode("utf-8")
    actual = encode_review_send(send())
    assert actual.canonical_bytes == expected
    assert actual.checksum == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("change", [{"organizationId": True}, {"organizationId": 0},
    {"marketplaceAccountId": 1.5}, {"version": "1"}, {"marketplace": "WB"},
    {"policyId": "00000000-0000-0000-0000-000000000000"}, {"policyId": "private-value"},
    {"approvalMode": "automatic"}, {"templateVersion": ""}, {"modelVersion": "unsafe model"},
    {"schemaVersion": "review-policy-v2"}, {"extra": "synthetic-private"}])
def test_invalid_policy_cannot_be_encoded(change):
    with pytest.raises(StoragePayloadError) as error:
        encode_review_policy(dict(policy(), **change))
    assert str(error.value) == "REVIEW_STORAGE_PAYLOAD_INVALID"


def test_generation_requires_exact_time_format_and_manual_edit_parent():
    base = generation()
    fake = encode_review_generation(base)
    edit = encode_review_generation(dict(base, mode="manual_edit", previousDraftId=ID))
    assert edit != fake
    with pytest.raises(StoragePayloadError):
        encode_review_generation(dict(base, mode="manual_edit"))
    with pytest.raises(StoragePayloadError):
        encode_review_generation(dict(base, previousDraftId=ID))


@pytest.mark.parametrize("change", [{"startedAt": "2026-09-09T00:00:00Z"},
    {"startedAt": "٢٠٢٦-09-09T00:00:00.000000Z"},
    {"startedAt": "2026-09-09T00:00:00.000000+00:00"}, {"startedAt": "2026-09-09T00:00:02.000000Z"},
    {"completedAt": "2026-02-30T00:00:00.000000Z"}, {"mode": "real_llm"},
    {"actorMembershipId": False}, {"policyChecksum": "B" * 64}, {"sourceChecksum": "bad"},
    {"prompt": "synthetic-private"}])
def test_invalid_generation_cannot_be_encoded(change):
    with pytest.raises(StoragePayloadError):
        encode_review_generation(dict(generation(), **change))


@pytest.mark.parametrize("change", [{"organizationId": 2}, {"marketplaceAccountId": 12},
    {"marketplace": "wb"}, {"externalReviewId": "other-review"}, {"draftRevision": 2},
    {"decisionId": "00000000-0000-4000-8000-000000000002"}, {"textChecksum": "e" * 64},
    {"bindingChecksum": "f" * 64}])
def test_send_binding_changes_produce_different_replay_bytes(change):
    first = encode_review_send(send())
    changed = encode_review_send(dict(send(), **change))
    assert first.canonical_bytes != changed.canonical_bytes
    assert first.checksum != changed.checksum


@pytest.mark.parametrize("change", [{"operationKind": "review.answer.delete.v1"},
    {"externalReviewId": 123}, {"externalReviewId": ""}, {"externalReviewId": "  test-review-001 "},
    {"draftRevision": True}, {"textChecksum": None}, {"token": "synthetic-private"},
    {"approved": True}, {"text": "synthetic-answer"}])
def test_send_rejects_unbound_inputs_and_unsupported_actions(change):
    with pytest.raises(StoragePayloadError) as error:
        encode_review_send(dict(send(), **change))
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("payload", [None, [], {}, "raw-private"])
@pytest.mark.parametrize("encode", [encode_review_policy, encode_review_generation, encode_review_send])
def test_missing_or_non_object_payload_is_rejected(payload, encode):
    with pytest.raises(StoragePayloadError):
        encode(payload)
