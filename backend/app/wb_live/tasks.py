"""Metadata-only Celery entry points; shared app registration belongs to bootstrap."""

from celery import shared_task

from app.wb_live.contracts import JobLocator, WbLiveError
from app.wb_live.history_provider import ReadOnlyWbOrdersProvider
from app.wb_live.provider import ReadOnlyWbProvider
from app.wb_live.worker import dispatch_due, run_one_batch


def _repository():
    from app.infra.db import get_session_factory
    from app.platform.integrations.credential_store import _load_keyring
    from app.wb_live.repository import WbLiveRepository

    return WbLiveRepository(
        session_factory=get_session_factory(), keyring_loader=_load_keyring
    )


@shared_task(
    name="wb_live.run_batch",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    soft_time_limit=85,
    time_limit=100,
)
def run_batch(organization_id: int, marketplace_account_id: int, job_id: str):
    locator = JobLocator(organization_id, marketplace_account_id, job_id)
    code = "WB_LIVE_UNAVAILABLE"
    try:
        return run_one_batch(
            _repository(),
            ReadOnlyWbProvider(),
            locator,
            history_provider=ReadOnlyWbOrdersProvider(),
        )
    except WbLiveError as error:
        code = error.code
    except Exception:  # noqa: BLE001 -- redact DB/driver/config errors at the Celery log boundary.
        code = "WB_LIVE_UNAVAILABLE"
    # Outside the handler: no chained raw exception or driver DSN in Celery logs.
    raise WbLiveError(code)


@shared_task(name="wb_live.dispatch_pending", ignore_result=True)
def dispatch_pending():
    code = "WB_LIVE_UNAVAILABLE"
    try:
        return dispatch_due(
            _repository(),
            lambda org, account, job: run_batch.apply_async(
                args=(org, account, job), queue="vella.wb-live", retry=False
            ),
        )
    except WbLiveError as error:
        code = error.code
    except Exception:  # noqa: BLE001 -- same secret-safe runtime boundary as run_batch.
        code = "WB_LIVE_UNAVAILABLE"
    raise WbLiveError(code)
