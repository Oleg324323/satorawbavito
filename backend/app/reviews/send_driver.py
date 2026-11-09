"""One explicit guarded send opportunity through a trusted injected transport.

No concrete provider client, URL, production bootstrap, task or retry loop. An
injected fake transport can exercise this sequence in final synthetic acceptance.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from app.platform.integrations.review_job_contract import ReviewJobError


@dataclass(frozen=True, slots=True, repr=False)
class ReviewAnswerObservation:
    """Trusted adapter's observed answer only, not a user-asserted 'sent' flag."""
    provider_answer_id: str | None
    answer_text: str | None

    def __post_init__(self):
        identifier, answer = self.provider_answer_id, self.answer_text
        if identifier is not None and (type(identifier) is not str or not identifier.strip() or len(identifier) > 512):
            raise ReviewJobError("REVIEW_CONTRACT_INVALID")
        if answer is not None and type(answer) is not str:
            raise ReviewJobError("REVIEW_CONTRACT_INVALID")
        try:
            for value in (identifier, answer):
                if value is not None:
                    value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ReviewJobError("REVIEW_CONTRACT_INVALID") from None

    def __repr__(self):
        return "<ReviewAnswerObservation redacted>"

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __deepcopy__(self, memo):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __reduce_ex__(self, protocol):
        raise TypeError("REVIEW_CONTRACT_INVALID")


class ReviewAnswerTransport(Protocol):
    def post_answer(self, *, locator, external_review_id: str, text: str,
                    resolved_credential) -> ReviewAnswerObservation: ...


class ReviewAnswerReadTransport(Protocol):
    def read_answer(self, *, locator, external_review_id: str,
                    resolved_credential) -> ReviewAnswerObservation: ...


def reconcile_once(*, service, authenticated_actor, locator, expected,
                   transport: ReviewAnswerReadTransport, verifier_version: str):
    """One fresh GET after a sealed capture; never another POST or automatic retry.

    The trusted adapter must disable its own retries and preserve exact observed
    answer bytes. A missing/malformed/failed read is incomplete evidence, not proof
    of absence and not permission to resend. DB publication errors propagate.
    """
    from app.reviews.send_service import _intent_payload

    if (type(verifier_version) is not str or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", verifier_version) is None
            or not callable(getattr(transport, "read_answer", None))):
        raise ReviewJobError("REVIEW_CONTRACT_INVALID")
    capture = service.reconciliation.capture_read(authenticated_actor=authenticated_actor,
                                                  locator=locator, expected=expected)
    target = _intent_payload(capture.intent)["externalReviewId"]
    observation = None
    try:
        observation = transport.read_answer(locator=capture.locator, external_review_id=target,
                                            resolved_credential=capture.resolved_credential)
        if type(observation) is not ReviewAnswerObservation:
            observation = None
        else:
            observation.__post_init__()
    except Exception:  # noqa: BLE001 - untrusted transport errors never become response evidence.
        observation = None
    if observation is None:
        observation = ReviewAnswerObservation(None, None)
    return service.observe_reconciliation(authenticated_actor=authenticated_actor,
        read_authority=capture, observation=observation, verifier_version=verifier_version)


def send_once(*, service, locator, expected, transport: ReviewAnswerTransport, verifier_version: str):
    """Starts only from queued; duplicate broker delivery cannot resume a marker.

    All objects are trusted bootstrap/domain dependencies, not HTTP/broker bodies.
    Unknown DB commit is propagated for explicit durable readback, never retried.
    """
    from app.reviews.send_service import ReviewDomainBlocked

    if (type(verifier_version) is not str or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", verifier_version) is None
            or not callable(getattr(transport, "post_answer", None))):
        raise ReviewJobError("REVIEW_CONTRACT_INVALID")
    if expected.state != "queued" or expected.attempt_id is not None:
        raise ReviewJobError("REVIEW_CONFLICT")
    try:
        claimed = service.claim(locator=locator, expected=expected)
    except ReviewDomainBlocked as error:
        return service.block(locator=locator, expected=expected, reason=error.reason)
    resolved = service.executor.resolve_fetch(locator=locator, expected=claimed.expected)
    try:
        dispatch, approved = service.mark_dispatch(locator=locator, expected=claimed.expected, resolved_credential=resolved)
    except ReviewDomainBlocked as error:
        # Proven domain change before any marker; no guessed provider reason.
        return service.block(locator=locator, expected=claimed.expected, reason=error.reason)
    # Returns only after marker's physical commit. Failure consumes the object;
    # recovering its stored marker can never reconstruct another POST grant.
    service.executor.before_provider_io(dispatch=dispatch, resolved_credential=resolved)
    observation = None
    try:
        observation = transport.post_answer(locator=locator, external_review_id=approved.external_review_id,
                                            text=approved.text, resolved_credential=resolved)
        if type(observation) is not ReviewAnswerObservation:
            observation = None
        else:
            observation.__post_init__()
    except Exception:  # noqa: BLE001 - provider may have succeeded; never leak or retry.
        # Even a transport exception may follow provider success. No exception
        # text, retry, second provider call or marker reset is permitted.
        observation = None
    if observation is None:
        return service.mark_ambiguous(locator=locator, expected=dispatch.expected)
    try:
        result = service.observe_ack(locator=locator, expected=dispatch.expected,
                                     observation=observation, verifier_version=verifier_version)
    except ReviewJobError as error:
        if error.code != "REVIEW_CONFLICT":
            # Unknown COMMIT, unavailable authority and persistence failures are
            # not evidence of rollback and do not authorize even a local replay.
            raise
        fresh = service.executor.readback(locator=locator)
        if (fresh.expected.attempt_id, fresh.expected.lease_token) != (dispatch.expected.attempt_id, dispatch.expected.lease_token):
            raise ReviewJobError("REVIEW_CONFLICT") from None
        if fresh.expected.state not in {"leased", "ambiguous"}:
            return fresh
        # Lease expiry/lifecycle race: preserve the observed ACK, never relabel
        # it as a fresh reconciliation read or repeat the remote POST.
        result = service.observe_ack(locator=locator, expected=fresh.expected,
            observation=observation, verifier_version=verifier_version, retain_only=True)
        return service.mark_ambiguous(locator=locator, expected=result.expected) if result.expected.state == "leased" else result
    if observation.provider_answer_id is None or observation.answer_text is None:
        return service.mark_ambiguous(locator=locator, expected=result.expected)
    return result
