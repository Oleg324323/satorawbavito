"""Read provenance fingerprints, never substitutes for live authorization."""

import hashlib
import hmac
import json
import re

from app.modules.orders import OrderContractValidationError
from app.platform.integrations.publication_guard import ExpectedAccountBinding

ACCOUNT_BINDING_SCHEMA_VERSION = 1


def serialize_account_bindings(organization_id, accounts) -> bytes:
    if type(organization_id) is not int or not 0 < organization_id < 2**31:
        raise OrderContractValidationError("Invalid account binding organization")
    if (
        type(accounts) is not tuple
        or not accounts
        or any(type(account) is not ExpectedAccountBinding for account in accounts)
    ):
        raise OrderContractValidationError("Exact account bindings required")
    if len({account.marketplace_account_id for account in accounts}) != len(accounts):
        raise OrderContractValidationError("Duplicate account binding")
    value = [
        organization_id,
        sorted(
            [
                [
                    account.marketplace_account_id,
                    account.provider,
                    account.external_account_id,
                    account.credential_ref,
                ]
                for account in accounts
            ],
            key=lambda account: account[0],
        ),
    ]
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def deserialize_account_bindings(payload: bytes):
    try:
        if type(payload) is not bytes:
            raise ValueError
        value = json.loads(payload)
        if type(value) is not list or len(value) != 2 or type(value[1]) is not list:
            raise ValueError
        if any(type(account) is not list or len(account) != 4 for account in value[1]):
            raise ValueError
        accounts = tuple(ExpectedAccountBinding(*account) for account in value[1])
        if serialize_account_bindings(value[0], accounts) != payload:
            raise ValueError
        return value[0], accounts
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise OrderContractValidationError("Invalid account binding payload") from None


def account_binding_checksum(organization_id, accounts):
    return hashlib.sha256(
        serialize_account_bindings(organization_id, accounts)
    ).hexdigest()


def validate_run_binding(
    organization_id,
    account,
    *,
    schema_version,
    external_account_id,
    credential_ref,
    payload,
    checksum,
) -> None:
    """Check immutable 0067 fields against an independently guarded live binding.

    Legacy unbound runs cannot be upgraded using current metadata or audit data.
    This validates provenance only, not authorization or provider completeness.
    """
    try:
        expected = serialize_account_bindings(organization_id, (account,))
        if (
            type(schema_version) is not int
            or schema_version != ACCOUNT_BINDING_SCHEMA_VERSION
            or type(payload) is not bytes
            or payload != expected
            or type(external_account_id) is not str
            or external_account_id != account.external_account_id
            or (credential_ref is not None and type(credential_ref) is not str)
            or credential_ref != account.credential_ref
            or type(checksum) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not hmac.compare_digest(checksum, hashlib.sha256(expected).hexdigest())
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise OrderContractValidationError("Invalid source run binding") from None


def bound_high_water_mark(content_checksum, organization_id, accounts):
    if type(content_checksum) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", content_checksum
    ):
        raise OrderContractValidationError("Invalid snapshot content checksum")
    return (
        "orders-view-v3:"
        + content_checksum
        + ":"
        + account_binding_checksum(organization_id, accounts)
    )


def validate_snapshot_binding(mark, organization_id, accounts):
    if type(mark) is not str or not re.fullmatch(
        r"orders-view-v3:[0-9a-f]{64}:[0-9a-f]{64}", mark
    ):
        raise OrderContractValidationError("Snapshot binding missing")
    if not hmac.compare_digest(
        mark.rsplit(":", 1)[1], account_binding_checksum(organization_id, accounts)
    ):
        raise OrderContractValidationError("Snapshot binding changed")
