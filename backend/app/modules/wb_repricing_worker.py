"""Dormant T2 domain composition of T1's exact repricer executor roots.

No task, real transport, connection, policy or credential resolver is registered.
Bootstrap must supply the reviewed executor and a single-POST transport with no
internal retry. Receipt acceptance is not proof that WB applied a price. Provider
history authority/reconciliation remains a separate prerequisite.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from app.modules.wb_repricing import build_action_key
from app.modules.wb_repricing_dispatch import (
    ApplyOutcome,
    AttemptStatus,
    CanonicalApplyRequest,
)
from app.modules.wb_repricing_postgres import ApprovalTransaction
from app.modules.wb_repricing_repository import (
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
)
from app.modules.wb_repricing_upload_receipt import extract_post_upload_id_from_bytes
from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.platform.integrations.repricer_job_contract import (
    ClosingAction,
    InitiationAction,
    Redacted,
    RepricerApprovalBinding,
    RepricerExpectedState,
    RepricerJobError,
    RepricerJobLocator,
    RepricerReadback,
    RepricerReadbackRequired,
    RepricerUploadObservation,
    timestamp,
)
from app.platform.integrations.repricer_job_executor import (
    RepricerClosingHandle,
    RepricerInitiationHandle,
    RepricerJobExecutor,
)


def canonical_request(binding: RepricerApprovalBinding) -> CanonicalApplyRequest:
    """Reconstruct ONLY the stored immutable request, retaining bytes/action key."""
    try:
        if type(binding) is not RepricerApprovalBinding:
            raise ValueError
        binding.__post_init__()
        data = json.loads(binding.canonical_request_bytes)
        request = CanonicalApplyRequest(
            ApprovalRepositoryScope(
                data["organizationId"], data["accountId"], data["approvalId"]
            ),
            data["catalogSkuId"],
            data["nmId"],
            data["articleId"],
            data["priceKopecks"],
            data["discountPct"],
            data["sizeId"],
            data["minPriceKopecks"],
        )
        if (
            request.scope.organization_id,
            request.scope.marketplace_account_id,
            request.scope.approval_id,
        ) != (
            binding.organization_id,
            binding.marketplace_account_id,
            binding.approval_id,
        ) or request.canonical_bytes != binding.canonical_request_bytes:
            raise ValueError
        if (
            build_action_key(
                binding.organization_id,
                str(binding.marketplace_account_id),
                binding.approval_id,
                request.checksum,
            )
            != binding.action_key
        ):
            raise ValueError
        return request
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise RepricerJobError("REPRICER_CONTRACT_INVALID") from None


def _participant(session, handle, expected, action):
    cls = (
        RepricerClosingHandle
        if action is ClosingAction.OUTCOME
        else RepricerInitiationHandle
    )
    if type(handle) is not cls or type(expected) is not RepricerExpectedState:
        raise RepricerJobError("REPRICER_FENCE_INVALID")
    handle.require_participation(session, action)
    if handle.expected != expected:
        raise RepricerJobError("REPRICER_CONFLICT")
    request = canonical_request(expected.approval)
    return ApprovalTransaction(session, request.scope)


def _actor(handle, expected):
    # Only the authenticated exact initiation handle supplies actor identity.
    return AuthenticatedApprovalActor(
        expected.approval.organization_id, handle.initiator_membership_id
    )


@dataclass(frozen=True, slots=True, repr=False)
class OriginalPricePostResponse(Redacted):
    raw_body: bytes
    status_code: int
    ok: bool
    observed_at: datetime

    def __post_init__(self):
        if (
            type(self.raw_body) is not bytes
            or type(self.status_code) is not int
            or type(self.ok) is not bool
        ):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        object.__setattr__(self, "observed_at", timestamp(self.observed_at))


class SinglePostTransport(Protocol):
    def post_price_once(
        self, *, body: bytes, credential: ResolvedCredentialForFetch
    ) -> OriginalPricePostResponse:
        """Original POST /api/v2/upload/task response; no retry/polling/coercion."""
        ...


class WorkerDisposition(str, Enum):
    closed = "closed"
    reconciliation_required = "reconciliation_required"


@dataclass(frozen=True, slots=True, repr=False)
class WorkerProgress(Redacted):
    disposition: WorkerDisposition
    state: RepricerReadback


class ReceiptPublicationPending(RepricerReadbackRequired):
    """Retain the safe known upload observation for receipt-only recovery.

    A process crash can still lose it if the DB is unavailable. Never replace
    receipt-only readback/retry with another provider POST; no raw body is retained.
    """

    def __init__(self, locator, expected, observation):
        super().__init__(locator=locator)
        self.expected, self.observation = expected, observation


class DurableApprovalWorker(Redacted):
    def __init__(
        self,
        *,
        executor: RepricerJobExecutor,
        transport: SinglePostTransport,
        max_response_bytes: int,
    ):
        if type(executor) is not RepricerJobExecutor or not callable(
            getattr(transport, "post_price_once", None)
        ):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        prepare = getattr(transport, "prepare_price_once", None)
        prepared_post = getattr(transport, "post_prepared_once", None)
        if (prepare is not None or prepared_post is not None) and not (
            callable(prepare) and callable(prepared_post)
        ):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self._prepare, self._prepared_post = prepare, prepared_post
        self._executor, self._transport, self._budget = (
            executor,
            transport,
            max_response_bytes,
        )

    def claim(self, *, locator, expected):
        def apply(session, handle):
            tx = _participant(session, handle, expected, InitiationAction.CLAIM)
            tx.claim(expected.approval_version, _actor(handle, expected))

        return self._executor.claim(
            locator=locator, expected=expected, participant=apply
        )

    def reserve(self, *, locator, expected):
        def apply(session, handle):
            tx = _participant(session, handle, expected, InitiationAction.RESERVE)
            tx.reserve_attempt(expected.approval_version, _actor(handle, expected))

        return self._executor.reserve(
            locator=locator, expected=expected, participant=apply
        )

    def _dispatch(self, *, locator, expected, credential):
        def apply(session, handle):
            tx = _participant(session, handle, expected, InitiationAction.DISPATCH)
            tx.mark_dispatch(
                str(expected.attempt_id),
                expected.approval_version,
                expected.attempt_version,
                _actor(handle, expected),
            )

        return self._executor.mark_dispatch(
            locator=locator,
            expected=expected,
            resolved_credential=credential,
            participant=apply,
        )

    def record_verified_outcome(self, *, locator, expected, outcome: ApplyOutcome):
        """Trusted outcome adapter only; queue payload cannot choose an outcome.

        Receipt equality is necessary for applied, not sufficient proof of actual
        application. Caller must supply separately verified provider result. No
        polling/GET authority is inferred or implemented by this entrypoint.
        """
        if type(outcome) is not ApplyOutcome:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        if outcome.status is AttemptStatus.applied:
            current = self._executor.readback(locator=locator)
            if (
                current.expected != expected
                or current.receipt is None
                or current.receipt.wb_upload_id != outcome.wb_upload_id
            ):
                raise RepricerJobError("REPRICER_CONFLICT")

        def apply(session, handle):
            tx = _participant(session, handle, expected, ClosingAction.OUTCOME)
            tx.record_attempt_outcome(
                str(expected.attempt_id),
                expected.approval_version,
                expected.attempt_version,
                outcome,
            )

        return self._executor.close_outcome(
            locator=locator, expected=expected, participant=apply
        )

    def _uncertain(self, locator, expected, code):
        state = self.record_verified_outcome(
            locator=locator,
            expected=expected,
            outcome=ApplyOutcome(AttemptStatus.ambiguous, code),
        )
        return WorkerProgress(WorkerDisposition.reconciliation_required, state)

    def run_once(self, queue_payload) -> WorkerProgress:
        """One bounded delivery; no loops, retries, queue ACK or scheduler mutation.

        A duplicate observing a marker does not close an in-flight operation or
        mint a new send handle. It reports reconciliation required. Recovery must
        establish abandonment separately; a marker alone cannot distinguish a
        crashed worker from a still-running POST.
        """
        locator = RepricerJobLocator.from_queue(queue_payload)
        state = self._executor.readback(locator=locator)
        canonical_request(state.expected.approval)
        if state.approval_status in (
            "applied",
            "failed",
            "ambiguous",
            "rejected",
            "blocked",
        ):
            disposition = (
                WorkerDisposition.reconciliation_required
                if state.approval_status == "ambiguous"
                else WorkerDisposition.closed
            )
            return WorkerProgress(disposition, state)
        if state.attempt_status == "dispatched":
            return WorkerProgress(WorkerDisposition.reconciliation_required, state)
        if state.approval_status == "pending":
            state = self.claim(locator=locator, expected=state.expected)
        if state.attempt_status is None:
            state = self.reserve(locator=locator, expected=state.expected)
        if state.approval_status != "applying" or state.attempt_status != "reserved":
            raise RepricerJobError("REPRICER_CONFLICT")
        request = canonical_request(state.expected.approval)
        # Any resolve/marker failure escapes without provider I/O. In particular,
        # an uncertain marker COMMIT must never be immediately repeated.
        credential = self._executor.resolve_fetch(
            locator=locator, expected=state.expected
        )
        # Real transport validates the exact reserved intent and obtains pacing
        # after the resolver root closed, BEFORE a dispatch marker can exist.
        # Quota denial/unavailability escapes, leaving the attempt reserved.
        # This handle is pacing only, never a replacement for the live fence.
        prepared_expected = state.expected
        prepared = None
        if self._prepare is not None:
            prepared = self._prepare(
                body=request.provider_bytes,
                credential=credential,
                locator=locator,
                expected=prepared_expected,
            )
            if prepared is None:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        dispatch = self._dispatch(
            locator=locator, expected=state.expected, credential=credential
        )
        try:
            self._executor.before_provider_io(
                dispatch=dispatch, resolved_credential=credential
            )
        except RepricerReadbackRequired:
            raise
        except Exception:  # noqa: BLE001 - post-marker failures must fail closed without raw details.
            return self._uncertain(
                locator, dispatch.expected, "WB_APPLY_AUTHORIZATION_FAILED"
            )
        try:
            if self._prepared_post is not None:
                response = self._prepared_post(
                    prepared=prepared,
                    body=request.provider_bytes,
                    credential=credential,
                    locator=locator,
                    expected=prepared_expected,
                )
            else:
                # Existing synthetic/non-preparing transports retain the
                # single-POST protocol; never infer an admission bypass flag.
                response = self._transport.post_price_once(
                    body=request.provider_bytes, credential=credential
                )
        except Exception:  # noqa: BLE001 - transport implementations have no common exception base.
            # A timeout/reset/exception does not prove provider nonacceptance.
            return self._uncertain(
                locator, dispatch.expected, "WB_APPLY_TRANSPORT_ERROR"
            )
        try:
            if type(response) is not OriginalPricePostResponse:
                raise ValueError
            upload = extract_post_upload_id_from_bytes(
                response.raw_body,
                method="POST",
                path="/api/v2/upload/task",
                status_code=response.status_code,
                ok=response.ok,
                apply_mode="wb_api",
                max_response_bytes=self._budget,
            )
            observation = RepricerUploadObservation(upload, response.observed_at)
        except (ValueError, RepricerJobError):
            return self._uncertain(locator, dispatch.expected, "WB_RESULT_UNAVAILABLE")
        try:
            self._executor.publish_receipt(
                locator=locator, expected=dispatch.expected, observation=observation
            )
        except Exception:  # noqa: BLE001 - uncertain publication retains receipt, never retries POST.
            raise ReceiptPublicationPending(
                locator, dispatch.expected, observation
            ) from None
        # Upload accepted/queued is NOT an applied price. Retain the receipt before
        # optional future status polling. No polling or auto-resend happens here.
        return WorkerProgress(
            WorkerDisposition.reconciliation_required,
            self._executor.readback(locator=locator),
        )
