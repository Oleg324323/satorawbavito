"""Actual PostgreSQL/role reads on an owned 10k-row synthetic database only."""

import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.publication_guard import PublicationGuardError
from app.wb_live.contracts import WbLiveError
from app.wb_live.products_http import ProductsQuery
from app.wb_live.products_read import (
    ProductsReadError,
    page_statement,
    read_products_page,
)
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_exact_text_migration import migrated_database
from tests.test_wb_live_products import CODEC

cluster = candidate.cluster


@pytest.fixture(scope="module")
def product_db(cluster):
    with migrated_database(cluster, "head") as (owner, runtime):
        user, login = "synthetic-live-user", "synthetic-live-session"
        now = datetime.now(UTC)
        credential, job = uuid4(), uuid4()
        with Session(owner) as s, s.begin():
            s.execute(
                text("""INSERT INTO marketplace_accounts
              (marketplace_account_id,organization_id,marketplace,external_account_id,status)
              VALUES (92101,91001,'wb','synthetic-live-a','connected'),
                     (92102,91001,'wb','synthetic-live-b','connected'),
                     (92201,91002,'wb','synthetic-live-c','connected')""")
            )
            s.add(
                LkUserRow(
                    user_id=user,
                    organization_id=91001,
                    email="synthetic-live@example.invalid",
                    password_hash="synthetic-unusable",
                    full_name="Synthetic",
                    permission_profile="custom",
                )
            )
            s.flush()
            member = IamMembershipRow(
                organization_id=91001,
                user_id=user,
                role="custom",
                permissions=["catalog:read"],
                scope_mode="selected",
                allowed_account_ids=[92101],
            )
            s.add(member)
            s.add(
                LkSessionRow(
                    session_id=login,
                    user_id=user,
                    issued_at=now,
                    last_seen_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            s.flush()
            s.execute(
                text("""INSERT INTO marketplace_account_credentials
              (credential_id,organization_id,marketplace_account_id,provider,credential_kind,
               key_version,nonce,ciphertext,generation,algorithm,aad_version,payload_schema_version)
              VALUES (:id,91001,92101,'wb','wb_api',1,:nonce,:ciphertext,1,'AES-256-GCM',1,1)"""),
                {
                    "id": credential,
                    "nonce": b"synthetic123",
                    "ciphertext": b"synthetic-unusable-ciphertext",
                },
            )
            s.execute(
                text("""INSERT INTO wb_live_sync_jobs
              (organization_id,marketplace_account_id,job_id,credential_id,credential_generation,
               account_incarnation,external_account_id,user_id,membership_id,session_id,state)
              VALUES (91001,92101,:job,:credential,1,1,'synthetic-live-a',:user,:member,:login,'partial')"""),
                {
                    "job": job,
                    "credential": credential,
                    "user": user,
                    "member": member.membership_id,
                    "login": login,
                },
            )
            for name, state in (("content", "completed"), ("prices", "queued")):
                s.execute(
                    text("""INSERT INTO wb_live_sync_sources
                  (organization_id,marketplace_account_id,job_id,source,run_id,state,processed,revision)
                  VALUES (91001,92101,:job,:source,:run,:state,10000,1)"""),
                    {"job": job, "source": name, "run": uuid4(), "state": state},
                )
            s.execute(
                text("""INSERT INTO wb_live_products
              (organization_id,marketplace_account_id,nm_id,vendor_code,title,brand,content_updated_at)
              SELECT 91001,92101,i,'000-synthetic-'||i,
                CASE WHEN i%5=0 THEN NULL ELSE 'Synthetic '||(i%3) END,
                CASE WHEN i%7=0 THEN NULL ELSE 'Synthetic brand' END,clock_timestamp()
              FROM generate_series(1,10000) AS i""")
            )
            s.execute(
                text("""INSERT INTO wb_live_products (organization_id,marketplace_account_id,nm_id,title)
              VALUES (91001,92102,1,'Other account synthetic'),(91002,92201,1,'Other org synthetic')""")
            )
            s.execute(
                text("""INSERT INTO wb_live_product_sizes
              (organization_id,marketplace_account_id,nm_id,chrt_id,tech_size,skus,price_kopecks,discounted_price_kopecks)
              SELECT 91001,92101,i,i,'M','["000-synthetic"]'::jsonb,NULL,0 FROM generate_series(1,10000) i""")
            )
            s.execute(
                text("""INSERT INTO wb_live_product_sizes
              (organization_id,marketplace_account_id,nm_id,chrt_id,tech_size,skus)
              SELECT 91001,92101,1,10000+i,'Synthetic extra',
              (SELECT jsonb_agg('000-synthetic-'||j) FROM generate_series(1,101) j)
              FROM generate_series(1,101) i""")
            )
            s.execute(
                text(
                    "UPDATE wb_live_product_sizes SET skus=NULL WHERE marketplace_account_id=92101 AND nm_id=2"
                )
            )
        with owner.begin() as c:
            c.exec_driver_sql("ANALYZE wb_live_products")
            c.exec_driver_sql("ANALYZE wb_live_product_sizes")
        actor = ActorContext(
            actor_id="user:" + user,
            user_id=user,
            organization_id=91001,
            permission_profile="custom",
            permissions=frozenset({"catalog:read"}),
            session_id=login,
        )
        yield owner, runtime, actor


def read(runtime, actor, *, account=92101, query=None, cursor=None):
    with Session(runtime) as session:
        result = read_products_page(
            session,
            actor=actor,
            account_id=account,
            query=query or ProductsQuery(),
            codec=CODEC,
            cursor=cursor,
        )
        assert not session.in_transaction()
        return result


def test_real_guard_bounded_page_query_count_and_measurement(product_db):
    _, runtime, actor = product_db
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(runtime, "before_cursor_execute", trace)
    try:
        start = perf_counter()
        page = read(runtime, actor)
        elapsed = perf_counter() - start
    finally:
        event.remove(runtime, "before_cursor_execute", trace)
    assert len(page.items) == 50 and page.next_cursor
    assert page.readiness == "partial"
    assert len(page.items[0].sizes) == 5 and page.items[0].sizes_truncated
    assert page.items[0].sizes[1].skus_truncated and page.items[0].sizes[1].skus == []
    assert page.items[1].sizes[0].price_kopecks is None
    assert page.items[1].sizes[0].skus is None
    assert page.items[1].sizes[0].discounted_price_kopecks == "0"
    assert sum("WITH latest_job AS" in sql for sql in statements) == 1
    second = read(runtime, actor, cursor=page.next_cursor)
    assert not {x.nm_id for x in page.items} & {x.nm_id for x in second.items}
    print(
        "WB_PRODUCTS_SYNTHETIC_MEASUREMENT="
        + json.dumps(
            {
                "rows": 10000,
                "limit": 50,
                "elapsed_ms": round(elapsed * 1000, 2),
                "response_bytes": len(page.model_dump_json(by_alias=True).encode()),
                "statements_including_auth": len(statements),
                "data_statements": 1,
            }
        )
    )


@pytest.mark.parametrize("account", [92102, 92201])
def test_fresh_membership_blocks_other_account_and_other_org(product_db, account):
    _, runtime, actor = product_db
    with pytest.raises((WbLiveError, PublicationGuardError)):
        read(runtime, actor, account=account)


def test_rls_is_independent_of_application_where(product_db):
    _, runtime, _ = product_db
    with runtime.begin() as c:
        assert (
            c.execute(text("SELECT count(*) FROM wb_live_products")).scalar_one() == 0
        )
        c.execute(
            text(
                "SELECT set_config('app.organization_id','91001',true),set_config('app.marketplace_account_id','92101',true)"
            )
        )
        assert (
            c.execute(text("SELECT count(*) FROM wb_live_products")).scalar_one()
            == 10000
        )


@pytest.mark.parametrize("sort", ["nmId", "title", "brand", "vendorCode"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_actual_sql_nullable_ties_keyset_and_filters(product_db, sort, direction):
    _, runtime, actor = product_db
    query = ProductsQuery(
        limit=3,
        sort=sort,
        direction=direction,
        q="000-synthetic-",
        brand="Synthetic brand",
    )
    first = read(runtime, actor, query=query)
    second = read(runtime, actor, query=query, cursor=first.next_cursor)
    assert len(first.items) == len(second.items) == 3
    assert not {x.nm_id for x in first.items} & {x.nm_id for x in second.items}
    assert all(x.brand == "Synthetic brand" for x in first.items + second.items)


def test_source_revision_conflict_and_explain(product_db):
    owner, runtime, actor = product_db
    first = read(runtime, actor)
    with owner.begin() as c:
        c.execute(
            text(
                "UPDATE wb_live_sync_sources SET revision=revision+1 WHERE organization_id=91001 AND marketplace_account_id=92101 AND source='content'"
            )
        )
    with pytest.raises(ProductsReadError, match="WB_PRODUCTS_CHANGED"):
        read(runtime, actor, cursor=first.next_cursor)
    statement, params = page_statement(ProductsQuery(), None)
    with runtime.begin() as c:
        c.execute(
            text(
                "SELECT set_config('app.organization_id','91001',true),set_config('app.marketplace_account_id','92101',true)"
            )
        )
        plan = c.execute(
            text("EXPLAIN (ANALYZE, FORMAT JSON) " + str(statement)),
            dict(params, org=91001, account=92101),
        ).scalar_one()
    print("WB_PRODUCTS_SYNTHETIC_EXPLAIN=" + json.dumps(plan))
