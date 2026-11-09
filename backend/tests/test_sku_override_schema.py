"""Literal codec, normalized storage and migration proof on allocator-owned data."""
# Separate transaction contexts expose physical commit failures.

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_repricer_approvals_schema as preservation
from tests.test_orders_schema_integration import runtime_script

cluster = candidate.cluster
PREVIOUS = "20260909_0069"
TARGET = "20260909_0070"
V = "wb_repricing_sku_override_versions"
H = "wb_repricing_sku_override_heads"
A = "wb_repricing_sku_override_audit"
TABLES = (V, H, A)
PURE = ("wb_sku_override_decimal(numeric)", "wb_sku_override_integral(numeric)",
        "wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)")
TRIGGERS = ("wb_sku_override_account_lock()", "wb_sku_override_row_guard()",
            "wb_sku_override_validate()")
FIXTURE = candidate.ROOT / "tests/fixtures/t1_wb_repricing_override_golden_v1.json"
VECTORS = json.loads(FIXTURE.read_text())["vectors"]
MONEY = ("p_min_kopecks", "p_max_kopecks", "rrp_kopecks", "min_margin_kopecks", "max_margin_kopecks")
PERCENT = ("min_margin_pct", "max_margin_pct", "price_step_pct")
BOOL = ("automation_enabled", "allow_negative_margin", "night_median_enabled")
INT = ("price_step_minutes", "basket_norm_manual")
FIELDS = tuple(VECTORS[0]["inputs"]["values"])
TYPES = {**dict.fromkeys(MONEY + PERCENT, "numeric"), **dict.fromkeys(BOOL, "boolean"),
         **dict.fromkeys(INT, "integer"), "basket_norm_mode": "text"}
PROJECTION = "jsonb_build_object(" + ",".join(
    f"'{k}',CAST(:{k} AS {TYPES[k]})" for k in FIELDS) + ")"
CODEC = "public.wb_sku_override_bytes(CAST(:organization_id AS integer),CAST(:marketplace_account_id AS integer),CAST(:catalog_sku_id AS integer),CAST(:actor_membership_id AS integer),CAST(:command_id AS uuid),CAST(:expected_version AS numeric)," + PROJECTION + ")"


def scope(c, org=7, account=42):
    for setting, value in (("app.organization_id", org), ("app.marketplace_account_id", account)):
        c.execute(text("SELECT set_config(:s,:v,true)"), {"s": setting, "v": str(value)})


def seed(c):
    preservation.seed(c)
    c.exec_driver_sql("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES(45,7,'avito','synthetic-45','connected')")
    c.exec_driver_sql("INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) VALUES(101,7,'second')")


@pytest.fixture(scope="module")
def db(cluster):
    role = "sku_override_runtime_" + uuid4().hex
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


def params(vector=None, **overrides):
    inputs = (vector or VECTORS[0])["inputs"]
    p = {k: v for k, v in inputs.items() if k != "values"}
    p.update(inputs["values"])
    p.update(overrides)
    for key in MONEY + PERCENT + ("expected_version",):
        if p[key] is not None:
            p[key] = Decimal(str(p[key]))
    return p


def sql_bytes(c, p):
    return bytes(c.execute(text("SELECT " + CODEC), p).scalar_one())


def scripts():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_override_contract_present(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", TARGET)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.connect() as connection:
                actual = connection.exec_driver_sql(
                    "SELECT to_regprocedure('public.wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)')"
                ).scalar_one()
                assert actual is not None
        finally:
            owner.dispose()


def test_fixture_is_byte_identical_committed_authority():
    # Byte equality against git show was established at preparation; runtime
    # verification remains available in shallow clones and packaged sources.
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "a774bc630dccb42d4e36c7d00d15d50294159d7545562050320a54d73db4256d"


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_literal_sql_bytes_and_typed_projection(db, vector):
    with db[0].connect() as c:
        p = params(vector)
        actual = sql_bytes(c, p)
        types = c.execute(text("SELECT " + ",".join(
            f"jsonb_typeof(({PROJECTION})->'{key}')" for key in FIELDS)), p).one()
    assert actual == vector["canonical_ascii"].encode("ascii")
    assert len(actual) == vector["byte_count"]
    assert hashlib.sha256(actual).hexdigest() == vector["sha256"]
    for key, actual_type in zip(FIELDS, types, strict=True):
        want = "null" if p[key] is None else ("boolean" if key in BOOL else "string" if key == "basket_norm_mode" else "number")
        assert actual_type == want


@pytest.mark.parametrize("value,want", [("-0.00", "0"), ("1E-20", "0.00000000000000000001"), ("1E20", "100000000000000000000"), ("-123.4500", "-123.45"), ("0.12345678901234567890123456789", "0.12345678901234567890123456789")])
def test_decimal_exact_finite_normalization(db, value, want):
    with db[0].connect() as c:
        assert c.execute(text("SELECT wb_sku_override_decimal(CAST(:v AS numeric))"), {"v": value}).scalar_one() == want


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-Infinity"])
def test_scalar_helpers_fail_closed_for_nonfinite_and_null(db, value):
    with db[0].connect() as c:
        assert c.execute(text("SELECT wb_sku_override_integral(CAST(:v AS numeric))"), {"v": value}).scalar_one() is False
    with pytest.raises(DBAPIError), db[0].begin() as c:
        c.execute(text("SELECT wb_sku_override_decimal(CAST(:v AS numeric))"), {"v": value})


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("bad", [[], {}, "bad"])
def test_codec_rejects_wrong_field_types(db, field, bad):
    values = dict(VECTORS[0]["inputs"]["values"])
    values[field] = bad
    query = CODEC.replace(PROJECTION, "CAST(:values AS jsonb)")
    with pytest.raises(DBAPIError), db[0].begin() as c:
        c.execute(text("SELECT " + query), {**params(), "values": json.dumps(values)})


@pytest.mark.parametrize("values", [None, [], {}, {**VECTORS[0]["inputs"]["values"], "extra": None}])
def test_codec_requires_exact_object_key_set(db, values):
    query = CODEC.replace(PROJECTION, "CAST(:values AS jsonb)")
    with pytest.raises(DBAPIError), db[0].begin() as c:
        c.execute(text("SELECT " + query), {**params(), "values": json.dumps(values)})


@pytest.mark.parametrize("field,value", [(k, v) for k in INT for v in (1.5, True, "1", 2147483648)] + [(k, 0) for k in BOOL] + [(k, False) for k in MONEY + PERCENT])
def test_json_number_boolean_and_int4_domains_are_distinct(db, field, value):
    values = {**VECTORS[0]["inputs"]["values"], field: value}
    query = CODEC.replace(PROJECTION, "CAST(:values AS jsonb)")
    with pytest.raises(DBAPIError), db[0].begin() as c:
        c.execute(text("SELECT " + query), {**params(), "values": json.dumps(values)})


@pytest.mark.parametrize("field,value", [(k, v) for k in MONEY for v in ("-1", "0.5", "NaN", "Infinity", "-Infinity")] + [(k, v) for k in PERCENT for v in ("NaN", "Infinity", "-Infinity")] + [("expected_version", v) for v in ("-1", "1.5", "NaN", "Infinity", "-Infinity")] + [("price_step_minutes", "0"), ("basket_norm_manual", "-1"), ("basket_norm_mode", "unknown")])
def test_codec_rejects_invalid_numeric_and_enum(db, field, value):
    with pytest.raises(DBAPIError), db[0].begin() as c:
        sql_bytes(c, params(**{field: value}))


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id", "catalog_sku_id", "actor_membership_id"])
@pytest.mark.parametrize("value", [None, 0, -1, 2147483648])
def test_codec_owner_int4_bounds(db, field, value):
    with pytest.raises(DBAPIError), db[0].begin() as c:
        sql_bytes(c, params(**{field: value}))


@pytest.mark.parametrize("command", [None, "12345678-1234-1234-8234-123456789abc", "12345678-1234-4234-7234-123456789abc"])
def test_codec_requires_uuid4_rfc_variant(db, command):
    with pytest.raises(DBAPIError), db[0].begin() as c:
        sql_bytes(c, params(command_id=command))


def test_physical_types_forced_rls_and_helpers(db):
    with db[0].connect() as c:
        for table in TABLES:
            assert c.execute(text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:t)"), {"t": table}).one() == (True, True)
        rows = dict(c.execute(text("SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid=to_regclass(:t) AND attnum>0"), {"t": V}).all())
        assert all(rows[key] == kind for key, kind in TYPES.items())
        assert all(rows[key] == "numeric" for key in ("revision", "parent_revision"))
        for function in PURE:
            assert c.execute(text("SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE oid=to_regprocedure(:f)"), {"f": function}).one() == ("i", False, ["search_path=pg_catalog, public"])


def test_graph_retains_predecessor_and_allows_future_successor():
    graph = scripts()
    assert graph.get_revision(TARGET).down_revision == PREVIOUS
    assert len(graph.get_heads()) == 1
    assert TARGET in {r.revision for r in graph.walk_revisions()}
    graph.revision_map.add_revision(Script(type("Module", (), {"down_revision": graph.get_heads()[0], "branch_labels": None, "depends_on": None})(), "future_sku_override_test", "synthetic"))
    assert len(graph.get_heads()) == 1


def old_acls(c):
    relation, columns, defaults = preservation.snapshot_old_acls(c)
    return (tuple(r for r in relation if r[1] not in TABLES), tuple(r for r in columns if r[1] not in TABLES), defaults)


def test_empty_roundtrip_and_populated_predecessor_preservation(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                preservation.seed_old_business_rows(c)
                before = preservation.snapshot_old_rows(c), old_acls(c)
            for action, revision in (("upgrade", TARGET), ("downgrade", PREVIOUS), ("upgrade", TARGET)):
                result = candidate.migrate(database.url, action, revision)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    assert (preservation.snapshot_old_rows(c), old_acls(c)) == before
                    for table in TABLES:
                        present = c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one()
                        assert bool(present) == (revision == TARGET)
                        if present:
                            assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
        finally:
            owner.dispose()
