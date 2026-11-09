"""Actual local GUCs and root cleanup in an owned disposable PostgreSQL DB."""
# Distinct root contexts make completion and reborrow assertions explicit.
# ruff: noqa: SIM117

import traceback
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.infra.db import set_tenant_context
from tests import test_orders_schema_candidate as candidate
from tests import test_publication_guard_postgres as guard_tests
from tests.test_marketplace_account_context import api

cluster = candidate.cluster
pg_database = guard_tests.pg_database
pg_store = guard_tests.pg_store
data = guard_tests.data
KEY = "satorna_marketplace_account_context"
SETTINGS = text("SELECT current_setting('app.organization_id', true), "
                "current_setting('app.marketplace_account_id', true)")


class SyntheticBase(DeclarativeBase):
    pass


class SyntheticRow(SyntheticBase):
    __tablename__ = "synthetic_context_row"
    id: Mapped[int] = mapped_column(primary_key=True)


@pytest.fixture(scope="module")
def context_engine(cluster):
    role = "account_context_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True,
                                pool_size=1, max_overflow=0)
        try:
            SyntheticBase.metadata.create_all(owner)
            with owner.begin() as c:
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT SELECT, INSERT, DELETE ON synthetic_context_row TO {role}")
            yield runtime
        finally:
            runtime.dispose()
            owner.dispose()


def apply(session, org=1, account=11):
    api()[0](session, organization_id=org, marketplace_account_id=account)


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_local_scope_repeat_root_completion_and_pool_reborrow(context_engine, finish):
    with Session(context_engine) as session:
        session.info["unrelated"] = "preserved"
        session.begin()
        assert all(x in (None, "") for x in session.execute(SETTINGS).one())
        apply(session)
        root = session.get_transaction()
        assert session.execute(SETTINGS).one() == ("1", "11")
        assert session.get_transaction() is root
        apply(session)
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session, account=12)
        # A failure requires rollback; commit coverage uses a fresh clean root.
        session.rollback()
        session.begin()
        apply(session)
        getattr(session, finish)()
        assert KEY not in session.info
        assert session.info["unrelated"] == "preserved"
        with session.begin():
            assert all(x in (None, "") for x in session.execute(SETTINGS).one())
            apply(session, 2, 12)
            assert session.execute(SETTINGS).one() == ("2", "12")
    with Session(context_engine) as reborrowed, reborrowed.begin():
        assert KEY not in reborrowed.info
        assert all(x in (None, "") for x in reborrowed.execute(SETTINGS).one())
        apply(reborrowed, 2**31 - 1, 2**31 - 1)
        assert reborrowed.execute(SETTINGS).one() == ("2147483647", "2147483647")


@pytest.mark.parametrize("setting", ["app.organization_id", "app.marketplace_account_id"])
@pytest.mark.parametrize("value", ["2", "01", " 1", "-1", "0", "true", "2147483648",
                                   "synthetic-secret-canary"])
@pytest.mark.parametrize("repeat", [False, True])
def test_conflict_or_tamper_never_overwrites_settings(context_engine, setting, value, repeat):
    with Session(context_engine) as session, session.begin():
        if repeat:
            apply(session)
        session.execute(text("SELECT set_config(:setting, :value, true)"),
                        {"setting": setting, "value": value})
        before = session.execute(SETTINGS).one()
        with pytest.raises(api()[1], match="^account_context_invalid$") as caught:
            apply(session)
        assert session.execute(SETTINGS).one() == before
        assert "synthetic-secret-canary" not in "".join(traceback.format_exception(caught.value))
        session.rollback()


@pytest.mark.parametrize("repeat", [False, True])
def test_connection_savepoint_denied_before_context_sql(context_engine, repeat):
    with Session(context_engine) as session, session.begin():
        if repeat:
            apply(session)
        connection = session.connection()
        nested = connection.begin_nested()
        assert not session.in_nested_transaction()
        statements = []
        def capture(c, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", capture)
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session)
        event.remove(connection, "before_cursor_execute", capture)
        assert statements == []
        nested.rollback()
        session.rollback()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_wrong_isolation_rejected(context_engine, isolation):
    with Session(context_engine.execution_options(isolation_level=isolation)) as session, session.begin():
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session)
        assert all(x in (None, "") for x in session.execute(SETTINGS).one())
        session.rollback()


@pytest.mark.parametrize("configuration", ["engine", "execution_options"])
def test_dbapi_autocommit_is_not_a_transaction_local_root(context_engine, configuration):
    engine = (create_engine(context_engine.url, isolation_level="AUTOCOMMIT")
              if configuration == "engine"
              else context_engine.execution_options(isolation_level="AUTOCOMMIT"))
    try:
        with Session(engine) as session, session.begin():
            # PostgreSQL still reports READ COMMITTED in DBAPI autocommit mode.
            assert session.connection().get_isolation_level() == "READ COMMITTED"
            with pytest.raises(api()[1], match="^account_context_invalid$"):
                apply(session)
            assert KEY not in session.info
            session.rollback()
    finally:
        if configuration == "engine":
            engine.dispose()


@pytest.mark.parametrize("state", ["new", "dirty", "deleted"])
def test_pending_orm_work_not_autoflushed(context_engine, state):
    with Session(context_engine) as session, session.begin():
        row = SyntheticRow(id=1)
        session.add(row)
        if state != "new":
            session.flush()
            if state == "dirty":
                row.id = 2
            else:
                session.delete(row)
        def forbidden_flush(*args):
            pytest.fail("helper autoflushed caller work")
        event.listen(session, "before_flush", forbidden_flush)
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session)
        assert all(x in (None, "") for x in session.execute(SETTINGS).one())
        session.rollback()


def test_existing_tenant_semantics_and_own_marker_cleanup(context_engine):
    with Session(context_engine) as session:
        session.begin()
        set_tenant_context(session, 1)
        tenant_marker = session.info["satorna_tenant_context"]
        assert session.execute(SETTINGS).one()[1] in (None, "")
        apply(session)
        assert session.info["satorna_tenant_context"] == tenant_marker
        session.commit()
        assert KEY not in session.info
        assert session.info["satorna_tenant_context"] == tenant_marker


def test_sql_failure_exposes_only_safe_error(context_engine):
    with Session(context_engine) as session, session.begin():
        def fail(c, cursor, statement, parameters, context, executemany):
            if "set_config('app.organization_id'" in statement:
                c.exec_driver_sql("SELECT CAST('synthetic-secret-canary' AS integer)")
        connection = session.connection()
        event.listen(connection, "before_cursor_execute", fail)
        with pytest.raises(api()[1], match="^account_context_failed$") as caught:
            apply(session)
        event.remove(connection, "before_cursor_execute", fail)
        assert caught.value.__cause__ is None
        assert "synthetic-secret-canary" not in repr(caught.value)
        assert "synthetic-secret-canary" not in "".join(traceback.format_exception(caught.value))
        assert KEY not in session.info
        session.rollback()


@pytest.mark.parametrize("marker", [None, (), (None, 1, 11), ("root", 1, 12),
                                    ("root", True, 11)])
def test_malformed_or_foreign_root_marker_denies(context_engine, marker):
    with Session(context_engine) as session, session.begin():
        if marker and marker[0] == "root":
            marker = (session.get_transaction(), *marker[1:])
        session.info[KEY] = marker
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session)
        assert all(x in (None, "") for x in session.execute(SETTINGS).one())
        session.rollback()


def test_matching_preexisting_context_is_legal(context_engine):
    with Session(context_engine) as session, session.begin():
        session.execute(text("SELECT set_config('app.organization_id', '1', true), "
                             "set_config('app.marketplace_account_id', '11', true)"))
        apply(session)
        assert session.execute(SETTINGS).one() == ("1", "11")


def test_actual_session_savepoint_denies(context_engine):
    with Session(context_engine) as session, session.begin(), session.begin_nested():
        with pytest.raises(api()[1], match="^account_context_invalid$"):
            apply(session)
        assert all(x in (None, "") for x in session.execute(SETTINGS).one())
        session.rollback()


def test_context_composes_with_guard_and_does_not_change_its_cleanup(data):
    with data.factory() as session:
        with session.begin():
            guard = guard_tests.acquire(session, data)
            apply(session, data.org, data.org)
            guard.revalidate_before_write()
            guard_tests.proof(session, data)
        assert KEY not in session.info
        with pytest.raises(guard_tests.api().PublicationGuardError):
            guard.revalidate_before_write()
    assert guard_tests.count_proof(data) == 2


def test_context_alone_does_not_make_inactive_principal_authorized(data):
    with Session(data.engine) as session, session.begin():
        guard_tests.mutate(session, data, "user_inactive")
    with pytest.raises(guard_tests.api().PublicationGuardError):
        with data.factory() as session, session.begin():
            apply(session, data.org, data.org)
            guard_tests.acquire(session, data)
            guard_tests.proof(session, data)
    assert guard_tests.count_proof(data) == 0


@pytest.mark.parametrize("join_mode", ["default", "rollback_only"])
def test_external_connection_root_denied_without_changing_owner_work(context_engine, join_mode):
    options = {} if join_mode == "default" else {"join_transaction_mode": join_mode}
    with context_engine.connect() as connection:
        external = connection.begin()
        try:
            connection.execute(text("INSERT INTO synthetic_context_row (id) VALUES (77)"))
            connection.execute(text("SELECT set_config('app.organization_id', '1', true), "
                                    "set_config('app.marketplace_account_id', '', true)"))
            before = connection.execute(SETTINGS).one()
            statements = []

            def capture(c, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            with Session(connection, **options) as session:
                session.begin()
                event.listen(connection, "before_cursor_execute", capture)
                try:
                    try:
                        apply(session)
                    except api()[1] as error:
                        assert str(error) == "account_context_invalid"
                    else:
                        # RED witness: logical Session completion leaves the
                        # externally owned physical transaction/context alive.
                        session.commit()
                        assert external.is_active
                        assert KEY not in session.info
                        assert connection.execute(SETTINGS).one() == ("1", "11")
                        pytest.fail("external root retains account context after Session commit")
                finally:
                    event.remove(connection, "before_cursor_execute", capture)
                assert statements == []
                assert KEY not in session.info
                session.rollback()
            assert external.is_active and connection.get_transaction() is external
            assert connection.execute(SETTINGS).one() == before
            assert connection.scalar(text("SELECT count(*) FROM synthetic_context_row WHERE id=77")) == 1
        finally:
            external.rollback()
        assert all(value in (None, "") for value in connection.execute(SETTINGS).one())
        assert connection.scalar(text("SELECT count(*) FROM synthetic_context_row WHERE id=77")) == 0


@pytest.mark.parametrize("join_mode", ["conditional_savepoint", "rollback_only",
                                      "control_fully", "create_savepoint"])
@pytest.mark.parametrize("external_active", [False, True])
def test_every_connection_binding_denied_before_sql(context_engine, join_mode, external_active):
    with context_engine.connect() as connection:
        external = connection.begin() if external_active else None
        statements = []

        def capture(c, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        try:
            with Session(connection, join_transaction_mode=join_mode) as session, session.begin():
                event.listen(connection, "before_cursor_execute", capture)
                try:
                    with pytest.raises(api()[1], match="^account_context_invalid$"):
                        apply(session)
                finally:
                    event.remove(connection, "before_cursor_execute", capture)
                assert statements == []
                assert KEY not in session.info
                session.rollback()
            assert connection.in_transaction() is external_active
            if external is not None:
                assert connection.get_transaction() is external and external.is_active
        finally:
            if external is not None:
                external.rollback()
