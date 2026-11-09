"""One real transport boundary with synthetic HTTP, never provider traffic."""

import importlib
import json
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from app.config import Settings
from app.control_plane.auth import ActorContext
from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from tests.test_account_avito_stats_transport import resolved


def api():
    return importlib.import_module("app.reviews.canonical_avito_fetch")


def raw():
    return {
        "reviews": [
            {
                "id": "001",
                "createdAt": 1785230400,
                "text": " exact\0😀 ",
                "answer": None,
            }
        ],
        "total": 999,
    }


def response(body=None, *, status=200, headers=None):
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        stream=httpx.ByteStream(json.dumps(raw()).encode() if body is None else body),
    )


def fetch(*, credential=None, offset=0, send=None):
    calls = []

    def transport(request):
        calls.append(request)
        return response() if send is None else send(request)

    value = (
        api()
        .BoundedAvitoReviewSourceClient(
            resolved() if credential is None else credential,
            transport=httpx.MockTransport(transport),
        )
        .fetch_page(offset=offset)
    )
    return value, calls


def test_one_exact_get_preserves_native_text_without_rating_request_or_defaults():
    rows, calls = fetch(offset=50)
    assert rows == (
        {"id": "001", "createdAt": 1785230400, "text": " exact\0😀 ", "answer": None},
    )
    assert [(r.method, r.url.host, r.url.path, dict(r.url.params)) for r in calls] == [
        ("GET", "api.avito.ru", "/ratings/v1/reviews", {"limit": "50", "offset": "50"})
    ]
    assert calls[0].headers["accept-encoding"] == "identity"
    assert "score" not in rows[0]


@pytest.mark.parametrize("offset", [True, -1, 1.0, "0", None, 2**63])
def test_invalid_offset_never_calls_transport(offset):
    def never(_):
        pytest.fail("invalid request reached HTTP")

    with pytest.raises(api().AvitoReviewFetchError):
        fetch(offset=offset, send=never)


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b"[]",
        b"{}",
        b'{"reviews":null}',
        b'{"reviews":[null]}',
        b'{"reviews":[],"reviews":[]}',
        b'{"reviews":[],"x":NaN}',
        b'{"reviews":[],"x":Infinity}',
        b'{"reviews":[],"accountId":null}',
        b'{"reviews":[],"userId":"123"}',
        b'{"reviews":[],"sellerId":"123"}',
        json.dumps({"reviews": [{}] * 51}).encode(),
        b" " * (4 * 1024 * 1024 + 1),
    ],
    ids=[
        "json",
        "utf8",
        "array",
        "missing",
        "null",
        "row",
        "duplicate",
        "nan",
        "infinity",
        "account",
        "user",
        "seller",
        "count",
        "bytes",
    ],
)
def test_invalid_or_oversize_raw_response_is_closed_error(body):
    with pytest.raises(api().AvitoReviewFetchError) as error:
        fetch(send=lambda _: response(body))
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "status,headers",
    [
        (302, {"location": "https://example.invalid"}),
        (401, {}),
        (429, {}),
        (500, {}),
        (200, {"content-type": "text/html"}),
        (200, {"content-encoding": "gzip"}),
        (200, {"content-length": "4194305"}),
        (200, {"content-length": "abc"}),
        (200, {"content-length": "0"}),
    ],
)
def test_bad_http_response_never_retries_or_redirects(status, headers):
    calls = []

    def send(request):
        calls.append(request)
        return response(status=status, headers=headers)

    with pytest.raises(api().AvitoReviewFetchError):
        fetch(send=send)
    assert len(calls) == 1


def test_transport_exception_drops_raw_cause():
    def send(_):
        raise RuntimeError("synthetic-private-provider-text")

    with pytest.raises(api().AvitoReviewFetchError) as error:
        fetch(send=send)
    assert error.value.__context__ is None


def test_expired_paired_access_never_calls_http():
    def never(_):
        pytest.fail("expired access reached HTTP")

    with pytest.raises(api().AvitoReviewFetchError):
        fetch(credential=resolved(expired=True), send=never)


def test_wrong_account_identity_in_pair_never_calls_http():
    value = resolved()
    value = ResolvedCredentialForFetch(
        value.secret,
        replace(
            value.binding,
            credential_identity=replace(
                value.binding.credential_identity, marketplace_account_id=3
            ),
        ),
    )

    def never(_):
        pytest.fail("mismatched pair reached HTTP")

    with pytest.raises(api().AvitoReviewFetchError):
        fetch(credential=value, send=never)


def test_empty_page_remains_empty_not_synthesized_from_total():
    rows, _ = fetch(send=lambda _: response(b'{"reviews":[],"total":100}'))
    assert rows == ()


def test_elapsed_budget_checked_on_each_transport_chunk_not_buffered_64k(monkeypatch):
    clock = [0]
    monkeypatch.setattr(api().time, "monotonic", lambda: clock[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 31
            yield b"{"
            pytest.fail("elapsed fetch consumed another transport chunk")

    with pytest.raises(api().AvitoReviewFetchError):
        fetch(
            send=lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=SlowStream()
            )
        )


def test_sync_disabled_before_any_key_or_database_access():
    module = importlib.import_module("app.reviews.canonical_avito_sync")

    def never():
        pytest.fail("disabled sync read keyring")

    actor = ActorContext(
        "ignored", "user", 1, "custom", frozenset(), session_id="login"
    )
    with pytest.raises(module.AvitoReviewSyncError, match="DISABLED"):
        module.sync_canonical_avito_page(
            object(),
            actor=actor,
            settings=Settings(),
            marketplace_account_id=2,
            offset=0,
            request_id=UUID("11111111-1111-4111-8111-111111111111"),
            keyring_loader=never,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("marketplace_account_id", True),
        ("marketplace_account_id", 0),
        ("marketplace_account_id", "2"),
        ("offset", -1),
        ("offset", True),
        ("request_id", "11111111-1111-4111-8111-111111111111"),
    ],
)
def test_invalid_sync_request_before_key_or_database(field, value):
    module = importlib.import_module("app.reviews.canonical_avito_sync")

    def never():
        pytest.fail("invalid sync read keyring")

    args = {
        "marketplace_account_id": 2,
        "offset": 0,
        "request_id": UUID("11111111-1111-4111-8111-111111111111"),
    }
    args[field] = value
    actor = ActorContext(
        "ignored", "user", 1, "custom", frozenset(), session_id="login"
    )
    with pytest.raises(module.AvitoReviewSyncError, match="INVALID"):
        module.sync_canonical_avito_page(
            object(),
            actor=actor,
            settings=Settings(
                review_shadow_enabled=True, review_shadow_account_pairs=((1, 2),)
            ),
            keyring_loader=never,
            **args,
        )
