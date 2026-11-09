"""Offline quota composition/transport evidence, not a PG atomicity proof."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.modules.wb_repricing import build_action_key
from app.modules.wb_repricing_dispatch import CanonicalApplyRequest, build_dispatch_key
from app.modules.wb_repricing_http import PriceHttpError
from app.modules.wb_repricing_repository import ApprovalRepositoryScope
from app.modules.wb_repricing_worker import (
    DurableApprovalWorker,
    ReceiptPublicationPending,
    WorkerDisposition,
)
from app.platform.integrations.repricer_job_contract import (
    RepricerApprovalBinding,
    RepricerExpectedState,
    RepricerJobLocator,
    RepricerReadbackRequired,
)
from app.platform.integrations.repricer_job_executor import RepricerJobExecutor
from tests.test_wb_live_history_worker import credential
from tests.test_wb_repricing_http import adapter


def inputs():
    scope = ApprovalRepositoryScope(1, 2, str(uuid4()))
    request = CanonicalApplyRequest(scope, None, 123, "article", 130000, 0)
    action = build_action_key(1, "2", scope.approval_id, request.checksum)
    approval = RepricerApprovalBinding(
        1,
        2,
        uuid4(),
        scope.approval_id,
        action,
        request.checksum,
        request.canonical_bytes,
    )
    attempt = uuid4()
    return {
        "body": request.provider_bytes,
        "credential": credential(),
        "locator": RepricerJobLocator(1, 2, uuid4()),
        "expected": RepricerExpectedState(
            approval, 2, attempt, 0, build_dispatch_key(scope, action, str(attempt)), 1
        ),
    }


def test_prepare_reserves_once_without_io_and_consume_posts_once():
    events = []

    def admit(*args):
        events.append("admit")
        return True

    def http(request):
        events.append("POST")
        return httpx.Response(200, content=b'{"data":{"id":123}}')

    transport, values = adapter(http, admit), inputs()
    prepared = transport.prepare_price_once(**values)
    assert events == ["admit"]
    result = transport.post_prepared_once(prepared=prepared, **values)
    assert result.status_code == 200 and events == ["admit", "POST"]
    with pytest.raises(PriceHttpError):
        transport.post_prepared_once(prepared=prepared, **values)
    assert events == ["admit", "POST"]


@pytest.mark.parametrize(
    "mutate",
    ["body", "credential", "locator", "attempt", "version", "adapter", "clone"],
)
def test_prepared_post_cannot_change_binding(mutate):
    posts = []
    transport = adapter(lambda request: posts.append(request))
    original = inputs()
    prepared = transport.prepare_price_once(**original)
    values = dict(original)
    if mutate == "body":
        values["body"] = values["body"].replace(b"1300", b"1400")
    elif mutate == "credential":
        values["credential"] = credential()
    elif mutate == "locator":
        values["locator"] = replace(values["locator"], job_id=uuid4())
    elif mutate == "attempt":
        values["expected"] = replace(values["expected"], attempt_id=uuid4())
    elif mutate == "version":
        values["expected"] = replace(values["expected"], approval_version=3)
    elif mutate == "clone":
        transport = copy.copy(transport)
    else:
        transport = adapter(lambda request: posts.append(request))
    with pytest.raises(PriceHttpError):
        transport.post_prepared_once(prepared=prepared, **values)
    assert posts == []


def test_prepared_handle_noncopyable_and_redacted():
    values = inputs()
    prepared = adapter(
        lambda request: pytest.fail("unexpected network")
    ).prepare_price_once(**values)
    assert "synthetic" not in repr(prepared) and values["body"].decode() not in repr(
        prepared
    )
    with pytest.raises(TypeError):
        copy.copy(prepared)
    with pytest.raises(TypeError):
        copy.deepcopy(prepared)


def test_prepared_consumption_is_atomic_across_two_threads():
    posts = []
    transport = adapter(
        lambda request: posts.append(request) or httpx.Response(200, content=b"{}")
    )
    values = inputs()
    prepared = transport.prepare_price_once(**values)

    def consume(_):
        try:
            transport.post_prepared_once(prepared=prepared, **values)
            return "sent"
        except PriceHttpError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == ["conflict", "sent"]
    assert len(posts) == 1


@pytest.mark.parametrize("case", ["body", "canonical", "account", "not_reserved"])
def test_invalid_binding_rejected_before_quota(case):
    admissions = []
    values = inputs()
    if case == "body":
        values["body"] = b"{}"
    elif case == "canonical":
        values["body"] = values["body"].replace(b"1300", b"1400")
    elif case == "account":
        values["credential"] = credential(9)
    else:
        values["expected"] = replace(values["expected"], attempt_version=1)
    with pytest.raises(PriceHttpError):
        adapter(
            lambda request: pytest.fail("unexpected network"),
            lambda *args: admissions.append(args) or True,
        ).prepare_price_once(**values)
    assert admissions == []


def worker_fixture(monkeypatch, *, failure=None):
    """Mock only executor persistence boundary; real worker and HTTP adapter.

    This proves call order, not physical transaction/authorization correctness.
    Actual executor and role behavior require the separate PostgreSQL gate.
    """
    values, events = inputs(), []
    state = SimpleNamespace(
        expected=values["expected"],
        approval_status="applying",
        attempt_status="reserved",
        receipt=None,
    )
    executor = object.__new__(RepricerJobExecutor)
    monkeypatch.setattr(executor, "readback", lambda **kwargs: state)

    def resolve(**kwargs):
        events.append("resolve_closed")
        return values["credential"]

    monkeypatch.setattr(executor, "resolve_fetch", resolve)

    def admit(*args):
        events.append("quota")
        if failure == "quota_error":
            raise RuntimeError("synthetic-private-quota")
        return failure != "quota_denied"

    def mark(**kwargs):
        events.append("marker")
        if failure == "marker_unknown":
            raise RepricerReadbackRequired(locator=values["locator"])
        return SimpleNamespace(
            expected=replace(values["expected"], approval_version=3, attempt_version=1)
        )

    monkeypatch.setattr(executor, "mark_dispatch", mark)

    def before(**kwargs):
        events.append("before_io")
        if failure == "auth":
            raise RuntimeError("synthetic-final-auth")

    monkeypatch.setattr(executor, "before_provider_io", before)

    def publish(**kwargs):
        events.append("receipt")
        assert kwargs["observation"].wb_upload_id == "123"
        if failure == "receipt":
            raise RuntimeError("synthetic-private-commit")

    monkeypatch.setattr(executor, "publish_receipt", publish)

    def close(**kwargs):
        events.append("outcome")
        return state

    monkeypatch.setattr(executor, "close_outcome", close)

    def send(request):
        events.append("POST")
        if failure == "transport":
            raise httpx.ReadTimeout("synthetic-only", request=request)
        return httpx.Response(200, content=b'{"data":{"id":123}}')

    worker = DurableApprovalWorker(
        executor=executor, transport=adapter(send, admit), max_response_bytes=4096
    )
    return worker, events, values


def test_real_adapter_preparation_occurs_before_marker(monkeypatch):
    worker, events, values = worker_fixture(monkeypatch)
    worker.run_once(values["locator"].queue_payload())
    assert events == [
        "resolve_closed",
        "quota",
        "marker",
        "before_io",
        "POST",
        "receipt",
    ]


@pytest.mark.parametrize("failure", ["quota_denied", "quota_error"])
def test_quota_failure_is_premarker_without_fake_ambiguous_outcome(
    monkeypatch, failure
):
    worker, events, values = worker_fixture(monkeypatch, failure=failure)
    with pytest.raises(PriceHttpError) as error:
        worker.run_once(values["locator"].queue_payload())
    assert events == ["resolve_closed", "quota"]
    assert error.value.__context__ is None


def test_unknown_marker_never_consumes_prepared_post(monkeypatch):
    worker, events, values = worker_fixture(monkeypatch, failure="marker_unknown")
    with pytest.raises(RepricerReadbackRequired):
        worker.run_once(values["locator"].queue_payload())
    assert events == ["resolve_closed", "quota", "marker"]


@pytest.mark.parametrize("failure", ["auth", "transport"])
def test_postmarker_uncertainty_still_closes_ambiguous_without_retry(
    monkeypatch, failure
):
    worker, events, values = worker_fixture(monkeypatch, failure=failure)
    progress = worker.run_once(values["locator"].queue_payload())
    assert progress.disposition is WorkerDisposition.reconciliation_required
    assert events == ["resolve_closed", "quota", "marker", "before_io"] + (
        ["POST"] if failure == "transport" else []
    ) + ["outcome"]


def test_known_receipt_recovery_still_preserves_observation_without_second_post(
    monkeypatch,
):
    worker, events, values = worker_fixture(monkeypatch, failure="receipt")
    with pytest.raises(ReceiptPublicationPending) as error:
        worker.run_once(values["locator"].queue_payload())
    assert error.value.observation.wb_upload_id == "123"
    assert events == [
        "resolve_closed",
        "quota",
        "marker",
        "before_io",
        "POST",
        "receipt",
    ]
