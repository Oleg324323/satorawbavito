"""Publication fences on an owned disposable PostgreSQL database, never SQLite."""
# Separate contexts expose commit and rollback boundaries.
# ruff: noqa: SIM117

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow, LkSessionRow, LkUserRow
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations import credential_store as store
from app.platform.integrations import ingestion_tokens
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountIngestionTokenRow,
    MarketplaceAccountRow,
)
from tests import test_marketplace_credential_fetch_postgres as fetch_tests
from tests.test_publication_guard import api

cluster = fetch_tests.cluster
pg_database = fetch_tests.pg_database
pg_store = fetch_tests.pg_store
wait_blocked = fetch_tests.wait_blocked


@pytest.fixture
def data(pg_store):
    owner, factory, owner_engine = pg_store
    org = owner.organization_id
    expiry = datetime.now(UTC) + timedelta(hours=1)
    credential = store.put_marketplace_credential(owner, "wb_api", {"token": "synthetic-guard"})
    token_id = uuid4()
    with Session(owner_engine) as session, session.begin():
        session.add(IamMembershipRow(membership_id=org, organization_id=org,
                    user_id=f"owner-{org}", role="custom", permissions=["sync:run", "cabinet:read"],
                    scope_mode="all", allowed_account_ids=[], is_active=True))
        session.add(LkSessionRow(session_id=f"login-{org}", user_id=f"owner-{org}",
                    issued_at=datetime.now(UTC), last_seen_at=datetime.now(UTC), expires_at=expiry,
                    refresh_token_hash="synthetic-refresh-canary"))
        session.add(MarketplaceAccountRow(marketplace_account_id=org + 100000,
                    organization_id=org, marketplace="avito", external_account_id="avito-seller",
                    status="connected", credential_ref=None))
        session.flush()
        session.add(MarketplaceAccountIngestionTokenRow(token_id=token_id,
                    organization_id=org, marketplace_account_id=org + 100000, provider="avito",
                    scope="avito.browser_snapshot.write", verifier=b"v" * 32,
                    issued_at=datetime.now(UTC), expires_at=expiry,
                    binding_schema_version=1, binding_external_account_id="avito-seller",
                    binding_credential_ref=None, binding_version=1))
    return SimpleNamespace(owner=owner, org=org, factory=factory, engine=owner_engine,
                           expiry=expiry, credential=credential, token_id=token_id)


def arguments(d, *, token=False, read=False):
    g = api()
    if token:
        account = g.ExpectedAccountBinding(d.org + 100000, "avito", "avito-seller", None)
        authority = g.ExpectedIngestionToken(account.marketplace_account_id, d.token_id,
                                             "avito.browser_snapshot.write", d.expiry)
    else:
        account = g.ExpectedAccountBinding(d.org, "wb", f"legacy-{d.org}", None)
        authority = g.ExpectedCredential(d.org, d.credential.credential_id, "wb_api", 1, 1, None)
    return {"principal": g.UserSessionPrincipal(d.org, f"owner-{d.org}", d.org, f"login-{d.org}"),
            "required_permissions": frozenset({"cabinet:read" if read else "sync:run"}),
            "accounts": (account,), "authorities": () if read else (authority,)}


def acquire(session, d, **kwargs):
    return api().acquire_publication_guard(session, **arguments(d, **kwargs))


def proof(session, d):
    for action in ("synthetic.publication", "synthetic.proof"):
        session.add(LkAuditEventRow(organization_id=d.org, actor_user_id=f"owner-{d.org}",
                                  action=action, object_type="synthetic", object_id=str(d.org)))


def count_proof(d):
    with Session(d.engine) as session:
        return session.scalar(select(func.count()).select_from(LkAuditEventRow).where(
            LkAuditEventRow.organization_id == d.org,
            LkAuditEventRow.action.in_(["synthetic.publication", "synthetic.proof"])))


@contextmanager
def guard_statements(connection):
    """Observe guard SQL only, excluding driver initialization and test setup."""
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", capture)


@pytest.mark.parametrize("mode", ["engine_option", "engine_constructor", "bound_connection"])
def test_physical_admission_rejects_autocommit_before_sql(data, mode):
    d, g = data, api()
    # A separate pool makes every mode/setting change local to this test.
    engine = create_engine(d.factory.kw["bind"].url, hide_parameters=True,
                           **({"isolation_level": "AUTOCOMMIT"} if mode == "engine_constructor" else {}))
    bind = engine.execution_options(isolation_level="AUTOCOMMIT") if mode == "engine_option" else engine
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT") if mode == "bound_connection" else None
    try:
        with Session(connection if connection is not None else bind) as session:
            with session.begin():
                physical = session.connection()
                assert session.get_transaction().is_active
                assert physical.get_isolation_level() == "READ COMMITTED"
                assert physical.connection.dbapi_connection.autocommit is True
                # Reproduce old guard acceptance, not merely its later tenant
                # mismatch: AUTOCOMMIT can retain a session-level tenant value.
                physical.execute(text("SELECT set_config('app.organization_id', :org, false)"), {"org": str(d.org)})
                try:
                    with guard_statements(physical) as statements:
                        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
                            acquire(session, d)
                    assert statements == []
                finally:
                    physical.execute(text("SELECT set_config('app.organization_id', '', false)"))
                    session.rollback()
    finally:
        if connection is not None:
            connection.close()
        engine.dispose()
    assert count_proof(d) == 0


def test_physical_admission_rejects_initial_connection_savepoint_before_sql(data):
    d, g = data, api()
    with d.factory() as session, session.begin():
        connection = session.connection()
        nested = connection.begin_nested()
        try:
            assert not session.in_nested_transaction()
            with guard_statements(connection) as statements:
                with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
                    acquire(session, d)
            assert statements == []
        finally:
            nested.rollback()
            session.rollback()


@pytest.mark.parametrize("boundary", ["revalidate", "commit"])
def test_physical_admission_rejects_later_connection_savepoint_and_poisons_root(data, boundary):
    d, g = data, api()
    with d.factory() as session:
        session.begin()
        guard = acquire(session, d)
        proof(session, d)
        session.flush()
        connection = session.connection()
        nested = connection.begin_nested()
        try:
            with guard_statements(connection) as statements:
                with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
                    guard.revalidate_before_write() if boundary == "revalidate" else session.commit()
            assert statements == []
        finally:
            if nested.is_active:
                nested.rollback()
        # Closing the unsupported SAVEPOINT cannot recover a poisoned handle.
        for operation in (guard.revalidate_before_write, session.commit):
            with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
                operation()
        session.rollback()
    assert count_proof(d) == 0


@pytest.mark.parametrize("join_mode", [None, "rollback_only", "create_savepoint", "control_fully"])
def test_physical_admission_rejects_external_root_without_touching_owner(data, join_mode):
    d, g = data, api()
    with d.engine.connect() as connection:
        root = connection.begin()
        try:
            connection.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(d.org)})
            connection.execute(text("INSERT INTO lk_audit_events (organization_id, actor_user_id, action, object_type, object_id) "
                                    "VALUES (:org, :user, 'synthetic.owner', 'synthetic', :object)"),
                               {"org": d.org, "user": f"owner-{d.org}", "object": str(d.org)})
            options = {} if join_mode is None else {"join_transaction_mode": join_mode}
            with Session(connection, **options) as session:
                with session.begin():
                    with guard_statements(connection) as statements:
                        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
                            acquire(session, d)
                    assert statements == []
            assert connection.get_transaction() is root and root.is_active
            assert not connection.in_nested_transaction()
            assert connection.scalar(text("SELECT current_setting('app.organization_id', true)")) == str(d.org)
            assert connection.scalar(text("SELECT count(*) FROM lk_audit_events WHERE organization_id=:org AND action='synthetic.owner'"),
                                     {"org": d.org}) == 1
        finally:
            if root.is_active:
                root.rollback()
    assert count_proof(d) == 0


def test_physical_admission_accepts_engine_root_and_commits_both_proofs(data):
    d = data
    with d.factory() as session, session.begin():
        connection = session.connection()
        assert connection.get_isolation_level() == "READ COMMITTED"
        assert connection.connection.dbapi_connection.autocommit is False
        assert connection.get_transaction().is_active
        assert not connection.in_nested_transaction()
        guard = acquire(session, d)
        assert guard.revalidate_before_write().tzinfo is not None
        proof(session, d)
    assert count_proof(d) == 2


def mutate(session, d, change):
    if change.startswith("user_"):
        values = {"is_active": False} if change == "user_inactive" else {"organization_id": d.org + 200000}
        session.execute(update(LkUserRow).where(LkUserRow.user_id == f"owner-{d.org}").values(**values))
    elif change.startswith("membership_"):
        values = {"membership_inactive": {"is_active": False}, "membership_permissions": {"permissions": []},
                  "membership_scope": {"scope_mode": "selected", "allowed_account_ids": []}}[change]
        session.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(**values))
    elif change == "session_revoke":
        session.execute(update(LkSessionRow).where(LkSessionRow.session_id == f"login-{d.org}").values(revoked_at=func.clock_timestamp()))
    elif change.startswith("account_"):
        values = {"account_disconnect": {"status": "disconnected"},
                  "account_rebind": {"external_account_id": "replacement"},
                  "account_ref": {"credential_ref": "replacement"}}[change]
        session.execute(update(MarketplaceAccountRow).where(MarketplaceAccountRow.marketplace_account_id == d.org).values(**values))
    elif change.startswith("credential_"):
        values = ({"generation": 2} if change == "credential_reencrypt" else
                  {"revoked_at": func.clock_timestamp(), "revocation_reason_code": "credential_replaced"})
        session.execute(update(MarketplaceAccountCredentialRow).where(
            MarketplaceAccountCredentialRow.credential_id == d.credential.credential_id).values(**values))
    else:
        session.execute(update(MarketplaceAccountIngestionTokenRow).where(
            MarketplaceAccountIngestionTokenRow.token_id == d.token_id).values(
                revoked_at=func.clock_timestamp(), revocation_reason_code="token_rotated"))


CHANGES = ["user_inactive", "user_org", "membership_inactive", "membership_permissions", "membership_scope",
           "session_revoke", "account_disconnect", "account_rebind", "account_ref",
           "credential_revoke", "credential_reencrypt", "token_revoke"]


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("publisher_first", [False, True])
def test_both_lock_winners_fence_publication(data, change, publisher_first):
    d = data
    g = api()
    started = Event()
    pids = {}

    def publish():
        with d.factory() as session, session.begin():
            pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            acquire(session, d, token=change.startswith("token"))
            proof(session, d)

    def updater():
        with Session(d.engine) as session, session.begin():
            pids["updater"] = session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            mutate(session, d, change)

    with ThreadPoolExecutor(max_workers=1) as pool:
        if publisher_first:
            with d.factory() as session, session.begin():
                pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
                acquire(session, d, token=change.startswith("token"))
                future = pool.submit(updater)
                assert started.wait(5)
                with d.engine.connect() as observer:
                    wait_blocked(observer, pids["updater"], pids["publisher"])
                assert not future.done()
                proof(session, d)
            future.result(timeout=8)
            assert count_proof(d) == 2
        else:
            with Session(d.engine) as session, session.begin():
                pids["updater"] = session.scalar(text("SELECT pg_backend_pid()"))
                mutate(session, d, change)
                future = pool.submit(publish)
                assert started.wait(5)
                with d.engine.connect() as observer:
                    wait_blocked(observer, pids["publisher"], pids["updater"])
            with pytest.raises(g.PublicationGuardError):
                future.result(timeout=8)
            assert count_proof(d) == 0


@pytest.mark.parametrize("change", CHANGES)
def test_before_commit_flushes_and_rejects_same_transaction_authority_mutation(data, change):
    d = data
    with pytest.raises(api().PublicationGuardError):
        with d.factory() as session, session.begin():
            acquire(session, d, token=change.startswith("token"))
            proof(session, d)
            mutate(session, d, change)
    assert count_proof(d) == 0


@pytest.mark.parametrize("writer_kind", ["replace", "revoke", "reencrypt", "token_rotate", "token_revoke"])
@pytest.mark.parametrize("publisher_first", [False, True])
def test_actual_owned_writers_both_lock_winners(data, monkeypatch, writer_kind, publisher_first):
    d, g = data, api()
    token = writer_kind.startswith("token")
    owner = store.MarketplaceAccountCredentialOwner(d.org, d.org + 100000, "avito") if token else d.owner
    monkeypatch.setattr(ingestion_tokens, "get_session_factory", lambda: d.factory)
    writer_engine = d.factory.kw["bind"]
    writer_started, mutated, release = Event(), Event(), Event()
    pids = {}

    def before(connection, cursor, statement, parameters, context, executemany):
        from threading import current_thread
        if current_thread().name.startswith("owned-writer"):
            pids["writer"] = connection.connection.driver_connection.info.backend_pid
            writer_started.set()

    def after(connection, cursor, statement, parameters, context, executemany):
        from threading import current_thread
        if (current_thread().name.startswith("owned-writer") and statement.startswith("UPDATE")
                and not publisher_first and not mutated.is_set()):
            mutated.set()
            assert release.wait(8)

    def write():
        if writer_kind == "replace":
            return store.put_marketplace_credential(owner, "wb_api", {"token": "synthetic-replaced"})
        if writer_kind == "revoke":
            return store.revoke_marketplace_credential(owner, "wb_api", "operator_revoked")
        if writer_kind == "reencrypt":
            return store.reencrypt_credential(d.credential.credential_id, 1, 8, account_identity=owner)
        if writer_kind == "token_rotate":
            return ingestion_tokens.issue_ingestion_token(owner, expires_at=d.expiry)
        return ingestion_tokens.revoke_ingestion_tokens(owner, "operator_revoked")

    publisher_started = Event()

    def publish():
        with d.factory() as session, session.begin():
            pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
            publisher_started.set()
            acquire(session, d, token=token)
            proof(session, d)

    event.listen(writer_engine, "before_cursor_execute", before)
    event.listen(writer_engine, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="owned-writer") as writers:
            if publisher_first:
                with d.factory() as session, session.begin():
                    pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
                    acquire(session, d, token=token)
                    future = writers.submit(write)
                    assert writer_started.wait(5)
                    with d.engine.connect() as observer:
                        wait_blocked(observer, pids["writer"], pids["publisher"])
                    proof(session, d)
                future.result(timeout=8)
                assert count_proof(d) == 2
            else:
                future = writers.submit(write)
                try:
                    assert mutated.wait(5)
                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="guard-publisher") as publishers:
                        publishing = publishers.submit(publish)
                        try:
                            assert publisher_started.wait(5)
                            with d.engine.connect() as observer:
                                wait_blocked(observer, pids["publisher"], pids["writer"])
                        finally:
                            release.set()
                        future.result(timeout=8)
                        with pytest.raises(g.PublicationGuardError):
                            publishing.result(timeout=8)
                finally:
                    release.set()
                assert count_proof(d) == 0
    finally:
        release.set()
        event.remove(writer_engine, "before_cursor_execute", before)
        event.remove(writer_engine, "after_cursor_execute", after)


def test_decimal_scope_compatibility_with_leading_zeroes(data):
    d = data
    with Session(d.engine) as session, session.begin():
        session.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            scope_mode="selected", allowed_account_ids=["0" * 20 + str(d.org)]))
    with d.factory() as session, session.begin():
        acquire(session, d)


@pytest.mark.parametrize("value", [[], {}, "attacker-canary", 3])
def test_malformed_tenant_marker_is_safe(data, value):
    d = data
    with d.factory() as session, session.begin():
        session.info["satorna_tenant_context"] = value
        with pytest.raises(api().PublicationGuardError, match="^publication_context_invalid$"):
            acquire(session, d)
        session.rollback()


def test_multiple_distinct_tokens_for_same_account(data):
    d = data
    second_id = uuid4()
    with Session(d.engine) as session, session.begin():
        session.add(MarketplaceAccountIngestionTokenRow(token_id=second_id,
                    organization_id=d.org, marketplace_account_id=d.org + 100000, provider="avito",
                    scope="avito.browser_snapshot.write", verifier=b"s" * 32,
                    issued_at=datetime.now(UTC), expires_at=d.expiry))
    args = arguments(d, token=True)
    args["authorities"] += (replace(args["authorities"][0], token_id=second_id),)
    with d.factory() as session, session.begin():
        api().acquire_publication_guard(session, **args)


def test_credential_independent_trusted_permission_not_limited_to_read(data):
    d = data
    with Session(d.engine) as session, session.begin():
        session.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            role="custom", permissions=["reviews:approve"]))
    args = arguments(d, read=True)
    args["required_permissions"] = frozenset({"reviews:approve"})
    with d.factory() as session, session.begin():
        api().acquire_publication_guard(session, **args)


def test_orm_auth_mutation_during_flush_is_fenced(data):
    d = data
    with pytest.raises(api().PublicationGuardError, match="^publication_access_denied$"):
        with d.factory() as session, session.begin():
            acquire(session, d)
            user = session.get(LkUserRow, f"owner-{d.org}")
            user.is_active = False
            proof(session, d)
    assert count_proof(d) == 0


def test_flush_failure_is_safe_and_atomic(data):
    d = data
    with pytest.raises(api().PublicationGuardError, match="^publication_persistence_failed$") as caught:
        with d.factory() as session, session.begin():
            acquire(session, d)
            proof(session, d)
            session.add(LkAuditEventRow(organization_id=d.org, action="secret-canary" * 100,
                                       object_type="synthetic", object_id="bad"))
    assert "canary" not in repr(caught.value)
    assert count_proof(d) == 0


@pytest.mark.parametrize("scope,ids,allowed", [
    ("all", [], True), ("selected", "self", True), ("selected", "decimal", True),
    ("selected", [], False), ("all", {}, False), ("all", None, False),
    ("selected", [True], False), ("selected", ["١"], False),
    ("selected", ["1x"], False), ("unknown", [], False),
    ("all", [0], False), ("all", [1.5], False),
])
def test_live_membership_scope_fail_closed(data, scope, ids, allowed):
    d = data
    ids = [d.org] if ids == "self" else [str(d.org)] if ids == "decimal" else ids
    with Session(d.engine) as session, session.begin():
        session.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            scope_mode=scope, allowed_account_ids=ids))
    if allowed:
        with d.factory() as session, session.begin():
            acquire(session, d)
            proof(session, d)
        assert count_proof(d) == 2
    else:
        with pytest.raises(api().PublicationGuardError):
            with d.factory() as session, session.begin():
                acquire(session, d)


@pytest.mark.parametrize("role,permissions,allowed", [
    ("owner", [], True), ("custom", ["sync:run"], True),
    ("viewer", ["sync:run"], True), ("viewer", [], False),
    ("unknown-canary", ["sync:run"], False), ("admin", {}, False),
    ("admin", [True], False), ("admin", None, False),
])
def test_actual_membership_permission_union(data, role, permissions, allowed):
    d = data
    with Session(d.engine) as session, session.begin():
        session.execute(update(IamMembershipRow).where(IamMembershipRow.membership_id == d.org).values(
            role=role, permissions=permissions))
    with d.factory() as session, session.begin():
        if allowed:
            acquire(session, d)
        else:
            with pytest.raises(api().PublicationGuardError):
                acquire(session, d)
            session.rollback()


def test_stale_identity_map_does_not_authorize(data):
    d = data
    with d.factory() as session, session.begin():
        set_tenant_context(session, d.org)
        cached = session.get(IamMembershipRow, d.org)
        assert cached.is_active
        with Session(d.engine) as writer, writer.begin():
            mutate(writer, d, "membership_inactive")
        assert cached.is_active
        with pytest.raises(api().PublicationGuardError):
            acquire(session, d)
        session.rollback()


def test_metadata_projections_and_lock_order(data):
    d = data
    statements = []
    account_lock_ids = []
    engine = d.factory.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if "FROM marketplace_accounts" in statement and "FOR UPDATE" in statement:
            account_lock_ids.append(parameters["marketplace_account_id_1"])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with d.factory() as session, session.begin():
            args = arguments(d)
            other = arguments(d, token=True)
            args["accounts"] = other["accounts"] + args["accounts"]
            args["authorities"] += other["authorities"]
            guard = api().acquire_publication_guard(session, **args)
            now = guard.revalidate_before_write()
            assert now.tzinfo is not None
            proof(session, d)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    selects = [s for s in statements if s.startswith("SELECT")]
    assert all(not any(secret in s for secret in (
        "password_hash", "refresh_token_hash", "ciphertext", "nonce", "verifier", "key_version")) for s in selects)
    locks = [s for s in selects if "FOR " in s]
    tables = ["lk_users", "iam_memberships", "lk_sessions", "marketplace_accounts",
              "marketplace_accounts", "marketplace_account_credentials", "marketplace_account_ingestion_tokens"]
    assert all(f"FROM {table}" in sql for sql, table in zip(locks[:7], tables, strict=True))
    assert all("FOR SHARE" in locks[i] for i in (0, 1, 2, 5, 6))
    assert "FOR UPDATE" in locks[3] and "FOR UPDATE" in locks[4]
    assert account_lock_ids[:2] == [d.org, d.org + 100000]
    assert count_proof(d) == 2


@pytest.mark.parametrize("mutation", ["duplicate_account", "conflict_account", "duplicate_authority",
                                     "foreign_authority", "wrong_provider", "empty_permissions", "mutable_permissions"])
def test_binding_set_contract(data, mutation):
    d, g = data, api()
    args = arguments(d)
    if mutation == "duplicate_account":
        args["accounts"] *= 2
    elif mutation == "conflict_account":
        args["accounts"] += (replace(args["accounts"][0], credential_ref="other"),)
    elif mutation == "duplicate_authority":
        args["authorities"] *= 2
    elif mutation == "foreign_authority":
        args["authorities"] = (replace(args["authorities"][0], marketplace_account_id=d.org + 100000),)
    elif mutation == "wrong_provider":
        args["accounts"] = (replace(args["accounts"][0], provider="avito"),)
    elif mutation == "empty_permissions":
        args["required_permissions"] = frozenset()
    else:
        args["required_permissions"] = {"sync:run"}
    with d.factory() as session, session.begin():
        with pytest.raises(g.PublicationGuardError):
            g.acquire_publication_guard(session, **args)
        session.rollback()


@pytest.mark.parametrize("change", ["sql_tenant", "info_tenant", "nested", "second_guard", "pending", "isolation"])
def test_context_rejection(data, change):
    d, g = data, api()
    with d.factory() as session:
        with pytest.raises(g.PublicationGuardError):
            with session.begin():
                if change == "pending":
                    proof(session, d)
                    acquire(session, d)
                elif change == "isolation":
                    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                    acquire(session, d)
                else:
                    acquire(session, d)
                    proof(session, d)
                    if change == "sql_tenant":
                        session.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(d.org + 1)})
                    elif change == "info_tenant":
                        session.info["satorna_tenant_context"] = (session.get_transaction(), d.org + 1)
                    elif change == "nested":
                        session.begin_nested()
                    else:
                        acquire(session, d)
        assert count_proof(d) == 0


def test_session_and_pool_reuse_clears_guard_and_rejects_old_handle(data):
    d, g = data, api()
    with d.factory() as session:
        for _ in range(3):
            with session.begin():
                guard = acquire(session, d)
            with pytest.raises(g.PublicationGuardError):
                guard.revalidate_before_write()
        with session.begin():
            set_tenant_context(session, d.org + 200000)
            assert session.scalar(text("SELECT current_setting('app.organization_id')")) == str(d.org + 200000)
        with session.begin():
            acquire(session, d)


@pytest.mark.parametrize("change", ["credential_revoke", "absent"])
def test_credential_independent_read(data, change):
    d = data
    with Session(d.engine) as session, session.begin():
        if change == "absent":
            session.execute(text("DELETE FROM marketplace_account_credentials WHERE organization_id=:org"), {"org": d.org})
        else:
            mutate(session, d, change)
    with d.factory() as session, session.begin():
        acquire(session, d, read=True).revalidate_before_write()


def test_exact_old_uuid_and_generation_never_select_latest(data):
    d = data
    store.put_marketplace_credential(d.owner, "wb_api", {"token": "synthetic-new"})
    with d.factory() as session, session.begin():
        with pytest.raises(api().PublicationGuardError, match="^publication_authority_invalid$"):
            acquire(session, d)
        session.rollback()


@pytest.mark.parametrize("field,value", [
    ("principal_user", "wrong-user"), ("principal_session", "wrong-session"),
    ("principal_membership", 1), ("principal_org", 1),
    ("account_external", "different"), ("account_ref", "unexpected"),
    ("credential_uuid", None), ("credential_generation", 2),
    ("token_uuid", None), ("token_expiry", None),
])
def test_exact_live_expectations_not_nearby_authority(data, field, value):
    d, g = data, api()
    args = arguments(d, token=field.startswith("token"))
    if field.startswith("principal"):
        attr = {"principal_user": "user_id", "principal_session": "session_id",
                "principal_membership": "membership_id", "principal_org": "organization_id"}[field]
        args["principal"] = replace(args["principal"], **{attr: value})
    elif field.startswith("account"):
        attr = "external_account_id" if field == "account_external" else "credential_ref"
        args["accounts"] = (replace(args["accounts"][0], **{attr: value}),)
    else:
        attr = {"credential_uuid": "credential_id", "credential_generation": "generation",
                "token_uuid": "token_id", "token_expiry": "expires_at"}[field]
        if value is None:
            value = d.expiry + timedelta(microseconds=1) if field == "token_expiry" else uuid4()
        args["authorities"] = (replace(args["authorities"][0], **{attr: value}),)
    with d.factory() as session, session.begin():
        with pytest.raises(g.PublicationGuardError):
            g.acquire_publication_guard(session, **args)
        session.rollback()


@pytest.mark.parametrize("authority", ["credential", "token"])
def test_expiring_authority_during_account_lock_wait(data, authority):
    d = data
    expiry = datetime.now(UTC) + timedelta(seconds=0.8)
    credential_id = uuid4()
    with Session(d.engine) as session, session.begin():
        if authority == "token":
            # Issued token expiry is immutable. Create the short-lived authority
            # for this wait test instead of rewriting an existing token's history.
            d.token_id = uuid4()
            session.add(MarketplaceAccountIngestionTokenRow(token_id=d.token_id,
                        organization_id=d.org, marketplace_account_id=d.org + 100000,
                        provider="avito", scope="avito.browser_snapshot.write", verifier=b"v" * 32,
                        issued_at=datetime.now(UTC), expires_at=expiry,
                        binding_schema_version=1, binding_external_account_id="avito-seller",
                        binding_credential_ref=None, binding_version=1))
        else:
            session.add(MarketplaceAccountCredentialRow(credential_id=credential_id,
                        organization_id=d.org, marketplace_account_id=d.org + 100000, provider="avito",
                        credential_kind="avito_oauth_access", payload_schema_version=1,
                        algorithm="AES-256-GCM", key_version=1, aad_version=1,
                        nonce=b"n" * 12, ciphertext=b"synthetic-not-used" * 2, generation=1, expires_at=expiry))
    args = arguments(d, token=True)
    args["authorities"] = ((replace(args["authorities"][0], expires_at=expiry),) if authority == "token"
                           else (api().ExpectedCredential(d.org + 100000, credential_id,
                                                         "avito_oauth_access", 1, 1, expiry),))
    ready, pids = Event(), {}

    def publish():
        with d.factory() as session, session.begin():
            pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            api().acquire_publication_guard(session, **args)
            proof(session, d)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(d.engine) as locker, locker.begin():
            blocker = locker.scalar(text("SELECT pg_backend_pid()"))
            locker.execute(select(MarketplaceAccountRow.marketplace_account_id).where(
                MarketplaceAccountRow.marketplace_account_id == d.org + 100000).with_for_update())
            future = pool.submit(publish)
            assert ready.wait(5)
            with d.engine.connect() as observer:
                wait_blocked(observer, pids["publisher"], blocker)
                wait_expired(observer, expiry)
        with pytest.raises(api().PublicationGuardError, match="^publication_expired$"):
            future.result(timeout=8)
    assert count_proof(d) == 0


def test_finalizer_catches_context_change_during_flush(data):
    d = data

    def after_flush(session, context):
        session.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(d.org + 1)})

    with pytest.raises(api().PublicationGuardError, match="^publication_context_invalid$"):
        with d.factory() as session, session.begin():
            acquire(session, d)
            event.listen(session, "after_flush_postexec", after_flush)
            proof(session, d)
    assert count_proof(d) == 0


def test_no_stale_session_listeners_or_poison_after_rollback(data):
    d, g = data, api()
    with d.factory() as session:
        original = len(session.dispatch.before_commit)
        for _ in range(3):
            with session.begin():
                handle = acquire(session, d)
                assert len(session.dispatch.before_commit) == original + 1
                session.rollback()
            with pytest.raises(g.PublicationGuardError):
                handle.revalidate_before_write()
        with session.begin():
            # An unguarded later transaction must not consult the ended principal.
            session.execute(update(LkSessionRow).where(LkSessionRow.session_id == f"login-{d.org}").values(
                revoked_at=func.clock_timestamp()))
        assert len(session.dispatch.before_commit) == original + 1


def test_database_commit_failure_rolls_back_publication_and_audit(data):
    d = data
    table = "synthetic_guard_commit_" + uuid4().hex
    role = d.factory.kw["bind"].url.username
    with d.engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE TABLE {table} (id integer PRIMARY KEY, parent integer, "
                                  f"FOREIGN KEY (parent) REFERENCES {table}(id) DEFERRABLE INITIALLY DEFERRED)")
        connection.exec_driver_sql(f"GRANT INSERT ON {table} TO {role}")
    with d.factory() as session:
        # The driver owns physical COMMIT errors. The guard owns and sanitizes
        # errors from its acquisition/revalidation/final-flush operations.
        with pytest.raises(IntegrityError):
            with session.begin():
                acquire(session, d)
                proof(session, d)
                session.execute(text(f"INSERT INTO {table} (id, parent) VALUES (1, 2)"))
        session.rollback()
        with session.begin():
            acquire(session, d, read=True)
    assert count_proof(d) == 0


def test_finalizer_rejects_orm_auth_mutation_left_by_after_flush_postexec(data):
    d, g = data, api()
    mutations = []
    with d.factory() as session:
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            with session.begin():
                acquire(session, d)
                user = session.get(LkUserRow, f"owner-{d.org}")

                def leave_pending_auth(current_session, context):
                    if not mutations:
                        mutations.append("pending inactive user")
                        user.is_active = False

                event.listen(session, "after_flush_postexec", leave_pending_auth)
                proof(session, d)
        assert mutations == ["pending inactive user"]
        assert count_proof(d) == 0


@pytest.mark.parametrize("mutate_user", [True, False])
def test_finalizer_rejects_any_later_instance_before_commit_callback(data, mutate_user):
    d, g = data, api()
    called = []
    with d.factory() as session:
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            with session.begin():
                acquire(session, d)
                user = session.get(LkUserRow, f"owner-{d.org}")

                def later_callback(current_session):
                    called.append("later")
                    if mutate_user:
                        user.is_active = False

                event.listen(session, "before_commit", later_callback)
                proof(session, d)
        # Reject at guard entry, before an unsupported callback can do any work.
        assert called == []
        assert count_proof(d) == 0


@pytest.mark.parametrize("placement", ["instance_before", "class_before", "class_after"])
def test_finalizer_allows_trusted_callbacks_effectively_before_guard(data, placement):
    d = data
    calls = []

    class CallerSession(Session):
        pass

    def trusted_callback(session):
        calls.append("trusted")
        session.add(LkAuditEventRow(organization_id=d.org, action="synthetic.trusted_callback",
                                   object_type="synthetic", object_id="trusted"))

    with CallerSession(d.factory.kw["bind"]) as session:
        target = session if placement == "instance_before" else CallerSession
        if placement != "class_after":
            event.listen(target, "before_commit", trusted_callback)
        try:
            with session.begin():
                acquire(session, d)
                if placement == "class_after":
                    # SQLAlchemy invokes class listeners before instance ones,
                    # including class listeners registered after guard creation.
                    event.listen(target, "before_commit", trusted_callback)
                proof(session, d)
        finally:
            event.remove(target, "before_commit", trusted_callback)
    assert calls == ["trusted"]
    assert count_proof(d) == 2
    with Session(d.engine) as session:
        assert session.scalar(select(func.count()).select_from(LkAuditEventRow).where(
            LkAuditEventRow.organization_id == d.org,
            LkAuditEventRow.action == "synthetic.trusted_callback")) == 1


def test_finalizer_checks_effective_order_again_on_reused_session_and_poisons_handle(data):
    d, g = data, api()

    def later_callback(session):
        pass

    with d.factory() as session:
        with session.begin():
            acquire(session, d, read=True)
        # Registration precedes this root's acquisition but follows the retained
        # guard callback in effective order, so the second commit must deny.
        event.listen(session, "before_commit", later_callback)
        session.begin()
        guard = acquire(session, d)
        proof(session, d)
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            session.commit()
        event.remove(session, "before_commit", later_callback)
        # Repairing callback order cannot rehabilitate a failed root transaction.
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            guard.revalidate_before_write()
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            session.commit()
        session.rollback()
        with session.begin():
            acquire(session, d, read=True)
    assert count_proof(d) == 0


@pytest.mark.parametrize("pending_kind", ["new", "dirty", "deleted"])
def test_finalizer_rejects_orm_work_created_during_final_validation(data, pending_kind):
    d, g = data, api()
    with d.factory() as session:
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            with session.begin():
                acquire(session, d)
                user = session.get(LkUserRow, f"owner-{d.org}")
                proof(session, d)
                added = []

                def during_validation(execute_state):
                    # A supported Session ORM event queues work when final
                    # validation reads DB time. It performs no SQL of its own.
                    if not added and "clock_timestamp()" in str(execute_state.statement):
                        added.append("pending")
                        if pending_kind == "dirty":
                            user.is_active = False
                        elif pending_kind == "new":
                            proof(session, d)
                        else:
                            session.delete(user)

                event.listen(session, "do_orm_execute", during_validation)
        assert added == ["pending"]
        assert count_proof(d) == 0


def wait_expired(connection, expiry):
    deadline = monotonic() + 5
    while connection.scalar(select(func.clock_timestamp())) <= expiry:
        assert monotonic() < deadline, "synthetic expiry did not elapse"
        sleep(0.01)


@pytest.mark.parametrize("wait_at", ["account", "membership", "domain", "flush"])
def test_database_clock_expiry_after_lock_or_flush_wait_rolls_back(data, wait_at):
    d = data
    expiry = datetime.now(UTC) + timedelta(seconds=0.8)
    with Session(d.engine) as session, session.begin():
        session.execute(update(LkSessionRow).where(LkSessionRow.session_id == f"login-{d.org}").values(expires_at=expiry))
        session.add(LkAuditEventRow(organization_id=d.org, action="synthetic.lock", object_type="synthetic", object_id="lock"))
    ready = Event()
    pids = {}

    def publish():
        with d.factory() as session, session.begin():
            pids["publisher"] = session.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            guard = acquire(session, d)
            if wait_at in ("domain", "flush"):
                row = session.scalar(select(LkAuditEventRow).where(
                    LkAuditEventRow.organization_id == d.org, LkAuditEventRow.action == "synthetic.lock"))
                if wait_at == "domain":
                    session.execute(select(LkAuditEventRow.event_id).where(
                        LkAuditEventRow.event_id == row.event_id).with_for_update())
                    guard.revalidate_before_write()
                else:
                    row.reason = "synthetic pending flush"
            proof(session, d)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(d.engine) as locker, locker.begin():
            pids["locker"] = locker.scalar(text("SELECT pg_backend_pid()"))
            if wait_at == "account":
                locker.execute(select(MarketplaceAccountRow.marketplace_account_id).where(
                    MarketplaceAccountRow.marketplace_account_id == d.org).with_for_update())
            elif wait_at == "membership":
                locker.execute(select(IamMembershipRow.membership_id).where(
                    IamMembershipRow.membership_id == d.org).with_for_update())
            else:
                locker.execute(select(LkAuditEventRow.event_id).where(
                    LkAuditEventRow.organization_id == d.org, LkAuditEventRow.action == "synthetic.lock").with_for_update())
            future = pool.submit(publish)
            assert ready.wait(5)
            with d.engine.connect() as observer:
                wait_blocked(observer, pids["publisher"], pids["locker"])
                wait_expired(observer, expiry)
        with pytest.raises(api().PublicationGuardError, match="^publication_expired$"):
            future.result(timeout=8)
    assert count_proof(d) == 0
