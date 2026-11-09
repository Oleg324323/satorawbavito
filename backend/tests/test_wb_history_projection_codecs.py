"""SQL0085 bytes versus frozen T3 codecs; owned fixtures, no publication stubs."""
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.orders.history_bridge import HistoryPageEvidence, decode_history_chunk
from app.platform.integrations.wb_history_projection_contract import (
    history_header_bytes,
)
from app.wb_live.repository import WbLiveRepository
from app.wb_live.statistics_orders import iter_orders_page
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_live_history_postgres as history
from tests import test_wb_live_platform as platform

cluster = candidate.cluster
pg_store = platform.pg_store
data = platform.data
live = platform.live
SCHEMA_REVISION = "20260910_0085"  # Explicit GREEN target after the actual0084 missing-helper RED.
CODECS = ("public.wb_history_projection_header_bytes(public.wb_live_history_pages)",
          "public.wb_history_projection_chunk_bytes(integer,integer,uuid,uuid,integer)")


@pytest.fixture(scope="module")
def pg_database(cluster):
    api, worker, dispatcher = ("codec_" + uuid4().hex for _ in range(3))
    with candidate.disposable_database(cluster, (api, worker, dispatcher)) as database:
        migrated = candidate.migrate(database.url, "upgrade", SCHEMA_REVISION)
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=api), hide_parameters=True)
        try:
            script = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            script = "\n".join(line for line in script.splitlines() if not line.startswith("\\"))
            for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                             ("wb_live_api", api), ("wb_live_worker", worker), ("wb_live_dispatch", dispatcher)):
                script = script.replace(old, new)
            with owner.begin() as connection:
                connection.execute(text(script))
                # Explicit synthetic codec caller uses existing captured-row reads.
                # RED must fail in the test at explicit presence assertions, not
                # during setup through GRANT on nonexistent0085 functions.
                for signature in CODECS:
                    if connection.scalar(text("SELECT to_regprocedure(:signature) IS NOT NULL"), {"signature": signature}):
                        connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION {signature} TO "{worker}"')
                connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION public.wb_state_json(jsonb),public.repricer_ascii_json_string(text),public.wb_sku_override_decimal(numeric) TO "{worker}"')
            runtime._history_api_role, runtime._history_worker_role = api, worker
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def capture(d, *, count=1, published=True):
    # Called from each test, not fixture setup: missing migration yields a clear
    # assertion failure before data capture or entry-point invocation.
    with d.engine.connect() as connection:
        for signature in CODECS:
            assert connection.scalar(text("SELECT to_regprocedure(:signature) IS NOT NULL"), {"signature": signature}), f"Missing frozen SQL codec: {signature}"
    locator = history.prepare(d)
    engine = d.factory.kw["bind"]
    worker = create_engine(engine.url.set(username=engine._history_worker_role), hide_parameters=True)
    try:
        d.repo = WbLiveRepository(sessionmaker(worker, expire_on_commit=False), d.keys)
        lease = d.repo.claim_batch(locator)
        page_id = history.begin(d, lease)
        raw = json.dumps([{"srid": f'unit-{n}-Ж-é-é-😀-"-\\-\t-\n-x', "nmId": 2**63 - 1,
                          "barcode": 'barcode-Ж-😀-"-\\-\t-x', "isCancel": False,
                          "lastChangeDate": "2026-09-02T03:00:00.12345+03:00"}
                         for n in range(count)], ensure_ascii=True).encode("ascii")
        chunks = (raw[start:start + 65536] for start in range(0, len(raw), 65536))
        events = list(iter_orders_page(chunks, organization_id=d.org, marketplace_account_id=d.org,
                                      date_from=history.CURSOR, observed_at=datetime(2026, 9, 10, 1, 2, 3, 456789, tzinfo=UTC)))
        for first in range(0, count, 1000):
            assert d.repo.stage_history_rows(lease, page_id=page_id, first_ordinal=first,
                                             rows=tuple(events[first:min(first + 1000, count)]))
        if published:
            assert d.repo.commit_history_page(lease, page_id=page_id, end=events[-1], next_due_at=datetime.now(UTC))
        return worker, {"org": d.org, "account": d.org, "job": UUID(locator.job_id), "page": UUID(str(page_id))}
    except BaseException:
        worker.dispose()
        raise


def scoped(connection, params):
    connection.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                       {"org": str(params["org"]), "account": str(params["account"])})


PREDICATE = "organization_id=:org AND marketplace_account_id=:account AND job_id=:job AND page_id=:page AND source='wb-statistics-supplier-orders'"


def evidence(connection, params):
    row = connection.execute(text("SELECT * FROM wb_live_history_pages WHERE " + PREDICATE), params).mappings().one()
    receipt = row["receipt"]
    return HistoryPageEvidence(organization_id=row["organization_id"], marketplace_account_id=row["marketplace_account_id"],
        job_id=str(row["job_id"]), page_id=str(row["page_id"]), run_id=str(row["run_id"]),
        credential_id=str(row["credential_id"]), credential_generation=row["credential_generation"],
        account_incarnation=row["account_incarnation"], request_checksum=row["request_checksum"],
        raw_checksum=receipt["raw_checksum"], input_date_from=row["input_date_from"],
        next_date_from=receipt["next_date_from"], row_count=receipt["row_count"], terminal=receipt["terminal"],
        published_at=row["published_at"], state=row["state"])


def sql_chunk(connection, params, first):
    return bytes(connection.scalar(text("SELECT public.wb_history_projection_chunk_bytes(:org,:account,:job,:page,:first)"),
                                   {**params, "first": first}))


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kathmandu"])
def test_header_bytes_exact_t3_under_connection_timezone(live, zone):
    worker, params = capture(live)
    try:
        with worker.begin() as connection:
            scoped(connection, params)
            connection.execute(text("SELECT set_config('TimeZone',:zone,true)"), {"zone": zone})
            page = evidence(connection, params)
            actual = connection.scalar(text("SELECT public.wb_history_projection_header_bytes(p) FROM wb_live_history_pages p WHERE " + PREDICATE), params)
            assert bytes(actual) == history_header_bytes(page)
    finally:
        worker.dispose()


@pytest.mark.parametrize("first", [0, 1000])
def test_unicode_control_huge_integer_bounded_chunk_matches_t3(live, first):
    worker, params = capture(live, count=1001)
    try:
        with worker.begin() as connection:
            scoped(connection, params)
            connection.execute(text("SET LOCAL TIME ZONE 'Asia/Kathmandu'"))
            page = evidence(connection, params)
            rows = tuple(dict(row) for row in connection.execute(text(
                "SELECT ordinal,srid,nm_id,barcode,semantic_checksum,source_row_checksum,observation,source_revision,effective_at "
                "FROM wb_live_history_rows WHERE " + PREDICATE + " AND ordinal>=:first AND ordinal<:stop ORDER BY ordinal"),
                {**params, "first": first, "stop": min(first + 1000, page.row_count)}).mappings())
            plan = decode_history_chunk(page=page, first_ordinal=first, rows=rows)
            encoded = sql_chunk(connection, params, first)
            assert hashlib.sha256(encoded).hexdigest() == plan.input_checksum
            assert len(json.loads(encoded)["rows"]) == min(1000, 1001 - first)
    finally:
        worker.dispose()


def test_empty_terminal_chunk_matches_t3(live):
    worker, params = capture(live, count=0)
    try:
        with worker.begin() as connection:
            scoped(connection, params)
            page = evidence(connection, params)
            assert page.terminal and page.row_count == 0
            expected = decode_history_chunk(page=page, first_ordinal=0, rows=())
            assert hashlib.sha256(sql_chunk(connection, params, 0)).hexdigest() == expected.input_checksum
    finally:
        worker.dispose()


def test_wrong_scope_cannot_encode_existing_page(live):
    worker, params = capture(live)
    try:
        for field in ("org", "account"):
            with worker.begin() as connection:
                scoped(connection, {**params, field: params[field] + 100000})
                with pytest.raises(DBAPIError):
                    sql_chunk(connection, params, 0)
    finally:
        worker.dispose()


def test_nonboundary_or_past_end_ordinal_rejected(live):
    worker, params = capture(live)
    try:
        for first in (-1, 1, 1000):
            with worker.begin() as connection:
                scoped(connection, params)
                with pytest.raises(DBAPIError):
                    sql_chunk(connection, params, first)
    finally:
        worker.dispose()


def test_staging_page_cannot_produce_committed_chunk(live):
    worker, params = capture(live, published=False)
    try:
        with worker.begin() as connection:
            scoped(connection, params)
            with pytest.raises(DBAPIError):
                sql_chunk(connection, params, 0)
    finally:
        worker.dispose()
