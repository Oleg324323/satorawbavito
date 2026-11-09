"""First human draft contract; offline only, no persistence admission claim."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.reviews.local_command_payloads import encode_review_local_request
from app.reviews.local_preparation import prepare_local_review_draft
from app.reviews.storage_payloads import StoragePayloadError, encode_review_generation
from tests.test_review_local_command_payload import value


def manual_command():
    command = value("draft-publish")
    command["input"]["generation"]["mode"] = "manual"
    command["input"]["text"] = "  Спасибо!\0 café é 😀\n"
    return command


def authoring_context():
    command = manual_command()
    data, generation = command["input"], command["input"]["generation"]
    return {"schemaVersion": "review-local-context-v1",
        **{key: command[key] for key in ("organizationId", "marketplaceAccountId", "marketplace", "actorMembershipId")},
        "policy": {"policyId": generation["policyId"], "version": generation["policyVersion"],
            "templateVersion": generation["templateVersion"], "modelVersion": generation["modelVersion"]},
        "policyHead": {"headId": data["expectedPolicyHeadId"], "version": data["expectedPolicyHeadVersion"],
            "policyChecksum": generation["policyChecksum"]},
        "review": {key: data[key] for key in ("reviewId", "externalReviewId")}
            | {key: generation[key] for key in ("sourceObservationId", "sourceChecksum")},
        "workflowHead": None, "draft": None}


def prepare(context, text="Спасибо!"):
    return prepare_local_review_draft(context, local_command_id=uuid4(), draft_id=uuid4(),
        generation_id=uuid4(), mode="manual", text=text, prepared_at=datetime(2026, 9, 9, tzinfo=UTC))


def test_first_manual_text_is_lossless_and_deterministic():
    command = manual_command()
    encoded = encode_review_local_request(command)
    assert json.loads(encoded.canonical_bytes) == command
    assert encode_review_local_request(dict(reversed(list(command.items())))) == encoded
    context = authoring_context()
    result = json.loads(prepare(context, command["input"]["text"]).canonical_bytes)
    assert result["input"]["text"] == "  Спасибо!\0 café é 😀\n"
    assert result["input"]["expectedHeadVersion"] == result["input"]["expectedDraftRevision"] == 0
    assert result["input"]["generation"]["mode"] == "manual"
    assert "previousDraftId" not in result["input"]["generation"]
    for key in ("sourceObservationId", "sourceChecksum", "policyId", "policyVersion",
                "policyChecksum", "templateVersion", "modelVersion", "actorMembershipId"):
        assert result["input"]["generation"][key] == command["input"]["generation"][key]


@pytest.mark.parametrize("head,revision", [(1, 1), (4, 2), (0, 1), (1, 0)])
def test_manual_cannot_represent_a_subsequent_draft(head, revision):
    command = manual_command()
    command["input"].update(expectedHeadVersion=head, expectedDraftRevision=revision)
    with pytest.raises(StoragePayloadError):
        encode_review_local_request(command)


@pytest.mark.parametrize("field", ["policy", "policyHead", "review"])
def test_manual_requires_existing_policy_and_source(field):
    context = authoring_context()
    context[field] = None
    with pytest.raises(StoragePayloadError):
        prepare(context)


@pytest.mark.parametrize("field", ["workflowHead", "draft"])
def test_manual_preparation_rejects_any_existing_head_or_draft(field):
    context = authoring_context()
    context[field] = {"version": 1, "revision": 1, "draftId": str(UUID(int=1))}
    with pytest.raises(StoragePayloadError):
        prepare(context)


@pytest.mark.parametrize("text", [None, "", " \t\n", 1, "bad\ud800"])
def test_manual_rejects_invalid_human_text(text):
    with pytest.raises(StoragePayloadError):
        prepare(authoring_context(), text)


def test_manual_generation_cannot_claim_a_predecessor_or_ai_provider():
    generation = manual_command()["input"]["generation"]
    encode_review_generation(generation)
    for key, item in (("previousDraftId", str(uuid4())), ("provider", "openai"), ("sent", True)):
        with pytest.raises(StoragePayloadError):
            encode_review_generation({**generation, key: item})
