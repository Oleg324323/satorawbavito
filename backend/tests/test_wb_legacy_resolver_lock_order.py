"""Actual legacy resolver races in owned disposable PostgreSQL databases."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.cabinet.orm import (
    LkAuditEventRow,
    LkOrganizationRow,
    LkUserRow,
    LkUserWbTokenRow,
)
from app.infra.db import set_tenant_context
from app.platform.integrations import wb_credentials as wb
from app.platform.integrations.orm import MarketplaceAccountRow
from tests import test_marketplace_credential_fetch_postgres as fetch

cluster = fetch.cluster
pg_database = fetch.pg_database
pg_store = fetch.pg_store
CANARY = fetch.CANARY


@pytest.fixture
def legacy(pg_store):
    owner, factory, observer_engine = pg_store
    reference = f"lk_user_wb_tokens:{owner.organization_id}"
    with factory() as session:
        set_tenant_context(session, owner.organization_id)
        authority = session.execute(text("""
            SELECT NOT r.rolsuper AND NOT r.rolbypassrls,
                   c.relowner <> r.oid, inet_server_addr() IS NULL
            FROM pg_roles r CROSS JOIN pg_class c
            WHERE r.rolname = current_user AND c.oid = 'marketplace_accounts'::regclass
        """)).one()
        assert tuple(authority) == (True, True, True)
        session.get(MarketplaceAccountRow, owner.marketplace_account_id).credential_ref = reference
        session.commit()
    with factory() as session:
        result = wb.resolve_bound_wb_credential(session, owner.organization_id)
        assert result[:2] == (owner.marketplace_account_id, reference)
        assert result[2] == CANARY
    return owner, factory, observer_engine, reference


def resolve(session, owner, reference, **kwargs):
    return wb.resolve_bound_wb_credential(
        session, owner.organization_id,
        marketplace_account_id=owner.marketplace_account_id,
        credential_ref=reference, wb_token=CANARY, lock=True, **kwargs,
    )


def test_actual_legacy_resolver_and_publisher_both_complete(legacy):
    # An account→user UPDATE inversion makes the desired successful race fail.
    owner, factory, observer_engine, reference = legacy
    engine = factory.kw["bind"]
    resolver_has_account, allow_token, publisher_ready = Event(), Event(), Event()
    pids, outcomes, resolved, lock_sql = {}, {}, [], []
    observed_publisher_wait = False

    def pause(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("legacy-resolver") and "FOR " in statement:
            lock_sql.append(statement)
            if "FROM marketplace_accounts" in statement:
                pids["resolver"] = connection.connection.driver_connection.info.backend_pid
                resolver_has_account.set()
                assert allow_token.wait(8), "controller did not release resolver"

    def publisher():
        with factory() as session:
            try:
                set_tenant_context(session, owner.organization_id)
                pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
                session.execute(select(LkUserRow.user_id).where(
                    LkUserRow.user_id == f"owner-{owner.organization_id}"
                ).with_for_update(read=True))
                publisher_ready.set()
                assert resolver_has_account.wait(8)
                session.execute(select(MarketplaceAccountRow.marketplace_account_id).where(
                    MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id
                ).with_for_update())
                session.commit()
                outcomes["publisher"] = "success"
            except DBAPIError as exc:
                outcomes["publisher"] = exc.orig.sqlstate
            finally:
                session.rollback()

    def resolver():
        with factory() as session:
            try:
                resolved[:] = resolve(session, owner, reference)
                session.commit()
                outcomes["resolver"] = "success"
            except DBAPIError as exc:
                outcomes["resolver"] = exc.orig.sqlstate
            finally:
                session.rollback()

    event.listen(engine, "after_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="legacy-publisher") as publishing:
            publish_future = publishing.submit(publisher)
            try:
                assert publisher_ready.wait(8)
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="legacy-resolver") as resolving:
                    resolve_future = resolving.submit(resolver)
                    try:
                        assert resolver_has_account.wait(8)
                        with observer_engine.connect() as observer:
                            fetch.wait_blocked(observer, pids["publisher"], pids["resolver"])
                        observed_publisher_wait = True
                    finally:
                        allow_token.set()
                    resolve_future.result(timeout=12)
            finally:
                allow_token.set()
                resolver_has_account.set()
            publish_future.result(timeout=12)
    finally:
        allow_token.set()
        event.remove(engine, "after_cursor_execute", pause)
    assert outcomes == {"publisher": "success", "resolver": "success"}
    assert observed_publisher_wait is True
    assert tuple(resolved[0:2]) == (owner.marketplace_account_id, reference)
    assert resolved[2] == CANARY
    assert "FROM lk_users" in lock_sql[0] and "FOR SHARE" in lock_sql[0]
    assert "FROM marketplace_accounts" in lock_sql[1] and "FOR UPDATE" in lock_sql[1]
    assert "marketplace_accounts.marketplace_account_id =" in lock_sql[1]
    assert "FOR UPDATE OF lk_user_wb_tokens" in lock_sql[2]
    assert len(lock_sql) == 3
    assert all("password_hash" not in sql and "refresh_token_hash" not in sql for sql in lock_sql)


CHANGES = (
    "rotation", "deletion", "revocation", "account_ref", "account_status",
    "inactive_user", "user_org", "token_owner", "token_org", "external_id",
    "account_provider", "account_org", "second_account",
)


def snapshot(session, owner):
    """Comparable metadata and secret equality flags; never disclose token text."""
    account = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
    user = session.get(LkUserRow, f"owner-{owner.organization_id}")
    token = session.get(LkUserWbTokenRow, owner.organization_id)
    return {
        "account": (account.organization_id, account.marketplace, account.status,
                    account.external_account_id, account.credential_ref),
        "user": (user.organization_id, user.is_active),
        "token": None if token is None else (
            token.user_id, token.organization_id,
            token.wb_token == CANARY, token.wb_token == CANARY + "-rotated"),
        "accounts": session.scalar(select(func.count()).select_from(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == owner.organization_id)),
        "audit": session.scalar(select(func.count()).select_from(LkAuditEventRow).where(
            LkAuditEventRow.organization_id == owner.organization_id)),
    }


def mutate(session, owner, change):
    account = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
    user = session.get(LkUserRow, f"owner-{owner.organization_id}")
    token = session.get(LkUserWbTokenRow, owner.organization_id)
    if change == "rotation":
        token.wb_token = CANARY + "-rotated"
    elif change == "deletion":
        session.delete(token)
    elif change == "revocation":
        account.credential_ref = None
    elif change == "account_ref":
        account.credential_ref = "lk_user_wb_tokens:1"
    elif change == "account_status":
        account.status = "disconnected"
    elif change == "inactive_user":
        user.is_active = False
    elif change == "user_org":
        user.organization_id = owner.organization_id + 200000
    elif change == "token_owner":
        token.user_id = f"replacement-{owner.organization_id}"
    elif change == "token_org":
        token.organization_id = owner.organization_id + 200000
    elif change == "external_id":
        account.external_account_id = "changed-synthetic-seller"
    elif change == "account_provider":
        account.marketplace = "avito"
    elif change == "account_org":
        account.organization_id = owner.organization_id + 200000
    elif change == "second_account":
        session.add(MarketplaceAccountRow(
            marketplace_account_id=owner.marketplace_account_id + 400000,
            organization_id=owner.organization_id, marketplace="wb",
            external_account_id="second-synthetic-seller", status="connected"))
    else:
        raise AssertionError("unknown synthetic mutation")


def expected_change(before, owner, change):
    expected = dict(before)
    account, user, token = list(before["account"]), list(before["user"]), list(before["token"])
    if change == "rotation":
        token[2:] = [False, True]
    elif change == "deletion":
        token = None
    elif change in {"revocation", "account_ref"}:
        account[4] = None if change == "revocation" else "lk_user_wb_tokens:1"
    elif change == "account_status":
        account[2] = "disconnected"
    elif change == "inactive_user":
        user[1] = False
    elif change == "user_org":
        user[0] = owner.organization_id + 200000
    elif change == "token_owner":
        token[0] = f"replacement-{owner.organization_id}"
    elif change == "token_org":
        token[1] = owner.organization_id + 200000
    elif change == "external_id":
        account[3] = "changed-synthetic-seller"
    elif change == "account_provider":
        account[1] = "avito"
    elif change == "account_org":
        account[0] = owner.organization_id + 200000
        expected["accounts"] -= 1
    elif change == "second_account":
        expected["accounts"] += 1
    expected.update(account=tuple(account), user=tuple(user),
                    token=None if token is None else tuple(token))
    return expected


def mutation_target(owner, change):
    if change in {"inactive_user", "user_org"}:
        return select(LkUserRow.user_id).where(LkUserRow.user_id == f"owner-{owner.organization_id}")
    if change in {"rotation", "deletion", "token_owner", "token_org"}:
        return select(LkUserWbTokenRow.token_id).where(LkUserWbTokenRow.token_id == owner.organization_id)
    return select(MarketplaceAccountRow.marketplace_account_id).where(
        MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id)


@pytest.mark.parametrize("change", CHANGES)
def test_mutation_winning_wait_denies_stale_binding(legacy, change):
    owner, factory, observer_engine, reference = legacy
    engine = factory.kw["bind"]
    lock_started, resolver_pid = Event(), []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith("legacy-change") and "FOR " in statement:
            resolver_pid[:] = [connection.connection.driver_connection.info.backend_pid]
            lock_started.set()

    def resolving():
        with factory() as session:
            try:
                # Keep a stale ORM instance to exercise populate_existing after waits.
                set_tenant_context(session, owner.organization_id)
                stale_account = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
                assert stale_account.credential_ref == reference
                resolve(session, owner, reference)
                return "unexpected_success"
            except wb.WbCredentialBindingError as exc:
                assert str(exc) == "wb_credential_binding_invalid"
                assert CANARY not in str(exc) and CANARY not in repr(exc)
                assert session.in_transaction()
                return exc.error_code
            finally:
                session.rollback()

    event.listen(engine, "before_cursor_execute", observe)
    try:
        # Only this adversarial metadata mutator uses the disposable DB owner,
        # allowing cross-org moves; resolution always uses the real runtime role.
        with sessionmaker(bind=observer_engine)() as mutator:
            before = snapshot(mutator, owner)
            pid = mutator.scalar(text("SELECT pg_backend_pid()"))
            mutator.execute(mutation_target(owner, change).with_for_update())
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="legacy-change") as pool:
                future = pool.submit(resolving)
                try:
                    assert lock_started.wait(8)
                    with observer_engine.connect() as observer:
                        fetch.wait_blocked(observer, resolver_pid[0], pid)
                    mutate(mutator, owner, change)
                    mutator.commit()
                finally:
                    mutator.rollback()
                assert future.result(timeout=12) == "wb_credential_binding_invalid"
        with sessionmaker(bind=observer_engine)() as session:
            assert snapshot(session, owner) == expected_change(before, owner, change)
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("change", CHANGES[:-1])
def test_success_retains_locks_until_caller_releases_transaction(legacy, change):
    owner, factory, observer_engine, reference = legacy
    started, writer_pid = Event(), []
    with sessionmaker(bind=observer_engine)() as check:
        before = snapshot(check, owner)

    def writer():
        with sessionmaker(bind=observer_engine)() as session:
            try:
                writer_pid[:] = [session.scalar(text("SELECT pg_backend_pid()"))]
                started.set()
                session.execute(mutation_target(owner, change).with_for_update())
                mutate(session, owner, change)
                session.commit()
                return "success"
            finally:
                session.rollback()

    with factory() as session:
        result = resolve(session, owner, reference)
        assert result[:2] == (owner.marketplace_account_id, reference)
        assert result[2] == CANARY
        resolver_pid = session.scalar(text("SELECT pg_backend_pid()"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            try:
                assert started.wait(8)
                with observer_engine.connect() as observer:
                    fetch.wait_blocked(observer, writer_pid[0], resolver_pid)
                with sessionmaker(bind=observer_engine)() as check:
                    assert snapshot(check, owner) == before
            finally:
                session.rollback()
            assert future.result(timeout=12) == "success"
    with sessionmaker(bind=observer_engine)() as check:
        assert snapshot(check, owner) == expected_change(before, owner, change)


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("invalid", [
    "missing_account", "ambiguous_account", "explicit_account", "explicit_ref",
    "explicit_token", "wrong_org", "missing_token", "inactive_user",
    "leading_zero", "unicode_id", "zero_id", "missing_ref", "wrong_prefix",
    "blank_token", "padded_caller_token", "padded_caller_ref",
])
def test_existing_denials_and_normalization_are_preserved(legacy, lock, invalid):
    owner, factory, observer_engine, reference = legacy
    arguments = {"marketplace_account_id": owner.marketplace_account_id,
                 "credential_ref": reference, "wb_token": CANARY, "lock": lock}
    organization_id = owner.organization_id
    with sessionmaker(bind=observer_engine)() as session:
        account = session.get(MarketplaceAccountRow, owner.marketplace_account_id)
        if invalid == "missing_account":
            account.status = "disconnected"
        elif invalid == "ambiguous_account":
            mutate(session, owner, "second_account")
        elif invalid == "explicit_account":
            arguments["marketplace_account_id"] += 1
        elif invalid == "explicit_ref":
            arguments["credential_ref"] = "lk_user_wb_tokens:1"
        elif invalid == "explicit_token":
            arguments["wb_token"] = "wrong-synthetic-token"
        elif invalid == "wrong_org":
            organization_id += 200000
        elif invalid == "missing_token":
            mutate(session, owner, "deletion")
        elif invalid == "inactive_user":
            mutate(session, owner, "inactive_user")
        elif invalid == "blank_token":
            session.get(LkUserWbTokenRow, owner.organization_id).wb_token = "  "
        elif invalid == "padded_caller_token":
            arguments["wb_token"] = " " + CANARY + " "
        elif invalid == "padded_caller_ref":
            arguments["credential_ref"] = " " + reference + " "
        else:
            account.credential_ref = {
                "leading_zero": f"lk_user_wb_tokens:0{owner.organization_id}",
                "unicode_id": "lk_user_wb_tokens:١", "zero_id": "lk_user_wb_tokens:0",
                "missing_ref": None, "wrong_prefix": f"other:{owner.organization_id}",
            }[invalid]
        session.commit()
    with factory() as session:
        transaction_events = []
        event.listen(session, "after_commit", lambda _session: transaction_events.append("commit"))
        event.listen(session, "after_rollback", lambda _session: transaction_events.append("rollback"))
        with pytest.raises(wb.WbCredentialBindingError) as error:
            wb.resolve_bound_wb_credential(session, organization_id, **arguments)
        assert error.value.error_code == "wb_credential_binding_invalid"
        assert CANARY not in str(error.value) and CANARY not in repr(error.value)
        assert session.in_transaction() and transaction_events == []
        session.rollback()


@pytest.mark.parametrize("lock", [False, True])
def test_success_preserves_caller_work_rows_and_normalization(legacy, lock):
    owner, factory, observer_engine, reference = legacy
    with sessionmaker(bind=observer_engine)() as session:
        session.get(MarketplaceAccountRow, owner.marketplace_account_id).credential_ref = " " + reference + " "
        session.get(LkUserWbTokenRow, owner.organization_id).wb_token = " " + CANARY + " "
        session.commit()
        before = snapshot(session, owner)
    with factory() as session:
        set_tenant_context(session, owner.organization_id)
        organization = session.get(LkOrganizationRow, owner.organization_id)
        organization.name = "caller-owned-uncommitted-work"
        session.flush()
        transaction = session.get_transaction()
        transaction_events, statements = [], []
        event.listen(session, "after_commit", lambda _session: transaction_events.append("commit"))
        event.listen(session, "after_rollback", lambda _session: transaction_events.append("rollback"))

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            result = wb.resolve_bound_wb_credential(
                session, owner.organization_id, credential_ref=reference, wb_token=CANARY, lock=lock)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert result[:2] == (owner.marketplace_account_id, reference)
        assert result[2] == CANARY
        assert transaction_events == [] and session.get_transaction() is transaction
        assert organization.name == "caller-owned-uncommitted-work"
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        if not lock:
            assert not any("FOR UPDATE" in sql or "FOR SHARE" in sql for sql in statements)
        session.rollback()
    with sessionmaker(bind=observer_engine)() as session:
        assert snapshot(session, owner) == before
        assert session.get(LkOrganizationRow, owner.organization_id).name == "Synthetic"
