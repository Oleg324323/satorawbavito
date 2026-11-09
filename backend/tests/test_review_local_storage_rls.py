"""Real nonowner privileges and independent forced-RLS account isolation."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as facts
from tests import test_review_local_storage_schema as storage
from tests.test_orders_schema_integration import runtime_script
from tests.test_review_local_storage_lifecycle import (
    canonical,
    decision_command,
    digest,
    execute,
    literal_policy,
    policy_command,
    preparation,
    setup_policy,
    snapshot,
)
from tests.test_review_local_storage_schema import (
    HEADS,
    PREVIOUS,
    TABLES,
    TARGET,
    scope,
)
from tests.test_review_lossless_migration import scripts

cluster = candidate.cluster
db = storage.db


def test_runtime_is_nonowner_without_bypass_or_inheritance(db):
    with db[1].connect() as c:
        assert c.exec_driver_sql("SELECT rolsuper,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=current_user").one() == (False, False, False)
        assert c.execute(text("SELECT count(*) FROM pg_class WHERE relname=ANY(:t) AND relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)"),
                         {"t": list(TABLES)}).scalar_one() == 0


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"])
def test_runtime_privilege_intersection(db, table, privilege):
    with db[0].connect() as c:
        params = {"r": db[1].url.username, "t": table, "p": privilege}
        assert c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), params).scalar_one() == (
            privilege in {"SELECT", "INSERT"} or (privilege == "UPDATE" and table in HEADS))
        params["p"] = privilege + " WITH GRANT OPTION"
        assert c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), params).scalar_one() is False


@pytest.mark.parametrize("signature", ["review_local_account_lock()", "review_local_row_guard()", "review_local_validate()"])
def test_runtime_cannot_execute_trigger_helpers(db, signature):
    with db[0].connect() as c:
        assert c.execute(text("SELECT has_function_privilege(:r,:s,'EXECUTE')"),
                         {"r": db[1].url.username, "s": signature}).scalar_one() is False


def test_runtime_script_replay_is_idempotent(db):
    result = runtime_script(db[0], db[1].url.username)
    assert result.returncode == 0, result.stderr
    with db[0].connect() as c:
        for table in TABLES:
            assert c.execute(text("SELECT has_table_privilege(:r,:t,'DELETE')"),
                             {"r": db[1].url.username, "t": table}).scalar_one() is False


@pytest.fixture(scope="module")
def populated(db):
    with db[1].begin() as c:
        setup_policy(c)
        publish, _ = preparation(c)
        execute(c, publish)
        execute(c, decision_command(c, publish))
    return db


@pytest.mark.parametrize("org,account", [(None, None), (91002, 91201), (91001, 91102), ("091001", 91101)])
def test_rls_hides_each_relation_with_ordinary_triggers_disabled(populated, org, account):
    owner, runtime = populated
    with owner.connect() as c:
        transaction = c.begin()
        try:
            for table in TABLES:
                c.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
            scope(c)
            for table in TABLES:
                assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() > 0
            c.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                      {"org": "" if org is None else str(org), "account": "" if account is None else str(account)})
            for table in TABLES:
                assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
        finally:
            transaction.rollback()


@pytest.mark.parametrize("org,account", [(None, None), (91002, 91201), (91001, 91102)])
def test_independent_rls_with_check_rejects_other_account_insert(populated, org, account):
    owner, runtime = populated
    request = policy_command()
    p = literal_policy(request["input"]["policy"])
    payload = canonical(request["input"]["policy"])
    with owner.connect() as c:
        transaction = c.begin()
        try:
            for table in TABLES:
                c.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            scope(c)
            audit_id = c.exec_driver_sql("SELECT event_id FROM review_local_audit LIMIT 1").scalar_one()
            p.update(policy_payload=payload, policy_checksum=digest(payload), actor_membership_id=77,
                     created_at="2026-09-09T12:00:02.987654Z", audit_event_id=audit_id)
            c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
            c.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                      {"org": "" if org is None else str(org), "account": "" if account is None else str(account)})
            with pytest.raises(DBAPIError) as caught:
                facts.insert(c, "review_policy_versions", p)
            assert caught.value.orig.sqlstate == "42501"
            assert "row-level security" in caught.value.orig.diag.message_primary
        finally:
            transaction.rollback()


@pytest.mark.parametrize("table", sorted(HEADS))
@pytest.mark.parametrize("org,account", [(None, None), (91002, 91201), (91001, 91102)])
def test_independent_head_update_using_hides_wrong_scope(populated, table, org, account):
    owner, runtime = populated
    with owner.connect() as c:
        tx = c.begin()
        try:
            before = snapshot(c)
            c.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
            # Correct-scope positive UPDATE proves permission, independent of guards.
            assert c.exec_driver_sql(f"UPDATE {table} SET version=version RETURNING head_id").all()
            c.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                      {"org": "" if org is None else str(org), "account": "" if account is None else str(account)})
            assert c.exec_driver_sql(f"UPDATE {table} SET version=version+1 RETURNING head_id").all() == []
        finally:
            tx.rollback()
        assert snapshot(c) == before


@pytest.mark.parametrize("table", sorted(HEADS))
@pytest.mark.parametrize("assignment", ["organization_id=91002", "marketplace_account_id=91102"])
def test_independent_head_update_with_check_rejects_scope_escape(populated, table, assignment):
    owner, runtime = populated
    with owner.connect() as c:
        tx = c.begin()
        try:
            before = snapshot(c)
            c.exec_driver_sql(f"ALTER TABLE {table} DISABLE TRIGGER USER")
            c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
            assert c.exec_driver_sql(f"UPDATE {table} SET version=version RETURNING head_id").all()
            with pytest.raises(DBAPIError) as caught:
                c.exec_driver_sql(f"UPDATE {table} SET {assignment}")
            assert caught.value.orig.sqlstate == "42501"
            assert "row-level security" in caught.value.orig.diag.message_primary
        finally:
            tx.rollback()
        assert snapshot(c) == before


@pytest.mark.parametrize("table", TABLES)
def test_runtime_script_requires_every_new_forced_rls_flag(db, table):
    with db[0].begin() as c:
        c.exec_driver_sql(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    try:
        result = runtime_script(db[0], db[1].url.username)
        assert result.returncode != 0
        assert "canonical tables without forced RLS" in result.stderr
    finally:
        with db[0].begin() as c:
            c.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def test_runtime_failure_is_atomic_and_cannot_leave_broad_grants(db):
    owner, runtime = db
    with owner.connect() as c:
        before = c.execute(text("SELECT relname,relacl::text FROM pg_class WHERE relname=ANY(:t) ORDER BY relname"),
                           {"t": list(TABLES)}).all()
        defaults = c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all()
    result = runtime_script(owner, runtime.url.username, fail_after_broad=True)
    assert result.returncode != 0
    with owner.connect() as c:
        assert c.execute(text("SELECT relname,relacl::text FROM pg_class WHERE relname=ANY(:t) ORDER BY relname"),
                         {"t": list(TABLES)}).all() == before
        assert c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all() == defaults


@pytest.mark.parametrize("phase", ["migration_intersection", "runtime_script"])
def test_hostile_column_grants_and_grant_options_are_narrowed(db, phase):
    owner, runtime = db
    role = runtime.url.username
    with owner.begin() as c:
        for table in TABLES:
            c.exec_driver_sql(f"GRANT SELECT (organization_id),INSERT (organization_id),UPDATE (organization_id),REFERENCES (organization_id) ON {table} TO PUBLIC")
            c.exec_driver_sql(f"GRANT SELECT (organization_id),INSERT (organization_id),UPDATE (organization_id),REFERENCES (organization_id) ON {table} TO {role} WITH GRANT OPTION")
    try:
        if phase == "migration_intersection":
            with owner.begin() as c:
                c.execute(text(scripts().get_revision(TARGET).module.acl()))
        else:
            result = runtime_script(owner, role)
            assert result.returncode == 0, result.stderr
        with owner.connect() as c:
            for table in TABLES:
                assert c.execute(text("SELECT count(*) FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attrelid=to_regclass(:t) AND x.grantee=0"), {"t": table}).scalar_one() == 0
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    values = {"r": role, "t": table, "p": privilege}
                    assert c.execute(text("SELECT has_column_privilege(:r,:t,'organization_id',:p)"), values).scalar_one() is (
                        privilege in {"SELECT", "INSERT"} or privilege == "UPDATE" and table in HEADS)
                    values["p"] += " WITH GRANT OPTION"
                    assert c.execute(text("SELECT has_column_privilege(:r,:t,'organization_id',:p)"), values).scalar_one() is False
    finally:
        # Restore the real role script even after a failed diagnostic assertion.
        result = runtime_script(owner, role)
        assert result.returncode == 0, result.stderr


def test_migration_narrows_only_new_inherited_acl_and_preserves_old_defaults(cluster):
    reader, writer = "review_local_reader_" + uuid4().hex, "review_local_writer_" + uuid4().hex
    with candidate.disposable_database(cluster, (reader, writer)) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {reader}")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {writer} WITH GRANT OPTION")
                c.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO PUBLIC")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {reader},{writer} WITH GRANT OPTION")
                old = c.exec_driver_sql("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").all()
                defaults = c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all()
            result = candidate.migrate(database.url, "upgrade", TARGET)
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                old_ids = [row[0] for row in old]
                assert c.execute(text("SELECT oid,relacl::text FROM pg_class WHERE oid=ANY(:ids) ORDER BY oid"), {"ids": old_ids}).all() == old
                assert c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all() == defaults
                for table in TABLES:
                    for role in (reader, writer):
                        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                            permitted = privilege == "SELECT" or (role == writer and (
                                privilege == "INSERT" or (privilege == "UPDATE" and table in HEADS)))
                            params = {"r": role, "t": table, "p": privilege}
                            assert c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), params).scalar_one() is permitted
                            params["p"] += " WITH GRANT OPTION"
                            assert c.execute(text("SELECT has_table_privilege(:r,:t,:p)"), params).scalar_one() is False
                    assert c.execute(text("SELECT count(*) FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid=to_regclass(:t) AND a.grantee=0"), {"t": table}).scalar_one() == 0
        finally:
            owner.dispose()


@pytest.mark.parametrize("hidden", [False, True])
def test_nonempty_downgrade_refuses_before_destructive_ddl(populated, hidden):
    owner, runtime = populated
    with owner.connect() as c:
        transaction = c.begin()
        try:
            before_revision = c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            before_rows = snapshot(c)
            if hidden:
                for table in TABLES:
                    c.exec_driver_sql(f"ALTER TABLE {table} OWNER TO {runtime.url.username}")
                c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
                scope(c, 91002, 91201)
                assert c.exec_driver_sql("SELECT count(*) FROM review_local_command_receipts").scalar_one() == 0
            with pytest.raises(DBAPIError) as caught, Operations.context(MigrationContext.configure(c)):
                scripts().get_revision(TARGET).module.downgrade()
            assert caught.value.orig.sqlstate == ("42501" if hidden else "55000")
            if not hidden:
                assert caught.value.orig.diag.message_primary == "review_local_downgrade_nonempty"
        finally:
            transaction.rollback()
    with owner.connect() as c:
        for table in TABLES:
            assert c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one() is not None
        # This calls the historical0071 downgrade directly inside a rolled-back
        # root, not an Alembic chain downgrade. The actual-head fixture's version
        # and data must remain exactly as observed, not be rewritten to0071.
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == before_revision
        assert snapshot(c) == before_rows


def test_unrelated_helper_overload_acl_survives_upgrade_and_runtime_script(cluster):
    reader, runtime_role = "review_local_overload_reader_" + uuid4().hex, "review_local_overload_runtime_" + uuid4().hex
    with candidate.disposable_database(cluster, (reader, runtime_role)) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        signature = "public.review_local_integer_text(text)"

        def snapshot(c):
            definition = c.execute(text("SELECT oid,proowner,proacl::text,pg_get_functiondef(oid) FROM pg_proc WHERE oid=to_regprocedure(:s)"),
                                   {"s": signature}).one()
            privileges = c.execute(text("SELECT has_function_privilege(:reader,:s,'EXECUTE'),has_function_privilege(:reader,:s,'EXECUTE WITH GRANT OPTION'),has_function_privilege(:runtime,:s,'EXECUTE')"),
                                   {"s": signature, "reader": reader, "runtime": runtime_role}).one()
            assert c.exec_driver_sql("SELECT public.review_local_integer_text('synthetic-unrelated'::text)").scalar_one() == "synthetic-unrelated"
            return definition, privileges

        try:
            with owner.begin() as c:
                c.exec_driver_sql("CREATE FUNCTION public.review_local_integer_text(value text) RETURNS text LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT value $$")
                c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {reader} WITH GRANT OPTION")
                # Pre-provision runtime's harmless EXECUTE; comparison must isolate
                # new-only ACL narrowing rather than unrelated broad provisioning.
                c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO {runtime_role}")
                before = snapshot(c)
            result = candidate.migrate(database.url, "upgrade", TARGET)
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                after_upgrade = snapshot(c)
            # Preserve the exact0071 historical check, then install actual head
            # before invoking today's fail-closed runtime grants. Missing future
            # canonical tables at0071 are not evidence of a grants-script bug.
            assert after_upgrade == before
            result = candidate.migrate(database.url, "upgrade", "head")
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                after_head = snapshot(c)
            result = runtime_script(owner, runtime_role)
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                after_runtime = snapshot(c)
            assert {"upgrade": after_upgrade, "head": after_head, "runtime": after_runtime} == {
                "upgrade": before, "head": before, "runtime": before,
            }
        finally:
            owner.dispose()
