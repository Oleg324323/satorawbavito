"""Offline streamed HTTP boundary; actor dependency is explicitly bypassed."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.reviews import local_http
from app.reviews.local_command_payloads import (
    encode_review_local_request,
    encode_review_local_result,
)
from app.reviews.local_repository import ReviewLocalError
from tests.test_review_local_command_payload import value

BUDGET = 64 * 1024


def request(chunks, headers=None):
    received = []
    async def receive():
        index = len(received)
        received.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index + 1 < len(chunks)}
    return Request({"type": "http", "method": "POST", "path": "/api/v2/reviews/local/commands",
        "headers": [(b"content-type", b"application/json")] if headers is None else headers}, receive), received


@pytest.fixture
def executed(monkeypatch):
    calls = []
    def execute(engine, *, actor, settings, request):
        encode_review_local_request(request)
        calls.append(request)
        return encode_review_local_result(value("draft-publish", "result"))
    monkeypatch.setattr(local_http, "execute_local_review", execute)
    return calls


def invoke(req):
    return asyncio.run(local_http.local_review_command(req, actor=None, engine=None, settings=None))


def wire():
    return json.dumps(local_http._versions(value("draft-publish")), ensure_ascii=False).encode("utf-8")


def invalid(req, executed):
    with pytest.raises(HTTPException) as caught:
        invoke(req)
    assert caught.value.status_code == 400
    assert caught.value.detail == {"code": "REVIEW_LOCAL_INVALID"}
    assert caught.value.headers == {"Cache-Control": "no-store"}
    assert caught.value.__context__ is None
    assert executed == []


def test_exact_budget_stream_preserves_canonical_command_and_wire(executed):
    body = wire()
    body += b" " * (BUDGET - len(body))
    req, received = request([body[:100], body[100:]],
        [(b"content-type", b'application/json; charset="UTF-8"')])
    result = invoke(req)
    assert result.status_code == 200
    assert json.loads(result.body) == local_http._versions(value("draft-publish", "result"))
    assert executed == [value("draft-publish")]
    assert received == [0, 1]


def test_overflow_stops_before_consuming_remaining_chunks(executed):
    req, received = request([b" " * BUDGET, b"x", b"PRIVATE_UNREAD"])
    invalid(req, executed)
    assert received == [0, 1]
    assert not hasattr(req, "_body")


@pytest.mark.parametrize("headers", [[], [(b"content-type", b"text/plain")],
    [(b"content-type", b"application/json; charset=latin-1")],
    [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")],
    [(b"content-type", b"application/json"), (b"content-type", b"text/plain")],
    [(b"content-type", b"application/json; charset=utf-8; charset=latin-1")]])
def test_invalid_media_rejected_without_reading_body(headers, executed):
    req, received = request([wire()], headers)
    invalid(req, executed)
    assert received == []


@pytest.mark.parametrize("body", [b'{"private":"secret",', b'{"input":{},"input":{}}',
    b'{"input":{"text":"one","text":"two"}}', b'{"value":NaN}', b"\xff", b"[" * 2000],
    ids=["malformed", "duplicate-root", "duplicate-nested", "nan", "non-utf8", "deep"])
def test_malformed_duplicate_non_utf8_and_deep_json_have_no_raw_context(body, executed):
    req, _ = request([body])
    invalid(req, executed)


def test_disconnected_stream_is_safe_before_execution(executed):
    async def receive():
        return {"type": "http.disconnect"}
    req = Request({"type": "http", "headers": [(b"content-type", b"application/json")]}, receive)
    invalid(req, executed)


def test_service_conflict_keeps_status_without_exception_context(monkeypatch):
    def fail(*args, **kwargs):
        raise ReviewLocalError("REVIEW_LOCAL_CONFLICT")
    monkeypatch.setattr(local_http, "execute_local_review", fail)
    req, _ = request([wire()])
    with pytest.raises(HTTPException) as caught:
        invoke(req)
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "REVIEW_LOCAL_CONFLICT"}
    assert caught.value.__context__ is None
