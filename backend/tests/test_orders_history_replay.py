"""Real history replay, EOF and reconciliation; no substituted publication handle."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.orders.history_publication import persist_wb_history_chunk
from app.wb_live import repository, statistics_orders
from app.wb_live.contracts import JobLocator
from tests import test_wb_history_projection_decisions as history
from tests import test_wb_live_history_postgres as capture

cluster = history.cluster
pg_database = history.pg_database
pg_store = history.pg_store
data = history.data
live = history.live
prepared = history.prepared


def _scope(d):
    return {"org": d.org, "account": d.org}


def _selection(d, claim):
    with d.engine.connect() as connection:
        return connection.execute(
            text("""SELECT history_job_id,history_run_id FROM user_orders_jobs
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job"""),
            dict(_scope(d), job=claim.locator.job_id),
        ).one()


def _new_projection(d, service, history_job_id, history_run_id):
    view = service.create(
        authenticated_actor=d.actor,
        organization_id=d.org,
        marketplace_account_id=d.org,
        history_job_id=history_job_id,
        history_run_id=history_run_id,
        idempotency_key=uuid4(),
    )
    return service.claim(
        organization_id=d.org, marketplace_account_id=d.org, job_id=view.job_id
    )


def _canonical_snapshot(d):
    result = {}
    with d.engine.connect() as connection:
        for table in (
            "marketplace_orders",
            "marketplace_order_items",
            "order_observations",
            "order_sync_memberships",
            "order_sync_runs",
            "order_status_observations",
            "order_lifecycle_events",
            "order_sync_coverage",
        ):
            result[table] = connection.scalar(
                text(f"""SELECT COALESCE(
                jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)
                FROM {table} t WHERE organization_id=:org AND marketplace_account_id=:account"""),
                _scope(d),
            )
    return result


def test_new_authorized_job_replays_exact_run_without_rewriting_orders(prepared):
    d, service, claim = prepared
    first = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    before = _canonical_snapshot(d)
    second_claim = _new_projection(d, service, *_selection(d, claim))
    assert second_claim.locator.job_id != claim.locator.job_id
    replay = service.publish_chunk(
        claim=second_claim, participant=persist_wb_history_chunk
    )
    assert (
        replay.publication.run_id,
        replay.publication.state,
        replay.publication.replayed,
        replay.publication.reconciliation_count,
    ) == (first.publication.run_id, "partial", True, 0)
    assert _canonical_snapshot(d) == before
    with d.engine.connect() as connection:
        receipts = connection.execute(
            text("""SELECT job_id,history_input_checksum,
            history_reconciliation_count FROM user_orders_job_audit
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND event_kind='job.chunk_committed' AND history_result_sync_run_id=:run"""),
            dict(_scope(d), run=first.publication.run_id),
        ).all()
        assert {row.job_id for row in receipts} == {
            claim.locator.job_id,
            second_claim.locator.job_id,
        }
        assert len(receipts) == 2
        assert len({row.history_input_checksum for row in receipts}) == 1
        assert all(row.history_reconciliation_count == 0 for row in receipts)


def test_terminal_empty_chunk_commits_partial_receipt_without_fabricated_orders(
    prepared,
):
    d, service, claim = prepared
    first = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    before = _canonical_snapshot(d)
    final = service.publish_chunk(
        claim=first.continuation_claim, participant=persist_wb_history_chunk
    )
    assert final.continuation_claim is None
    assert final.publication.run_id != first.publication.run_id
    assert (final.publication.state, final.publication.reconciliation_count) == (
        "partial",
        0,
    )
    after = _canonical_snapshot(d)
    for table in before.keys() - {"order_sync_runs", "order_sync_coverage"}:
        assert after[table] == before[table], table
    with d.engine.connect() as connection:
        run = connection.execute(
            text("""SELECT state,manifest_state,page_count,order_count,item_count,
            expected_order_count FROM order_sync_runs
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run"""),
            dict(_scope(d), run=final.publication.run_id),
        ).one()
        assert tuple(run) == ("partial", "partial", 1, 0, 0, None)
        receipt = connection.execute(
            text("""SELECT history_first_ordinal,history_next_ordinal,
            history_reconciliation_count FROM user_orders_job_audit
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job
              AND event_kind='job.chunk_committed' AND history_result_sync_run_id=:run"""),
            dict(_scope(d), job=claim.locator.job_id, run=final.publication.run_id),
        ).one()
        assert tuple(receipt) == (0, 0, 0)
        job = connection.execute(
            text("""SELECT state,result_sync_run_id,result_coverage_state,
            history_progress_version FROM user_orders_jobs
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job"""),
            dict(_scope(d), job=claim.locator.job_id),
        ).one()
        assert tuple(job) == ("succeeded", final.publication.run_id, "partial", 2)


def test_existing_empty_identity_can_receive_its_first_real_projection(prepared):
    d, service, claim = prepared
    with d.engine.begin() as connection:
        external = connection.scalar(
            text("""SELECT h.srid FROM user_orders_history_selection_pages p
            JOIN wb_live_history_rows h ON
              (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)=
              (p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id)
            WHERE p.organization_id=:org AND p.marketplace_account_id=:account
              AND p.job_id=:job AND p.page_index=0 AND h.ordinal=0"""),
            dict(_scope(d), job=claim.locator.job_id),
        )
        # Fixture prestate only: no observation, membership, status or authority is fabricated.
        order_id = connection.scalar(
            text("""INSERT INTO marketplace_orders
            (organization_id,marketplace_account_id,marketplace,external_order_id)
            VALUES(:org,:account,'wb',:external) RETURNING order_id"""),
            dict(_scope(d), external=external),
        )
    result = service.publish_chunk(
        claim=claim, participant=persist_wb_history_chunk
    ).publication
    assert result.reconciliation_count == 0
    with d.engine.connect() as connection:
        parent = connection.execute(
            text("""SELECT version,last_seen_sync_run_id FROM marketplace_orders
            WHERE organization_id=:org AND marketplace_account_id=:account AND order_id=:order"""),
            dict(_scope(d), order=order_id),
        ).one()
        assert tuple(parent) == (2, result.run_id)
        decision = connection.execute(
            text("""SELECT history_pre_order_version,history_pre_sync_run_id,
            history_pre_observation_id,history_outcome FROM order_sync_memberships
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run
              AND order_id=:order AND order_item_id IS NULL"""),
            dict(_scope(d), order=order_id, run=result.run_id),
        ).one()
        assert tuple(decision) == (1, None, None, "initial_projection")


def test_changed_cancelled_observation_reconciles_without_rewriting_current(
    prepared, pg_database
):
    d, service, claim = prepared
    first = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    before = _canonical_snapshot(d)
    parent = next(
        row for row in before["order_observations"] if row["order_item_id"] is None
    )
    payload = parent["normalized_evidence"]["observation"]
    old_job_id, _ = _selection(d, claim)
    with d.engine.connect() as connection:
        date_from = connection.scalar(
            text("""SELECT checkpoint->>'dateFrom' FROM wb_live_sync_sources
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job
              AND source='wb-statistics-supplier-orders'"""),
            dict(_scope(d), job=old_job_id),
        )
    # A periodic resumed run alone is not an explicit manual-history selection.
    # Create its real request through the existing API-role initializer first.
    _, api_engine = pg_database
    api_repo = repository.WbLiveRepository(
        sessionmaker(api_engine, expire_on_commit=False), d.keys
    )
    request_key = "synthetic-changed-history-" + uuid4().hex
    source_job = api_repo.create_history_job(
        d.actor, d.org, request_key, date_from=date_from
    )
    source_job_id = UUID(source_job["jobId"])
    with d.engine.begin() as connection:
        request = connection.execute(
            text("""SELECT job_id,date_from FROM wb_live_history_requests
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND source='wb-statistics-supplier-orders' AND idempotency_key=:key"""),
            dict(_scope(d), key=request_key),
        ).one()
        assert tuple(request) == (source_job_id, date_from)
        connection.execute(
            text("""UPDATE wb_live_sync_sources SET next_due_at=clock_timestamp()
            WHERE organization_id=:org AND marketplace_account_id=:account AND job_id=:job
              AND source='wb-statistics-supplier-orders'"""),
            dict(_scope(d), job=source_job_id),
        )
    lease = d.repo.claim_batch(JobLocator(d.org, d.org, str(source_job_id)))
    assert lease is not None and lease.source == "wb-statistics-supplier-orders"
    (item,) = payload["items"]
    raw = json.dumps(
        [
            {
                "srid": payload["identity"]["external_order_id"],
                "nmId": int(item["identity"]["external_item_id"]),
                "isCancel": True,
                "lastChangeDate": (
                    datetime.fromisoformat(payload["effective_at"]) + timedelta(days=1)
                ).isoformat(),
            }
        ]
    ).encode()
    events = tuple(
        statistics_orders.iter_orders_page(
            [raw],
            organization_id=d.org,
            marketplace_account_id=d.org,
            date_from=lease.checkpoint["dateFrom"],
            observed_at=datetime.now(UTC),
        )
    )
    page_id = capture.begin(d, lease)
    assert d.repo.stage_history_rows(
        lease, page_id=page_id, first_ordinal=0, rows=events[:-1]
    )
    assert d.repo.commit_history_page(
        lease, page_id=page_id, end=events[-1], next_due_at=datetime.now(UTC)
    )
    # Commit enforces the provider cooldown. Advance only this synthetic source's
    # due time, as prepared does between pages; do not change runtime quota rules.
    with d.engine.begin() as connection:
        page_scope = dict(
            _scope(d), job=UUID(lease.locator.job_id), run=UUID(lease.run_id)
        )
        assert connection.scalar(
            text("""SELECT next_due_at>clock_timestamp()
            FROM wb_live_sync_sources WHERE organization_id=:org AND marketplace_account_id=:account
              AND job_id=:job AND run_id=:run AND source='wb-statistics-supplier-orders'
              AND state='queued'"""),
            page_scope,
        )
        due_run = connection.scalar(
            text("""UPDATE wb_live_sync_sources SET next_due_at=clock_timestamp()
            WHERE organization_id=:org AND marketplace_account_id=:account
              AND job_id=:job AND run_id=:run AND source='wb-statistics-supplier-orders'
              AND state='queued' RETURNING run_id"""),
            page_scope,
        )
        assert due_run == UUID(lease.run_id)
    terminal_lease = d.repo.claim_batch(lease.locator)
    assert terminal_lease is not None and terminal_lease.run_id == lease.run_id
    terminal_page = capture.begin(d, terminal_lease)
    (end,) = statistics_orders.iter_orders_page(
        [b"[]"],
        organization_id=d.org,
        marketplace_account_id=d.org,
        date_from=terminal_lease.checkpoint["dateFrom"],
        observed_at=datetime.now(UTC),
    )
    assert d.repo.commit_history_page(
        terminal_lease, page_id=terminal_page, end=end, next_due_at=datetime.now(UTC)
    )
    changed_claim = _new_projection(
        d, service, UUID(lease.locator.job_id), UUID(lease.run_id)
    )
    result = service.publish_chunk(
        claim=changed_claim, participant=persist_wb_history_chunk
    ).publication
    assert result.run_id != first.publication.run_id
    assert (result.state, result.replayed, result.reconciliation_count) == (
        "partial",
        False,
        1,
    )
    after = _canonical_snapshot(d)
    assert after["marketplace_orders"] == before["marketplace_orders"]
    assert after["marketplace_order_items"] == before["marketplace_order_items"]
    with d.engine.connect() as connection:
        member = connection.execute(
            text("""SELECT m.history_outcome,e.normalized_evidence
            FROM order_sync_memberships m JOIN order_observations e ON
              (e.organization_id,e.marketplace_account_id,e.order_id,e.observation_id)=
              (m.organization_id,m.marketplace_account_id,m.order_id,m.observation_id)
            WHERE m.organization_id=:org AND m.marketplace_account_id=:account
              AND m.sync_run_id=:run AND m.order_item_id IS NULL"""),
            dict(_scope(d), run=result.run_id),
        ).one()
        assert member.history_outcome == "reconciliation_required"
        assert (
            member.normalized_evidence["observation"]["status"]["canonical_status"]
            == "cancelled"
        )
        assert (
            connection.scalar(
                text("""SELECT count(*) FROM order_sync_memberships
            WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run
              AND order_item_id IS NOT NULL"""),
                dict(_scope(d), run=result.run_id),
            )
            == 0
        )
