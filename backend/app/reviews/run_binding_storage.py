"""Pure0068 binding columns; legacy sentinel is not authorization or admission."""

from collections.abc import Mapping

from app.reviews.historical_binding import (
    ReviewBindingDescriptor,
    ReviewBindingDescriptorError,
    decode_review_binding_descriptor,
    encode_review_binding_descriptor,
)

_FIELDS = (
    "account_binding_schema_version", "account_binding_external_account_id",
    "account_binding_credential_ref", "account_binding_payload", "account_binding_checksum",
)


def encode_review_run_binding(descriptor: ReviewBindingDescriptor) -> dict[str, object]:
    encoded = encode_review_binding_descriptor(descriptor)
    return dict(zip(_FIELDS, (
        descriptor.schema_version, descriptor.external_account_id,
        descriptor.credential_ref, encoded.canonical_bytes, encoded.checksum,
    ), strict=True))


def decode_review_run_binding(row: Mapping[str, object]) -> ReviewBindingDescriptor | None:
    if not isinstance(row, Mapping) or any(key not in row for key in _FIELDS):
        raise ReviewBindingDescriptorError()
    if all(row[key] is None for key in _FIELDS):
        return None
    try:
        normalized = ReviewBindingDescriptor(
            organization_id=row["organization_id"],
            marketplace_account_id=row["marketplace_account_id"],
            marketplace=row["marketplace"],
            external_account_id=row["account_binding_external_account_id"],
            credential_ref=row["account_binding_credential_ref"],
            schema_version=row["account_binding_schema_version"],
        )
        decoded = decode_review_binding_descriptor(
            row["account_binding_payload"], checksum=row["account_binding_checksum"],
        )
        if normalized != decoded:
            raise ReviewBindingDescriptorError()
        return decoded
    except (KeyError, TypeError, ValueError):
        raise ReviewBindingDescriptorError() from None
