"""Actual non-owner FORCE RLS, new ACL intersection and atomic grant replay."""
# Root transaction and role boundaries are intentional.

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_sku_override_lifecycle as lifecycle
from tests import test_sku_override_schema as schema
from tests.test_orders_schema_integration import runtime_script

cluster = candidate.cluster
db = schema.db
V, H, A = schema.TABLES
scope = schema.scope


def assert_acl(c, role, *, reader=False):
    for table in schema.TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            allowed = privilege == "SELECT" or (not reader and (privilege == "INSERT" or (privilege == "UPDATE" and table == H)))
            p = {"r": role, "t": table, "p": privilege}
            assert c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), p).scalar_one() == allowed
            assert not c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), {**p, "p": privilege + " WITH GRANT OPTION"}).scalar_one()
    for function in schema.PURE + schema.TRIGGERS:
        assert c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"), {"r": role, "f": function}).scalar_one() == (function in schema.PURE)
        assert not c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE WITH GRANT OPTION')"), {"r": role, "f": function}).scalar_one()


def test_real_runtime_roles_narrow_grants_and_allowed_transaction(db):
    owner, runtime = db
    with owner.connect() as c:
        assert_acl(c, runtime.url.username)
        assert c.execute(text("SELECT rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=:r"), {"r": runtime.url.username}).one() == (False,) * 6
        assert c.execute(text("SELECT count(*) FROM pg_class WHERE relname=ANY(:tables) AND relowner=(SELECT oid FROM pg_roles WHERE rolname=:r)"), {"tables": list(schema.TABLES), "r": runtime.url.username}).scalar_one() == 0
    sku = lifecycle.fresh(db)
    with runtime.begin() as c:
        scope(c)
        lifecycle.participant(c, lifecycle.prepare(c, sku))
    with runtime.begin() as c:
        scope(c)
        assert lifecycle.counts(c, sku) == (1, 1, 1)
    with runtime.connect() as c:
        assert lifecycle.counts(c, sku) == (0, 0, 0)


@pytest.mark.parametrize("org,account", [(None, None), (7, None), (None, 42), (8, 42), (7, 43), (7, 45), (0, 42), (7, 0), ("07", 42), (7, "+42"), (7, "42 "), (7, "2147483648"), (7, "1" * 100)])
def test_context_scope_and_provider_fail_closed(db, org, account):
    sku = lifecycle.fresh(db)
    with db[1].begin() as c:
        scope(c)
        p = lifecycle.prepare(c, sku)
        lifecycle.participant(c, p)
    with db[1].begin() as c:
        if org is not None:
            c.execute(text("SELECT set_config('app.organization_id',:v,true)"), {"v": str(org)})
        if account is not None:
            c.execute(text("SELECT set_config('app.marketplace_account_id',:v,true)"), {"v": str(account)})
        assert lifecycle.counts(c, sku) == (0, 0, 0)
    with pytest.raises(DBAPIError), db[1].begin() as c:
        if org is not None:
            c.execute(text("SELECT set_config('app.organization_id',:v,true)"), {"v": str(org)})
        if account is not None:
            c.execute(text("SELECT set_config('app.marketplace_account_id',:v,true)"), {"v": str(account)})
        lifecycle.insert_version(c, {**p, "revision": 2, "parent_revision": 1})


@pytest.mark.parametrize("table,operation", [(t, op) for t in schema.TABLES for op in ("DELETE", "TRUNCATE", "UPDATE", "REFERENCES", "TRIGGER") if (t, op) != (H, "UPDATE")])
def test_runtime_denied_history_and_ddl_privileges(db, table, operation):
    role = db[1].url.username
    with db[0].connect() as c:
        assert not c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), {"r": role, "t": table, "p": operation}).scalar_one()
    if operation in ("REFERENCES", "TRIGGER"):
        return  # Privilege boundary above; no unrelated synthetic DDL needed.
    statement = f"TRUNCATE {table}" if operation == "TRUNCATE" else f"DELETE FROM {table} WHERE false" if operation == "DELETE" else f"UPDATE {table} SET organization_id=organization_id WHERE false"
    with pytest.raises(DBAPIError), db[1].begin() as c:
        scope(c)
        c.exec_driver_sql(statement)


@pytest.mark.parametrize("org,account", [(None, None), (8, 42), (7, 43)])
def test_policies_independently_deny_select_insert_update_delete(db, org, account):
    sku = lifecycle.fresh(db)
    with db[1].begin() as c:
        scope(c)
        p = lifecycle.prepare(c, sku)
        lifecycle.participant(c, p)
    # Rollback-only diagnostic removes guards and broadens only our role in this
    # disposable DB to isolate USING/WITH CHECK from the independent guards.
    with db[0].connect() as c:
        tx = c.begin()
        try:
            for table in schema.TABLES:
                c.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
                c.exec_driver_sql(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO {db[1].url.username}")
            c.exec_driver_sql(f"SET LOCAL ROLE {db[1].url.username}")
            if org is not None:
                scope(c, org, account)
            for table in schema.TABLES:
                assert c.execute(text(f"SELECT count(*) FROM {table} WHERE catalog_sku_id=:sku"), {"sku": sku}).scalar_one() == 0
                assert c.execute(text(f"UPDATE {table} SET organization_id=organization_id WHERE catalog_sku_id=:sku RETURNING organization_id"), {"sku": sku}).all() == []
                assert c.execute(text(f"DELETE FROM {table} WHERE catalog_sku_id=:sku RETURNING organization_id"), {"sku": sku}).all() == []
            with pytest.raises(DBAPIError):
                lifecycle.head(c, {**p, "catalog_sku_id": 101})
        finally:
            tx.rollback()


def test_runtime_script_replay_narrows_columns_and_rolls_back_failures(db):
    owner, runtime = db
    role = runtime.url.username
    with owner.begin() as c:
        c.exec_driver_sql(f"GRANT UPDATE(request_checksum),REFERENCES(command_id) ON {V} TO {role} WITH GRANT OPTION")
        c.exec_driver_sql(f"GRANT UPDATE(event_kind) ON {A} TO PUBLIC")
    failed = runtime_script(owner, role, fail_after_broad=True)
    assert failed.returncode != 0
    result = runtime_script(owner, role)
    assert result.returncode == 0, result.stderr
    result = runtime_script(owner, role)
    assert result.returncode == 0, result.stderr
    with owner.connect() as c:
        assert_acl(c, role)
        assert not c.execute(text("SELECT has_column_privilege(:r,:t,'request_checksum','UPDATE')"), {"r": role, "t": V}).scalar_one()
    with owner.begin() as c:
        c.exec_driver_sql(f"ALTER TABLE {H} NO FORCE ROW LEVEL SECURITY")
    try:
        result = runtime_script(owner, role)
        assert result.returncode != 0
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(f"ALTER TABLE {H} FORCE ROW LEVEL SECURITY")
    with owner.connect() as c:
        assert_acl(c, role)


def test_inherited_hostile_defaults_intersection_preserves_old_acls(cluster):
    broad, reader = ("sku_override_" + label + "_" + uuid4().hex for label in ("broad", "reader"))
    with candidate.disposable_database(cluster, (broad, reader)) as database:
        result = candidate.migrate(database.url, "upgrade", schema.PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {broad} WITH GRANT OPTION")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {reader} WITH GRANT OPTION")
                c.exec_driver_sql("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {broad},{reader} WITH GRANT OPTION")
                before = schema.old_acls(c)
            result = candidate.migrate(database.url, "upgrade", schema.TARGET)
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                assert schema.old_acls(c) == before
                assert_acl(c, broad)
                assert_acl(c, reader, reader=True)
                assert c.execute(text("SELECT count(*) FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.relname=ANY(:tables) AND (a.grantee=0 OR a.is_grantable)"), {"tables": list(schema.TABLES)}).scalar_one() == 0
        finally:
            owner.dispose()


def test_column_acl_intersection_and_hidden_nonempty_downgrade(cluster, monkeypatch):
    column, relation_owner = ("sku_override_" + label + "_" + uuid4().hex for label in ("column", "owner"))
    with candidate.disposable_database(cluster, (column, relation_owner)) as database:
        result = candidate.migrate(database.url, "upgrade", schema.PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            migration = schema.scripts().get_revision(schema.TARGET).module
            with owner.begin() as c:
                schema.seed(c)
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {column},{relation_owner}")
                before = schema.old_acls(c)
                original = migration.op.execute

                def inject(statement, *args, **kwargs):
                    result = original(statement, *args, **kwargs)
                    if statement == migration.MODEL:
                        original(f"GRANT SELECT(revision),INSERT(command_id),UPDATE(request_checksum),REFERENCES(actor_membership_id) ON {V} TO {column} WITH GRANT OPTION")
                        original(f"GRANT UPDATE(event_kind) ON {A} TO PUBLIC")
                        for table in schema.TABLES:
                            original(f"ALTER TABLE {table} OWNER TO {relation_owner}")
                    return result

                with Operations.context(MigrationContext.configure(c)), monkeypatch.context() as patch:
                    patch.setattr(migration.op, "execute", inject)
                    migration.upgrade()
                assert schema.old_acls(c) == before
                for col, privilege, want in (("revision", "SELECT", True), ("command_id", "INSERT", True), ("request_checksum", "UPDATE", False), ("actor_membership_id", "REFERENCES", False)):
                    p = {"r": column, "t": V, "c": col, "p": privilege}
                    assert c.execute(text("SELECT has_column_privilege(:r,:t,:c,:p)"), p).scalar_one() == want
                    assert not c.execute(text("SELECT has_column_privilege(:r,:t,:c,:p)"), {**p, "p": privilege + " WITH GRANT OPTION"}).scalar_one()
                for role in (column, relation_owner):
                    for function in schema.PURE:
                        assert c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"), {"r": role, "f": function}).scalar_one()
                scope(c)
                lifecycle.participant(c, lifecycle.prepare(c, 99))
            with pytest.raises(DBAPIError), owner.begin() as c:
                scope(c, account=43)
                with Operations.context(MigrationContext.configure(c)):
                    migration.downgrade()
            with pytest.raises(DBAPIError), owner.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {relation_owner}")
                assert c.exec_driver_sql(f"SELECT count(*) FROM {V}").scalar_one() == 0
                with Operations.context(MigrationContext.configure(c)):
                    migration.downgrade()
            with owner.connect() as c:
                assert lifecycle.counts(c, 99) == (1, 1, 1)
        finally:
            owner.dispose()
