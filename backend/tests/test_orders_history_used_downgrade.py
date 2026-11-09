"""Used 0085 must refuse downgrade without changing real published history."""

from sqlalchemy import text

from app.orders.history_publication import persist_wb_history_chunk
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_history_projection_decisions as history

cluster = history.cluster
pg_database = history.pg_database
pg_store = history.pg_store
data = history.data
live = history.live
prepared = history.prepared

_TABLES = (
    "order_sync_runs",
    "marketplace_orders",
    "marketplace_order_items",
    "order_observations",
    "order_sync_memberships",
    "order_sync_coverage",
    "order_status_observations",
    "order_lifecycle_events",
    "user_orders_jobs",
    "user_orders_job_authorities",
    "user_orders_job_attempts",
    "user_orders_job_audit",
    "user_orders_history_selection_pages",
)


def _snapshot(engine, scope):
    with engine.connect() as connection:
        revision = tuple(
            connection.scalars(text("SELECT version_num FROM alembic_version"))
        )
        rows = {}
        for table in _TABLES:
            # Names are fixed above; only this disposable fixture's account is read.
            rows[table] = tuple(
                connection.execute(
                    text(f"""SELECT count(*),
                encode(sha256(convert_to(COALESCE(
                  jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb
                )::text,'UTF8')),'hex')
                FROM public.{table} t
                WHERE organization_id=:org AND marketplace_account_id=:account"""),
                    scope,
                ).one()
            )
        return revision, rows


def test_used_0085_downgrade_refuses_and_preserves_published_history(prepared, cluster):
    d, service, claim = prepared
    progress = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    assert progress.publication.state == "partial"
    assert progress.continuation_claim is not None
    scope = {"org": d.org, "account": d.org}
    before = _snapshot(d.engine, scope)
    assert before[0] == ("20260910_0085",)
    assert before[1]["order_observations"][0] == 2
    assert before[1]["order_sync_memberships"][0] == 2
    with d.engine.connect() as connection:
        assert (
            connection.scalar(
                text("""SELECT count(*) FROM user_orders_job_audit
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND event_kind='job.chunk_committed' AND history_result_sync_run_id=:run"""),
                dict(scope, run=progress.publication.run_id),
            )
            == 1
        )

    # Never accept a configured application/live URL as a migration target.
    _, root, port, owner = cluster
    url = d.engine.url
    assert url.database.startswith("orders_test_")
    assert url.username == owner and url.password is None and url.host is None
    assert url.query["host"] == ("/tmp" if root is None else str(root))
    assert int(url.query["port"]) == port
    result = candidate.migrate(
        url.render_as_string(hide_password=True), "downgrade", "20260910_0084"
    )
    assert result.returncode != 0
    refused_as_nonempty = "orders_history_downgrade_nonempty" in result.stderr
    assert refused_as_nonempty, "Downgrade did not fail at the used-history guard"
    assert _snapshot(d.engine, scope) == before
