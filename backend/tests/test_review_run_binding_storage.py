"""Pure0068 representation; no database/authorization/provider acceptance."""

import pytest

from app.reviews.historical_binding import (
    ReviewBindingDescriptor,
    ReviewBindingDescriptorError,
)
from app.reviews.run_binding_storage import (
    decode_review_run_binding,
    encode_review_run_binding,
)

FIELDS = (
    "account_binding_schema_version", "account_binding_external_account_id",
    "account_binding_credential_ref", "account_binding_payload", "account_binding_checksum",
)
OWNER = {"organization_id": 1, "marketplace_account_id": 11, "marketplace": "wb"}
RAW = b'{"credentialRef":null,"externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}'
HASH = "71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60"


def bound():
    return {
        **OWNER, "account_binding_schema_version": 1,
        "account_binding_external_account_id": "seller-A",
        "account_binding_credential_ref": None,
        "account_binding_payload": RAW, "account_binding_checksum": HASH,
    }


def test_literal_vector_and_exact_insert_allowlist():
    descriptor = ReviewBindingDescriptor(1, 11, "wb", "seller-A", None)
    encoded = encode_review_run_binding(descriptor)
    assert encoded == {key: bound()[key] for key in FIELDS}
    assert decode_review_run_binding(bound()) == descriptor
    assert decode_review_run_binding({**bound(), "status": "running"}) == descriptor


def test_all_null_is_explicit_legacy_without_inferred_binding():
    assert decode_review_run_binding({**OWNER, **dict.fromkeys(FIELDS)}) is None


@pytest.mark.parametrize("key", FIELDS)
def test_missing_is_not_null_even_for_legacy(key):
    row = {**OWNER, **dict.fromkeys(FIELDS)}
    del row[key]
    with pytest.raises(ReviewBindingDescriptorError, match="^REVIEW_BINDING_DESCRIPTOR_INVALID$"):
        decode_review_run_binding(row)


@pytest.mark.parametrize("key", [key for key in FIELDS if key != "account_binding_credential_ref"])
def test_partial_null_bound_row_rejected(key):
    with pytest.raises(ReviewBindingDescriptorError):
        decode_review_run_binding({**bound(), key: None})


@pytest.mark.parametrize("key,value", [
    ("organization_id", 2), ("marketplace_account_id", 12), ("marketplace", "avito"),
    ("organization_id", True), ("marketplace_account_id", False),
    ("account_binding_schema_version", True), ("account_binding_schema_version", 2),
    ("account_binding_external_account_id", "seller-B"),
    ("account_binding_external_account_id", b"seller-A"),
    ("account_binding_external_account_id", ""),
    ("account_binding_external_account_id", "\ud800"),
    ("account_binding_credential_ref", ""), ("account_binding_credential_ref", " "),
    ("account_binding_payload", RAW + b" "),
    ("account_binding_payload", RAW.decode("ascii")),
    ("account_binding_checksum", HASH.upper()), ("account_binding_checksum", "0" * 64),
    ("account_binding_checksum", 1),
])
def test_normalized_and_encoded_columns_must_agree_exactly(key, value):
    with pytest.raises(ReviewBindingDescriptorError, match="^REVIEW_BINDING_DESCRIPTOR_INVALID$"):
        decode_review_run_binding({**bound(), key: value})


@pytest.mark.parametrize("key", OWNER)
def test_bound_owner_fields_are_required(key):
    row = bound()
    del row[key]
    with pytest.raises(ReviewBindingDescriptorError):
        decode_review_run_binding(row)


@pytest.mark.parametrize("reference", [None, "", " ", "ссылка", "e\u0301", "😀"])
def test_reference_and_unicode_are_lossless(reference):
    descriptor = ReviewBindingDescriptor(1, 11, "wb", "кабинет😀", reference)
    assert decode_review_run_binding({**OWNER, **encode_review_run_binding(descriptor)}) == descriptor


@pytest.mark.parametrize("value", [None, [], "sensitive-synthetic-value", 1])
def test_wrong_container_rejected_without_echo(value):
    with pytest.raises(ReviewBindingDescriptorError) as error:
        decode_review_run_binding(value)
    assert str(error.value) == "REVIEW_BINDING_DESCRIPTOR_INVALID"


@pytest.mark.parametrize("value", [None, {}, "sensitive-synthetic-value"])
def test_encoder_uses_existing_strict_descriptor_validation(value):
    with pytest.raises(ReviewBindingDescriptorError):
        encode_review_run_binding(value)
