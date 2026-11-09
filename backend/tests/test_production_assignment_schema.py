"""Production storage proof using fresh disposable PostgreSQL only.

The SQL participant simulates trusted context, not authentication or permission.
It intentionally imports no T3 domain implementation or codec.
"""

import hashlib
import json

import pytest
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_repricer_approvals_schema as preservation

cluster = candidate.cluster
PREVIOUS = "20260909_0068"
TARGET = "20260909_0069"
TABLES = (
    "production_work_items",
    "production_assignment_receipts",
    "production_assignment_history",
)
GOLDEN = b'{"command":{"catalog_sku_id":3,"expected_version":2,"idempotency_key":"synthetic-key","reason":"synthetic-reason","work_item_id":1},"schema_version":1}'


@pytest.fixture(scope="module")
def db(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", TARGET)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(
                    "INSERT INTO lk_organizations(organization_id,slug,name) VALUES(91001,'production-one','Synthetic')"
                )
                c.exec_driver_sql(
                    "INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES(91101,91001,'avito','synthetic-a','connected')"
                )
                candidate.scope(c)
                order_id = candidate.order(c)
                item_id = candidate.item(c, order_id)
                assert c.execute(
                    text(
                        "SELECT quantity,version FROM marketplace_order_items WHERE order_item_id=:id"
                    ),
                    {"id": item_id},
                ).one() == (1, 1)
                assert c.execute(
                    text(
                        "SELECT canonical_status,last_seen_sync_run_id FROM marketplace_orders WHERE order_id=:id"
                    ),
                    {"id": order_id},
                ).one() == (None, None)
            print(
                f"Production setup and unbound Orders positive control passed: revision={TARGET}"
            )
            yield owner, order_id, item_id
        finally:
            owner.dispose()


def test_sql_literal_assignment_codec(db):
    owner, _, _ = db
    with owner.connect() as c:
        actual, sql_checksum = c.execute(
            text(
                "SELECT payload, encode(sha256(payload),'hex') FROM (SELECT public.production_assignment_bytes(1,2,3,'synthetic-key','synthetic-reason') AS payload) AS generated"
            )
        ).one()
    assert bytes(actual) == GOLDEN
    assert sql_checksum == hashlib.sha256(GOLDEN).hexdigest()


def test_native_jsonb_retained_numeric_representation(db):
    owner, _, _ = db
    with owner.connect() as c:
        row = c.exec_driver_sql("""SELECT ('{\"n\":1e0}'::jsonb->>'n'),
            ('{\"n\":1.0}'::jsonb->>'n'),
            jsonb_typeof('{\"n\":1e0}'::jsonb->'n')""").one()
    assert row == ("1", "1.0", "number")
    print(
        f"Native JSONB characterization: exponent={row[0]!r}; decimal={row[1]!r}; type={row[2]!r}; original exponent token is not recoverable"
    )


def test_initial_work_item_insert(db):
    owner, order_id, item_id = db
    with owner.begin() as c:
        candidate.scope(c)
        c.exec_driver_sql(
            "SELECT set_config('app.marketplace_account_id','91101',true)"
        )
        row = c.execute(
            text("""INSERT INTO production_work_items
            (organization_id,marketplace_account_id,order_id,order_item_id,
             source_item_version,required_quantity)
            VALUES(91001,91101,:order_id,:item_id,1,1)
            RETURNING version,catalog_sku_id,planned_quantity,remaining_quantity,
                      current_assignment_receipt_id"""),
            {"order_id": order_id, "item_id": item_id},
        ).one()
        assert row == (1, None, 0, 1, None)


@pytest.mark.parametrize(
    "value",
    [
        'quote"slash\\',
        "a\b\t\n\f\r\x01z",
        "DEL\x7fz",
        "e\u0301😀Кириллица",
        "x" * 20000,
    ],
)
def test_codec_independent_stdlib_unicode_and_long_text(db, value):
    owner, _, _ = db
    command = {
        "work_item_id": 1,
        "expected_version": 2,
        "catalog_sku_id": 3,
        "idempotency_key": value,
        "reason": value,
    }
    expected = json.dumps(
        {"command": command, "schema_version": 1},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    with owner.connect() as c:
        actual = c.execute(
            text("SELECT public.production_assignment_bytes(1,2,3,:v,:v)"), {"v": value}
        ).scalar_one()
    assert bytes(actual) == expected


@pytest.mark.parametrize(
    "code",
    [
        9,
        10,
        11,
        12,
        13,
        28,
        29,
        30,
        31,
        32,
        133,
        160,
        5760,
        *range(8192, 8203),
        8232,
        8233,
        8239,
        8287,
        12288,
    ],
)
def test_complete_python_whitespace_edge_set(db, code):
    owner, _, _ = db
    with owner.connect() as c:
        for value in (chr(code) + "x", "x" + chr(code), "x" + chr(code) + "x"):
            assert c.execute(
                text("SELECT public.production_exact_text(:v)"), {"v": value}
            ).scalar_one() == (value == value.strip())


def scripts():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_graph_allows_one_future_successor():
    from types import SimpleNamespace

    graph = scripts()
    assert len(graph.get_heads()) == 1
    assert graph.get_revision(TARGET).down_revision == PREVIOUS

    graph.revision_map.add_revision(
        Script(
            SimpleNamespace(
                down_revision=graph.get_current_head(),
                branch_labels=None,
                depends_on=None,
            ),
            "production_synthetic_future",
            "synthetic",
        )
    )
    assert len(graph.get_heads()) == 1
    assert graph.get_revision(TARGET).down_revision == PREVIOUS
    assert graph.get_current_head() == "production_synthetic_future"


def test_physical_types_generated_ids_and_no_text_identity_index(db):
    owner, _, _ = db
    with owner.connect() as c:
        columns = c.execute(
            text(
                "SELECT table_name,column_name,data_type,identity_generation,is_generated FROM information_schema.columns WHERE table_name=ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        ).all()
        types = {
            (table, name): (typ, identity, generated)
            for table, name, typ, identity, generated in columns
        }
        for table, identity in zip(
            TABLES, ("work_item_id", "receipt_id", "assignment_event_id"), strict=True
        ):
            assert types[table, identity][:2] == ("bigint", "ALWAYS")
            for col in ("organization_id", "marketplace_account_id"):
                assert types[table, col][0] == "integer"
        assert types[TABLES[0], "remaining_quantity"][2] == "ALWAYS"
        for col in ("source_item_version", "version", "order_id", "order_item_id"):
            assert types[TABLES[0], col][0] == "bigint"
        assert (
            c.execute(
                text("""SELECT count(*) FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
            WHERE t.relname=ANY(:tables) AND a.atttypid='text'::regtype"""),
                {"tables": list(TABLES)},
            ).scalar_one()
            == 0
        )
        for fn in (
            "production_exact_text",
            "production_ascii_json_string",
            "production_assignment_bytes",
        ):
            assert c.execute(
                text(
                    "SELECT provolatile,prosecdef,proconfig FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=:fn"
                ),
                {"fn": fn},
            ).one() == ("i", False, ["search_path=pg_catalog, public"])


@pytest.mark.parametrize("invalid", ["nul\x00text", "surrogate\ud800"])
def test_native_text_boundary_rejects_unrepresentable_commands(db, invalid):
    owner, _, _ = db
    with pytest.raises((DBAPIError, UnicodeError)) as error, owner.begin() as c:
        c.execute(
            text("SELECT production_assignment_bytes(1,2,3,:v,'reason')"),
            {"v": invalid},
        )
    if isinstance(error.value, DBAPIError):
        if error.value.orig.sqlstate is None:
            assert "\x00" in invalid
            assert "PostgreSQL text fields cannot contain NUL" in str(error.value.orig)
        else:
            assert error.value.orig.sqlstate in ("22021", "22000")


def old_acls(c):
    relations = c.execute(
        text("""SELECT relname,relacl::text FROM pg_class
        WHERE relnamespace='public'::regnamespace AND relkind IN ('r','S')
        AND relname NOT LIKE 'production_%' ORDER BY relname""")
    ).all()
    columns = c.exec_driver_sql("""SELECT t.relname,a.attname,a.attacl::text FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid
        WHERE t.relnamespace='public'::regnamespace AND t.relname NOT LIKE 'production_%%' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2""").all()
    defaults = c.exec_driver_sql(
        "SELECT defaclrole,defaclnamespace,defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid"
    ).all()
    return relations, columns, defaults


@pytest.mark.parametrize("populated", [False, True])
def test_empty_chain_and_populated_previous_head_roundtrip(cluster, populated):
    with candidate.disposable_database(cluster) as database:
        owner = create_engine(database.url, hide_parameters=True)
        try:
            if populated:
                migrated = candidate.migrate(database.url, "upgrade", PREVIOUS)
                assert migrated.returncode == 0, migrated.stderr
                with owner.begin() as c:
                    preservation.seed_old_business_rows(c)
                with owner.connect() as c:
                    before = preservation.snapshot_old_rows(c)
                    assert all(before.values())
                    acls = old_acls(c)
            for action, target in (
                ("upgrade", TARGET),
                ("downgrade", PREVIOUS),
                ("upgrade", TARGET),
            ):
                result = candidate.migrate(database.url, action, target)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    if populated:
                        assert preservation.snapshot_old_rows(c) == before
                        assert old_acls(c) == acls
                    for table in TABLES:
                        exists = c.execute(
                            text("SELECT to_regclass(:t)"), {"t": table}
                        ).scalar_one()
                        assert bool(exists) == (target == TARGET)
                        if exists:
                            assert (
                                c.exec_driver_sql(
                                    f"SELECT count(*) FROM {table}"
                                ).scalar_one()
                                == 0
                            )
        finally:
            owner.dispose()
