"""Lossless Review schema acceptance in owned disposable PostgreSQL only."""
# Transaction boundaries and savepoint rollback are deliberate.
# ruff: noqa: SIM117

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as legacy

cluster = candidate.cluster
REVISION = "20260909_0065"
PAIRS = (
    ("review_facts", "external_review_id", True, True),
    ("review_observations", "external_product_id", False, True),
    ("review_observations", "text", False, False),
    ("review_observations", "source_status", False, True),
    ("review_observations", "source_schema_version", True, True),
    ("review_observations", "normalization_version", True, True),
    ("review_sync_runs_v2", "source_run_id", True, True),
    ("review_sync_runs_v2", "coverage", True, False),
)
KEYS = {"review_facts": "review_id", "review_observations": "observation_id",
        "review_sync_runs_v2": "sync_run_id"}


def scripts():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def revise(c, action):
    with Operations.context(MigrationContext.configure(c)):
        getattr(scripts().get_revision(REVISION).module, action)()


@contextmanager
def migrated(cluster, target, roles=()):
    with candidate.disposable_database(cluster, roles) as database:
        result = candidate.migrate(database.url, "upgrade", target)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                legacy.seed(c)
            yield SimpleNamespace(owner=owner, database=database)
        finally:
            owner.dispose()


@pytest.fixture(scope="module")
def old_db(cluster):
    with migrated(cluster, "20260909_0064") as db:
        yield db.owner


@pytest.fixture(scope="module")
def db(cluster):
    """Historical feature fixture is pinned; runtime script uses another DB."""
    with migrated(cluster, REVISION) as db:
        yield db.owner


def insert(c, table, values, decode_legacy=None):
    binds = [f"convert_from(:{key},'UTF8')" if key == decode_legacy else f":{key}"
             for key in values]
    return c.execute(text(f"INSERT INTO {table} ({','.join(values)}) VALUES "
                          f"({','.join(binds)}) RETURNING *"), values).mappings().one()


def unit(c, changes=None, account=91101, org=91001, decode_legacy=None, existing_run=None):
    """A complete root, observation, watermark and membership; no disabled guards."""
    changes = changes or {}
    owner = {"organization_id": org, "marketplace_account_id": account,
             "marketplace": "wb" if account == 91201 else "avito"}
    run_values = dict(owner, sync_run_id=uuid4(), source_run_id=uuid4().hex,
                      request_checksum="a" * 64, status="running", completeness="partial",
                      started_at="2026-09-09T00:00:00Z", coverage="{}")
    run_values.update(changes.get("review_sync_runs_v2", {}))
    run = existing_run if existing_run is not None else insert(c, "review_sync_runs_v2", run_values, decode_legacy)
    fact_values = dict(owner, review_id=uuid4(), external_review_id=uuid4().hex,
                       first_observed_at="2026-09-09T00:00:00Z",
                       last_observed_at="2026-09-09T00:00:00Z")
    fact_values.update(changes.get("review_facts", {}))
    fact = insert(c, "review_facts", fact_values, decode_legacy)
    obs_values = dict(owner, observation_id=uuid4(), review_id=fact["review_id"],
                      source_run_id=run["sync_run_id"], revision=1,
                      source_created_at="2026-09-09T00:00:00Z", answered=False,
                      observed_at="2026-09-09T00:00:00Z", text=None,
                      source_schema_version="v1", normalization_version="v1",
                      content_checksum="a" * 64)
    obs_values.update(changes.get("review_observations", {}))
    obs = insert(c, "review_observations", obs_values, decode_legacy)
    assert legacy.advance(c, fact["review_id"], obs["observation_id"],
                          last_source_run_id=run["sync_run_id"],
                          last_source_run_sequence=run["run_sequence"]) == 1
    legacy.item(c, fact["review_id"], obs["observation_id"], run["sync_run_id"], **owner)
    return {"review_sync_runs_v2": run, "review_facts": fact, "review_observations": obs}


@pytest.mark.parametrize("table,column,required,nonempty", PAIRS[:-1])
def test_previous_head_cannot_represent_admitted_nul(old_db, table, column, required, nonempty):
    with old_db.begin() as c:
        unit(c)  # Real successful legacy insert including deferred root completion.
        with pytest.raises(DBAPIError) as error:
            with c.begin_nested():
                unit(c, {table: {column: b"synthetic\x00value"}}, decode_legacy=column)
        assert error.value.orig.sqlstate == "22021"
        assert c.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_previous_head_cannot_represent_escaped_nul_coverage(old_db):
    with old_db.begin() as c:
        unit(c, {"review_sync_runs_v2": {"coverage": '{"streams":[{"name":"plain"}]}'}})
        with pytest.raises(DBAPIError) as error:
            with c.begin_nested():
                unit(c, {"review_sync_runs_v2": {
                    "coverage": '{"streams":[{"name":"synthetic\\u0000name"}]}'}})
        assert error.value.orig.sqlstate == "22P05"


def test_forward_revision_registered_after_previous_head():
    graph = scripts()
    assert len(graph.get_heads()) == 1
    assert graph.get_revision(REVISION).down_revision == "20260909_0064"
    assert REVISION in {item.revision for item in graph.walk_revisions()}
    successor = Script(SimpleNamespace(down_revision=graph.get_current_head(),
                       branch_labels=None, depends_on=None), "synthetic_future", "<in-memory>")
    graph.revision_map.add_revision(successor)
    assert graph.get_heads() == ["synthetic_future"]
    assert REVISION in {item.revision for item in graph.walk_revisions()}


@pytest.mark.parametrize("payload,want", [
    (b"", True), (b"\x00", True), (b"a\x00b", True), ("е\u0301🚀".encode(), True),
    (b"\xf4\x8f\xbf\xbf", True), (b"\x80", False), (b"\xc2", False),
    (b"\xc0\x80", False), (b"\xed\xa0\x80", False), (b"\xf4\x90\x80\x80", False),
    (b"\xc2\x00\xa0", False), (b"\x00" * 20000, True),
], ids=["empty", "nul", "interior-nul", "unicode", "max-scalar", "continuation",
        "truncated", "overlong", "surrogate", "above-max", "nul-split", "many-nuls"])
def test_strict_utf8_boundaries(db, payload, want):
    with db.connect() as c:
        assert c.execute(text("SELECT public.review_strict_utf8(:payload)"),
                         {"payload": payload}).scalar_one() is want


@pytest.mark.parametrize("scalar", ["¢", "€", "🚀", "\U0010ffff"])
def test_nul_at_each_multibyte_boundary(db, scalar):
    encoded = scalar.encode("utf-8")
    with db.connect() as c:
        for index in range(len(encoded) + 1):
            payload = encoded[:index] + b"\x00" + encoded[index:]
            assert c.execute(text("SELECT public.review_strict_utf8(:payload)"),
                             {"payload": payload}).scalar_one() is (index in (0, len(encoded)))


@pytest.mark.parametrize("payload,want", [
    (b"{}", True), ('{"streams":[{"name":"е́🚀\\u0000"}]}'.encode(), True),
    (b"[]", False), (b'"text"', False), (b"1", False), (b"true", False),
    (b"null", False), (b"{", False), (b'{"a":}', False), (b'{"a":"\x00"}', False),
    (b'{"a":"\x80"}', False),
])
def test_coverage_requires_actual_json_object(db, payload, want):
    with db.connect() as c:
        assert c.execute(text("SELECT public.review_coverage_json_object_utf8(:payload)"),
                         {"payload": payload}).scalar_one() is want


@pytest.mark.parametrize("value", ["\x00", "\x00start", "middle\x00value", "end\x00", "е́🚀"])
@pytest.mark.parametrize("table,column,required,nonempty", PAIRS)
def test_each_pair_roundtrips_exact_semantics(db, value, table, column, required, nonempty):
    semantic = {"streams": [{"name": value}]} if column == "coverage" else value
    payload = (json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
               if column == "coverage" else value).encode("utf-8")
    with db.begin() as c:
        rows = unit(c, {table: {column: None, column + "_utf8": payload}})
        row = c.execute(text(f"SELECT * FROM {table} WHERE {KEYS[table]}=:id"),
                        {"id": rows[table][KEYS[table]]}).mappings().one()
        assert row[column] is None
        assert row[column + "_utf8"] == payload
        decoded = row[column + "_utf8"].decode("utf-8")
        assert (json.loads(decoded) if column == "coverage" else decoded) == semantic
        assert rows["review_observations"]["content_checksum"] == "a" * 64


@pytest.mark.parametrize("table,column,required,nonempty", PAIRS)
@pytest.mark.parametrize("case", ["dual", "missing", "empty", "invalid"])
def test_pair_constraints(db, table, column, required, nonempty, case):
    data = {column: None, column + "_utf8": b"x"}
    reject = True
    if case == "dual":
        data[column] = "{}" if column == "coverage" else "x"
        data[column + "_utf8"] = b"{}" if column == "coverage" else b"x"
    elif case == "missing":
        data[column + "_utf8"] = None
        reject = required
    elif case == "empty":
        data[column + "_utf8"] = b""
        reject = nonempty or column == "coverage"
    else:
        data[column + "_utf8"] = b"\xc2\x00\xa0"
    if reject:
        with pytest.raises(DBAPIError) as error:
            with db.begin() as c:
                unit(c, {table: data})
        assert error.value.orig.sqlstate == "23514"
    else:
        with db.begin() as c:
            row = unit(c, {table: data})[table]
            assert row[column + "_utf8"] == data[column + "_utf8"]
