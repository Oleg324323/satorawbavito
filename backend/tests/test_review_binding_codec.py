import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.reviews.historical_binding import (
    ReviewBindingDescriptor,
    ReviewBindingDescriptorError,
    decode_review_binding_descriptor,
    encode_review_binding_descriptor,
)


def descriptor(**changes):
    values = {
        "organization_id": 1,
        "marketplace_account_id": 11,
        "marketplace": "wb",
        "external_account_id": "seller-A",
        "credential_ref": None,
    }
    return ReviewBindingDescriptor(**(values | changes))


VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/review_binding_descriptor_v1.json").read_text()
)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_independent_literal_vectors_for_sql_parity(vector):
    payload = vector["descriptor"]
    value = ReviewBindingDescriptor(
        organization_id=payload["organizationId"],
        marketplace_account_id=payload["marketplaceAccountId"],
        marketplace=payload["marketplace"],
        external_account_id=payload["externalAccountId"],
        credential_ref=payload["credentialRef"],
        schema_version=payload["schemaVersion"],
    )
    raw = vector["canonicalAscii"].encode("ascii")
    encoded = encode_review_binding_descriptor(value)
    assert encoded.canonical_bytes == raw
    assert encoded.checksum == vector["sha256"]
    assert decode_review_binding_descriptor(raw, checksum=vector["sha256"]) == value


@pytest.mark.parametrize(
    "seller,ref,raw,digest",
    [
        (
            "seller-A",
            None,
            b'{"credentialRef":null,"externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}',
            "71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60",
        ),
        (
            "seller-A",
            "",
            b'{"credentialRef":"","externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}',
            "c367232907c44843dbd7ac1ab6d9c3da34cd5c72c68cfcedaecac9ce576432e6",
        ),
        (
            "seller-B",
            None,
            b'{"credentialRef":null,"externalAccountId":"seller-B","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}',
            "645db76e3ce7686e2233bdcd1a5f26a4751b3d8b01a9fc22ede0c255f200d92a",
        ),
    ],
)
def test_literal_request_vectors(seller, ref, raw, digest):
    value = descriptor(external_account_id=seller, credential_ref=ref)
    encoded = encode_review_binding_descriptor(value)
    assert encoded.canonical_bytes == raw
    assert encoded.checksum == digest
    assert decode_review_binding_descriptor(raw, checksum=digest) == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", True),
        ("organization_id", 0),
        ("organization_id", 2147483648),
        ("marketplace_account_id", False),
        ("marketplace_account_id", 1.0),
        ("marketplace_account_id", "11"),
        ("schema_version", True),
        ("schema_version", 2),
        ("marketplace", "WB"),
        ("marketplace", "wb "),
        ("external_account_id", ""),
        ("external_account_id", None),
        ("external_account_id", "\ud800"),
        ("external_account_id", "\ud83d\ude80"),
        ("credential_ref", "\udfff"),
        ("credential_ref", 42),
    ],
)
def test_rejects_invalid_descriptor_fields(field, value):
    with pytest.raises(
        ReviewBindingDescriptorError, match="^REVIEW_BINDING_DESCRIPTOR_INVALID$"
    ):
        descriptor(**{field: value})


def test_exact_unicode_control_and_null_empty_semantics_without_trim():
    for external in [" ", "\x00", " Магазин🚀e\u0301 ", "é", "e\u0301", "\ufeffseller"]:
        encodings = []
        for ref in [None, "", " ", "ref/ключ\x00\n"]:
            value = descriptor(external_account_id=external, credential_ref=ref)
            encoded = encode_review_binding_descriptor(value)
            assert encoded.canonical_bytes.isascii()
            assert (
                decode_review_binding_descriptor(
                    encoded.canonical_bytes, checksum=encoded.checksum
                )
                == value
            )
            encodings.append(encoded.canonical_bytes)
        assert len(set(encodings)) == 4
    assert (
        encode_review_binding_descriptor(descriptor(external_account_id="é")).checksum
        != encode_review_binding_descriptor(
            descriptor(external_account_id="e\u0301")
        ).checksum
    )


def test_descriptor_is_immutable_and_repr_does_not_expose_metadata():
    value = descriptor(
        external_account_id="METADATA_SENTINEL", credential_ref="REF_SENTINEL"
    )
    with pytest.raises(FrozenInstanceError):
        value.external_account_id = "changed"
    encoded = encode_review_binding_descriptor(value)
    assert "SENTINEL" not in repr(value)
    assert "SENTINEL" not in repr(encoded)


@pytest.mark.parametrize(
    "transform",
    [
        lambda raw: raw + b"\n",
        lambda raw: b"\xef\xbb\xbf" + raw,
        lambda raw: raw.replace(b'"schemaVersion":1', b'"schemaVersion":1.0'),
        lambda raw: raw.replace(b'"schemaVersion":1', b'"schemaVersion":true'),
        lambda raw: raw.replace(b'"organizationId":1', b'"organizationId":1e0'),
        lambda raw: raw.replace(b'"seller-A"', b'"\\u0073eller-A"'),
        lambda raw: raw.replace(b'"credentialRef":null,', b""),
        lambda raw: raw.replace(b"{", b'{"extra":null,', 1),
        lambda raw: raw.replace(b"{", b'{"schemaVersion":1,', 1),
        lambda raw: raw.replace(b'"seller-A"', b'"\\ud800"'),
        lambda raw: raw.replace(b'"organizationId":1', b'"organizationId":NaN'),
        lambda raw: raw.replace(b'"organizationId":1', b'"organizationId":Infinity'),
        lambda raw: raw.replace(b'"seller-A"', '"магазин"'.encode()),
        lambda raw: json.dumps(
            dict(reversed(list(json.loads(raw).items()))), separators=(",", ":")
        ).encode(),
        lambda raw: json.dumps(json.loads(raw), sort_keys=True).encode(),
        lambda raw: b"[" + raw + b"]",
    ],
)
def test_rejects_alternate_or_invalid_bytes_even_with_matching_checksum(transform):
    raw = transform(encode_review_binding_descriptor(descriptor()).canonical_bytes)
    with pytest.raises(
        ReviewBindingDescriptorError, match="^REVIEW_BINDING_DESCRIPTOR_INVALID$"
    ):
        decode_review_binding_descriptor(raw, checksum=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("checksum", [None, "", "0" * 64, "A" * 64, b"a" * 64, 123])
def test_rejects_missing_invalid_or_wrong_checksum(checksum):
    raw = encode_review_binding_descriptor(descriptor()).canonical_bytes
    with pytest.raises(ReviewBindingDescriptorError):
        decode_review_binding_descriptor(raw, checksum=checksum)


def test_each_binding_field_changes_the_exact_identity():
    value = descriptor()
    baseline = encode_review_binding_descriptor(value).canonical_bytes
    for changes in [
        {"organization_id": 2},
        {"marketplace_account_id": 12},
        {"marketplace": "avito"},
        {"external_account_id": "seller-B"},
        {"credential_ref": ""},
    ]:
        assert (
            encode_review_binding_descriptor(replace(value, **changes)).canonical_bytes
            != baseline
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        b"[" * 5000 + b"]" * 5000,
        b'{"externalAccountId":"METADATA_SENTINEL"',
    ],
)
def test_malformed_or_deep_json_has_only_safe_error(raw):
    with pytest.raises(ReviewBindingDescriptorError) as error:
        decode_review_binding_descriptor(raw, checksum=hashlib.sha256(raw).hexdigest())
    assert error.value.args == ("REVIEW_BINDING_DESCRIPTOR_INVALID",)
    assert "SENTINEL" not in repr(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("raw", [None, "{}", bytearray(b"{}"), memoryview(b"{}"), 1])
def test_decoder_requires_exact_bytes(raw):
    with pytest.raises(ReviewBindingDescriptorError):
        decode_review_binding_descriptor(raw, checksum="0" * 64)


def test_encoder_revalidates_typed_input_and_rejects_dictionary_shortcuts():
    with pytest.raises(ReviewBindingDescriptorError):
        encode_review_binding_descriptor({"externalAccountId": "METADATA_SENTINEL"})
    value = descriptor()
    object.__setattr__(value, "organization_id", True)
    with pytest.raises(ReviewBindingDescriptorError):
        encode_review_binding_descriptor(value)


def test_alternative_unicode_escape_case_and_control_spelling_are_noncanonical():
    for value, old, new in [("М", b"\\u041c", b"\\u041C"), ("\n", b"\\n", b"\\u000a")]:
        raw = encode_review_binding_descriptor(
            descriptor(external_account_id=value)
        ).canonical_bytes.replace(old, new)
        with pytest.raises(ReviewBindingDescriptorError):
            decode_review_binding_descriptor(
                raw, checksum=hashlib.sha256(raw).hexdigest()
            )
