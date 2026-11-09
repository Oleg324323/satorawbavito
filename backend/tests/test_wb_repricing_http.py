import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.modules.wb_repricing_dispatch import CanonicalApplyRequest
from app.modules.wb_repricing_http import (
    PriceHttpError,
    WbPriceHttpAdapter,
    matches_single_product_application,
    parse_history,
)
from app.modules.wb_repricing_repository import ApprovalRepositoryScope
from app.modules.wb_repricing_upload_receipt import extract_post_upload_id_from_bytes
from tests.test_wb_live_history_worker import NOW, credential

BODY = b'{"data":[{"discount":0,"nmID":123,"price":1300}]}'


def adapter(
    handler, admit=lambda *args: True, budget=4096, cooldown=lambda *args: True
):
    return WbPriceHttpAdapter(
        organization_id=1,
        marketplace_account_id=2,
        admit_request=admit,
        record_cooldown=cooldown,
        max_response_bytes=budget,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )


def test_original_receipt_is_lossless_and_post_is_single_exact_request():
    calls, admissions = [], []
    raw = b'{"data":{"id":9007199254740993,"alreadyExists":false},"error":false}'

    def handle(request):
        calls.append(request)
        assert request.content == BODY and request.method == "POST"
        assert (
            str(request.url)
            == "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"
        )
        assert request.headers["Authorization"] == "synthetic-only"
        return httpx.Response(200, content=raw)

    def admit(*args):
        admissions.append(args)
        return True

    response = adapter(handle, admit).post_price_once(
        body=BODY, credential=credential()
    )
    assert response.raw_body == raw and response.observed_at == NOW
    assert (
        extract_post_upload_id_from_bytes(
            raw,
            method="POST",
            path="/api/v2/upload/task",
            status_code=response.status_code,
            ok=response.ok,
            apply_mode="wb_api",
            max_response_bytes=4096,
        )
        == "9007199254740993"
    )
    assert len(calls) == 1 and admissions == [
        (1, 2, "POST", "/api/v2/upload/task", 900)
    ]


@pytest.mark.parametrize("status", [208, 302, 401, 429, 500])
def test_no_redirect_or_http_status_retry(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://example.org"}, content=b"{}"
        )

    result = adapter(handle).post_price_once(body=BODY, credential=credential())
    assert result.status_code == status and len(calls) == 1


def test_timeout_has_safe_error_and_no_second_post():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic sensitive detail", request=request)

    with pytest.raises(PriceHttpError) as caught:
        adapter(handle).post_price_once(body=BODY, credential=credential())
    assert len(calls) == 1 and caught.value.__context__ is None
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [("sizeID", 5), ("minPrice", 900), ("price", True), ("discount", 100)],
)
def test_unsupported_or_invalid_intent_never_posts(field, value):
    calls = []
    data = json.loads(BODY)
    data["data"][0][field] = value
    with pytest.raises(PriceHttpError):
        adapter(lambda r: calls.append(r)).post_price_once(
            body=json.dumps(data).encode(), credential=credential()
        )
    assert not calls


def test_wrong_account_denied_quota_and_size_limit():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 20)

    with pytest.raises(PriceHttpError):
        adapter(handle).post_price_once(body=BODY, credential=credential(9))
    with pytest.raises(PriceHttpError):
        adapter(handle, admit=lambda *args: 1).post_price_once(
            body=BODY, credential=credential()
        )
    assert not calls
    with pytest.raises(PriceHttpError):
        adapter(handle, budget=10).post_price_once(body=BODY, credential=credential())
    assert len(calls) == 1


def test_history_state_exact_identity_and_next_admission_no_sleep():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.params["uploadID"] == "999"
        return httpx.Response(
            200,
            json={
                "data": {
                    "uploadID": 999,
                    "status": 3,
                    "overAllGoodsNumber": 1,
                    "successGoodsNumber": 1,
                }
            },
        )

    result = adapter(handle).read_history_once(
        credential=credential(), expected_upload_id="999"
    )
    assert result.result.status == 3 and result.result.succeeded == 1
    assert result.next_not_before == NOW + timedelta(seconds=900) and len(calls) == 1
    assert not hasattr(result, "applied")


def good():
    return {
        "nmID": 123,
        "vendorCode": "item",
        "sizeID": None,
        "price": 1300,
        "currencyIsoCode4217": "RUB",
        "discount": 0,
        "status": 2,
        "errorText": None,
    }


def parse(rows, upload=999):
    return parse_history(
        json.dumps({"data": {"uploadID": upload, "historyGoods": rows}}).encode(),
        expected_upload_id="999",
        path="/api/v2/history/goods/task",
        limit=1000,
    )


def test_history_goods_retains_product_grain_no_raw_error():
    row = good()
    row.update(status=3, errorText="synthetic vendor error text")
    result = parse([row])
    assert result.goods[0].has_error and result.goods[0].size_id is None
    assert not hasattr(result.goods[0], "error_text")


@pytest.mark.parametrize(
    "field,value",
    [
        ("nmID", True),
        ("price", 1300.0),
        ("status", 1),
        ("currencyIsoCode4217", "USD"),
        ("errorText", "failure with status2"),
    ],
)
def test_bad_goods_evidence_rejected(field, value):
    row = good()
    row[field] = value
    with pytest.raises(PriceHttpError):
        parse([row])


def test_foreign_upload_duplicate_goods_and_duplicate_json_rejected():
    with pytest.raises(PriceHttpError):
        parse([good()], upload=998)
    with pytest.raises(PriceHttpError):
        parse([good(), good()])
    with pytest.raises(PriceHttpError):
        parse_history(
            b'{"data":{},"data":{}}',
            expected_upload_id="999",
            path="/api/v2/history/tasks",
        )


def test_single_product_evidence_checks_full_intent_not_just_upload_status():
    def handle(request):
        data = {"uploadID": 999}
        if request.url.path.endswith("goods/task"):
            data["historyGoods"] = [good()]
        else:
            data.update(status=3, overAllGoodsNumber=1, successGoodsNumber=1)
        return httpx.Response(200, json={"data": data})

    http = adapter(handle)
    state = http.read_history_once(credential=credential(), expected_upload_id="999")
    goods = http.read_history_once(
        credential=credential(), expected_upload_id="999", details=True
    )
    request = CanonicalApplyRequest(
        ApprovalRepositoryScope(1, 2, "approval-1"), 3, 123, "item", 130000, 0
    )
    assert matches_single_product_application(
        request, state, goods, receipt_upload_id="999"
    )
    assert not matches_single_product_application(
        replace(request, price_kopecks=140000), state, goods, receipt_upload_id="999"
    )
    assert not matches_single_product_application(
        replace(request, article_id="other"), state, goods, receipt_upload_id="999"
    )
    assert not matches_single_product_application(
        request,
        state,
        replace(goods, result=replace(goods.result, limit=1)),
        receipt_upload_id="999",
    )
    with pytest.raises(PriceHttpError):
        matches_single_product_application(
            request,
            state,
            replace(goods, marketplace_account_id=9),
            receipt_upload_id="999",
        )
    with pytest.raises(PriceHttpError):
        matches_single_product_application(
            request, state, goods, receipt_upload_id="998"
        )


def test_read_429_preserves_safe_retry_deadline_no_loop():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1800"})

    with pytest.raises(PriceHttpError) as caught:
        adapter(handle).read_history_once(
            credential=credential(), expected_upload_id="999"
        )
    assert caught.value.next_not_before == NOW + timedelta(seconds=1800)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["post_429", "broken_stream", "malformed_get"])
def test_durable_throttle_feedback_precedes_body_and_survives_failure(mode):
    events = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            events.append("read_body")
            raise httpx.ReadError("synthetic truncated body")
            yield b""  # pragma: no cover -- generator protocol

    def handle(request):
        events.append("request")
        if mode == "broken_stream":
            return httpx.Response(
                200, headers={"Retry-After": "3600"}, stream=BrokenStream()
            )
        return httpx.Response(
            429 if mode == "post_429" else 200,
            headers={"Retry-After": "3600"},
            content=b"invalid",
        )

    def cooldown(org, account, deadline):
        events.append(("cooldown", org, account, deadline))
        return True

    http = adapter(handle, cooldown=cooldown)
    if mode == "post_429":
        assert (
            http.post_price_once(body=BODY, credential=credential()).status_code == 429
        )
    else:
        with pytest.raises(PriceHttpError):
            http.read_history_once(credential=credential(), expected_upload_id="999")
    assert events[:2] == ["request", ("cooldown", 1, 2, NOW + timedelta(seconds=3600))]
    assert events.count("request") == 1


def test_failed_cooldown_commit_fails_closed_without_second_post():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b'{"data":{"id":999}}')

    with pytest.raises(PriceHttpError):
        adapter(handle, cooldown=lambda *args: False).post_price_once(
            body=BODY, credential=credential()
        )
    assert len(calls) == 1
