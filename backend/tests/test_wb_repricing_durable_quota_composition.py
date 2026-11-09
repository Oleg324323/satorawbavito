"""Run ONLY on assembled ROOT with accepted 0084/grants/SQL quota service.

Actual dedicated executor + quota roots; MockTransport never leaves the process.
This module intentionally does not create or substitute missing schema/services.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.modules.wb_repricing_http import PriceHttpError, WbPriceHttpAdapter
from app.modules.wb_repricing_worker import DurableApprovalWorker
from app.platform.integrations.wb_price_quota import WbPriceQuotaService
from tests import test_wb_repricing_worker_postgres as existing

cluster, worker_db, job = existing.cluster, existing.worker_db, existing.job


@pytest.fixture
def composition(job, worker_db):
    owner, _, worker_engine, identity = worker_db
    # Only the allocator-owned synthetic fixture account, not runtime state.
    with owner.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM wb_price_quota WHERE organization_id=7 AND marketplace_account_id=42"
            )
        )
    quota = WbPriceQuotaService(
        session_factory=sessionmaker(bind=worker_engine), identity=identity
    )
    sends = []

    def send(request):
        # Independent physical root can lock BOTH marker and quota at HTTP time.
        # Neither preparation nor authorization roots may span provider I/O.
        with owner.begin() as connection:
            status = connection.execute(
                text(
                    "SELECT status FROM wb_repricer_price_apply_attempts "
                    "WHERE approval_row_id=:id FOR UPDATE NOWAIT"
                ),
                {"id": job.row_id},
            ).scalar_one()
            quota_deadline = connection.execute(
                text(
                    "SELECT next_allowed_at FROM wb_price_quota "
                    "WHERE organization_id=7 AND marketplace_account_id=42 FOR UPDATE NOWAIT"
                )
            ).scalar_one()
        assert status == "dispatched" and quota_deadline is not None
        assert request.method == "POST" and request.url.path == "/api/v2/upload/task"
        sends.append(datetime.now(UTC))
        return httpx.Response(
            200,
            headers={"Retry-After": "3600"},
            content=b'{"data":{"id":9007199254740993}}',
        )

    adapter = WbPriceHttpAdapter(
        organization_id=7,
        marketplace_account_id=42,
        admit_request=quota.admit_request,
        record_cooldown=quota.record_cooldown,
        max_response_bytes=4096,
        transport=httpx.MockTransport(send),
    )
    service = DurableApprovalWorker(
        executor=job.executor, transport=adapter, max_response_bytes=4096
    )
    return service, quota, sends, worker_engine


def deadline(job):
    with job.owner.connect() as connection:
        return connection.execute(
            text(
                "SELECT next_allowed_at FROM wb_price_quota WHERE organization_id=7 AND marketplace_account_id=42"
            )
        ).scalar_one()


def assert_reserved_without_marker(job):
    state = job.executor.readback(locator=job.locator)
    assert state.approval_status == "applying" and state.attempt_status == "reserved"
    assert state.receipt is None
    with job.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT dispatch_at FROM wb_repricer_price_apply_attempts WHERE approval_row_id=:id"
                ),
                {"id": job.row_id},
            ).scalar_one()
            is None
        )


def test_actual_category_read_admission_denies_post_before_marker(job, composition):
    service, quota, sends, _ = composition
    assert quota.admit_request(7, 42, "GET", "/api/v2/list/goods/filter", 900) is True
    original = deadline(job)
    with pytest.raises(PriceHttpError):
        service.run_once(job.locator.queue_payload())
    assert_reserved_without_marker(job)
    assert sends == [] and deadline(job) == original


def test_actual_quota_post_header_feedback_receipt_and_duplicate(job, composition):
    service, _, sends, _ = composition
    state = service.run_once(job.locator.queue_payload()).state
    assert state.receipt.wb_upload_id == "9007199254740993"
    assert state.attempt_status == "dispatched" and len(sends) == 1
    stored = deadline(job)
    assert stored >= sends[0] + timedelta(seconds=3600)
    assert service.run_once(job.locator.queue_payload()).state == state
    assert len(sends) == 1 and deadline(job) == stored


def test_actual_quota_commit_then_lost_ack_never_marks_or_posts(
    job, composition, monkeypatch
):
    service, _, sends, engine = composition
    state = {"quota_update": False, "injected": False}
    original_commit = engine.dialect.do_commit

    def statement_seen(connection, cursor, statement, parameters, context, many):
        if "UPDATE wb_price_quota SET next_allowed_at" in statement:
            state["quota_update"] = True

    def commit_then_lost_ack(connection):
        original_commit(connection)
        if state["quota_update"] and not state["injected"]:
            state["injected"] = True
            # Server committed, but no caller may treat this as permission.
            raise RuntimeError("synthetic-lost-quota-commit-ack")

    event.listen(engine, "before_cursor_execute", statement_seen)
    monkeypatch.setattr(engine.dialect, "do_commit", commit_then_lost_ack)
    try:
        with pytest.raises(PriceHttpError) as error:
            service.run_once(job.locator.queue_payload())
        assert state["injected"] and error.value.__context__ is None
    finally:
        event.remove(engine, "before_cursor_execute", statement_seen)
        monkeypatch.setattr(engine.dialect, "do_commit", original_commit)
    assert_reserved_without_marker(job)
    assert sends == [] and deadline(job) > datetime.now(UTC)
