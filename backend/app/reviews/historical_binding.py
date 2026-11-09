"""Pure Review binding representation; not authorization or SQL admission.

Control characters, including NUL, are valid pure scalar input. Existing SQL
account VARCHAR cannot represent NUL: physical admission is a separate boundary.
Never expose this descriptor or its credential reference through public APIs.
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Literal


class ReviewBindingDescriptorError(ValueError):
    def __init__(self):
        super().__init__("REVIEW_BINDING_DESCRIPTOR_INVALID")


def _require(condition):
    if not condition:
        raise ReviewBindingDescriptorError()


def _scalar(value, *, nonempty=False):
    _require(type(value) is str and (not nonempty or value != ""))
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ReviewBindingDescriptorError() from None


@dataclass(frozen=True, slots=True, repr=False)
class ReviewBindingDescriptor:
    organization_id: int
    marketplace_account_id: int
    marketplace: Literal["wb", "avito"]
    external_account_id: str
    credential_ref: str | None
    schema_version: int = 1

    def __post_init__(self):
        for value in (self.organization_id, self.marketplace_account_id):
            _require(type(value) is int and 0 < value <= 2147483647)
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.marketplace) is str and self.marketplace in {"wb", "avito"})
        _scalar(self.external_account_id, nonempty=True)
        if self.credential_ref is not None:
            _scalar(self.credential_ref)


@dataclass(frozen=True, slots=True, repr=False)
class EncodedReviewBinding:
    canonical_bytes: bytes
    checksum: str


def encode_review_binding_descriptor(
    descriptor: ReviewBindingDescriptor,
) -> EncodedReviewBinding:
    _require(type(descriptor) is ReviewBindingDescriptor)
    descriptor.__post_init__()
    payload = {
        "schemaVersion": descriptor.schema_version,
        "organizationId": descriptor.organization_id,
        "marketplaceAccountId": descriptor.marketplace_account_id,
        "marketplace": descriptor.marketplace,
        "externalAccountId": descriptor.external_account_id,
        "credentialRef": descriptor.credential_ref,
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return EncodedReviewBinding(raw, hashlib.sha256(raw).hexdigest())


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _reject_constant(_value):
    raise ReviewBindingDescriptorError()


def decode_review_binding_descriptor(
    raw: object,
    *,
    checksum: object,
) -> ReviewBindingDescriptor:
    _require(type(raw) is bytes)
    _require(type(checksum) is str and re.fullmatch(r"[0-9a-f]{64}", checksum))
    _require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(), checksum))
    try:
        payload = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _require(
            type(payload) is dict
            and payload.keys()
            == {
                "schemaVersion",
                "organizationId",
                "marketplaceAccountId",
                "marketplace",
                "externalAccountId",
                "credentialRef",
            }
        )
        descriptor = ReviewBindingDescriptor(
            organization_id=payload["organizationId"],
            marketplace_account_id=payload["marketplaceAccountId"],
            marketplace=payload["marketplace"],
            external_account_id=payload["externalAccountId"],
            credential_ref=payload["credentialRef"],
            schema_version=payload["schemaVersion"],
        )
        _require(encode_review_binding_descriptor(descriptor).canonical_bytes == raw)
        return descriptor
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReviewBindingDescriptorError() from None
