"""Immutable run provenance, tested only in disposable PostgreSQL databases."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import Script
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_exact_text_migration as exact
from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
REVISION = "20260909_0067"
FIELDS = tuple("account_binding_" + x for x in (
    "schema_version", "external_account_id", "credential_ref", "payload", "checksum"))
HELPERS = (
    "orders_binding_text(text,integer)", "orders_binding_ascii_string(text)",
    "orders_run_binding_bytes(integer,integer,text,text,text)",
)


def stamp(org=91001, account=91101, provider="avito", external="synthetic-a", ref=None):
    # Additional synthetic expectations use stdlib, never SQL or domain code.
    payload = json.dumps([org, [[account, provider, external, ref]]],
                         ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return dict(zip(FIELDS, (1, external, ref, payload, hashlib.sha256(payload).hexdigest()), strict=True))


def golden_vectors():
    return json.loads((Path(__file__).parent / "fixtures/orders/account_binding_golden_v1.json").read_text())


@pytest.fixture(scope="module")
def db(cluster):
    with exact.migrated_database(cluster, REVISION) as engines:
        yield engines


def test_old_unbound_run_positive_control(db):
    owner, _ = db
    with owner.begin() as c:
        assert exact.make_run(c) > 0


def test_literal_single_account_vectors(db):
    owner, _ = db
    for vector in golden_vectors()["vectors"][:4]:
        account, provider, external, ref = vector["accounts"][0]
        with owner.begin() as c:
            payload = bytes(c.execute(text("""
                SELECT public.orders_run_binding_bytes(:org,:account,:provider,:external,:ref)
            """), {"org": vector["organization_id"], "account": account,
                   "provider": provider, "external": external, "ref": ref}).scalar_one())
        assert payload == vector["canonical_ascii"].encode("ascii")
        assert hashlib.sha256(payload).hexdigest() == vector["sha256"]


def test_new_binding_columns_exist(db):
    owner, _ = db
    with owner.begin() as c:
        assert exact.make_run(c, account_binding_schema_version=None) > 0


def test_column_types_defaults_and_function_contract(db):
    owner, _ = db
    with owner.connect() as c:
        columns = c.execute(text("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),
            a.attnotnull,pg_get_expr(d.adbin,d.adrelid),co.collname
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            LEFT JOIN pg_collation co ON co.oid=a.attcollation
            WHERE a.attrelid='order_sync_runs'::regclass AND a.attname=ANY(:fields)"""),
                            {"fields": list(FIELDS)}).all()
        assert {row[0]: row[1] for row in columns} == dict(zip(FIELDS, ("smallint", "text", "text", "bytea", "text"), strict=True))
        assert all(not row[2] and row[3] is None for row in columns)
        assert all(row[4] == "C" for row in columns if row[0] in FIELDS[1:3])
        for helper in HELPERS:
            assert c.execute(text("SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:helper)"),
                             {"helper": "public." + helper}).one() == ("i", False, ["search_path=pg_catalog, public"])
        for helper in ("orders_run_binding_insert_guard()", "orders_run_binding_update_guard()"):
            assert c.execute(text("SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:helper)"),
                             {"helper": "public." + helper}).one() == ("v", False, ["search_path=pg_catalog, public"])


@pytest.mark.parametrize("mask", [i for i in range(1, 32) if i not in (27, 31)])
def test_every_partial_null_shape_is_rejected(db, mask):
    owner, _ = db
    values = stamp(ref="synthetic-ref")
    values = {field: value if mask & (1 << i) else None
              for i, (field, value) in enumerate(values.items())}
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        exact.make_run(c, **values)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("field,value", [
    (FIELDS[0], 0), (FIELDS[0], 2), (FIELDS[1], ""), (FIELDS[1], " \t\u3000"),
    (FIELDS[1], "x" * 129), (FIELDS[2], ""), (FIELDS[2], "\u001c\u00a0"),
    (FIELDS[2], "x" * 256), (FIELDS[4], "a" * 64), (FIELDS[4], "A" * 64),
    (FIELDS[4], "0" * 63), (FIELDS[3], b""),
])
def test_invalid_bound_shape_and_checksum(db, field, value):
    owner, _ = db
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        exact.make_run(c, **dict(stamp(), **{field: value}))
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("payload", [
    b'{"organization_id":91001,"accounts":[[91101,"avito","synthetic-a",null]]}',
    b'[91001,[["91101","avito","synthetic-a",null]]]',
    b'[91001,[[91101.0,"avito","synthetic-a",null]]]',
    b'[91001,[[true,"avito","synthetic-a",null]]]',
    b'[91001,[[91101,"avito","synthetic-a",null,1]]]',
    b'[91001,[[91101,"avito","synthetic-a",null],[91101,"avito","synthetic-a",null]]]',
    b'[91001, [[91101,"avito","synthetic-a",null]]]',
    b'[91002,[[91101,"avito","synthetic-a",null]]]',
    b'[91001,[[91102,"avito","synthetic-a",null]]]',
    b'[91001,[[91101,"wb","synthetic-a",null]]]',
])
def test_recomputed_hash_cannot_admit_alternate_bytes(db, payload):
    owner, _ = db
    values = stamp()
    values.update(account_binding_payload=payload, account_binding_checksum=hashlib.sha256(payload).hexdigest())
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        exact.make_run(c, **values)
    assert error.value.orig.sqlstate == "23514"


def test_literal_multiaccount_vector_is_not_a_single_run(db):
    owner, _ = db
    vector = golden_vectors()["vectors"][4]
    values = stamp(org=1, account=1, external="synthetic-first")
    values.update(account_binding_payload=vector["canonical_ascii"].encode("ascii"),
                  account_binding_checksum=vector["sha256"])
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        c.exec_driver_sql("INSERT INTO lk_organizations(organization_id,slug,name) VALUES (1,'binding-multi','Synthetic')")
        c.exec_driver_sql("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES (1,1,'avito','synthetic-first','connected'),(2,1,'wb','synthetic-second','connected')")
        exact.make_run(c, organization_id=1, marketplace_account_id=1, **values)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("external,ref", [
    ("x" * 128, "\U0001f600" * 255),
    (' \t\\"x\b\f\n\r\x01\x7f\u0085\u00a0\u043a\U0010ffffe\u0301 ', None),
    ("\u200b", "\ufeff"),
])
def test_exact_unicode_and_max_codepoint_lengths_roundtrip(db, external, ref):
    owner, _ = db
    expected = stamp(external=external, ref=ref)
    with owner.begin() as c:
        payload = c.execute(text("SELECT public.orders_run_binding_bytes(91001,91101,'avito',:external,:ref)"),
                            {"external": external, "ref": ref}).scalar_one()
        assert bytes(payload) == expected[FIELDS[3]]
        c.execute(text("UPDATE marketplace_accounts SET external_account_id=:external,credential_ref=:ref WHERE marketplace_account_id=91101"),
                  {"external": external, "ref": ref})
        rid = exact.make_run(c, **expected)
        assert dict(c.execute(text(f"SELECT {','.join(FIELDS)} FROM order_sync_runs WHERE sync_run_id=:id"),
                              {"id": rid}).mappings().one()) == expected
        c.rollback()


@pytest.mark.parametrize("value", ["", "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000", "x" * 129, None])
def test_text_validation_nonblank_matches_python_strip(db, value):
    owner, _ = db
    with owner.begin() as c:
        assert c.execute(text("SELECT public.orders_binding_text(:value,128)"), {"value": value}).scalar_one() is False


@pytest.mark.parametrize("args", [(0, 1, "avito", "x", None), (-1, 1, "wb", "x", None),
    (1, 0, "wb", "x", None), (None, 1, "wb", "x", None), (1, None, "wb", "x", None),
    (1, 1, None, "x", None), (1, 1, "WB", "x", None), (1, 1, "wb", None, None),
    (1, 1, "wb", "x", " ")])
def test_invalid_typed_arguments_are_safe(db, args):
    owner, _ = db
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        c.execute(text("SELECT public.orders_run_binding_bytes(:o,:a,:p,:e,:r)"), dict(zip(("o", "a", "p", "e", "r"), args, strict=True)))
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == "orders_run_binding_invalid"


@pytest.mark.parametrize("value", ["\x00", "\ud800", "\udfff"])  # noqa: PT014 -- distinct high/low surrogates
def test_unsupported_text_rejected_before_or_at_storage(db, value):
    owner, _ = db
    with pytest.raises((DBAPIError, UnicodeError, ValueError)), owner.begin() as c:
        c.execute(text("SELECT public.orders_binding_ascii_string(:value)"), {"value": value})


def revision():
    return exact.migration_scripts().get_revision(REVISION).module


def test_historical_contract_accepts_future_successor():
    scripts = exact.migration_scripts()
    expected = scripts.get_revision(REVISION).module
    scripts.revision_map.add_revision(Script(SimpleNamespace(down_revision=scripts.get_current_head(),
        branch_labels=None, depends_on=None), "synthetic_binding_successor", "<in-memory>"))
    assert scripts.get_revision(REVISION).module is expected
    assert len(scripts.get_heads()) == 1
    assert REVISION in {item.revision for item in scripts.iterate_revisions(scripts.get_heads()[0], "base")}


def old_rows(owner):
    result = exact.rows(owner)
    result["order_sync_runs"] = [({k: v for k, v in row[0].items() if k not in FIELDS},)
                                 for row in result["order_sync_runs"]]
    return result


@pytest.mark.parametrize("populated", [False, True])
def test_empty_and_production_shaped_unbound_roundtrip(cluster, populated):
    role = "orders_binding_roundtrip_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0066").returncode == 0
        owner = create_engine(database.url, hide_parameters=True)
        try:
            exact.setup(owner, role)
            with owner.begin() as c:
                if populated:
                    exact.complete_evidence(c)
                    for index in range(8):
                        table, _, pk, values = exact.parents(c, index, uuid4().hex)
                        exact.insert(c, table, values, pk)
                before = exact.schema(c)
            original = old_rows(owner)
            if populated:
                assert all(original.values())
            for action, target in [("upgrade", REVISION), ("downgrade", "20260909_0066"), ("upgrade", REVISION)]:
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
                assert old_rows(owner) == original
                with owner.connect() as c:
                    current = exact.schema(c)
                    for key in ("access", "policies", "sequences", "indexes"):
                        assert current[key] == before[key]
                    if target == "20260909_0066":
                        assert current == before
                    else:
                        assert c.exec_driver_sql("SELECT count(*) FROM order_sync_runs WHERE " + " OR ".join(f"{f} IS NOT NULL" for f in FIELDS)).scalar_one() == 0
        finally:
            owner.dispose()


@pytest.mark.parametrize("mode", ["visible", "hidden", "partial"])
def test_bound_downgrade_refuses_before_any_drop(cluster, mode):
    role = "orders_binding_down_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        assert candidate.migrate(database.url, "upgrade", REVISION).returncode == 0
        owner = create_engine(database.url, hide_parameters=True)
        try:
            exact.setup(owner, role)
            with owner.begin() as c:
                if mode == "partial":
                    # Explicit malformed fixture in this disposable DB only; trigger
                    # and CHECK bypass is never a supported application write path.
                    c.exec_driver_sql("ALTER TABLE order_sync_runs DROP CONSTRAINT ck_orders_run_binding")
                    c.exec_driver_sql("ALTER TABLE order_sync_runs DISABLE TRIGGER orders_run_binding_insert")
                    exact.make_run(c, account_binding_checksum="a" * 64)
                else:
                    exact.make_run(c, **stamp())
                if mode == "hidden":
                    c.exec_driver_sql(f"ALTER TABLE order_sync_runs OWNER TO {role}")
                before = exact.schema(c)
            original = exact.rows(owner)
            with pytest.raises(DBAPIError) as error, owner.begin() as c:
                if mode == "hidden":
                    c.exec_driver_sql(f"SET LOCAL ROLE {role}")
                    candidate.scope(c, 91002)
                    assert c.exec_driver_sql("SELECT count(*) FROM order_sync_runs").scalar_one() == 0
                with Operations.context(MigrationContext.configure(c)):
                    revision().downgrade()
            assert error.value.orig.sqlstate == ("42501" if mode == "hidden" else "55000")
            if mode != "hidden":
                assert error.value.orig.diag.message_primary == "orders_run_binding_downgrade_bound"
            with owner.connect() as c:
                assert exact.schema(c) == before
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == REVISION
            assert exact.rows(owner) == original
        finally:
            owner.dispose()
