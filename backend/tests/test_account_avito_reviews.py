"""Synthetic metadata preview: no answer writes and no invented review facts."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.avito import account_reviews_http as http
from app.avito.account_reviews import (
    AccountReviewsError,
    BoundedAvitoReviewsClient,
    preview_wire,
)
from tests.test_account_avito_stats_transport import resolved


def payloads():
    # Same raw shapes as test_avito_reviews.py; only synthetic identifiers/text.
    return [
        {
            "isEnabled": True,
            "rating": {"score": 4.3, "reviewsCount": 21, "reviewsWithScoreCount": 12},
        },
        {
            "reviews": [
                {
                    "id": 92312343,
                    "score": 2,
                    "stage": "fell_through",
                    "text": "synthetic-private",
                    "usedInScore": True,
                    "canAnswer": False,
                    "createdAt": 1785230400,
                    "sender": {"name": "synthetic-private"},
                    "item": {"id": 9007199254740993, "title": "synthetic-private"},
                    "answer": {
                        "id": 777,
                        "status": "moderation",
                        "text": "synthetic-private",
                    },
                    "images": [{"sizes": [{"url": "https://example.invalid/private"}]}],
                }
            ],
            "total": 21,
        },
    ]


def response(body):
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(body).encode()),
    )


def fetch(bodies=None, *, credential=None, send=None, offset=0):
    bodies, calls = payloads() if bodies is None else bodies, []

    def transport(request):
        calls.append(request)
        return send(request, len(calls)) if send else response(bodies[len(calls) - 1])

    page = BoundedAvitoReviewsClient(
        credential or resolved(), transport=httpx.MockTransport(transport)
    ).fetch_preview(offset=offset)
    return preview_wire(page, "123"), calls, page


def test_two_existing_gets_exact_ids_and_no_sensitive_text_or_write_authority():
    wire, calls, _ = fetch(offset=10)
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", "/ratings/v1/info"),
        ("GET", "/ratings/v1/reviews"),
    ]
    assert all(r.url.host == "api.avito.ru" for r in calls)
    assert dict(calls[0].url.params) == {}
    assert dict(calls[1].url.params) == {"limit": "50", "offset": "10"}
    assert wire == {
        "total": "21",
        "rating": {
            "isEnabled": True,
            "score": "4.3",
            "reviewsCount": "21",
            "reviewsWithScoreCount": "12",
        },
        "rows": [
            {
                "reviewId": "92312343",
                "score": 2,
                "stage": "fell_through",
                "usedInScore": True,
                "canAnswer": False,
                "createdAt": "2026-07-28T09:20:00Z",
                "itemId": "9007199254740993",
                "answerId": "777",
                "answerStatus": "moderation",
                "accountEvidence": "credential_scope",
            }
        ],
    }
    assert "synthetic-private" not in str(wire) and "example.invalid" not in str(wire)


def test_missing_values_stay_null_not_default_five_false_or_page_count():
    wire, _, _ = fetch([{}, {"reviews": [{"id": "92312343"}]}])
    assert wire["total"] is None
    assert set(wire["rating"].values()) == {None}
    row = wire["rows"][0]
    assert row["reviewId"] == "92312343"
    assert all(
        row[k] is None
        for k in (
            "score",
            "stage",
            "usedInScore",
            "canAnswer",
            "createdAt",
            "itemId",
            "answerId",
            "answerStatus",
        )
    )


def test_explicit_zero_counts_false_flags_and_empty_page_are_preserved():
    wire, _, _ = fetch(
        [
            {
                "isEnabled": False,
                "rating": {"score": 0, "reviewsCount": 0, "reviewsWithScoreCount": 0},
            },
            {"reviews": [], "total": 0},
        ]
    )
    assert wire["total"] == "0" and wire["rows"] == []
    assert wire["rating"] == {
        "isEnabled": False,
        "score": "0",
        "reviewsCount": "0",
        "reviewsWithScoreCount": "0",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("id", True),
        ("id", 12.5),
        ("id", ""),
        ("id", 0),
        ("id", "2E4"),
        ("score", True),
        ("score", 0),
        ("score", 6),
        ("score", "2"),
        ("score", 2.0),
        ("usedInScore", "false"),
        ("canAnswer", 1),
        ("stage", " "),
        ("item", []),
        ("answer", True),
    ],
)
def test_invalid_fields_never_coerce_or_clamp(key, value):
    bodies = payloads()
    bodies[1]["reviews"][0][key] = value
    with pytest.raises(AccountReviewsError) as error:
        fetch(bodies)
    assert str(error.value) == "AVITO_REVIEWS_PREVIEW_UNAVAILABLE"
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "case",
    [
        "missing_list",
        "bad_row",
        "duplicate",
        "oversized_page",
        "bad_total",
        "fractional_total",
        "owner",
        "row_owner",
    ],
)
def test_malformed_page_cannot_be_published_as_empty_or_account_owned(case):
    bodies = payloads()
    if case == "missing_list":
        del bodies[1]["reviews"]
    elif case == "bad_row":
        bodies[1]["reviews"] = [None]
    elif case == "duplicate":
        bodies[1]["reviews"] *= 2
    elif case == "oversized_page":
        bodies[1]["reviews"] *= 51
    elif case == "bad_total":
        bodies[1]["total"] = True
    elif case == "fractional_total":
        bodies[1]["total"] = 1.0
    elif case == "owner":
        bodies[1]["accountId"] = None
    else:
        bodies[1]["reviews"][0]["sellerId"] = "999"
    with pytest.raises(AccountReviewsError):
        fetch(bodies)


@pytest.mark.parametrize(
    "key,value",
    [
        ("score", True),
        ("score", "4.3"),
        ("score", -1),
        ("score", 5.1),
        ("reviewsCount", 2.0),
        ("reviewsWithScoreCount", -1),
    ],
)
def test_rating_metadata_rejects_lossy_values(key, value):
    bodies = payloads()
    bodies[0]["rating"][key] = value
    with pytest.raises(AccountReviewsError):
        fetch(bodies)


@pytest.mark.parametrize("value", [None, True, "1785230400", -1, 10**30])
def test_unproven_source_time_is_null_not_current_time(value):
    bodies = payloads()
    bodies[1]["reviews"][0]["createdAt"] = value
    wire, _, _ = fetch(bodies)
    assert wire["rows"][0]["createdAt"] is None


@pytest.mark.parametrize(
    "case",
    [
        "redirect",
        "status",
        "body_limit",
        "compressed",
        "duplicate_json",
        "nonfinite",
        "length_mismatch",
        "wrong_mime",
        "transport",
    ],
)
def test_first_bad_get_stops_without_second_get_retry_or_raw_error(case):
    calls = []

    def send(request, count):
        calls.append(request)
        if case == "transport":
            raise RuntimeError("synthetic-private")
        headers, raw, status = {"content-type": "application/json"}, b"{}", 200
        if case == "redirect":
            status, headers["location"] = 302, "https://example.invalid/private"
        elif case == "status":
            status = 429
        elif case == "body_limit":
            headers["content-length"] = str(4 * 1024 * 1024 + 1)
        elif case == "compressed":
            headers["content-encoding"] = "gzip"
        elif case == "duplicate_json":
            raw = b'{"isEnabled":true,"isEnabled":false}'
        elif case == "nonfinite":
            raw = b'{"rating":{"score":NaN}}'
        elif case == "length_mismatch":
            headers["content-length"] = "3"
        elif case == "wrong_mime":
            headers["content-type"] = "text/html"
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(raw))

    with pytest.raises(AccountReviewsError) as error:
        fetch(send=send)
    assert len(calls) == 1 and error.value.__context__ is None


def test_expired_credential_never_makes_http_request():
    with pytest.raises(AccountReviewsError):
        fetch(
            credential=resolved(expired=True),
            send=lambda *_: pytest.fail("HTTP on expired credential"),
        )


def test_preview_rechecks_injected_client_external_owner():
    _, _, page = fetch()
    with pytest.raises(AccountReviewsError):
        preview_wire(replace(page, external_account_id="999"), "123")


@pytest.mark.parametrize(
    "raw,want", [("4.30", "4.30"), ("4.3e0", "4.3"), ("0", "0"), ("-0.0", "0.0")]
)
def test_rating_wire_uses_exact_decimal_not_float_roundtrip(raw, want):
    bodies = payloads()

    def send(request, number):
        if number == 1:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(('{"rating":{"score":' + raw + "}}").encode()),
            )
        return response(bodies[1])

    wire, _, _ = fetch(send=send)
    assert wire["rating"]["score"] == want


def test_unbounded_stream_without_content_length_stops_at_body_budget():
    calls = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b" " * (4 * 1024 * 1024 + 1)

    def send(request, number):
        calls.append(number)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=Body()
        )

    with pytest.raises(AccountReviewsError):
        fetch(send=send)
    assert calls == [1]


@pytest.mark.parametrize("identity", ["0001", "test-avito-review-shared-001"])
def test_opaque_source_ids_preserved_not_confused_with_numeric_account(identity):
    bodies = payloads()
    row = bodies[1]["reviews"][0]
    row["id"] = identity
    row["item"]["id"] = identity
    row["answer"]["id"] = identity
    wire, _, _ = fetch(bodies)
    assert wire["rows"][0]["reviewId"] == identity
    assert wire["rows"][0]["itemId"] == identity
    assert wire["rows"][0]["answerId"] == identity


def api(monkeypatch, *, fail=None):
    monkeypatch.setattr(
        http, "get_marketplace_credential_actor", lambda request: "actor"
    )

    class Service:
        def preview(self, actor, *, marketplace_account_id, offset):
            assert actor == "actor" and marketplace_account_id == 2 and offset == 0
            if fail:
                raise fail
            return {"coverageState": "partial", "rows": []}

    app = FastAPI()
    app.include_router(
        http.make_account_avito_reviews_router(service_dependency=lambda: Service())
    )
    return TestClient(app)


def test_required_account_route_is_partial_and_no_store(monkeypatch):
    reply = api(monkeypatch).get("/api/v2/avito/accounts/2/reviews/preview?offset=0")
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert reply.json()["data"]["coverageState"] == "partial"


@pytest.mark.parametrize(
    "path",
    [
        "2/reviews/preview",
        "2/reviews/preview?offset=-1",
        "02/reviews/preview?offset=0",
        "0/reviews/preview?offset=0",
        "2/reviews/preview?offset=00",
        "2/reviews/preview?offset=0&offset=0",
        "2/reviews/preview?offset=0&limit=10",
        "2/reviews/preview?offset=9223372036854775808",
    ],
)
def test_invalid_route_never_invokes_service(monkeypatch, path):
    reply = api(monkeypatch).get("/api/v2/avito/accounts/" + path)
    assert reply.status_code == 422
    assert reply.json() == {"detail": {"code": "AVITO_REVIEWS_PREVIEW_INVALID_REQUEST"}}


@pytest.mark.parametrize(
    "fail,status",
    [
        (RuntimeError("synthetic-private"), 503),
        (HTTPException(401, "synthetic-private"), 401),
        (AccountReviewsError("AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"), 403),
    ],
)
def test_errors_have_safe_no_store_envelope(monkeypatch, fail, status):
    reply = api(monkeypatch, fail=fail).get(
        "/api/v2/avito/accounts/2/reviews/preview?offset=0"
    )
    assert reply.status_code == status and reply.headers["cache-control"] == "no-store"
    assert "synthetic-private" not in reply.text
