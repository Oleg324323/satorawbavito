"""Actual executor roots + actual HTTP adapter; synthetic quota/HTTP only."""

import httpx
import pytest
from sqlalchemy import text

from app.modules.wb_repricing_http import PriceHttpError, WbPriceHttpAdapter
from app.modules.wb_repricing_worker import (
    DurableApprovalWorker,
    ReceiptPublicationPending,
)
from app.platform.integrations.repricer_job_contract import (
    RepricerJobError,
    RepricerReadbackRequired,
)
from tests import test_wb_repricing_worker_postgres as existing

cluster, worker_db, job = existing.cluster, existing.worker_db, existing.job


def attempt(job):
    with job.owner.begin() as connection:
        return tuple(
            connection.execute(
                text(
                    "SELECT status,dispatch_at FROM wb_repricer_price_apply_attempts "
                    "WHERE approval_row_id=:id FOR UPDATE NOWAIT"
                ),
                {"id": job.row_id},
            ).one()
        )


def actual_worker(job, *, deny=None):
    events = []

    def admit(*args):
        assert args == (7, 42, "POST", "/api/v2/upload/task", 900)
        assert attempt(job) == ("reserved", None)
        events.append("quota")
        if deny == "error":
            raise RuntimeError("synthetic-only-private-quota")
        return deny is None

    def send(request):
        status, dispatched = attempt(job)
        assert status == "dispatched" and dispatched is not None
        assert request.method == "POST" and request.url.path == "/api/v2/upload/task"
        events.append("POST")
        return httpx.Response(200, content=b'{"data":{"id":9007199254740993}}')

    transport = WbPriceHttpAdapter(
        organization_id=7,
        marketplace_account_id=42,
        admit_request=admit,
        record_cooldown=lambda *args: True,
        max_response_bytes=4096,
        transport=httpx.MockTransport(send),
    )
    return DurableApprovalWorker(
        executor=job.executor, transport=transport, max_response_bytes=4096
    ), events


@pytest.mark.parametrize("deny", ["false", "error"])
def test_quota_denial_persists_reserved_no_dispatch_receipt_or_outcome(job, deny):
    worker, events = actual_worker(job, deny=deny)
    with pytest.raises(PriceHttpError):
        worker.run_once(job.locator.queue_payload())
    state = job.executor.readback(locator=job.locator)
    assert state.approval_status == "applying" and state.attempt_status == "reserved"
    assert state.receipt is None and attempt(job) == ("reserved", None)
    assert events == ["quota"]


def test_actual_worker_one_quota_one_post_then_duplicate_no_action(job):
    worker, events = actual_worker(job)
    state = worker.run_once(job.locator.queue_payload()).state
    assert state.receipt.wb_upload_id == "9007199254740993"
    assert state.attempt_status == "dispatched"
    assert worker.run_once(job.locator.queue_payload()).state == state
    assert events == ["quota", "POST"]


def test_committed_but_unknown_marker_loses_capacity_never_posts(job, monkeypatch):
    worker, events = actual_worker(job)
    mark = job.executor.mark_dispatch

    def uncertain(**kwargs):
        mark(**kwargs)
        raise RepricerReadbackRequired(locator=job.locator)

    monkeypatch.setattr(job.executor, "mark_dispatch", uncertain)
    with pytest.raises(RepricerReadbackRequired):
        worker.run_once(job.locator.queue_payload())
    state = job.executor.readback(locator=job.locator)
    assert state.attempt_status == "dispatched" and state.receipt is None
    assert worker.run_once(job.locator.queue_payload()).state == state
    assert events == ["quota"]


def test_final_auth_failure_never_consumes_prepared_post(job, monkeypatch):
    worker, events = actual_worker(job)

    def denied(**kwargs):
        raise RepricerJobError("REPRICER_AUTHORITY_DENIED")

    monkeypatch.setattr(job.executor, "before_provider_io", denied)
    state = worker.run_once(job.locator.queue_payload()).state
    assert state.approval_status == "ambiguous" and state.receipt is None
    assert events == ["quota"]


def test_receipt_commit_unknown_keeps_original_id_without_resend(job, monkeypatch):
    worker, events = actual_worker(job)
    publish = job.executor.publish_receipt

    def uncertain(**kwargs):
        publish(**kwargs)
        raise RepricerReadbackRequired(locator=job.locator)

    monkeypatch.setattr(job.executor, "publish_receipt", uncertain)
    with pytest.raises(ReceiptPublicationPending) as error:
        worker.run_once(job.locator.queue_payload())
    assert error.value.observation.wb_upload_id == "9007199254740993"
    assert (
        worker.run_once(job.locator.queue_payload()).state.receipt.wb_upload_id
        == "9007199254740993"
    )
    assert events == ["quota", "POST"]
