"""Paired fetch secrets must carry exact, immutable, safe account evidence."""

import copy
import json
import pickle
from dataclasses import FrozenInstanceError, asdict

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import TypeAdapter
from pydantic.errors import PydanticSchemaGenerationError
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from app.platform.integrations import credential_store as store
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountRow,
)
from app.security.marketplace_credentials import (
    CredentialCryptoError,
    DecryptedCredential,
)
from tests import test_marketplace_credential_store as store_tests
from tests.test_marketplace_credential_store import CANARY, _wb_owner

store_db = store_tests.store_db


def resolve(owner=None, kind="wb_api"):
    resolver = getattr(store, "resolve_marketplace_credential_for_fetch", None)
    assert callable(resolver), "missing atomic paired fetch API"
    return resolver(_wb_owner() if owner is None else owner, kind)


def test_paired_fetch_exact_identity_and_closed_session(store_db, monkeypatch):
    factory, _ = store_db
    metadata = store.put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    with factory() as session:
        session.get(MarketplaceAccountRow, 101).credential_ref = "synthetic-ref"
        session.commit()

    sessions = []
    statements = []

    def tracked_factory():
        session = factory()
        sessions.append(session)
        return session

    def forbidden(*args, **kwargs):
        pytest.fail("paired fetch must not resolve or label in separate helper reads")

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    monkeypatch.setattr(store, "get_session_factory", lambda: tracked_factory)
    for name in ("resolve_marketplace_credential", "get_marketplace_credential_metadata",
                 "_account", "_active_row", "_latest_row"):
        monkeypatch.setattr(store, name, forbidden)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = resolve()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert isinstance(result.secret, DecryptedCredential)
    assert result.secret.reveal() == {"token": CANARY}
    assert result.binding.owner == _wb_owner()
    assert result.binding.external_account_id == "wb-one"
    assert result.binding.credential_ref == "synthetic-ref"
    identity = result.binding.credential_identity
    assert (identity.credential_id, identity.generation) == (metadata.credential_id, 1)
    assert (identity.organization_id, identity.marketplace_account_id) == (1, 101)
    assert (identity.provider, identity.credential_kind, identity.payload_schema_version) == ("wb", "wb_api", 1)
    assert identity.expires_at is None
    assert len(sessions) == 1 and not sessions[0].in_transaction()
    assert not sessions[0].identity_map
    assert len(statements) == 1 and "JOIN marketplace_account_credentials" in statements[0]
    with pytest.raises(FrozenInstanceError):
        result.binding.external_account_id = "changed"
    with pytest.raises(AttributeError):
        result.binding = None
    assert not {"key_version", "nonce", "ciphertext"} & set(asdict(result.binding))


@pytest.mark.parametrize("operation", [
    repr, str, json.dumps, pickle.dumps, copy.copy, copy.deepcopy, dict, vars,
    asdict, jsonable_encoder,
])
def test_envelope_never_generically_serializes_secret(store_db, operation):
    store.put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    result = resolve()
    if operation in (repr, str):
        assert operation(result) == "<ResolvedCredentialForFetch redacted>"
    else:
        expected_error = ValueError if operation is jsonable_encoder else TypeError
        with pytest.raises(expected_error) as caught:
            operation(result)
        assert CANARY not in str(caught.value)
    assert not hasattr(result, "__dict__")
    with pytest.raises(PydanticSchemaGenerationError) as caught:
        TypeAdapter(type(result))
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("change,code", [
    ("missing", "credential_missing"),
    ("revoked", "credential_missing"),
    ("disconnected", "credential_account_not_found"),
    ("wrong_tenant", "credential_account_not_found"),
    ("wrong_account", "credential_account_not_found"),
    ("wrong_kind", "credential_contract_invalid"),
    ("invalid_owner", "credential_contract_invalid"),
])
def test_paired_fetch_fails_closed_with_safe_codes(store_db, change, code):
    factory, _ = store_db
    if change != "missing":
        store.put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    if change == "revoked":
        store.revoke_marketplace_credential(_wb_owner(), "wb_api", "operator_revoked")
    if change == "disconnected":
        with factory() as session:
            session.get(MarketplaceAccountRow, 101).status = "disconnected"
            session.commit()
        # Old public behavior is deliberately preserved for unchanged consumers.
        assert store.resolve_marketplace_credential(_wb_owner(), "wb_api").reveal() == {"token": CANARY}
    owner = {"wrong_tenant": _wb_owner(organization_id=2),
             "wrong_account": _wb_owner(account_id=999),
             "invalid_owner": _wb_owner(organization_id=True)}.get(change, _wb_owner())
    with pytest.raises(store.CredentialStoreError) as caught:
        resolve(owner, CANARY if change == "wrong_kind" else "wb_api")
    assert caught.value.code == code
    assert CANARY not in repr(caught.value)


def test_expired_and_corrupt_exact_rows_are_not_fetchable(store_db):
    factory, _ = store_db
    owner = store.MarketplaceAccountCredentialOwner(2, 202, "avito")
    store.put_marketplace_credential(owner, "avito_oauth_access", {
        "accessToken": CANARY, "expiresAt": "2026-09-08T13:59:59Z",
    })
    with pytest.raises(store.CredentialStoreError, match="^credential_expired$"):
        resolve(owner, "avito_oauth_access")
    store.put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    with factory() as session:
        row = session.scalar(select(MarketplaceAccountCredentialRow).where(
            MarketplaceAccountCredentialRow.marketplace_account_id == 101))
        row.ciphertext = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
        session.commit()
    with pytest.raises(CredentialCryptoError, match="^credential_auth_failed$"):
        resolve()


def test_persistence_error_is_translated_without_input_details(store_db, monkeypatch):
    factory, _ = store_db

    def fail(*args, **kwargs):
        raise SQLAlchemyError(CANARY)

    monkeypatch.setattr(factory.class_, "execute", fail)
    with pytest.raises(store.CredentialStoreError) as caught:
        resolve()
    assert caught.value.code == "credential_persistence_failed"
    assert CANARY not in repr(caught.value)
