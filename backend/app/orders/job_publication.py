"""Orders persistence composed with T1 job authority; no provider handler registration."""

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.orders import OrderContractValidationError
from app.orders.ingestion import OrderManifest
from app.orders.publication_service import (
    OrdersPublicationResult,
    _persist_orders_manifest,
)
from app.platform.integrations.publication_guard import PublicationGuardError
from app.platform.integrations.user_orders_job_contract import (
    AvitoOrdersSourceRequest,
    ClaimedUserOrdersJob,
    OrdersJobError,
    OrdersJobRequest,
)
from app.platform.integrations.user_orders_jobs import UserOrdersJobs


def publish_claimed_orders_manifest(
    session: Session,
    *,
    jobs: UserOrdersJobs,
    claim: ClaimedUserOrdersJob,
    request: OrdersJobRequest,
    manifest: OrderManifest,
) -> OrdersPublicationResult:
    """Publish an already fetched partial page and seal its job in one root commit.

    The trusted adapter must retain the exact request used for acquisition. This
    entry point cannot prove remote page contents or completeness and never fetches.
    T1 owns session/account/credential/job/lease fences and final commit hooks.
    """
    if (
        not isinstance(session, Session)
        or session.in_transaction()
        or not session.is_active
        or not isinstance(session.bind, Engine)
        or type(jobs) is not UserOrdersJobs
        or type(claim) is not ClaimedUserOrdersJob
        or type(request) is not OrdersJobRequest
        or type(manifest) is not OrderManifest
    ):
        raise PublicationGuardError("publication_context_invalid")
    request_bytes = request.canonical_bytes
    if manifest.coverage_state != "partial":
        raise OrderContractValidationError("orders_job_complete_source_unproven")
    if not manifest.observations:
        raise OrderContractValidationError("orders_job_evidence_missing")
    binding = request.binding
    if (
        manifest.organization_id,
        manifest.marketplace_account_id,
        manifest.marketplace,
        manifest.source_kind,
        manifest.adapter_version,
        manifest.source_contract_version,
    ) != (
        request.organization_id,
        request.marketplace_account_id,
        binding.provider,
        binding.source_kind,
        binding.adapter_version,
        binding.source_contract_version,
    ) or any(
        row.status.mapping_version != binding.mapping_version
        for row in manifest.observations
    ):
        raise OrderContractValidationError("orders_job_source_binding_mismatch")
    page_number = (
        request.source_request.page
        if isinstance(request.source_request, AvitoOrdersSourceRequest)
        else 1
    )
    if (
        page_number > 2_147_483_647
        or len(manifest.pages) != 1
        or manifest.pages[0].number != page_number
    ):
        raise OrderContractValidationError("orders_job_source_page_mismatch")
    if isinstance(request.source_request, AvitoOrdersSourceRequest) and (
        len(manifest.observations) > request.source_request.limit
    ):
        raise OrderContractValidationError("orders_job_source_page_mismatch")
    commit_started = False
    try:
        with session.begin():
            handle = jobs.acquire_publication_guard(session, claim=claim)
            if handle.request.canonical_bytes != request_bytes:
                raise OrderContractValidationError("orders_job_source_binding_mismatch")
            result = _persist_orders_manifest(
                session,
                manifest=manifest,
                account=handle.account_binding,
                source_run_key=handle.source_run_key,
                actor_user_id=handle.initiating_user_id,
            )
            handle.complete(result_sync_run_id=result.run_id)
            commit_started = True
        return result
    except SQLAlchemyError:
        if commit_started:
            raise OrdersJobError("READBACK_REQUIRED") from None
        raise PublicationGuardError("publication_persistence_failed") from None
