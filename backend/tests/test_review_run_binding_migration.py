"""Review object provenance on native disposable PostgreSQL; no domain codec."""

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

from tests import test_orders_exact_text_migration as orders
from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as legacy
from tests.test_review_lossless_migration import migrated, scripts, unit

cluster = candidate.cluster
REVISION = "20260909_0068"
FIELDS = tuple("account_binding_" + name for name in (
    "schema_version", "external_account_id", "credential_ref", "payload", "checksum"))
HELPERS = ("review_binding_ascii_string(text)",
           "review_run_binding_bytes(integer,integer,text,text,text)")
VECTORS = json.loads((Path(__file__).parent / "fixtures/reviews/account_binding_descriptor_v1.json").read_text())


def stamp(org=91001, account=91101, provider="avito", external="synthetic-a", ref=None):
    payload = json.dumps({"schemaVersion": 1, "organizationId": org, "marketplaceAccountId": account,
        "marketplace": provider, "externalAccountId": external, "credentialRef": ref},
        sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    return dict(zip(FIELDS, (1, external, ref, payload, hashlib.sha256(payload).hexdigest()), strict=True))


@pytest.fixture(scope="module")
def db(cluster):
    with migrated(cluster, REVISION) as database:
        yield database.owner


def test_old_unbound_run_positive_control(db):
    with db.begin() as c:
        run_id, sequence = legacy.run(c)
        assert run_id and sequence > 0


def test_new_binding_columns_exist(db):
    with db.begin() as c:
        assert legacy.run(c, account_binding_schema_version=None)[1] > 0


def test_literal_single_account_vectors(db):
    for vector in [v for v in VECTORS if "pure-nul" not in v["name"]]:
        d = vector["descriptor"]
        with db.connect() as c:
            payload, checksum = c.execute(text("""
                SELECT p,encode(sha256(p),'hex') FROM
                (SELECT public.review_run_binding_bytes(:org,:account,:provider,:external,:ref) p) q
            """), {"org": d["organizationId"], "account": d["marketplaceAccountId"],
                "provider": d["marketplace"], "external": d["externalAccountId"], "ref": d["credentialRef"]}).one()
        assert bytes(payload) == vector["canonicalAscii"].encode("ascii")
        assert checksum == vector["sha256"]
        with db.begin() as c:
            c.execute(text("INSERT INTO lk_organizations(organization_id,slug,name) VALUES (:o,:slug,'Synthetic parity')"), {"o": d["organizationId"], "slug": uuid4().hex})
            c.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,credential_ref,status) VALUES (:a,:o,:p,:e,:r,'connected')"),
                      {"a": d["marketplaceAccountId"], "o": d["organizationId"], "p": d["marketplace"], "e": d["externalAccountId"], "r": d["credentialRef"]})
            values = dict(zip(FIELDS, (1, d["externalAccountId"], d["credentialRef"], vector["canonicalAscii"].encode("ascii"), vector["sha256"]), strict=True))
            rid, _ = legacy.run(c, organization_id=d["organizationId"], marketplace_account_id=d["marketplaceAccountId"], marketplace=d["marketplace"], **values)
            assert c.execute(text("SELECT account_binding_payload,account_binding_checksum FROM review_sync_runs_v2 WHERE sync_run_id=:id"), {"id": rid}).one() == (vector["canonicalAscii"].encode("ascii"), vector["sha256"])
            c.rollback()


@pytest.mark.parametrize("vector", [v for v in VECTORS if "pure-nul" in v["name"]], ids=lambda v: v["name"])
def test_literal_nul_vectors_native_text_rejection(db, vector):
    d = vector["descriptor"]
    # Send bytea to force PostgreSQL's native decoder to reject, not psycopg.
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.execute(text("""SELECT public.review_run_binding_bytes(:org,:account,:provider,
            convert_from(:external,'UTF8'),convert_from(:ref,'UTF8'))"""),
            {"org": d["organizationId"], "account": d["marketplaceAccountId"],
                 "provider": d["marketplace"], "external": d["externalAccountId"].encode(),
                 "ref": d["credentialRef"].encode()})
    assert error.value.orig.sqlstate == "22021"


@pytest.mark.parametrize("external", [" \t\u3000", '"\\\b\f\n\r\x01\x1f\x7f', "é", "e\u0301", "\U0010ffff🚀"])
@pytest.mark.parametrize("ref", [None, "", " \n"])
def test_exact_scalar_roundtrip_and_bound_insert(db, external, ref):
    expected = stamp(external=external, ref=ref)
    with db.begin() as c:
        c.execute(text("UPDATE marketplace_accounts SET external_account_id=:e,credential_ref=:r WHERE marketplace_account_id=91101"), {"e": external, "r": ref})
        rid, _ = legacy.run(c, **expected)
        assert dict(c.execute(text(f"SELECT {','.join(FIELDS)} FROM review_sync_runs_v2 WHERE sync_run_id=:id"), {"id": rid}).mappings().one()) == expected
        c.rollback()


@pytest.mark.parametrize("mask", [i for i in range(1, 32) if i not in (27, 31)])
def test_every_partial_null_shape_rejected(db, mask):
    values = {field: value if mask & (1 << i) else None
              for i, (field, value) in enumerate(stamp(ref="ref").items())}
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.execute(text("UPDATE marketplace_accounts SET credential_ref=:r WHERE marketplace_account_id=91101"), {"r": values[FIELDS[2]]})
        legacy.run(c, **values)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("field,value", [(FIELDS[0], 0), (FIELDS[0], 2), (FIELDS[1], ""),
    (FIELDS[3], b""), (FIELDS[4], "a" * 64), (FIELDS[4], "A" * 64), (FIELDS[4], "0" * 63)])
def test_invalid_shape_bytes_and_hash(db, field, value):
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        legacy.run(c, **(stamp() | {field: value}))
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("transform", [
    lambda p: p + b"\n", lambda p: b"\xef\xbb\xbf" + p,
    lambda p: p.replace(b'"organizationId":91001', b'"organizationId":"91001"'),
    lambda p: p.replace(b'"marketplaceAccountId":91101', b'"marketplaceAccountId":91101.0'),
    lambda p: p.replace(b'"schemaVersion":1', b'"schemaVersion":true'),
    lambda p: b'{"extra":null,' + p[1:], lambda p: b'{"schemaVersion":1,' + p[1:],
    lambda p: p.replace(b'"credentialRef":null,', b''),
    lambda p: json.dumps(dict(reversed(list(json.loads(p).items()))), separators=(",", ":")).encode(),
    lambda p: b"[" + p + b"]", lambda p: p.replace(b"synthetic-a", b"\\u0073ynthetic-a"),
])
def test_recomputed_hash_cannot_admit_noncanonical_bytes(db, transform):
    values = stamp()
    payload = transform(values[FIELDS[3]])
    values.update({FIELDS[3]: payload, FIELDS[4]: hashlib.sha256(payload).hexdigest()})
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        legacy.run(c, **values)
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("args", [(0, 1, "wb", "x", None), (1, -1, "wb", "x", None),
    (None, 1, "wb", "x", None), (1, None, "wb", "x", None), (1, 1, None, "x", None),
    (1, 1, "WB", "x", None), (1, 1, "wb", "", None), (1, 1, "wb", None, None)])
def test_invalid_arguments(db, args):
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.execute(text("SELECT public.review_run_binding_bytes(:o,:a,:p,:e,:r)"), dict(zip(("o", "a", "p", "e", "r"), args, strict=True)))
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == "review_run_binding_invalid"


def test_pure_codec_does_not_borrow_orders_length_or_strip_grammar(db):
    for external, ref in [("x" * 129, "r" * 256), (" \n\t", ""), ("x", " ")]:
        with db.connect() as c:
            payload, checksum = c.execute(text("SELECT p,encode(sha256(p),'hex') FROM (SELECT public.review_run_binding_bytes(91001,91101,'avito',:e,:r) p) q"), {"e": external, "r": ref}).one()
            expected = stamp(external=external, ref=ref)
            assert (bytes(payload), checksum) == (expected[FIELDS[3]], expected[FIELDS[4]])


def test_column_and_function_contract_and_future_successor(db):
    graph = scripts()
    assert graph.get_revision(REVISION).down_revision == "20260909_0067"
    graph.revision_map.add_revision(Script(SimpleNamespace(down_revision=graph.get_current_head(), branch_labels=None, depends_on=None), "future_review_binding", "<memory>"))
    assert len(graph.get_heads()) == 1
    assert REVISION in {r.revision for r in graph.walk_revisions()}
    with db.connect() as c:
        cols = c.execute(text("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid),co.collname FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            LEFT JOIN pg_collation co ON co.oid=a.attcollation
            WHERE a.attrelid='review_sync_runs_v2'::regclass AND a.attname=ANY(:fields)"""), {"fields": list(FIELDS)}).all()
        assert {r[0]: r[1] for r in cols} == dict(zip(FIELDS, ("smallint", "text", "text", "bytea", "text"), strict=True))
        assert all(not r[2] and r[3] is None for r in cols)
        assert all(r[4] == "C" for r in cols if r[0] in FIELDS[1:3])
        for signature in HELPERS:
            assert c.execute(text("SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:s)"), {"s": "public." + signature}).one() == ("i", False, ["search_path=pg_catalog, public"])
        for name in ("review_run_binding_insert_guard()", "review_run_binding_update_guard()"):
            assert c.execute(text("SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:s)"), {"s": "public." + name}).one() == ("v", False, ["search_path=pg_catalog, public"])


def rows(c):
    # Driver values retain timestamps/UUIDs/bytes; do not serialize through JSON.
    return {t: [dict(r) for r in c.exec_driver_sql(f"SELECT * FROM {t} ORDER BY 1,2,3,4").mappings()]
            for t in (*legacy.TABLES, *candidate.TABLES)}


def old_rows(c):
    result = rows(c)
    result[legacy.TABLES[0]] = [{k: v for k, v in row.items() if k not in FIELDS}
                              for row in result[legacy.TABLES[0]]]
    return result


def schema(c):
    return {
        "relations": c.exec_driver_sql("SELECT oid,relname,relowner,relacl::text,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").all(),
        "columns": c.execute(text("SELECT attrelid,attnum,attname,atttypid,attnotnull,attacl::text FROM pg_attribute WHERE attrelid IN (SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace) AND attnum>0 AND NOT attisdropped AND NOT (attrelid='review_sync_runs_v2'::regclass AND attname=ANY(:f)) ORDER BY 1,2"), {"f": list(FIELDS)}).all(),
        "functions": c.execute(text("SELECT oid,proowner,proacl::text,pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname NOT LIKE 'review_binding_%' AND proname NOT LIKE 'review_run_binding_%' ORDER BY oid")).all(),
        "defaults": c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all(),
        "policies": c.exec_driver_sql("SELECT * FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname").all(),
        "triggers": c.exec_driver_sql("SELECT t.oid,t.tgenabled,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace='public'::regnamespace AND t.tgname NOT IN ('review_run_binding_insert','review_run_binding_update') ORDER BY t.oid").all(),
        "constraints": c.exec_driver_sql("SELECT oid,pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace='public'::regnamespace AND conname<>'ck_review_run_binding' ORDER BY oid").all(),
    }


def binding_schema(c):
    return {
        "columns": c.execute(text("SELECT a.attname,a.atttypid,a.attcollation,a.attacl::text,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attrelid='review_sync_runs_v2'::regclass AND a.attname=ANY(:fields) ORDER BY a.attname"), {"fields": list(FIELDS)}).all(),
        "functions": c.execute(text("SELECT oid,proowner,proacl::text,pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace='public'::regnamespace AND (proname LIKE 'review_binding_%' OR proname LIKE 'review_run_binding_%') ORDER BY oid")).all(),
        "triggers": c.exec_driver_sql("SELECT oid,tgenabled,pg_get_triggerdef(oid) FROM pg_trigger WHERE tgrelid='review_sync_runs_v2'::regclass AND tgname IN ('review_run_binding_insert','review_run_binding_update') ORDER BY oid").all(),
        "constraints": c.exec_driver_sql("SELECT oid,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='review_sync_runs_v2'::regclass AND conname='ck_review_run_binding'").all(),
    }


@pytest.mark.parametrize("populated", [False, True])
def test_unstamped_empty_and_populated_history_cycles(cluster, populated):
    role = "review_binding_cycle_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260909_0067")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                legacy.seed(c)
                c.exec_driver_sql(f"GRANT INSERT({','.join((*legacy.RUN_COLUMNS, 'source_run_id_utf8', 'coverage_utf8'))}),UPDATE ON review_sync_runs_v2 TO {role}")
                if populated:
                    orders.complete_evidence(c)
                    for index in range(8):
                        table, _, pk, values = orders.parents(c, index, uuid4().hex)
                        orders.insert(c, table, values, pk)
                    unit(c, {"review_sync_runs_v2": {"source_run_id": None, "source_run_id_utf8": b"run\x00history", "coverage": None, "coverage_utf8": b'{"a":"\\u0000"}'},
                        "review_facts": {"external_review_id": None, "external_review_id_utf8": b"review\x00history"},
                        "review_observations": {"text_utf8": b"body\x00history"}})
                before, data = schema(c), old_rows(c)
                if populated:
                    assert all(data[t] for t in legacy.TABLES)
                    assert all(data[t] for t in candidate.TABLES)
            for action, target in [("upgrade", REVISION), ("downgrade", "20260909_0067"), ("upgrade", REVISION)]:
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    assert schema(c) == before
                    assert old_rows(c) == data
                    if target == REVISION:
                        assert c.exec_driver_sql("SELECT count(*) FROM review_sync_runs_v2 WHERE " + " OR ".join(f"{f} IS NOT NULL" for f in FIELDS)).scalar_one() == 0
        finally:
            owner.dispose()


@pytest.mark.parametrize("mode", ["visible", "hidden", *FIELDS])
def test_downgrade_refuses_before_any_ddl(cluster, mode):
    role = "review_binding_down_" + uuid4().hex
    with migrated(cluster, REVISION, (role,)) as database:
        owner = database.owner
        with owner.begin() as c:
            if mode in FIELDS:
                # Controller-authorized downgrade-only malformed fixture. Save and
                # restore exact guard/owner/ACL before exercising the real refusal.
                signature = "public.review_run_binding_insert_guard()"
                guard = c.execute(text("SELECT pg_get_functiondef(oid),proowner,proacl::text FROM pg_proc WHERE oid=to_regprocedure(:s)"), {"s": signature}).one()
                c.exec_driver_sql("ALTER TABLE review_sync_runs_v2 DROP CONSTRAINT ck_review_run_binding")
                c.exec_driver_sql("CREATE OR REPLACE FUNCTION public.review_run_binding_insert_guard() RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ BEGIN RETURN NEW; END $$")
                legacy.run(c, **{mode: stamp()[mode] if mode != FIELDS[2] else "synthetic-ref"})
                c.exec_driver_sql(guard[0])
                assert c.execute(text("SELECT pg_get_functiondef(oid),proowner,proacl::text FROM pg_proc WHERE oid=to_regprocedure(:s)"), {"s": signature}).one() == guard
            else:
                legacy.run(c, **stamp())
            if mode == "hidden":
                c.exec_driver_sql(f"ALTER TABLE review_sync_runs_v2 OWNER TO {role}")
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            before, data = schema(c), rows(c)
            envelope = binding_schema(c)
        with pytest.raises(DBAPIError) as error, owner.begin() as c:
            if mode == "hidden":
                c.exec_driver_sql(f"SET LOCAL ROLE {role}")
                candidate.scope(c, 91002)
                assert c.exec_driver_sql("SELECT count(*) FROM review_sync_runs_v2").scalar_one() == 0
            with Operations.context(MigrationContext.configure(c)):
                scripts().get_revision(REVISION).module.downgrade()
        assert error.value.orig.sqlstate == ("42501" if mode == "hidden" else "55000")
        if mode != "hidden":
            assert error.value.orig.diag.message_primary == "review_run_binding_downgrade_bound"
        with owner.connect() as c:
            assert schema(c) == before and rows(c) == data
            assert binding_schema(c) == envelope
            assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == REVISION
