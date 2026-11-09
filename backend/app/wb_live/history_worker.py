"""History staging orchestration. Only a guarded EOF receipt publishes a page."""

import hashlib
from datetime import UTC, datetime, timedelta

from app.wb_live.contracts import WbLiveError
from app.wb_live.provider import WbReadError
from app.wb_live.statistics_orders import (
    HistoryOrderRow,
    OrdersPageEnd,
    OrdersPageError,
    iter_orders_page,
    orders_minimum_interval_seconds,
    orders_request,
)
from app.wb_live.worker import _safe_error


def _due(attempt, delay, now):
    if type(delay) is not int or not 0 <= delay <= 31_536_000:
        raise WbLiveError("WB_RESPONSE_INVALID")
    return now + timedelta(
        seconds=max(
            orders_minimum_interval_seconds(),
            delay,
            min(3600, 2 ** min(max(attempt, 0), 12)),
        )
    )


def run_history_lease(repository, provider, lease, *, clock=None):
    """Caller already claimed the exact history lease; no second claim here.

    DB staging and its lease guard are repository-owned and synchronous. A
    successful staging call must return after ending its transaction. There is
    no page buffer, loop over pages, manufactured start date or provider retry.
    """
    clock = clock or (lambda: datetime.now(UTC))
    if lease.source != "wb-statistics-supplier-orders" or set(lease.checkpoint) != {
        "dateFrom"
    }:
        raise WbLiveError("WB_RESPONSE_INVALID")
    locator = lease.locator
    try:
        _, manifest = orders_request(
            locator.org_id, locator.account_id, lease.checkpoint["dateFrom"]
        )
        credential = repository.resolve_for_fetch(lease)
        page_id = repository.begin_history_page(
            lease, request_checksum=hashlib.sha256(manifest).hexdigest()
        )
        receipt, batch, ordinal = None, [], 0
        with provider.open_page(
            credential=credential,
            organization_id=locator.org_id,
            marketplace_account_id=locator.account_id,
            date_from=lease.checkpoint["dateFrom"],
        ) as stream:
            observed_at = clock()
            for event in iter_orders_page(
                stream.chunks,
                organization_id=locator.org_id,
                marketplace_account_id=locator.account_id,
                date_from=lease.checkpoint["dateFrom"],
                observed_at=observed_at,
            ):
                if isinstance(event, HistoryOrderRow):
                    batch.append(event)
                    if len(batch) == 1000:
                        if not repository.stage_history_rows(
                            lease,
                            page_id=page_id,
                            first_ordinal=ordinal,
                            rows=tuple(batch),
                        ):
                            return {"status": "lease_lost"}
                        ordinal += len(batch)
                        batch.clear()
                elif isinstance(event, OrdersPageEnd):
                    receipt = event
            delay = stream.retry_after_seconds
        # Network closed before the final flush/EOF transaction. Earlier full
        # chunks may be durable but remain invisible until this guarded commit.
        if receipt is None:
            raise WbLiveError("WB_RESPONSE_INVALID")
        if batch and not repository.stage_history_rows(
            lease, page_id=page_id, first_ordinal=ordinal, rows=tuple(batch)
        ):
            return {"status": "lease_lost"}
        accepted = repository.commit_history_page(
            lease, page_id=page_id, end=receipt, next_due_at=_due(0, delay, clock())
        )
        return {
            "status": ("complete" if receipt.terminal else "partial")
            if accepted
            else "lease_lost"
        }
    except WbReadError as error:
        code = _safe_error(error)
        if error.retryable and lease.attempt < 8:
            try:
                due = _due(lease.attempt, error.retry_after_seconds, clock())
            except WbLiveError:
                code = "WB_RESPONSE_INVALID"
            else:
                accepted = repository.defer_batch(
                    lease, error_code=code, next_due_at=due
                )
                return {
                    "status": "deferred" if accepted else "lease_lost",
                    "error_code": code,
                }
        elif error.retryable:
            code = "WB_RETRY_EXHAUSTED"
    except OrdersPageError:
        code = "WB_RESPONSE_INVALID"
    except WbLiveError as error:
        code = error.code
    accepted = repository.fail_batch(lease, error_code=code)
    return {"status": "failed" if accepted else "lease_lost", "error_code": code}
