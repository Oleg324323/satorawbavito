"""Real snapshots and actual WB binding locks in exact disposable databases."""
# Distinct transaction scopes are essential to these concurrency assertions.

from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Event, current_thread
from time import monotonic, sleep

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

from app.cabinet.orm import LkOrganizationRow, LkUserRow, LkUserWbTokenRow
from app.infra.db import set_tenant_context
from app.platform.integrations import credential_store as store
from app.platform.integrations import wb_credentials as wb
from app.platform.integrations.orm import MarketplaceAccountRow
from app.security.marketplace_credentials import CredentialKeyring
from tests.test_credential_maintenance_inert_postgres import cluster, pg_database  # noqa: F401

COUNTER = count(92000)
CANARY = "synthetic-paired-fetch-token"
SELLER = "64f8d3e5-3d25-4b5c-9f23-0fd3937af451"


@pytest.fixture
def pg_store(pg_database, monkeypatch):
    owner_engine, runtime = pg_database
    org = next(COUNTER)
    factory = sessionmaker(bind=runtime, expire_on_commit=False)
    with sessionmaker(bind=owner_engine)() as session:
        session.add_all([
            LkOrganizationRow(organization_id=org, slug=f"fetch-{org}", name="Synthetic"),
            LkOrganizationRow(organization_id=org + 200000, slug=f"other-{org}", name="Synthetic"),
        ])
        session.flush()
        session.add_all([
            LkUserRow(user_id=f"owner-{org}", organization_id=org,
                      email=f"owner-{org}@example.invalid", password_hash="unused-synthetic",
                      full_name="Synthetic", permission_profile="admin", is_active=True),
            LkUserRow(user_id=f"replacement-{org}", organization_id=org,
                      email=f"replacement-{org}@example.invalid", password_hash="unused-synthetic",
                      full_name="Synthetic", permission_profile="admin", is_active=True),
            MarketplaceAccountRow(marketplace_account_id=org, organization_id=org,
                                  marketplace="wb", external_account_id=f"legacy-{org}", status="connected"),
        ])
        session.flush()
        session.add(LkUserWbTokenRow(token_id=org, user_id=f"owner-{org}", organization_id=org,
                                   wb_token=CANARY, token_masked="***"))
        session.commit()
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(store, "_load_keyring", lambda: CredentialKeyring(
        current_key_version=7, keys={7: b"a" * 32, 8: b"b" * 32}))
    monkeypatch.setattr(wb, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb, "fetch_wb_seller_id", lambda _token: SELLER)
    return store.MarketplaceAccountCredentialOwner(org, org, "wb"), factory, owner_engine


def paired(owner):
    resolver = getattr(store, "resolve_marketplace_credential_for_fetch", None)
    assert callable(resolver), "missing atomic paired fetch API"
    return resolver(owner, "wb_api")


@pytest.mark.parametrize("mutation", ["replace", "revoke", "reencrypt"])
def test_joined_statement_keeps_exact_snapshot_during_concurrent_change(pg_store, mutation):
    owner, factory, _owner_engine = pg_store
    first = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
    engine = factory.kw["bind"]
    statements = []
    changed = False

    def capture(connection, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if changed or "marketplace_accounts" not in statement or not statement.startswith("SELECT"):
            return
        statements.append(statement)
        # psycopg has received the one statement result, but the resolver has not
        # consumed/decrypted it. A second committed transaction changes the row.
        changed = True
        if mutation == "replace":
            store.put_marketplace_credential(owner, "wb_api", {"token": CANARY + "-new"})
        elif mutation == "revoke":
            store.revoke_marketplace_credential(owner, "wb_api", "operator_revoked")
        else:
            store.reencrypt_credential(first.credential_id, 1, 8, account_identity=owner)
        with factory() as session:
            set_tenant_context(session, owner.organization_id)
            row = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
            row.external_account_id = f"changed-{owner.organization_id}"
            row.credential_ref = "changed-ref"
            session.commit()

    event.listen(engine, "after_cursor_execute", capture)
    try:
        result = paired(owner)
    finally:
        event.remove(engine, "after_cursor_execute", capture)
    assert changed and len(statements) == 1
    assert "JOIN marketplace_account_credentials" in statements[0]
    assert "FOR UPDATE" not in statements[0] and "FOR SHARE" not in statements[0]
    assert result.secret.reveal() == {"token": CANARY}
    assert result.binding.external_account_id == f"legacy-{owner.organization_id}"
    assert result.binding.credential_ref is None
    assert result.binding.credential_identity.credential_id == first.credential_id
    assert result.binding.credential_identity.generation == 1
    if mutation == "revoke":
        with pytest.raises(store.CredentialStoreError, match="^credential_missing$"):
            paired(owner)
    else:
        latest = paired(owner)
        assert latest.binding.credential_identity.generation == 2
        assert latest.binding.external_account_id == f"changed-{owner.organization_id}"
        assert latest.binding.credential_ref == "changed-ref"


def bind(owner):
    return wb.bind_wb_credential(
        owner.organization_id, marketplace_account_id=owner.marketplace_account_id,
        token_id=owner.organization_id, expected_external_account_id=f"legacy-{owner.organization_id}")


def wait_blocked(observer, waiter, blocker):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        blocked = observer.execute(text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                                   {"blocker": blocker, "waiter": waiter}).scalar_one()
        if blocked:
            return
        sleep(0.01)
    pytest.fail("expected exact owned backend lock wait was not observed")


def test_actual_wb_bind_has_no_user_account_lock_inversion(pg_store):
    owner, factory, observer_engine = pg_store
    engine = factory.kw["bind"]
    binder_has_account = Event()
    allow_token = Event()
    publisher_start = Event()
    pids = {}
    lock_sql = []

    def pause_binder(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("wb-bind") and "FOR " in statement:
            lock_sql.append(statement)
            if "FROM marketplace_accounts" in statement:
                pids["binder"] = connection.connection.driver_connection.info.backend_pid
                binder_has_account.set()
                assert allow_token.wait(8), "controller did not release binder"

    def publisher():
        with factory() as session:
            set_tenant_context(session, owner.organization_id)
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
            session.execute(select(LkUserRow.user_id).where(
                LkUserRow.user_id == f"owner-{owner.organization_id}").with_for_update(read=True))
            publisher_start.set()
            assert binder_has_account.wait(8)
            session.execute(select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id).with_for_update())
            session.commit()

    event.listen(engine, "after_cursor_execute", pause_binder)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="publisher") as publishing:
            publish_future = publishing.submit(publisher)
            assert publisher_start.wait(8)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-bind") as binding:
                bind_future = binding.submit(bind, owner)
                try:
                    assert binder_has_account.wait(8)
                    with observer_engine.connect() as observer:
                        wait_blocked(observer, pids["publisher"], pids["binder"])
                finally:
                    allow_token.set()
                # On the original implementation PostgreSQL detects the actual
                # cycle: publisher waits account, binder waits publisher's user.
                result = bind_future.result(timeout=10)
            publish_future.result(timeout=10)
    finally:
        allow_token.set()
        event.remove(engine, "after_cursor_execute", pause_binder)
    assert result["sellerIdentityVerified"] is True
    assert "FROM lk_users" in lock_sql[0] and "FOR SHARE" in lock_sql[0]
    assert "FOR UPDATE OF lk_user_wb_tokens" in lock_sql[-1]
    assert all("password_hash" not in sql and "refresh_token_hash" not in sql for sql in lock_sql)
    with factory() as session:
        set_tenant_context(session, owner.organization_id)
        account = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
        assert (account.external_account_id, account.credential_ref) == (SELLER, f"lk_user_wb_tokens:{owner.organization_id}")


@pytest.mark.parametrize("change", ["inactive_user", "user_org", "token_owner", "token_org", "account_status"])
def test_binding_refreshes_metadata_after_lock_wait(pg_store, change):
    owner, factory, observer_engine = pg_store
    engine = factory.kw["bind"]
    lock_started = Event()
    binder_pid = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("wb-change") and "FOR " in statement:
            binder_pid[:] = [connection.connection.driver_connection.info.backend_pid]
            lock_started.set()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with sessionmaker(bind=observer_engine)() as mutator:
            set_tenant_context(mutator, owner.organization_id)
            pid = mutator.scalar(text("SELECT pg_backend_pid()"))
            user_change = change in {"inactive_user", "user_org"}
            if user_change:
                row = mutator.scalar(select(LkUserRow).where(
                    LkUserRow.user_id == f"owner-{owner.organization_id}").with_for_update())
            else:
                row = mutator.scalar(select(MarketplaceAccountRow).where(
                    MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id).with_for_update())
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-change") as pool:
                future = pool.submit(bind, owner)
                try:
                    assert lock_started.wait(8)
                    with observer_engine.connect() as observer:
                        wait_blocked(observer, binder_pid[0], pid)
                    if change == "inactive_user":
                        row.is_active = False
                    elif change == "user_org":
                        row.organization_id = owner.organization_id + 200000
                    elif change == "account_status":
                        row.status = "disconnected"
                    else:
                        token = mutator.get(LkUserWbTokenRow, owner.organization_id)
                        if change == "token_owner":
                            token.user_id = f"replacement-{owner.organization_id}"
                        else:
                            token.organization_id = owner.organization_id + 200000
                    mutator.commit()
                finally:
                    mutator.rollback()
                with pytest.raises(wb.WbCredentialBindingError, match="^wb_credential_binding_invalid$"):
                    future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
