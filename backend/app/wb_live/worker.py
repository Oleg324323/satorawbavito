"""One durable lease, one bounded read, one CAS publication; never sleep here."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.wb_live.contracts import JobLocator, WbLiveError
from app.wb_live.provider import WbReadError, minimum_interval


def _safe_error(error):
    if error.code == "WB_SOURCE_RATE_LIMITED":
        return "WB_RATE_LIMITED"
    if error.code in {"WB_SOURCE_ACCESS_DENIED", "WB_SOURCE_BILLING_REQUIRED"}:
        return "WB_ACCESS_DENIED"
    if error.code == "WB_SOURCE_BINDING_CHANGED":
        return "WB_BINDING_CHANGED"
    if error.code in {"WB_SOURCE_REQUEST_FAILED", "WB_SOURCE_TRANSPORT_FAILED"}:
        return "WB_PROVIDER_UNAVAILABLE"
    return "WB_RESPONSE_INVALID"


def _next_due(source, attempt, retry_after, now):
    # Persist scheduling, not an ETA task or an in-process sleep. Never retry
    # sooner than a provider hint; pathological hints are a visible failure.
    if type(retry_after) is not int or not 0 <= retry_after <= 31_536_000:
        raise WbLiveError("WB_RESPONSE_INVALID")
    return now + timedelta(
        seconds=max(
            minimum_interval(source),
            retry_after,
            min(3600, 2 ** min(max(attempt, 0), 12)),
        )
    )


def run_one_batch(
    repository, provider, locator: JobLocator, *, clock=None, history_provider=None
):
    clock = clock or (lambda: datetime.now(UTC))
    lease = repository.claim_batch(locator)
    if lease is None:
        return {"status": "skipped"}
    if (lease.locator.org_id, lease.locator.account_id) != (
        locator.org_id,
        locator.account_id,
    ):
        raise WbLiveError("WB_BINDING_CHANGED")
    if lease.source == "wb-statistics-supplier-orders":
        from app.wb_live.history_worker import run_history_lease

        if history_provider is None:
            raise WbLiveError("WB_LIVE_UNAVAILABLE")
        return run_history_lease(repository, history_provider, lease, clock=clock)
    try:
        credential = repository.resolve_for_fetch(lease)
        page = provider.fetch(
            lease.source,
            lease.checkpoint,
            credential=credential,
            organization_id=lease.locator.org_id,
            marketplace_account_id=lease.locator.account_id,
        )
        next_due = _next_due(lease.source, 0, page.retry_after_seconds, clock())
    except WbReadError as error:
        code = _safe_error(error)
        if error.retryable and lease.attempt < 8:
            try:
                next_due = _next_due(
                    lease.source, lease.attempt, error.retry_after_seconds, clock()
                )
            except WbLiveError:
                code = "WB_RESPONSE_INVALID"
                accepted = repository.fail_batch(lease, error_code=code)
            else:
                accepted = repository.defer_batch(
                    lease, error_code=code, next_due_at=next_due
                )
                return {
                    "status": "deferred" if accepted else "lease_lost",
                    "error_code": code,
                }
        else:
            code = "WB_RETRY_EXHAUSTED" if error.retryable else code
            accepted = repository.fail_batch(lease, error_code=code)
        return {"status": "failed" if accepted else "lease_lost", "error_code": code}
    except WbLiveError as error:
        # A failed resolve never reaches transport. Repository errors must be
        # safe typed errors; unexpected DB failures propagate, never fall back.
        accepted = repository.fail_batch(lease, error_code=error.code)
        return {
            "status": "failed" if accepted else "lease_lost",
            "error_code": error.code,
        }
    digest = hashlib.sha256(
        page.request_bytes + b"\n" + page.raw_checksum.encode("ascii")
    ).hexdigest()
    accepted = repository.commit_batch(
        lease,
        rows=page.rows,
        next_checkpoint=page.checkpoint,
        complete=page.complete,
        page_digest=digest,
        next_due_at=next_due,
    )
    return {
        "status": ("complete" if page.complete else "partial")
        if accepted
        else "lease_lost"
    }


def dispatch_due(repository, enqueue, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise WbLiveError("WB_SYNC_CONFLICT")
    jobs = repository.due_jobs(limit=limit)
    sent, failed = 0, False
    for locator in jobs:
        try:
            enqueue(locator.org_id, locator.account_id, locator.job_id)
        except Exception:  # noqa: BLE001 -- broker boundary must redact arbitrary transport errors.
            # Do not copy broker exceptions (which may include credentials).
            # Durable intents remain eligible on the next dispatcher tick.
            failed = True
        else:
            sent += 1
    if failed:
        raise WbLiveError("WB_BROKER_UNAVAILABLE")
    return {"enqueued": sent}
