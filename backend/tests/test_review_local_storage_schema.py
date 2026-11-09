"""Local Review physical contract on exact allocator-owned PostgreSQL only."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.script import Script
from sqlalchemy import create_engine, text

from tests import test_orders_exact_text_migration as orders
from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as facts
from tests import test_review_run_binding_migration as binding
from tests.test_orders_schema_integration import runtime_script
from tests.test_review_lossless_migration import scripts, unit

cluster = candidate.cluster
PREVIOUS = "20260909_0070"
TARGET = "20260909_0071"
TABLES = (
    "review_policy_versions", "review_policy_heads", "review_draft_revisions",
    "review_decisions", "review_workflow_heads", "review_local_audit",
    "review_local_command_receipts",
)
HEADS = {"review_policy_heads", "review_workflow_heads"}


def scope(c, organization_id=91001, marketplace_account_id=91101):
    for name, value in (("app.organization_id", organization_id),
                        ("app.marketplace_account_id", marketplace_account_id)):
        c.execute(text("SELECT set_config(:name,:value,true)"),
                  {"name": name, "value": str(value)})


def seed(c):
    facts.seed(c)
    c.exec_driver_sql("""INSERT INTO lk_users
        (user_id,organization_id,email,password_hash,full_name,permission_profile)
        VALUES ('local77',91001,'local77@invalid','synthetic','Synthetic','admin'),
               ('local78',91001,'local78@invalid','synthetic','Synthetic','admin'),
               ('local79',91002,'local79@invalid','synthetic','Synthetic','admin')""")
    c.exec_driver_sql("""INSERT INTO iam_memberships
        (membership_id,organization_id,user_id,role,permissions,scope_mode,allowed_account_ids,is_active)
        VALUES (77,91001,'local77','admin','[]','all','[]',true),
               (78,91001,'local78','admin','[]','all','[]',true),
               (79,91002,'local79','admin','[]','all','[]',true)""")


def preserved_catalog(c, identities=None):
    if identities is None:
        identities = {
            "relations": c.exec_driver_sql("SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").scalars().all(),
            "functions": c.exec_driver_sql("SELECT oid FROM pg_proc WHERE pronamespace='public'::regnamespace ORDER BY oid").scalars().all(),
        }
    return identities, {
        "relations": c.execute(text("SELECT oid,relowner,relacl::text,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=ANY(:ids) ORDER BY oid"), {"ids": identities["relations"]}).all(),
        "columns": c.execute(text("SELECT attrelid,attnum,attname,atttypid,atttypmod,attnotnull,attacl::text FROM pg_attribute WHERE attrelid=ANY(:ids) AND attnum>0 AND NOT attisdropped ORDER BY 1,2"), {"ids": identities["relations"]}).all(),
        "functions": c.execute(text("SELECT oid,proowner,proacl::text,pg_get_functiondef(oid) FROM pg_proc WHERE oid=ANY(:ids) ORDER BY oid"), {"ids": identities["functions"]}).all(),
        "defaults": c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all(),
    }


@pytest.fixture(scope="module")
def db(cluster):
    role = "review_local_runtime_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
            result = runtime_script(owner, role)
            assert result.returncode == 0, result.stderr
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def test_local_contract_present(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", TARGET)
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url, hide_parameters=True)
        try:
            with engine.connect() as c:
                actual = c.exec_driver_sql(
                    "SELECT to_regclass('public.review_local_command_receipts')"
                ).scalar_one()
                assert actual is not None
        finally:
            engine.dispose()


@pytest.mark.parametrize("populated", [False, True])
def test_predecessor_preserved_through_unstamped_cycle(cluster, populated):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
                scope(c)
                if populated:
                    orders.complete_evidence(c)
                    unit(c, {"review_sync_runs_v2": {
                        "source_run_id": None, "source_run_id_utf8": b"run\x00history",
                        "coverage": None, "coverage_utf8": b'{"integer":9223372036854775808,"text":"\\u0000"}',
                        **binding.stamp()}, "review_facts": {
                        "external_review_id": None,
                        "external_review_id_utf8": "review\x00é🚀".encode()},
                        "review_observations": {"text_utf8": b"body\x00history"}})
                before = binding.rows(c)
                old_ids, old_catalog = preserved_catalog(c)
                if populated:
                    assert all(before[table] for table in (*candidate.TABLES, *facts.TABLES))
            for action, target in (("upgrade", TARGET), ("downgrade", PREVIOUS),
                                   ("upgrade", TARGET)):
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    assert binding.rows(c) == before
                    assert preserved_catalog(c, old_ids)[1] == old_catalog
                    assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == target
        finally:
            owner.dispose()


def test_revision_is_in_actual_single_head_chain():
    directory = scripts()
    heads = directory.get_heads()
    assert len(heads) == 1
    assert TARGET in {r.revision for r in directory.iterate_revisions(heads[0], "base")}
    revision = directory.get_revision(TARGET)
    assert revision.down_revision == PREVIOUS
    assert not revision.branch_labels and not revision.dependencies


def test_future_successor_attaches_to_actual_current_head():
    directory = scripts()
    head = directory.get_current_head()
    successor = Script(SimpleNamespace(down_revision=head, branch_labels=None, depends_on=None),
                       "synthetic_review_local_successor", "<in-memory>")
    directory.revision_map.add_revision(successor)
    assert directory.get_heads() == ["synthetic_review_local_successor"]
    assert TARGET in {r.revision for r in directory.iterate_revisions("synthetic_review_local_successor", "base")}


def test_audit_head_refs_are_generated_and_have_scoped_deferred_fks(db):
    with db[0].connect() as c:
        actual = dict(c.exec_driver_sql("""SELECT a.attname,a.attgenerated FROM pg_attribute a
            WHERE a.attrelid='review_local_audit'::regclass AND a.attname IN ('policy_head_id','workflow_head_id')""").all())
        assert actual == {"policy_head_id": "s", "workflow_head_id": "s"}
        for name, parent in (("review_local_audit_phead_fk", "review_policy_heads"),
                             ("review_local_audit_whead_fk", "review_workflow_heads")):
            row = c.execute(text("""SELECT condeferrable,condeferred,confrelid=to_regclass(:parent),
                (SELECT array_agg(attname ORDER BY ordinal) FROM unnest(conkey) WITH ORDINALITY k(num,ordinal)
                 JOIN pg_attribute a ON a.attrelid=conrelid AND a.attnum=k.num)
                FROM pg_constraint WHERE conrelid='review_local_audit'::regclass AND conname=:name"""),
                {"parent": parent, "name": name}).one()
            assert row[:3] == (True, True, True)
            assert row[3][:3] == ["organization_id", "marketplace_account_id", "marketplace"]


@pytest.mark.parametrize("table", TABLES)
def test_owner_types_and_forced_rls(db, table):
    with db[0].connect() as c:
        assert c.execute(text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:t)"),
                         {"t": table}).one() == (True, True)
        actual = dict(c.execute(text("""SELECT attname,format_type(atttypid,atttypmod)
            FROM pg_attribute WHERE attrelid=to_regclass(:t)
              AND attname IN ('organization_id','marketplace_account_id')"""), {"t": table}).all())
        assert actual == {"organization_id": "integer", "marketplace_account_id": "integer"}


@pytest.mark.parametrize("table,column", [
    ("review_policy_versions", "version"), ("review_policy_heads", "version"),
    ("review_draft_revisions", "revision"), ("review_draft_revisions", "policy_version"),
    ("review_workflow_heads", "version"), ("review_workflow_heads", "current_draft_revision"),
    ("review_local_audit", "aggregate_version"),
    ("review_local_command_receipts", "expected_head_version"),
])
def test_versions_are_numeric_without_typmod(db, table, column):
    with db[0].connect() as c:
        assert c.execute(text("""SELECT format_type(atttypid,atttypmod),atttypmod
            FROM pg_attribute WHERE attrelid=to_regclass(:t) AND attname=:n"""),
            {"t": table, "n": column}).one() == ("numeric", -1)


def test_child_fk_indexes_cover_lookups_without_redundant_prefixes(db):
    with db[0].connect() as c:
        indexes = c.execute(text("""SELECT t.relname AS table_name,x.relname AS name,
            i.indisunique,i.indisvalid,i.indisready,i.indnkeyatts,
            i.indkey::smallint[] AS keys,i.indclass::oid[] AS classes,
            i.indcollation::oid[] AS collations,i.indoption::smallint[] AS options,
            am.amname,i.indpred IS NULL AS unfiltered,i.indexprs IS NULL AS plain
            FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
            JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_am am ON am.oid=x.relam
            WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(:tables)
            ORDER BY t.relname,x.relname"""), {"tables": list(TABLES)}).mappings().all()
        foreign_keys = c.execute(text("""SELECT t.relname AS table_name,k.conname,k.conkey
            FROM pg_constraint k JOIN pg_class t ON t.oid=k.conrelid
            WHERE t.relnamespace='public'::regnamespace AND t.relname=ANY(:tables)
              AND k.contype='f' ORDER BY t.relname,k.conname"""), {"tables": list(TABLES)}).mappings().all()
    usable = [i for i in indexes if i["indisvalid"] and i["indisready"] and i["unfiltered"]
              and i["plain"] and i["amname"] == "btree"]
    assert foreign_keys
    for fk in foreign_keys:
        assert any(i["table_name"] == fk["table_name"] and i["keys"][:len(fk["conkey"])] == fk["conkey"]
                   and i["indnkeyatts"] >= len(fk["conkey"]) for i in usable), fk["conname"]
    redundant = []
    for child in usable:
        # PK/UNIQUE indexes retain their distinct uniqueness semantics.
        if child["indisunique"] or not child["name"].startswith("review_local_child_"):
            continue
        width = child["indnkeyatts"]
        for other in usable:
            if other["name"] == child["name"] or other["table_name"] != child["table_name"] or other["indnkeyatts"] < width:
                continue
            if all(child[field][:width] == other[field][:width] for field in ("keys", "classes", "collations", "options")):
                redundant.append((child["name"], other["name"]))
    assert redundant == []
