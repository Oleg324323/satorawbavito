"""Account-scoped rekey under the exact disposable nonowner runtime role."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.cabinet.orm import LkAuditEventRow
from app.infra.db import set_tenant_context
from app.platform.integrations import credential_store as store
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountRow,
)
from app.security.marketplace_credentials import (
    CredentialCryptoError,
    CredentialKeyring,
    DecryptedCredential,
)
from tests import test_marketplace_credential_fetch_postgres as fetch_tests

cluster = fetch_tests.cluster
pg_database = fetch_tests.pg_database
pg_store = fetch_tests.pg_store
wait_blocked = fetch_tests.wait_blocked
CANARY = "synthetic-rekey-only-canary"


def test_runtime_owner_can_reencrypt_own_credential(pg_store):
    owner, factory, _owner_engine = pg_store
    with factory() as session:
        role = session.execute(text("""SELECT r.rolsuper, r.rolbypassrls,
            c.relowner = r.oid, c.relrowsecurity, c.relforcerowsecurity,
            inet_server_addr() IS NULL
            FROM pg_roles r CROSS JOIN pg_class c
            WHERE r.rolname = current_user AND c.oid = 'marketplace_account_credentials'::regclass
        """)).one()
        assert tuple(role) == (False, False, False, True, True, True)
    created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    assert store.resolve_marketplace_credential(owner, "wb_api").reveal() == {"token": CANARY}
    before, audits_before = snapshot(pg_store)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    runtime = factory.kw["bind"]
    event.listen(runtime, "before_cursor_execute", capture)
    try:
        rotated = store.reencrypt_credential(created.credential_id, 1, 8, account_identity=owner)
    finally:
        event.remove(runtime, "before_cursor_execute", capture)
    locking = [sql for sql in statements if "FOR UPDATE" in sql]
    updates = [sql for sql in statements if sql.startswith("UPDATE marketplace_account_credentials")]
    assert len(locking) == 2 and "FROM marketplace_accounts" in locking[0]
    assert len(updates) == 1
    for statement in [locking[1], updates[0]]:
        predicates = statement.split("WHERE", 1)[1]
        for column in ("organization_id", "marketplace_account_id", "provider", "credential_id"):
            assert f"marketplace_account_credentials.{column} =" in predicates
        assert "marketplace_account_credentials.revoked_at IS NULL" in predicates
    assert "marketplace_account_credentials.generation =" in updates[0].split("WHERE", 1)[1]
    assert rotated.generation == 2
    after, audits_after = snapshot(pg_store)
    first, changed = before[0], after[0]
    retained = set(first) - {"generation", "key_version", "nonce", "ciphertext", "updated_at"}
    assert {k: first[k] for k in retained} == {k: changed[k] for k in retained}
    assert changed["key_version"] == 8
    assert changed["nonce"] != first["nonce"] and changed["ciphertext"] != first["ciphertext"]
    assert len(audits_after) == len(audits_before) + 1
    assert audits_after[-1]["action"] == "integration.marketplace_credential.reencrypt"
    assert audits_after[-1]["details"] == {
        "credentialKind": "wb_api", "generation": 2,
        "marketplaceAccountId": owner.marketplace_account_id, "operation": "reencrypt",
        "provider": "wb", "resultCode": "ready",
    }
    assert CANARY not in repr(rotated) + repr(audits_after)
    assert store.resolve_marketplace_credential(owner, "wb_api").reveal() == {"token": CANARY}
    store.reencrypt_credential(created.credential_id, 2, 8, account_identity=owner)
    same_key, audit_same = snapshot(pg_store)
    assert same_key[0]["generation"] == 3
    assert same_key[0]["nonce"] != changed["nonce"]
    assert same_key[0]["ciphertext"] != changed["ciphertext"]
    assert len(audit_same) == len(audits_after) + 1
    assert store.resolve_marketplace_credential(owner, "wb_api").reveal() == {"token": CANARY}


def snapshot(pg_store):
    """Full stored envelopes and audits; no plaintext or key material in assertions."""
    owner, _factory, engine = pg_store
    with engine.connect() as connection:
        rows = connection.execute(select(MarketplaceAccountCredentialRow.__table__).where(
            MarketplaceAccountCredentialRow.organization_id.in_(
                [owner.organization_id, owner.organization_id + 200000])
        ).order_by(MarketplaceAccountCredentialRow.credential_id)).mappings().all()
        audits = connection.execute(select(LkAuditEventRow.__table__).where(
            LkAuditEventRow.organization_id.in_([owner.organization_id, owner.organization_id + 200000])
        ).order_by(LkAuditEventRow.event_id)).mappings().all()
        return [dict(row) for row in rows], [dict(row) for row in audits]


def assert_denied(call, code, caplog):
    with pytest.raises((store.CredentialStoreError, CredentialCryptoError)) as caught:
        call()
    assert caught.value.code == code
    assert CANARY not in str(caught.value) + repr(caught.value) + caplog.text
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


@pytest.mark.parametrize("wrong", ["same_org", "other_org", "provider", "uuid", "cross_pair"])
def test_exact_owner_scope_denies_without_mutation(pg_store, wrong, caplog):
    owner, _factory, engine = pg_store
    others = [store.MarketplaceAccountCredentialOwner(owner.organization_id,
              owner.marketplace_account_id + 100000, "wb"),
              store.MarketplaceAccountCredentialOwner(owner.organization_id + 200000,
              owner.marketplace_account_id + 200000, "wb")]
    with Session(engine) as session, session.begin():
        session.add_all([MarketplaceAccountRow(
            organization_id=o.organization_id, marketplace_account_id=o.marketplace_account_id,
            marketplace=o.provider, external_account_id=f"rekey-{o.marketplace_account_id}",
            status="connected") for o in others])
    created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    foreign = [store.put_marketplace_credential(o, "wb_api", {"token": CANARY}) for o in others]
    selected, credential_id, code = owner, created.credential_id, "credential_missing"
    if wrong == "same_org":
        selected = others[0]
    elif wrong == "other_org":
        selected = others[1]
    elif wrong == "provider":
        selected = store.MarketplaceAccountCredentialOwner(
            owner.organization_id, owner.marketplace_account_id, "avito")
        code = "credential_account_not_found"
    elif wrong == "uuid":
        credential_id = uuid4()
    else:
        credential_id = foreign[0].credential_id
    before = snapshot(pg_store)
    assert_denied(lambda: store.reencrypt_credential(
        credential_id, 1, 8, account_identity=selected), code, caplog)
    assert snapshot(pg_store) == before


def access_credential(pg_store, monkeypatch):
    owner, _factory, engine = pg_store
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    clock = [now]
    monkeypatch.setattr(store, "_utc_now", lambda: clock[0])
    access_owner = store.MarketplaceAccountCredentialOwner(
        owner.organization_id, owner.marketplace_account_id + 100000, "avito")
    with Session(engine) as session, session.begin():
        session.add(MarketplaceAccountRow(organization_id=access_owner.organization_id,
            marketplace_account_id=access_owner.marketplace_account_id, marketplace="avito",
            external_account_id="synthetic-rekey-avito", status="connected"))
    created = store.put_marketplace_credential(access_owner, "avito_oauth_access", {
        "accessToken": CANARY, "expiresAt": "2026-09-09T12:01:00Z"})
    return access_owner, created, clock


def test_access_expiry_and_payload_survive_success(pg_store, monkeypatch):
    owner, created, _clock = access_credential(pg_store, monkeypatch)
    rotated = store.reencrypt_credential(created.credential_id, 1, 8, account_identity=owner)
    assert rotated.expires_at == created.expires_at
    assert store.resolve_marketplace_credential(owner, "avito_oauth_access").reveal() == {
        "accessToken": CANARY, "expiresAt": "2026-09-09T12:01:00Z"}


@pytest.mark.parametrize("failure,code", [
    ("stale", "credential_concurrent_update"), ("revoked", "credential_missing"),
    ("expired", "credential_expired"), ("corrupt", "credential_auth_failed"),
    ("old_key", "credential_key_unavailable"), ("target_key", "credential_key_unavailable"),
    ("verify", "credential_auth_failed"), ("audit", "credential_persistence_failed"),
    ("database", "credential_persistence_failed"), ("factory", "credential_persistence_failed"),
])
def test_failed_rekey_rolls_back_full_envelope_and_audit(pg_store, monkeypatch, caplog, failure, code):
    owner, factory, engine = pg_store
    if failure == "expired":
        owner, created, clock = access_credential(pg_store, monkeypatch)
        clock[0] += timedelta(minutes=2)
    else:
        created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    if failure == "revoked":
        store.revoke_marketplace_credential(owner, "wb_api", "operator_revoked")
    elif failure == "corrupt":
        with Session(engine) as session, session.begin():
            row = session.get(MarketplaceAccountCredentialRow, created.credential_id)
            row.ciphertext = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
    elif failure in {"old_key", "target_key"}:
        keys = {8: b"b" * 32} if failure == "old_key" else {7: b"a" * 32}
        monkeypatch.setattr(store, "_load_keyring", lambda: CredentialKeyring(
            current_key_version=next(iter(keys)), keys=keys))
    elif failure == "verify":
        real_decrypt = store.decrypt_credential

        def mismatched(identity, encrypted, keyring):
            result = real_decrypt(identity, encrypted, keyring)
            return result if identity.generation == 1 else DecryptedCredential({"token": "different"})

        monkeypatch.setattr(store, "decrypt_credential", mismatched)

    def broken(*args, **kwargs):
        raise SQLAlchemyError(CANARY)

    if failure == "audit":
        real_audit = store._audit

        def failed_audit(*args, **kwargs):
            real_audit(*args, **kwargs)
            args[0].flush()
            broken()

        monkeypatch.setattr(store, "_audit", failed_audit)
    elif failure == "factory":
        monkeypatch.setattr(store, "get_session_factory", broken)
    runtime = factory.kw["bind"]

    def fail_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE marketplace_account_credentials"):
            broken()

    before = snapshot(pg_store)
    if failure == "database":
        event.listen(runtime, "before_cursor_execute", fail_update)
    try:
        assert_denied(lambda: store.reencrypt_credential(
            created.credential_id, 2 if failure == "stale" else 1, 8,
            account_identity=owner), code, caplog)
    finally:
        if failure == "database":
            event.remove(runtime, "before_cursor_execute", fail_update)
    assert snapshot(pg_store) == before


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ", "AUTOCOMMIT"])
def test_actual_transaction_admission(pg_store, monkeypatch, caplog, isolation):
    owner, factory, _engine = pg_store
    created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    before = snapshot(pg_store)
    isolated = create_engine(factory.kw["bind"].url, isolation_level=isolation, hide_parameters=True)
    try:
        with isolated.connect() as connection:
            assert connection.connection.dbapi_connection.autocommit is (isolation == "AUTOCOMMIT")
            assert connection.get_isolation_level() == (
                "READ COMMITTED" if isolation == "AUTOCOMMIT" else isolation)
        monkeypatch.setattr(store, "get_session_factory", lambda: sessionmaker(isolated))
        if isolation == "READ COMMITTED":
            assert store.reencrypt_credential(created.credential_id, 1, 8,
                                              account_identity=owner).generation == 2
        else:
            assert_denied(lambda: store.reencrypt_credential(created.credential_id, 1, 8,
                account_identity=owner), "credential_configuration_invalid", caplog)
            assert snapshot(pg_store) == before
    finally:
        isolated.dispose()


@pytest.mark.parametrize("mutation", ["replace", "revoke", "expire"])
def test_account_lock_precedes_credential_and_observes_committed_change(
    pg_store, monkeypatch, caplog, mutation,
):
    owner, factory, observer_engine = pg_store
    if mutation == "expire":
        owner, created, clock = access_credential(pg_store, monkeypatch)
    else:
        created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    runtime = factory.kw["bind"]
    started = Event()
    pids, locks = {}, []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("account-rekey") and "FOR UPDATE" in statement:
            locks.append(statement)
            pids["rekey"] = connection.connection.driver_connection.info.backend_pid
            started.set()

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        with factory() as mutator:
            set_tenant_context(mutator, owner.organization_id)
            pid = mutator.scalar(text("SELECT pg_backend_pid()"))
            mutator.scalar(select(MarketplaceAccountRow).where(
                MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id
            ).with_for_update())
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-rekey") as pool:
                future = pool.submit(store.reencrypt_credential, created.credential_id, 1, 8,
                                     account_identity=owner)
                try:
                    assert started.wait(5)
                    with observer_engine.connect() as observer:
                        wait_blocked(observer, pids["rekey"], pid)
                    # Would form a cycle if B locked the credential before the account.
                    row = mutator.scalar(select(MarketplaceAccountCredentialRow).where(
                        MarketplaceAccountCredentialRow.credential_id == created.credential_id
                    ).with_for_update())
                    if mutation == "expire":
                        clock[0] += timedelta(minutes=2)
                    else:
                        row.revoked_at = datetime.now(UTC)
                        row.revocation_reason_code = (
                            "credential_replaced" if mutation == "replace" else "operator_revoked")
                        if mutation == "replace":
                            # A commits a complete synthetic replacement while B waits on A's account.
                            identity = replace(store._identity(row), credential_id=uuid4(), generation=2)
                            encrypted = store.encrypt_credential(identity, {"token": CANARY + "-new"},
                                                                  store._load_keyring())
                            values = {column.name: getattr(row, column.name)
                                      for column in MarketplaceAccountCredentialRow.__table__.columns}
                            values.update(credential_id=identity.credential_id, generation=2,
                                nonce=encrypted.nonce, ciphertext=encrypted.ciphertext,
                                revoked_at=None, revocation_reason_code=None)
                            mutator.flush()
                            mutator.add(MarketplaceAccountCredentialRow(**values))
                    mutator.commit()
                    before = snapshot(pg_store)
                finally:
                    mutator.rollback()
                assert_denied(lambda: future.result(timeout=8),
                    "credential_expired" if mutation == "expire" else "credential_missing", caplog)
    finally:
        event.remove(runtime, "before_cursor_execute", observe)
    assert "FROM marketplace_accounts" in locks[0]
    assert snapshot(pg_store) == before
    if mutation == "replace":
        assert store.resolve_marketplace_credential(owner, "wb_api").reveal() == {
            "token": CANARY + "-new"}


def test_two_rekeys_same_generation_have_one_winner(pg_store):
    owner, factory, observer_engine = pg_store
    created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    before, audits_before = snapshot(pg_store)
    runtime = factory.kw["bind"]
    held, release, waiting = Event(), Event(), Event()
    pids = {}

    def after(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("first-rekey") and "FROM marketplace_accounts" in statement:
            pids["first"] = connection.connection.driver_connection.info.backend_pid
            held.set()
            assert release.wait(8)

    def before_second(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("second-rekey") and "FOR UPDATE" in statement:
            pids["second"] = connection.connection.driver_connection.info.backend_pid
            waiting.set()

    def rekey():
        try:
            store.reencrypt_credential(created.credential_id, 1, 8, account_identity=owner)
            return "success"
        except store.CredentialStoreError as error:
            return error.code

    event.listen(runtime, "after_cursor_execute", after)
    event.listen(runtime, "before_cursor_execute", before_second)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-rekey") as first_pool:
            first = first_pool.submit(rekey)
            try:
                assert held.wait(5)
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-rekey") as second_pool:
                    second = second_pool.submit(rekey)
                    try:
                        assert waiting.wait(5)
                        with observer_engine.connect() as observer:
                            wait_blocked(observer, pids["second"], pids["first"])
                    finally:
                        release.set()
                    codes = [first.result(timeout=8), second.result(timeout=8)]
            finally:
                release.set()
    finally:
        event.remove(runtime, "after_cursor_execute", after)
        event.remove(runtime, "before_cursor_execute", before_second)
    assert sorted(codes) == ["credential_concurrent_update", "success"]
    after_rows, audits_after = snapshot(pg_store)
    assert after_rows[0]["generation"] == before[0]["generation"] + 1
    assert len(audits_after) - len(audits_before) == 1
