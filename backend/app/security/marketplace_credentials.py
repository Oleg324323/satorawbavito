from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ALGORITHM = "AES-256-GCM"
AAD_VERSION = 1
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_PAYLOAD_BYTES = 16_384
MAX_SECRET_FIELD_BYTES = 8_192

_ALLOWED_SCHEMAS: dict[tuple[str, str, int], tuple[str, ...]] = {
    ("wb", "wb_api", 1): ("token",),
    ("avito", "avito_oauth_client", 1): ("clientId", "clientSecret"),
    ("avito", "avito_oauth_access", 1): ("accessToken", "expiresAt"),
}
_SAFE_ERROR_CODES = frozenset(
    {
        "credential_auth_failed",
        "credential_configuration_invalid",
        "credential_contract_invalid",
        "credential_key_unavailable",
        "credential_payload_invalid",
    }
)


class CredentialCryptoError(ValueError):
    """Fail-closed crypto error whose public representation is a safe code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "credential_contract_invalid"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"CredentialCryptoError(code={self.code!r})"


@dataclass(frozen=True)
class CredentialIdentity:
    organization_id: int
    marketplace_account_id: int
    provider: str
    credential_kind: str
    payload_schema_version: int
    credential_id: UUID
    generation: int
    expires_at: datetime | None = None


@dataclass(frozen=True, repr=False)
class EncryptedCredential:
    algorithm: str
    key_version: int
    aad_version: int
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return "<EncryptedCredential redacted>"

    __str__ = __repr__


class DecryptedCredential:
    """Explicitly revealable secret payload with non-disclosing string forms."""

    __slots__ = ("__payload",)

    def __init__(self, payload: Mapping[str, str]) -> None:
        self.__payload = dict(payload)

    def reveal(self) -> dict[str, str]:
        return dict(self.__payload)

    @staticmethod
    def _refuse_serialization() -> NoReturn:
        raise CredentialCryptoError("credential_contract_invalid")

    def __iter__(self) -> NoReturn:
        self._refuse_serialization()

    def __copy__(self) -> NoReturn:
        self._refuse_serialization()

    def __deepcopy__(self, memo: object) -> NoReturn:
        self._refuse_serialization()

    def __reduce__(self) -> NoReturn:
        self._refuse_serialization()

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        self._refuse_serialization()

    def __repr__(self) -> str:
        return "<DecryptedCredential redacted>"

    __str__ = __repr__


class CredentialKeyring:
    """Immutable, redacted mapping of positive key versions to AES-256 keys."""

    def __init__(self, *, current_key_version: int, keys: Mapping[int, bytes]) -> None:
        if not _positive_int(current_key_version):
            raise CredentialCryptoError("credential_configuration_invalid")
        copied: dict[int, bytes] = {}
        for version, key in keys.items():
            if not _positive_int(version) or not isinstance(key, bytes) or len(key) != 32:
                raise CredentialCryptoError("credential_configuration_invalid")
            copied[version] = bytes(key)
        if current_key_version not in copied:
            raise CredentialCryptoError("credential_configuration_invalid")
        self.current_key_version = current_key_version
        self._keys = MappingProxyType(copied)

    @classmethod
    def from_directory(
        cls,
        directory: str | Path,
        *,
        current_key_version: int,
        required_key_versions: tuple[int, ...] | None = None,
    ) -> CredentialKeyring:
        versions = set(required_key_versions or ())
        versions.add(current_key_version)
        root = Path(directory)
        keys: dict[int, bytes] = {}
        try:
            if root.is_symlink() or not root.is_dir():
                raise OSError
            for version in versions:
                if not _positive_int(version):
                    raise OSError
                key_file = root / str(version)
                if key_file.is_symlink() or not key_file.is_file():
                    raise OSError
                keys[version] = key_file.read_bytes()
        except (OSError, ValueError, TypeError):
            raise CredentialCryptoError("credential_configuration_invalid") from None
        return cls(current_key_version=current_key_version, keys=keys)

    def key_for(self, version: int) -> bytes:
        key = self._keys.get(version)
        if key is None:
            raise CredentialCryptoError("credential_key_unavailable")
        return key

    def __repr__(self) -> str:
        return "<CredentialKeyring redacted>"

    __str__ = __repr__


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_expiry(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None or value.microsecond != 0:
        raise CredentialCryptoError("credential_contract_invalid")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_identity(identity: CredentialIdentity) -> tuple[str, ...]:
    if (
        not _positive_int(identity.organization_id)
        or not _positive_int(identity.marketplace_account_id)
        or not _positive_int(identity.payload_schema_version)
        or not isinstance(identity.credential_id, UUID)
        or not _positive_int(identity.generation)
        or identity.provider != identity.provider.lower()
    ):
        raise CredentialCryptoError("credential_contract_invalid")
    fields = _ALLOWED_SCHEMAS.get(
        (identity.provider, identity.credential_kind, identity.payload_schema_version)
    )
    if fields is None:
        raise CredentialCryptoError("credential_contract_invalid")
    expiry = _canonical_expiry(identity.expires_at)
    if (identity.credential_kind == "avito_oauth_access") != (expiry is not None):
        raise CredentialCryptoError("credential_contract_invalid")
    return fields


def _encode_payload(
    identity: CredentialIdentity,
    payload: Mapping[str, Any],
    *,
    error_code: str,
) -> bytes:
    fields = _validate_identity(identity)
    if not isinstance(payload, Mapping) or set(payload) != set(fields):
        raise CredentialCryptoError(error_code)
    normalized: dict[str, str] = {}
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise CredentialCryptoError(error_code)
        try:
            encoded_value = value.encode("utf-8")
        except UnicodeEncodeError:
            raise CredentialCryptoError(error_code) from None
        if len(encoded_value) > MAX_SECRET_FIELD_BYTES:
            raise CredentialCryptoError(error_code)
        normalized[field] = value
    if identity.credential_kind == "avito_oauth_access":
        if normalized["expiresAt"] != _canonical_expiry(identity.expires_at):
            raise CredentialCryptoError(error_code)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise CredentialCryptoError(error_code)
    return encoded


def _decode_payload(identity: CredentialIdentity, plaintext: bytes) -> dict[str, str]:
    if not plaintext or len(plaintext) > MAX_PAYLOAD_BYTES:
        raise CredentialCryptoError("credential_payload_invalid")
    try:
        decoded = plaintext.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CredentialCryptoError("credential_payload_invalid") from None
    if not isinstance(payload, dict):
        raise CredentialCryptoError("credential_payload_invalid")
    _encode_payload(identity, payload, error_code="credential_payload_invalid")
    return payload


def _length_prefix(value: bytes | None) -> bytes:
    if value is None:
        return b"\xff\xff\xff\xff"
    if len(value) >= 0xFFFFFFFF:
        raise CredentialCryptoError("credential_contract_invalid")
    return len(value).to_bytes(4, "big") + value


def _decimal(value: int) -> bytes:
    if not _positive_int(value):
        raise CredentialCryptoError("credential_contract_invalid")
    return str(value).encode("ascii")


def _associated_data(
    identity: CredentialIdentity,
    *,
    algorithm: str,
    key_version: int,
    aad_version: int,
) -> bytes:
    _validate_identity(identity)
    expiry = _canonical_expiry(identity.expires_at)
    values: tuple[bytes | None, ...] = (
        b"satorna.marketplace-credential",
        _decimal(aad_version),
        algorithm.encode("ascii"),
        _decimal(key_version),
        str(identity.credential_id).encode("ascii"),
        _decimal(identity.generation),
        _decimal(identity.organization_id),
        _decimal(identity.marketplace_account_id),
        identity.provider.encode("utf-8"),
        identity.credential_kind.encode("utf-8"),
        _decimal(identity.payload_schema_version),
        expiry.encode("ascii") if expiry is not None else None,
    )
    return b"".join(_length_prefix(value) for value in values)


def _random_nonce() -> bytes:
    return secrets.token_bytes(NONCE_BYTES)


def _validate_encrypted(encrypted: EncryptedCredential) -> None:
    if (
        encrypted.algorithm != ALGORITHM
        or encrypted.aad_version != AAD_VERSION
        or not _positive_int(encrypted.key_version)
        or not isinstance(encrypted.nonce, bytes)
        or len(encrypted.nonce) != NONCE_BYTES
        or not isinstance(encrypted.ciphertext, bytes)
        or len(encrypted.ciphertext) <= TAG_BYTES
        or len(encrypted.ciphertext) > MAX_PAYLOAD_BYTES + TAG_BYTES
    ):
        raise CredentialCryptoError("credential_contract_invalid")


def encrypt_credential(
    identity: CredentialIdentity,
    plaintext: Mapping[str, Any],
    keyring: CredentialKeyring,
) -> EncryptedCredential:
    payload = _encode_payload(identity, plaintext, error_code="credential_contract_invalid")
    key_version = keyring.current_key_version
    key = keyring.key_for(key_version)
    nonce = _random_nonce()
    if len(nonce) != NONCE_BYTES:
        raise CredentialCryptoError("credential_configuration_invalid")
    aad = _associated_data(
        identity,
        algorithm=ALGORITHM,
        key_version=key_version,
        aad_version=AAD_VERSION,
    )
    try:
        ciphertext = AESGCM(key).encrypt(nonce, payload, aad)
    except (TypeError, ValueError):
        raise CredentialCryptoError("credential_contract_invalid") from None
    return EncryptedCredential(ALGORITHM, key_version, AAD_VERSION, nonce, ciphertext)


def decrypt_credential(
    identity: CredentialIdentity,
    encrypted: EncryptedCredential,
    keyring: CredentialKeyring,
) -> DecryptedCredential:
    _validate_encrypted(encrypted)
    key = keyring.key_for(encrypted.key_version)
    aad = _associated_data(
        identity,
        algorithm=encrypted.algorithm,
        key_version=encrypted.key_version,
        aad_version=encrypted.aad_version,
    )
    try:
        plaintext = AESGCM(key).decrypt(encrypted.nonce, encrypted.ciphertext, aad)
    except InvalidTag:
        raise CredentialCryptoError("credential_auth_failed") from None
    except (TypeError, ValueError):
        raise CredentialCryptoError("credential_auth_failed") from None
    return DecryptedCredential(_decode_payload(identity, plaintext))
