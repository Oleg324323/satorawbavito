"""Organization-scoped OpenAI keys; never return or log stored secrets."""
from datetime import datetime, timezone
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.cabinet.orm import OrganizationOpenAiKeyRow
from app.config import get_settings, load_marketplace_credential_keyring
from app.infra.db import get_session_factory, set_tenant_context


class OpenAiKeyError(ValueError):
    pass


def _aad(organization_id: int) -> bytes:
    if type(organization_id) is not int or organization_id < 1:
        raise OpenAiKeyError("OPENAI_KEY_SCOPE_INVALID")
    return f"satorna:organization:{organization_id}:openai:v1".encode()


def _keyring():
    try:
        return load_marketplace_credential_keyring(get_settings())
    except Exception:
        raise OpenAiKeyError("OPENAI_KEY_STORAGE_UNAVAILABLE") from None


def key_status(organization_id: int) -> dict:
    _aad(organization_id)
    available = False
    try:
        _keyring()
        available = True
    except OpenAiKeyError:
        pass
    settings = get_settings()
    if not settings.marketplace_credentials_enabled:
        return {"configured": False, "storageAvailable": available, "serverConfigured": bool(settings.openai_api_key)}
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        row = session.get(OrganizationOpenAiKeyRow, organization_id)
        return {"configured": row is not None, "storageAvailable": available,
                "serverConfigured": bool(settings.openai_api_key)}


def save_key(organization_id: int, api_key: str) -> dict:
    aad = _aad(organization_id)
    if (not isinstance(api_key, str) or not api_key.startswith("sk-")
            or not 40 <= len(api_key) <= 512 or not api_key.isascii()
            or any(char.isspace() or ord(char) < 33 for char in api_key)):
        raise OpenAiKeyError("OPENAI_KEY_INVALID")
    ring = _keyring()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(ring.key_for(ring.current_key_version)).encrypt(nonce, api_key.encode(), aad)
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        row = session.get(OrganizationOpenAiKeyRow, organization_id)
        if row is None:
            row = OrganizationOpenAiKeyRow(organization_id=organization_id)
            session.add(row)
        row.nonce, row.ciphertext, row.key_version = nonce, ciphertext, ring.current_key_version
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    return key_status(organization_id)


def delete_key(organization_id: int) -> dict:
    _aad(organization_id)
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        row = session.get(OrganizationOpenAiKeyRow, organization_id)
        if row is not None:
            session.delete(row)
            session.commit()
    return key_status(organization_id)


def resolve_key(organization_id: int) -> str | None:
    aad = _aad(organization_id)
    if not get_settings().marketplace_credentials_enabled:
        return None
    with get_session_factory()() as session:
        set_tenant_context(session, organization_id)
        row = session.get(OrganizationOpenAiKeyRow, organization_id)
        if row is None:
            return None
        ring = _keyring()
        try:
            return AESGCM(ring.key_for(row.key_version)).decrypt(row.nonce, row.ciphertext, aad).decode()
        except Exception:
            # A damaged/mismatched key cannot silently fall back to another
            # billable credential. No ciphertext or key appears in the error.
            raise OpenAiKeyError("OPENAI_KEY_UNREADABLE") from None
