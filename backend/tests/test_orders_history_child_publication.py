"""Actual PG composition regression; requires T1's genuine 0085 runtime fixture."""

from sqlalchemy import text

from app.orders.history_publication import persist_wb_history_chunk
from tests import test_wb_history_projection_decisions as history

cluster = history.cluster
pg_database = history.pg_database
pg_store = history.pg_store
data = history.data
live = history.live
prepared = history.prepared


def test_genuine_initial_chunk_commits_full_child_evidence_and_receipt(prepared):
    d, service, claim = prepared
    progress = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    result = progress.publication
    assert (result.state, result.replayed, result.reconciliation_count) == (
        "partial",
        False,
        0,
    )
    assert progress.continuation_claim is not None
    scope = {
        "org": d.org,
        "account": d.org,
        "run": result.run_id,
        "job": claim.locator.job_id,
    }
    # Owner connection inspects this disposable fixture's committed rows only.
    # Publication above uses the real service, claim, guard and runtime role.
    with d.engine.connect() as connection:
        run = connection.execute(
            text("""SELECT state,manifest_state,order_count,item_count,
            page_count,expected_order_count FROM order_sync_runs
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
            scope,
        ).one()
        assert tuple(run) == ("partial", "partial", 1, 1, 1, None)
        members = (
            connection.execute(
                text("""SELECT m.order_id,m.order_item_id,m.observation_id,
            m.coverage_role,m.observed_at AS membership_observed_at,m.history_outcome,
            e.source_kind,e.adapter_version,e.source_event_id,e.source_revision,
            e.payload_checksum,e.evidence_schema_version,e.normalized_evidence,
            e.source_effective_at,e.observed_at AS evidence_observed_at
            FROM order_sync_memberships m JOIN order_observations e ON
              (e.organization_id,e.marketplace_account_id,e.sync_run_id,e.order_id,e.observation_id)=
              (m.organization_id,m.marketplace_account_id,m.sync_run_id,m.order_id,m.observation_id)
              AND e.order_item_id IS NOT DISTINCT FROM m.order_item_id
            WHERE m.organization_id=:org AND m.marketplace_account_id=:account AND m.sync_run_id=:run
            ORDER BY m.order_item_id NULLS FIRST"""),
                scope,
            )
            .mappings()
            .all()
        )
        assert len(members) == 2
        parent, child = members
        assert parent["order_item_id"] is None
        assert parent["history_outcome"] == "initial_projection"
        assert child["order_item_id"] is not None
        assert child["observation_id"] != parent["observation_id"]
        assert child["history_outcome"] is None
        for field in (
            "order_id",
            "coverage_role",
            "membership_observed_at",
            "source_kind",
            "adapter_version",
            "source_event_id",
            "source_revision",
            "payload_checksum",
            "evidence_schema_version",
            "normalized_evidence",
            "source_effective_at",
            "evidence_observed_at",
        ):
            assert child[field] == parent[field], field
        assert child["coverage_role"] == "observed"
        item = connection.execute(
            text("""SELECT source_line_key,external_item_id,occurrence_index,
            quantity,version,source_updated_at FROM marketplace_order_items
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND order_id=:order AND order_item_id=:item"""),
            dict(scope, order=parent["order_id"], item=child["order_item_id"]),
        ).one()
        (normalized_item,) = parent["normalized_evidence"]["observation"]["items"]
        identity = normalized_item["identity"]
        assert tuple(item) == (
            identity["source_line_key"],
            identity["external_item_id"],
            identity["occurrence_index"],
            normalized_item["quantity"],
            1,
            parent["source_effective_at"],
        )
        assert (
            connection.scalar(
                text("""SELECT count(*) FROM order_observations
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
                scope,
            )
            == 2
        )
        receipt = connection.execute(
            text("""SELECT history_first_ordinal,history_next_ordinal,
            history_reconciliation_count FROM user_orders_job_audit
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job
              AND event_kind='job.chunk_committed' AND history_result_sync_run_id=:run"""),
            scope,
        ).one()
        assert tuple(receipt) == (0, 1, 0)
        job = connection.execute(
            text("""SELECT state,history_progress_version FROM user_orders_jobs
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job"""),
            scope,
        ).one()
        assert tuple(job) == ("running", 1)
