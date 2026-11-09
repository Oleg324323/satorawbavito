"""Full old-writer intersection and atomic runtime grants on native PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as legacy
from tests import test_review_run_binding_migration as binding
from tests.test_orders_schema_integration import runtime_script
from tests.test_review_lossless_migration import migrated
from tests.test_review_run_binding_rls import old_writer, read_stamp

cluster = candidate.cluster


def function_acl(c):
    return c.exec_driver_sql("SELECT oid,proowner,proacl::text FROM pg_proc WHERE pronamespace='public'::regnamespace ORDER BY oid").all()


def privilege(c, role, column, permission):
    return c.execute(text("SELECT has_column_privilege(:r,'review_sync_runs_v2',:c,:p)"), {"r": role, "c": column, "p": permission}).scalar_one()


def test_existing_distinct_relation_owner_keeps_check_evaluation_authority(cluster):
    role = "review_binding_owner_" + uuid4().hex
    with migrated(cluster, "20260909_0067", (role,)) as database:
        owner = database.owner
        with owner.begin() as c:
            old_writer(c, role)
            c.exec_driver_sql(f"ALTER TABLE review_sync_runs_v2 OWNER TO {role}")
            before = binding.schema(c)
        result = candidate.migrate(database.database.url, "upgrade", binding.REVISION)
        assert result.returncode == 0, result.stderr
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                rid, _ = legacy.run(c, **binding.stamp())
                assert read_stamp(c, rid) == binding.stamp()
            with owner.connect() as c:
                assert binding.schema(c) == before
        finally:
            runtime.dispose()


def test_acl_full_writer_intersection_inheritance_and_hostile_defaults(cluster):
    roles = ["review_binding_acl_" + uuid4().hex for _ in range(7)]
    writer, reader, partial, full, updater, member, hostile = roles
    with migrated(cluster, "20260909_0067", roles) as database:
        owner = database.owner
        with owner.begin() as c:
            old_writer(c, writer)
            c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {','.join(roles)}")
            c.exec_driver_sql(f"GRANT SELECT(sync_run_id) ON review_sync_runs_v2 TO {reader}")
            c.exec_driver_sql(f"GRANT INSERT(source_run_id) ON review_sync_runs_v2 TO {partial} WITH GRANT OPTION")
            c.exec_driver_sql(f"GRANT INSERT({','.join((*legacy.RUN_COLUMNS, 'source_run_id_utf8', 'coverage_utf8'))}) ON review_sync_runs_v2 TO {full} WITH GRANT OPTION")
            c.exec_driver_sql(f"GRANT UPDATE(observed_count) ON review_sync_runs_v2 TO {updater}")
            c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION review_strict_utf8(bytea),review_coverage_json_object_utf8(bytea) TO {updater}")
            c.exec_driver_sql(f"ALTER ROLE {member} INHERIT")
            c.exec_driver_sql(f"GRANT {full} TO {member}")
            c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {hostile} WITH GRANT OPTION")
            c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {reader} WITH GRANT OPTION")
            old = binding.schema(c)
            legacy.run(c)
            c.exec_driver_sql(f"SET LOCAL ROLE {updater}")
            candidate.scope(c)
            assert c.exec_driver_sql("UPDATE review_sync_runs_v2 SET observed_count=3").rowcount == 1
        result = candidate.migrate(database.database.url, "upgrade", binding.REVISION)
        assert result.returncode == 0, result.stderr
        with owner.connect() as c:
            assert binding.schema(c) == old
            for role in roles:
                for field in binding.FIELDS:
                    assert privilege(c, role, field, "INSERT") == (role in (writer, full, member))
                    assert privilege(c, role, field, "UPDATE") == (role == writer)
                    assert not privilege(c, role, field, "INSERT WITH GRANT OPTION")
                assert not privilege(c, role, "run_sequence", "INSERT")
                for helper in binding.HELPERS:
                    assert c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"), {"r": role, "f": "public." + helper}).scalar_one() == (role in (writer, partial, full, updater, member))
                    assert not c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE WITH GRANT OPTION')"), {"r": role, "f": "public." + helper}).scalar_one()
                for helper in ("review_run_binding_insert_guard()", "review_run_binding_update_guard()"):
                    assert not c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"), {"r": role, "f": "public." + helper}).scalar_one()
            assert not c.execute(text("SELECT EXISTS(SELECT 1 FROM pg_proc p,LATERAL aclexplode(p.proacl) a WHERE (p.proname LIKE 'review_binding_%' OR p.proname LIKE 'review_run_binding_%') AND a.grantee=0)")).scalar_one()
            # Membership adds only existing parent authority; migration must not
            # manufacture missing canonical-account or generated-ID permissions.
            for role in (full, member):
                assert not c.execute(text("SELECT has_table_privilege(:r,'marketplace_accounts','SELECT')"), {"r": role}).scalar_one()
        with owner.begin() as c:
            rid, _ = legacy.run(c, **binding.stamp())
        for role in (full, member):
            runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
            try:
                with pytest.raises(DBAPIError) as error, runtime.begin() as c:
                    candidate.scope(c)
                    legacy.run(c, **binding.stamp())
                assert error.value.orig.sqlstate == "42501"
            finally:
                runtime.dispose()
        # Reader and UPDATE-only roles exercise their actual allowed operation.
        for role in (reader, updater):
            runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
            try:
                with runtime.begin() as c:
                    candidate.scope(c)
                    if role == reader:
                        assert c.execute(text("SELECT sync_run_id FROM review_sync_runs_v2 WHERE sync_run_id=:id"), {"id": rid}).scalar_one() == rid
                    else:
                        assert c.exec_driver_sql("UPDATE review_sync_runs_v2 SET observed_count=7").rowcount == 2
            finally:
                runtime.dispose()
        with owner.connect() as c:
            assert c.execute(text("SELECT observed_count FROM review_sync_runs_v2 WHERE sync_run_id=:id"), {"id": rid}).scalar_one() == 7
            assert read_stamp(c, rid) == binding.stamp()


def test_runtime_script_latest_head_exact_rights_and_failure_rollback(cluster):
    role = "review_binding_script_" + uuid4().hex
    with migrated(cluster, "head", (role,)) as database:
        owner = database.owner
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            result = runtime_script(owner, role)
            assert result.returncode == 0, result.stderr
            with runtime.begin() as c:
                candidate.scope(c)
                rid, _ = legacy.run(c, **binding.stamp())
                assert read_stamp(c, rid) == binding.stamp()
            with owner.connect() as c:
                legacy.assert_acl(c, role)
                for f in binding.FIELDS:
                    assert privilege(c, role, f, "INSERT")
                    assert not privilege(c, role, f, "INSERT WITH GRANT OPTION")
                before, functions = binding.schema(c), function_acl(c)
                new_columns = c.execute(text("SELECT attname,attacl::text FROM pg_attribute WHERE attrelid='review_sync_runs_v2'::regclass AND attname LIKE 'account_binding_%' ORDER BY attname")).all()
            legacy.ordering_denials(runtime)
            result = runtime_script(owner, role, fail_after_broad=True)
            assert result.returncode != 0 and "division by zero" in result.stderr
            with owner.connect() as c:
                assert binding.schema(c) == before
                assert function_acl(c) == functions
                assert c.execute(text("SELECT attname,attacl::text FROM pg_attribute WHERE attrelid='review_sync_runs_v2'::regclass AND attname LIKE 'account_binding_%' ORDER BY attname")).all() == new_columns
        finally:
            runtime.dispose()
