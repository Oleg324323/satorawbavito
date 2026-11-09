"""Strict, nonsecret metadata for the credential-maintenance boundary.

Construction establishes only metadata shape, never operational authorization or
mapping proof. The trusted registrar/store must independently verify the proof,
direct database-session role, locked source/account identity, current validity,
revocation and operation permission in their own physical transaction. No source
payload, credential wrapper, proof verifier, SQL, configuration or I/O lives here.

Representations deliberately omit inventory fields. Explicit field access is for
trusted registrar/store code; these dataclasses are not HTTP or logging DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_INT4_MAX = 2_147_483_647
_OID_MAX = 4_294_967_295
_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_DAY = 86_400 * _MICROSECONDS_PER_SECOND


class MaintenanceContractError(ValueError):
    """A fixed diagnostic which never retains invalid metadata values."""

    def __init__(self) -> None:
        super().__init__("maintenance_contract_invalid")

    @property
    def code(self) -> str:
        return "maintenance_contract_invalid"

    def __str__(self) -> str:
        return "maintenance_contract_invalid"

    def __repr__(self) -> str:
        return "MaintenanceContractError('maintenance_contract_invalid')"


def _positive_integer(value: object, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise MaintenanceContractError()


def _non_nil_uuid(value: object) -> None:
    # Persisted maintenance identities are UUIDs, not a UUID-version policy.
    if type(value) is not UUID or value.int == 0:
        raise MaintenanceContractError()


def _text(value: object, *, minimum: int, maximum: int) -> bytes:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise MaintenanceContractError()
    if "\x00" in value:
        raise MaintenanceContractError()
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MaintenanceContractError() from None


def _owner(
    target_credential_id: object,
    organization_id: object,
    marketplace_account_id: object,
    provider: object,
    credential_kind: object,
) -> None:
    _non_nil_uuid(target_credential_id)
    _positive_integer(organization_id, _INT4_MAX)
    _positive_integer(marketplace_account_id, _INT4_MAX)
    if type(provider) is not str or type(credential_kind) is not str:
        raise MaintenanceContractError()
    if (provider, credential_kind) not in (
        ("wb", "wb_api"),
        ("avito", "avito_oauth_client"),
    ):
        raise MaintenanceContractError()


def _instant_microseconds(value: object) -> int:
    """Validate a finite native aware datetime and compare by its UTC instant.

    Native datetimes cannot represent infinity. Integer arithmetic preserves
    microseconds, fold/offset distinctions and boundary dates without float
    timestamps, local-time comparisons or overflowing a UTC datetime conversion.
    The supplied timezone and datetime are never normalized or replaced.
    """
    if type(value) is not datetime or value.tzinfo is None:
        raise MaintenanceContractError()
    try:
        offset = value.utcoffset()
    except Exception:  # noqa: BLE001 -- timezone errors must not echo metadata.
        raise MaintenanceContractError() from None
    if offset is None:
        raise MaintenanceContractError()
    local_microseconds = (
        value.toordinal() * _MICROSECONDS_PER_DAY
        + (value.hour * 3_600 + value.minute * 60 + value.second)
        * _MICROSECONDS_PER_SECOND
        + value.microsecond
    )
    offset_microseconds = (
        offset.days * _MICROSECONDS_PER_DAY
        + offset.seconds * _MICROSECONDS_PER_SECOND
        + offset.microseconds
    )
    return local_microseconds - offset_microseconds


def validate_operation(value: object) -> str:
    """Return an exact supported operation; this does not grant permission."""
    if type(value) is not str or value not in ("backfill", "verify"):
        raise MaintenanceContractError()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class MaintenanceTarget:
    """An immutable reservation tuple, not a verified mapping or authority."""

    target_credential_id: UUID
    organization_id: int
    marketplace_account_id: int
    provider: str
    credential_kind: str

    def __post_init__(self) -> None:
        _owner(
            self.target_credential_id,
            self.organization_id,
            self.marketplace_account_id,
            self.provider,
            self.credential_kind,
        )

    def __repr__(self) -> str:
        return "MaintenanceTarget(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class MaintenanceAuthorization:
    """Validated persisted metadata, NOT proof or current execution authority.

    Expired and revoked rows remain representable for immutable history. Opaque
    review/proof UUIDs do not verify their issuer or source. A role OID/name pair
    does not authenticate the caller. No clock, reviewer policy or TTL is inferred.
    Nullable fields are explicit constructor arguments, not invented defaults.
    """

    authorization_id: UUID
    target_credential_id: UUID
    organization_id: int
    marketplace_account_id: int
    provider: str
    credential_kind: str
    source_id: int
    source_user_id: str
    expected_external_account_id: str
    expected_credential_ref: str | None
    recipient_role_oid: int
    recipient_role_name: str
    review_id: UUID
    review_authority_id: UUID
    proof_reference_id: UUID
    reviewed_at: datetime
    allow_backfill: bool
    allow_verify: bool
    not_before: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revocation_reason_code: str | None

    def __post_init__(self) -> None:
        _owner(
            self.target_credential_id,
            self.organization_id,
            self.marketplace_account_id,
            self.provider,
            self.credential_kind,
        )
        for identity in (
            self.authorization_id,
            self.review_id,
            self.review_authority_id,
            self.proof_reference_id,
        ):
            _non_nil_uuid(identity)
        _positive_integer(self.source_id, _INT4_MAX)
        # The specified VARCHAR(64) adds no nonempty-user policy. Actual source
        # existence and active user/account ownership belong to the registrar.
        _text(self.source_user_id, minimum=0, maximum=64)
        _text(self.expected_external_account_id, minimum=1, maximum=128)
        if self.expected_credential_ref is not None:
            _text(self.expected_credential_ref, minimum=1, maximum=255)
        if self.provider == "wb" and self.expected_credential_ref != (
            f"lk_user_wb_tokens:{self.source_id}"
        ):
            raise MaintenanceContractError()

        _positive_integer(self.recipient_role_oid, _OID_MAX)
        role_bytes = _text(self.recipient_role_name, minimum=1, maximum=63)
        if len(role_bytes) > 63 or not self.recipient_role_name.strip():
            raise MaintenanceContractError()

        if type(self.allow_backfill) is not bool or type(self.allow_verify) is not bool:
            raise MaintenanceContractError()
        if not (self.allow_backfill or self.allow_verify):
            raise MaintenanceContractError()
        _instant_microseconds(self.reviewed_at)
        if _instant_microseconds(self.not_before) >= _instant_microseconds(self.expires_at):
            raise MaintenanceContractError()

        if self.revoked_at is None:
            if self.revocation_reason_code is not None:
                raise MaintenanceContractError()
        else:
            _instant_microseconds(self.revoked_at)
            if type(self.revocation_reason_code) is not str or (
                self.revocation_reason_code
                not in ("source_changed", "operator_revoked", "security_incident")
            ):
                raise MaintenanceContractError()

    def __repr__(self) -> str:
        return "MaintenanceAuthorization(<redacted>)"
