"""Literal T2 vectors and physical PostgreSQL boundaries; disposable data only."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_exact_text_migration as orders_exact
from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as review_facts
from tests import test_review_lossless_migration as review_lossless

cluster = candidate.cluster
FIXTURE = Path(__file__).parent / "fixtures/wb_repricing_sql_golden_vectors_v1.json"
VECTORS = json.loads(FIXTURE.read_text())["vectors"]
TABLES = (
    "wb_repricer_price_approvals",
    "wb_repricer_price_apply_attempts",
    "wb_repricer_price_approval_audit",
)
OLD_BUSINESS_PRIMARY_KEYS = {
    "lk_organizations": ("organization_id",),
    "marketplace_accounts": ("marketplace_account_id",),
    "order_sync_runs": ("sync_run_id",),
    "marketplace_orders": ("order_id",),
    "marketplace_order_items": ("order_item_id",),
    "order_observations": ("observation_id",),
    "review_sync_runs_v2": ("sync_run_id",),
    "review_facts": ("review_id",),
    "review_observations": ("observation_id",),
    "review_sync_run_items": (
        "organization_id",
        "marketplace_account_id",
        "marketplace",
        "sync_run_id",
        "review_id",
    ),
}
REVIEW_NUL_BYTES = "preserved\x00отзыв🚀".encode()


@pytest.fixture(scope="module")
def db(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", "20260909_0066")
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url, hide_parameters=True)
        try:
            with engine.begin() as c:
                seed(c)
            yield engine
        finally:
            engine.dispose()


def seed(c):
    c.exec_driver_sql(
        "INSERT INTO lk_organizations(organization_id,slug,name) VALUES (7,'repricer-one','Synthetic'),(8,'repricer-two','Synthetic')"
    )
    c.exec_driver_sql(
        "INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES(42,7,'wb','synthetic-42','connected'),(43,7,'wb','synthetic-43','connected'),(44,8,'wb','synthetic-44','connected')"
    )
    c.exec_driver_sql(
        "INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) VALUES(99,7,'synthetic'),(100,8,'foreign')"
    )
    c.exec_driver_sql(
        "INSERT INTO lk_users(user_id,organization_id,email,password_hash,full_name,permission_profile) VALUES('repricer77',7,'77@invalid','synthetic','Synthetic','admin'),('repricer78',7,'78@invalid','synthetic','Synthetic','admin'),('repricer79',8,'79@invalid','synthetic','Synthetic','admin')"
    )
    c.exec_driver_sql(
        "INSERT INTO iam_memberships(membership_id,organization_id,user_id,role,permissions,scope_mode,allowed_account_ids,is_active) VALUES(77,7,'repricer77','admin','[]','all','[]',true),(78,7,'repricer78','admin','[]','all','[]',true),(79,8,'repricer79','admin','[]','all','[]',true)"
    )


def seed_old_business_rows(c):
    review_facts.seed(c)
    candidate.scope(c)
    run_id = orders_exact.make_run(
        c, source_run_key="repricer-preservation-orders-run"
    )
    order_id = candidate.order(c, external="repricer-preservation-order")
    item_id = candidate.item(
        c,
        order_id,
        source_line_key="repricer-preservation-line",
        external_item_id="repricer-preservation-item",
    )
    observation_id = candidate.observation(
        c,
        order_id,
        run_id,
        checksum="1" * 64,
        revision="repricer-preservation-revision",
        event="repricer-preservation-event",
    )
    review = review_lossless.unit(
        c,
        {"review_observations": {"text": None, "text_utf8": REVIEW_NUL_BYTES}},
    )
    return {
        "order_run_id": run_id,
        "order_id": order_id,
        "order_item_id": item_id,
        "order_observation_id": observation_id,
        "review": review,
    }


def snapshot_old_rows(c):
    return {
        table: tuple(
            tuple(row)
            for row in c.exec_driver_sql(
                f"SELECT * FROM public.{table} ORDER BY {','.join(primary_key)}"
            ).all()
        )
        for table, primary_key in OLD_BUSINESS_PRIMARY_KEYS.items()
    }


def snapshot_old_acls(c):
    relation = tuple(
        c.execute(
            text(
                """SELECT n.nspname,c.relname,c.relkind,c.relacl::text
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r','p','S')
                  AND NOT (c.relname=ANY(:new_tables))
                ORDER BY n.nspname,c.relname,c.relkind"""
            ),
            {"new_tables": list(TABLES)},
        ).all()
    )
    column = tuple(
        c.execute(
            text(
                """SELECT n.nspname,t.relname,a.attname,a.attacl::text
                FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid
                JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE n.nspname='public' AND t.relkind IN ('r','p')
                  AND a.attnum>0 AND NOT a.attisdropped
                  AND NOT (t.relname=ANY(:new_tables))
                ORDER BY n.nspname,t.relname,a.attnum"""
            ),
            {"new_tables": list(TABLES)},
        ).all()
    )
    defaults = tuple(
        c.exec_driver_sql(
            """SELECT d.defaclrole::regrole::text,coalesce(n.nspname,''),
                      d.defaclobjtype,d.defaclacl::text
               FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
               ORDER BY 1,2,3,4"""
        ).all()
    )
    return relation, column, defaults


def assert_new_relations_empty_or_absent(c, revision):
    for table in TABLES:
        relation = c.execute(
            text("SELECT to_regclass(:table)"), {"table": table}
        ).scalar_one()
        if revision == "20260909_0065":
            assert relation is None
        else:
            assert relation == table
            assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0


def params(vector):
    s, r = vector["scope"], vector["request"]
    return {
        "org": s["organization_id"],
        "account": s["marketplace_account_id"],
        "approval": s["approval_id"],
        "catalog": r["catalog_sku_id"],
        "nm": r["nm_id"],
        "article": r["article_id"],
        "price": r["price_kopecks"],
        "discount": r["discount_pct"],
        "size": r["size_id"],
        "minimum": r["min_price_kopecks"],
    }


def sql_request_hash(c, value):
    return c.execute(
        text("SELECT encode(sha256(:value),'hex')"), {"value": value}
    ).scalar_one()


def sql_action_key(c, vector):
    return c.execute(
        text("SELECT public.repricer_action_key(:org,:account,:approval,:checksum)"),
        dict(params(vector), checksum=vector["request_checksum"]),
    ).scalar_one()


def sql_dispatch_key(c, vector):
    return c.execute(
        text(
            "SELECT public.repricer_dispatch_key(:org,:account,:approval,:key,CAST(:attempt AS uuid))"
        ),
        dict(params(vector), key=vector["action_key"], attempt=vector["attempt_id"]),
    ).scalar_one()


def test_literal_fixture_matches_committed_authority():
    original = subprocess.check_output(
        [
            "git",
            "show",
            "e15495a89c16422b96811e1ba0ffa06580191a9e:backend/tests/fixtures/wb_repricing_sql_golden_vectors_v1.json",
        ],
        cwd=candidate.ROOT,
    )
    assert FIXTURE.read_bytes() == original


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_sql_exact_golden_bytes_and_keys(db, vector):
    with db.begin() as c:
        actual = c.execute(
            text(
                "SELECT public.repricer_request_bytes(:org,:account,:approval,:catalog,:nm,:article,:price,CAST(:discount AS smallint),:size,:minimum)"
            ),
            params(vector),
        ).scalar_one()
        assert bytes(actual) == vector["canonical_request_ascii"].encode("ascii")
        assert sql_request_hash(c, actual) == vector["request_checksum"]
        assert sql_action_key(c, vector) == vector["action_key"]
        assert sql_dispatch_key(c, vector) == vector["dispatch_key"]


@pytest.mark.parametrize("table", TABLES)
def test_three_relations_exist(db, table):
    with db.begin() as c:
        assert (
            c.exec_driver_sql(f"SELECT count(*) FROM public.{table}").scalar_one() == 0
        )


@pytest.mark.parametrize("typ", ["integer", "bigint"])
def test_real_integer_boundary_requires_lossless_numeric(db, typ):
    value = VECTORS[-1]["request"]["nm_id"]
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.execute(text(f"SELECT CAST(:value AS {typ})"), {"value": value})
    assert error.value.orig.sqlstate == "22003"
    with db.begin() as c:
        assert (
            c.execute(
                text("SELECT CAST(:value AS numeric)"), {"value": value}
            ).scalar_one()
            == value
        )


def test_real_full_text_btree_boundary(db):
    long_id = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.exec_driver_sql("CREATE TEMP TABLE repricer_boundary (id text UNIQUE)")
        c.execute(text("INSERT INTO repricer_boundary VALUES (:id)"), {"id": long_id})
    assert error.value.orig.sqlstate == "54000"


def scripts():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def assert_graph(graph):
    assert len(graph.get_heads()) == 1
    assert "20260909_0066" in {s.revision for s in graph.walk_revisions()}
    assert graph.get_revision("20260909_0066").down_revision == "20260909_0065"


def test_graph_accepts_a_single_future_successor():
    graph = scripts()
    assert_graph(graph)
    from types import SimpleNamespace

    future = Script(
        SimpleNamespace(
            down_revision=graph.get_current_head(), branch_labels=None, depends_on=None
        ),
        "synthetic_future",
        "synthetic",
    )
    graph.revision_map.add_revision(future)
    assert_graph(graph)
    assert graph.get_current_head() == "synthetic_future"


@pytest.mark.parametrize("value", ["1.5", "NaN", "Infinity", "-Infinity"])
def test_numeric_helper_rejects_nonintegral_or_nonfinite(db, value):
    with db.begin() as c:
        assert (
            c.execute(
                text("SELECT public.repricer_integral_finite(CAST(:value AS numeric))"),
                {"value": value},
            ).scalar_one()
            is False
        )
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        c.execute(
            text("SELECT public.repricer_integer_decimal(CAST(:value AS numeric))"),
            {"value": value},
        )
    assert error.value.orig.diag.message_primary == "repricer_numeric_invalid"


@pytest.mark.parametrize(
    "value,want",
    [
        ("1.000", "1"),
        ("1e30", "1000000000000000000000000000000"),
        ("-0.00", "0"),
        (None, "null"),
    ],
)
def test_plain_decimal_output(db, value, want):
    with db.begin() as c:
        assert (
            c.execute(
                text("SELECT public.repricer_integer_decimal(CAST(:value AS numeric))"),
                {"value": value},
            ).scalar_one()
            == want
        )


@pytest.mark.parametrize(
    "codepoint",
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
def test_python_edge_whitespace_exactness(db, codepoint):
    char = chr(codepoint)
    with db.begin() as c:
        for value, want in [
            (char + "a", False),
            ("a" + char, False),
            ("a" + char + "z", True),
        ]:
            assert (
                c.execute(
                    text("SELECT public.repricer_exact_text(:value)"), {"value": value}
                ).scalar_one()
                == want
            )


def test_physical_types_and_no_unbounded_identity_indexes(db):
    with db.begin() as c:
        columns = c.execute(
            text(
                "SELECT table_name,column_name,data_type,numeric_scale FROM information_schema.columns WHERE table_name=ANY(:tables)"
            ),
            {"tables": list(TABLES)},
        ).all()
        for table, column, typ, scale in columns:
            if (
                column
                in (
                    "nm_id",
                    "recommended_price_kopecks",
                    "size_id",
                    "min_price_kopecks",
                    "claim_version",
                    "before_version",
                    "after_version",
                )
                or column == "version"
                and table == TABLES[0]
            ):
                assert typ == "numeric" and scale is None
            if column in (
                "organization_id",
                "marketplace_account_id",
                "catalog_sku_id",
                "actor_membership_id",
                "claimed_by_membership_id",
                "decided_by_membership_id",
            ):
                assert typ == "integer"
        assert (
            c.execute(
                text("""SELECT count(*) FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
            WHERE t.relname=ANY(:tables) AND a.attname IN ('approval_id','article_id','wb_upload_id','version','claim_version','after_version','before_version')"""),
                {"tables": list(TABLES)},
            ).scalar_one()
            == 0
        )


def test_empty_roundtrip_keeps_new_relations_empty(cluster):
    with candidate.disposable_database(cluster) as database:
        for action, target in [
            ("upgrade", "20260909_0065"),
            ("upgrade", "20260909_0066"),
            ("downgrade", "20260909_0065"),
            ("upgrade", "20260909_0066"),
        ]:
            result = candidate.migrate(database.url, action, target)
            assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                assert (
                    c.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar_one()
                    == "20260909_0066"
                )
                for table in TABLES:
                    assert (
                        c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
                        == 0
                    )
        finally:
            owner.dispose()


def test_populated_old_rows_and_acls_survive_0066_roundtrip(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", "20260909_0065")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                seeded = seed_old_business_rows(c)
            with owner.connect() as c:
                before = snapshot_old_rows(c)
                acl_before = snapshot_old_acls(c)
                assert all(before[table] for table in OLD_BUSINESS_PRIMARY_KEYS)
                review_row = c.execute(
                    text(
                        "SELECT current_observation_id,last_source_run_id,last_source_run_sequence "
                        "FROM review_facts WHERE review_id=:review_id"
                    ),
                    {"review_id": seeded["review"]["review_facts"]["review_id"]},
                ).one()
                assert review_row[0] == seeded["review"]["review_observations"]["observation_id"]
                assert review_row[1] == seeded["review"]["review_sync_runs_v2"]["sync_run_id"]
                assert review_row[2] == seeded["review"]["review_sync_runs_v2"]["run_sequence"]
                text_value, raw_value = c.execute(
                    text(
                        "SELECT text,text_utf8 FROM review_observations "
                        "WHERE observation_id=:observation_id"
                    ),
                    {
                        "observation_id": seeded["review"]["review_observations"][
                            "observation_id"
                        ]
                    },
                ).one()
                assert text_value is None
                assert bytes(raw_value) == REVIEW_NUL_BYTES

            with owner.connect() as c:
                transaction = c.begin()
                try:
                    candidate.scope(c)
                    assert (
                        c.execute(
                            text(
                                "UPDATE order_sync_runs SET page_count=page_count+1 "
                                "WHERE sync_run_id=:run_id"
                            ),
                            {"run_id": seeded["order_run_id"]},
                        ).rowcount
                        == 1
                    )
                    with pytest.raises(AssertionError):
                        assert snapshot_old_rows(c) == before
                finally:
                    transaction.rollback()

            with owner.connect() as c:
                assert snapshot_old_rows(c) == before
                assert snapshot_old_acls(c) == acl_before

            for action, revision in (
                ("upgrade", "20260909_0066"),
                ("downgrade", "20260909_0065"),
                ("upgrade", "20260909_0066"),
            ):
                result = candidate.migrate(database.url, action, revision)
                assert result.returncode == 0, result.stderr
                with owner.connect() as c:
                    assert snapshot_old_rows(c) == before
                    assert snapshot_old_acls(c) == acl_before
                    assert_new_relations_empty_or_absent(c, revision)
        finally:
            owner.dispose()
