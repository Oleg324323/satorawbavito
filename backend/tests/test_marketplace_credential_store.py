from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cabinet.orm import LkAuditEventRow, LkOrganizationRow, LkUserRow
from app.config import Settings, validate_security_settings
from app.infra.models import Base
from app.platform.integrations import credential_store
from app.platform.integrations.credential_store import (
    CredentialMetadata,
    CredentialStoreError,
    MarketplaceAccountCredentialOwner,
    put_marketplace_credential,
    reencrypt_credential,
    resolve_marketplace_credential,
    revoke_marketplace_credential,
)
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.security.marketplace_credentials import CredentialCryptoError, CredentialKeyring, DecryptedCredential


CANARY = "synthetic-store-secret-canary-b41d"
NOW = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def store_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_functions(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("octet_length", 1, lambda value: len(value) if value is not None else None)

    Base.metadata.create_all(
        engine,
        tables=[
            LkOrganizationRow.__table__,
            LkUserRow.__table__,
            MarketplaceAccountRow.__table__,
            MarketplaceAccountCredentialRow.__table__,
            LkAuditEventRow.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                MarketplaceAccountRow(
                    marketplace_account_id=101,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="wb-one",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=202,
                    organization_id=2,
                    marketplace="avito",
                    external_account_id="avito-two",
                    status="connected",
                ),
            ]
        )
        session.commit()
    keyring = CredentialKeyring(current_key_version=7, keys={7: b"a" * 32, 8: b"b" * 32})
    monkeypatch.setattr(credential_store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(credential_store, "_load_keyring", lambda: keyring)
    monkeypatch.setattr(credential_store, "_utc_now", lambda: NOW)
    return factory, keyring


def _wb_owner(*, organization_id: int = 1, account_id: int = 101) -> MarketplaceAccountCredentialOwner:
    return MarketplaceAccountCredentialOwner(
        organization_id=organization_id,
        marketplace_account_id=account_id,
        provider="wb",
    )


def test_put_and_resolve_use_exact_account_identity_and_redacted_types(store_db) -> None:
    factory, _keyring = store_db
    metadata = put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})

    assert isinstance(metadata, CredentialMetadata)
    assert metadata.organization_id == 1
    assert metadata.marketplace_account_id == 101
    assert metadata.provider == "wb"
    assert metadata.credential_kind == "wb_api"
    assert metadata.generation == 1
    assert "ciphertext" not in metadata.__dict__
    assert "nonce" not in metadata.__dict__

    resolved = resolve_marketplace_credential(_wb_owner(), "wb_api")
    assert isinstance(resolved, DecryptedCredential)
    assert resolved.reveal() == {"token": CANARY}
    with pytest.raises(CredentialStoreError) as caught:
        resolve_marketplace_credential(_wb_owner(organization_id=2), "wb_api")
    assert caught.value.code == "credential_account_not_found"

    with factory() as session:
        audit = session.scalars(select(LkAuditEventRow).order_by(LkAuditEventRow.event_id)).all()
        assert [row.action for row in audit] == ["integration.marketplace_credential.put"]
        assert CANARY not in repr(audit[0].details)
        assert set(audit[0].details) == {
            "credentialKind",
            "generation",
            "marketplaceAccountId",
            "operation",
            "provider",
            "resultCode",
        }


def test_put_rotates_atomically_and_only_new_row_is_active(store_db) -> None:
    factory, _keyring = store_db
    first = put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY + "-one"})
    second = put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY + "-two"})

    assert first.credential_id != second.credential_id
    assert second.generation == 2
    assert resolve_marketplace_credential(_wb_owner(), "wb_api").reveal() == {"token": CANARY + "-two"}
    with factory() as session:
        rows = session.scalars(select(MarketplaceAccountCredentialRow).order_by(MarketplaceAccountCredentialRow.generation)).all()
        assert len(rows) == 2
        assert rows[0].revocation_reason_code == "credential_replaced"
        assert rows[0].revoked_at.replace(tzinfo=timezone.utc) == NOW
        assert rows[1].revoked_at is None


def test_revoke_removes_active_resolution_without_deleting_ciphertext(store_db) -> None:
    factory, _keyring = store_db
    put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    revoked = revoke_marketplace_credential(_wb_owner(), "wb_api", "operator_revoked")
    assert revoked.revoked_at == NOW

    with pytest.raises(CredentialStoreError) as caught:
        resolve_marketplace_credential(_wb_owner(), "wb_api")
    assert caught.value.code == "credential_missing"
    with factory() as session:
        row = session.scalars(select(MarketplaceAccountCredentialRow)).one()
        assert row.ciphertext
        assert row.revocation_reason_code == "operator_revoked"


def test_put_after_explicit_revoke_keeps_generation_monotonic(store_db) -> None:
    put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    revoke_marketplace_credential(_wb_owner(), "wb_api", "operator_revoked")

    replaced = put_marketplace_credential(
        _wb_owner(),
        "wb_api",
        {"token": CANARY + "-new"},
    )

    assert replaced.generation == 2


def test_unknown_kind_is_contract_error_even_when_no_row_exists(store_db) -> None:
    with pytest.raises(CredentialStoreError) as caught:
        resolve_marketplace_credential(_wb_owner(), "unknown_kind")
    assert caught.value.code == "credential_contract_invalid"


def test_expired_access_credential_fails_closed(store_db) -> None:
    owner = MarketplaceAccountCredentialOwner(2, 202, "avito")
    expired = NOW - timedelta(seconds=1)
    put_marketplace_credential(
        owner,
        "avito_oauth_access",
        {"accessToken": CANARY, "expiresAt": "2026-09-08T13:59:59Z"},
    )
    with pytest.raises(CredentialStoreError) as caught:
        resolve_marketplace_credential(owner, "avito_oauth_access")
    assert caught.value.code == "credential_expired"


def test_corrupt_encrypted_row_never_falls_back(store_db) -> None:
    factory, _keyring = store_db
    put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    with factory() as session:
        row = session.scalars(select(MarketplaceAccountCredentialRow)).one()
        row.ciphertext = bytes((row.ciphertext[0] ^ 1,)) + row.ciphertext[1:]
        session.commit()

    with pytest.raises(CredentialCryptoError) as caught:
        resolve_marketplace_credential(_wb_owner(), "wb_api")
    assert caught.value.code == "credential_auth_failed"
    assert CANARY not in str(caught.value)


def test_reencrypt_uses_fresh_nonce_verifies_and_cas_updates(store_db) -> None:
    factory, _keyring = store_db
    created = put_marketplace_credential(_wb_owner(), "wb_api", {"token": CANARY})
    with factory() as session:
        before = session.get(MarketplaceAccountCredentialRow, created.credential_id)
        old_nonce = before.nonce
        old_ciphertext = before.ciphertext

    statements: list[str] = []

    def _capture(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", _capture)

    try:
        rotated = reencrypt_credential(created.credential_id, expected_generation=1, target_key_version=8,
                                       account_identity=_wb_owner())
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert rotated.generation == 2
    assert len(
        [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("UPDATE MARKETPLACE_ACCOUNT_CREDENTIALS")
        ]
    ) == 1
    assert resolve_marketplace_credential(_wb_owner(), "wb_api").reveal() == {"token": CANARY}
    with factory() as session:
        after = session.get(MarketplaceAccountCredentialRow, created.credential_id)
        assert after.key_version == 8
        assert after.generation == 2
        assert after.nonce != old_nonce
        assert after.ciphertext != old_ciphertext

    with pytest.raises(CredentialStoreError) as caught:
        reencrypt_credential(created.credential_id, expected_generation=1, target_key_version=8,
                             account_identity=_wb_owner())
    assert caught.value.code == "credential_concurrent_update"


def test_reason_and_owner_errors_are_safe(store_db) -> None:
    with pytest.raises(CredentialStoreError) as caught:
        revoke_marketplace_credential(_wb_owner(), "wb_api", CANARY)
    assert caught.value.code == "credential_reason_invalid"
    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_reencrypt_missing_owner_denies_before_io(monkeypatch) -> None:
    from uuid import uuid4

    def forbidden():
        raise AssertionError("rekey must validate before I/O")

    monkeypatch.setattr(credential_store, "_load_keyring", forbidden)
    monkeypatch.setattr(credential_store, "get_session_factory", forbidden)
    with pytest.raises(CredentialStoreError, match="^credential_contract_invalid$"):
        reencrypt_credential(uuid4(), 1, 8)


@pytest.mark.parametrize("field,value", [
    ("account_identity", None), ("account_identity", object()),
    *[(field, value) for field in ("organization_id", "marketplace_account_id")
      for value in (True, False, 0, -1, 2147483648, "1", None)],
    *[("provider", value) for value in (None, [], {}, "unknown")],
    *[("credential_id", value) for value in (None, True, "not-a-uuid")],
    *[(field, value) for field in ("expected_generation", "target_key_version")
      for value in (True, False, 0, -1, "1", None)],
    ("expected_generation", 9223372036854775807),
    ("expected_generation", 9223372036854775808),
    ("target_key_version", 2147483648),
])
def test_reencrypt_invalid_contract_denies_before_io(monkeypatch, field, value) -> None:
    from dataclasses import replace
    from uuid import uuid4

    def forbidden():
        raise AssertionError("rekey must validate before I/O")

    monkeypatch.setattr(credential_store, "_load_keyring", forbidden)
    monkeypatch.setattr(credential_store, "get_session_factory", forbidden)
    args = {"credential_id": uuid4(), "expected_generation": 1, "target_key_version": 8,
            "account_identity": _wb_owner()}
    if field in {"organization_id", "marketplace_account_id", "provider"}:
        args["account_identity"] = replace(args["account_identity"], **{field: value})
    else:
        args[field] = value
    with pytest.raises(CredentialStoreError, match="^credential_contract_invalid$"):
        reencrypt_credential(**args)


def test_reencrypt_maximum_schema_shape_reaches_key_policy(monkeypatch) -> None:
    from uuid import uuid4

    def no_configured_key():
        raise CredentialStoreError("credential_configuration_invalid")

    monkeypatch.setattr(credential_store, "_load_keyring", no_configured_key)
    with pytest.raises(CredentialStoreError, match="^credential_configuration_invalid$"):
        reencrypt_credential(uuid4(), 9223372036854775806, 2147483647,
                             account_identity=MarketplaceAccountCredentialOwner(
                                 2147483647, 2147483647, "wb"))


@pytest.mark.parametrize("mode", ["missing", "malformed", "unreadable"])
def test_encrypted_mode_startup_fails_closed_for_invalid_keyring(tmp_path: Path, mode: str) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    if mode != "missing":
        key_file = key_dir / "7"
        key_file.write_bytes(b"short" if mode == "malformed" else b"k" * 32)
        if mode == "unreadable":
            key_file.chmod(0)
    settings = Settings(
        marketplace_credentials_enabled=True,
        marketplace_credential_keyring_dir=str(key_dir),
        marketplace_credential_current_key_version=7,
        marketplace_credential_key_versions=(7,),
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            validate_security_settings(settings)
        assert str(caught.value) == "marketplace_credential_keyring_invalid"
        assert str(key_dir) not in str(caught.value)
    finally:
        if mode == "unreadable":
            (key_dir / "7").chmod(0o600)


def test_valid_keyring_configuration_passes_startup_validation(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    (key_dir / "7").write_bytes(b"k" * 32)
    settings = Settings(
        marketplace_credentials_enabled=True,
        marketplace_credential_keyring_dir=str(key_dir),
        marketplace_credential_current_key_version=7,
        marketplace_credential_key_versions=(7,),
    )
    validate_security_settings(settings)
