from __future__ import annotations

from datetime import date

import pytest

import app.wb_api.raw_advertising as raw_module
from app.platform.advertising.raw import (
    AdvertisingNormalizationError,
    normalize_raw_advertising,
)
from app.platform.period import Period
from app.wb_api.client import (
    RateLimitInfo,
    WbApiRequest,
    WbApiResponseEnvelope,
    classify_wb_error,
)

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 1))


class QueryAwareAdsClient:
    def __init__(
        self,
        *,
        campaign_ids: list[int] | None = None,
        upd_ids: list[int] | None = None,
        failing_path: str | None = None,
    ) -> None:
        self.campaign_ids = campaign_ids or []
        self.upd_ids = upd_ids or []
        self.failing_path = failing_path
        self.requests: list[WbApiRequest] = []

    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        self.requests.append(request)
        if request.path == self.failing_path:
            rate_limit = RateLimitInfo(retryAfterSeconds=0)
            return WbApiResponseEnvelope(
                request=request,
                statusCode=429,
                ok=False,
                error=classify_wb_error(
                    429, "do not publish this message", rate_limit
                ),
                rateLimit=rate_limit,
                wbRequestId="failed-request",
            )
        return WbApiResponseEnvelope(
            request=request,
            statusCode=200,
            ok=True,
            data=self._payload(request),
            wbRequestId=f"request-{len(self.requests)}",
        )

    def _payload(self, request: WbApiRequest):
        if request.path == "/adv/v1/promotion/count":
            return {
                "adverts": [
                    {
                        "type": 8,
                        "status": 9,
                        "advert_list": [
                            {"advertId": advert_id} for advert_id in self.campaign_ids
                        ],
                    }
                ],
                "all": len(self.campaign_ids),
            }
        if request.path == "/adv/v1/upd":
            return [
                {
                    "updNum": f"UPD-{advert_id}",
                    "updTime": "2026-09-01T12:00:00+03:00",
                    "updSum": 0,
                    "advertId": advert_id,
                    "campName": "campaign",
                    "advertType": 8,
                    "paymentType": "cpm",
                    "advertStatus": 9,
                }
                for advert_id in self.upd_ids
            ]
        if request.path == "/api/advert/v2/adverts":
            return []
        if request.path == "/adv/v3/fullstats":
            return []
        raise AssertionError(f"unexpected request {request.path}")


class AdapterShapedAdsClient(QueryAwareAdsClient):
    def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
        response = super().request(request)
        if response.ok and isinstance(response.data, list):
            return response.model_copy(update={"data": {"data": response.data}})
        return response

    def _payload(self, request: WbApiRequest):
        if request.path == "/adv/v1/upd":
            if not self.upd_ids:
                return []
            return [
                {
                    "updNum": f"UPD-{request.query['from']}",
                    "updTime": f"{request.query['from']}T12:00:00+03:00",
                    "updSum": 1,
                    "advertId": self.upd_ids[0],
                    "campName": "campaign",
                    "advertType": 8,
                    "paymentType": "cpm",
                    "advertStatus": 9,
                }
            ]
        if request.path == "/adv/v3/fullstats":
            business_date = request.query["beginDate"]
            campaign_id = int(request.query["ids"].split(",")[0])
            return [
                {
                    "advertId": campaign_id,
                    "views": 1,
                    "sum": 1,
                    "days": [
                        {
                            "date": f"{business_date}T00:00:00+03:00",
                            "views": 1,
                            "sum": 1,
                            "apps": [],
                        }
                    ],
                }
            ]
        return super()._payload(request)


class SiblingDataAdsClient(QueryAwareAdsClient):
    def __init__(self, sibling_path: str) -> None:
        super().__init__()
        self.sibling_path = sibling_path

    def _payload(self, request: WbApiRequest):
        if request.path == self.sibling_path:
            return {"data": [], "error": "unexpected response"}
        return super()._payload(request)


def test_raw_fullstats_obeys_existing_wb_request_interval(monkeypatch):
    client = QueryAwareAdsClient(campaign_ids=list(range(1, 52)))
    intervals = []

    def request(client, item, *, min_interval_s):
        intervals.append((item.path, min_interval_s))
        return client.request(item)

    monkeypatch.setattr(raw_module, "_request_with_retry", request)
    result = raw_module._execute_plan(client, PERIOD)
    assert result.state == "ready"
    assert intervals == [
        ("/adv/v1/promotion/count", 0.0),
        ("/adv/v1/upd", 0.0),
        ("/api/advert/v2/adverts", 0.0),
        ("/adv/v3/fullstats", 20.0),
        ("/adv/v3/fullstats", 20.0),
    ]


def test_raw_fetch_batches_campaigns_and_windows_in_exact_order(monkeypatch):
    campaign_ids = list(range(1000, 1051))
    fake = QueryAwareAdsClient(campaign_ids=campaign_ids)
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(
        Period(date(2026, 6, 2), date(2026, 7, 4)), wb_token="token"
    )

    assert result.state == "ready"
    assert [request.path for request in fake.requests] == [
        "/adv/v1/promotion/count",
        "/adv/v1/upd",
        "/adv/v1/upd",
        "/api/advert/v2/adverts",
        "/adv/v3/fullstats",
        "/adv/v3/fullstats",
        "/adv/v3/fullstats",
        "/adv/v3/fullstats",
    ]
    assert [request.query for request in fake.requests[1:4]] == [
        {"from": "2026-06-02", "to": "2026-07-02"},
        {"from": "2026-07-03", "to": "2026-07-04"},
        {"statuses": "7,9,11"},
    ]
    fullstats = fake.requests[4:]
    assert [len(request.query["ids"].split(",")) for request in fullstats] == [
        50,
        50,
        1,
        1,
    ]
    assert [
        (request.query["beginDate"], request.query["endDate"]) for request in fullstats
    ] == [
        ("2026-06-02", "2026-07-02"),
        ("2026-07-03", "2026-07-04"),
        ("2026-06-02", "2026-07-02"),
        ("2026-07-03", "2026-07-04"),
    ]


def test_raw_fetch_campaign_batches_ignore_discovery_order():
    campaign_ids = list(range(1000, 1051))
    first = QueryAwareAdsClient(campaign_ids=campaign_ids)
    second = QueryAwareAdsClient(campaign_ids=list(reversed(campaign_ids)))

    first_result = raw_module._execute_plan(
        raw_module.RateLimitedWbApiClient(first), PERIOD
    )
    second_result = raw_module._execute_plan(
        raw_module.RateLimitedWbApiClient(second), PERIOD
    )

    assert [
        request.query["ids"]
        for request in first.requests
        if request.path == "/adv/v3/fullstats"
    ] == [
        request.query["ids"]
        for request in second.requests
        if request.path == "/adv/v3/fullstats"
    ]
    assert (
        normalize_raw_advertising(PERIOD, first_result.bundle).snapshot_checksum
        == normalize_raw_advertising(PERIOD, second_result.bundle).snapshot_checksum
    )


def test_raw_fetch_uses_promotion_and_successful_upd_campaign_union(monkeypatch):
    fake = QueryAwareAdsClient(campaign_ids=[1001], upd_ids=[1002])
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.state == "ready"
    fullstats = [
        request for request in fake.requests if request.path == "/adv/v3/fullstats"
    ]
    assert [request.query["ids"] for request in fullstats] == ["1001,1002"]
    assert (
        normalize_raw_advertising(PERIOD, result.bundle).campaigns[0].campaign_id
        == 1001
    )


def test_raw_fetch_excludes_unsupported_promotion_statuses(monkeypatch):
    class StatusAwareAdsClient(QueryAwareAdsClient):
        def _payload(self, request: WbApiRequest):
            if request.path == "/adv/v1/promotion/count":
                return {
                    "adverts": [
                        {"status": 7, "advert_list": [{"advertId": 1001}]},
                        {"status": 8, "advert_list": [{"advertId": 9999}]},
                    ]
                }
            return super()._payload(request)

    fake = StatusAwareAdsClient()
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    fullstats = [
        request for request in fake.requests if request.path == "/adv/v3/fullstats"
    ]
    assert [request.query["ids"] for request in fullstats] == ["1001"]


def test_raw_fetch_marks_exhausted_request_partial_without_publishing_error(
    monkeypatch,
):
    fake = QueryAwareAdsClient(campaign_ids=[1001], failing_path="/adv/v3/fullstats")
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.state == "partial"
    assert result.expected_requests == 4
    assert result.completed_requests == 3
    assert result.failure_codes == ("rate_limited",)
    assert (
        len(
            [
                request
                for request in fake.requests
                if request.path == "/adv/v3/fullstats"
            ]
        )
        == 3
    )
    assert result.bundle["fullstats"] == []
    failed = result.bundle["manifest"][-1]
    assert failed["ok"] is False
    assert "message" not in failed
    assert "error" not in failed


def test_raw_fetch_retries_transient_rate_limit(monkeypatch):
    class TransientRateLimitedAdsClient(QueryAwareAdsClient):
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            if request.path == "/adv/v3/fullstats" and not any(
                item.path == request.path for item in self.requests
            ):
                self.requests.append(request)
                rate_limit = RateLimitInfo(retryAfterSeconds=0)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "rate limited", rate_limit),
                    rateLimit=rate_limit,
                )
            return super().request(request)

    fake = TransientRateLimitedAdsClient(campaign_ids=[1001])
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.state == "ready"
    assert result.expected_requests == result.completed_requests == 4
    assert len(result.bundle["manifest"]) == 4
    assert len(
        [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    ) == 2


def test_raw_fetch_deduplicates_error_codes_from_failed_pages(monkeypatch):
    fake = QueryAwareAdsClient(campaign_ids=[1001], failing_path="/adv/v1/upd")
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(
        Period(date(2026, 6, 2), date(2026, 7, 4)), wb_token="token"
    )

    assert result.state == "partial"
    assert result.failure_codes == ("rate_limited",)
    assert (
        len([request for request in fake.requests if request.path == "/adv/v1/upd"])
        == 6
    )
    assert result.bundle["upd"] == []


def test_raw_fetch_empty_successful_cabinet_is_ready_and_normalizable(monkeypatch):
    fake = QueryAwareAdsClient()
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.state == "ready"
    assert result.expected_requests == result.completed_requests == 3
    assert [request.path for request in fake.requests] == [
        "/adv/v1/promotion/count",
        "/adv/v1/upd",
        "/api/advert/v2/adverts",
    ]
    assert result.bundle["fullstats"] == []
    assert normalize_raw_advertising(PERIOD, result.bundle).facts == ()


def test_raw_fetch_normalizes_adapter_shaped_lists_across_subwindows(monkeypatch):
    period = Period(date(2026, 6, 2), date(2026, 7, 4))
    fake = AdapterShapedAdsClient(campaign_ids=[1001], upd_ids=[1001])
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(period, wb_token="token")
    evidence = normalize_raw_advertising(period, result.bundle)

    assert result.state == "ready"
    assert result.bundle["adverts"] == {"adverts": []}
    assert [page["payload"] for page in result.bundle["upd"]] == [
        [
            {
                "updNum": "UPD-2026-06-02",
                "updTime": "2026-06-02T12:00:00+03:00",
                "updSum": 1,
                "advertId": 1001,
                "campName": "campaign",
                "advertType": 8,
                "paymentType": "cpm",
                "advertStatus": 9,
            }
        ],
        [
            {
                "updNum": "UPD-2026-07-03",
                "updTime": "2026-07-03T12:00:00+03:00",
                "updSum": 1,
                "advertId": 1001,
                "campName": "campaign",
                "advertType": 8,
                "paymentType": "cpm",
                "advertStatus": 9,
            }
        ],
    ]
    assert [
        (fact.date_from, fact.date_to)
        for fact in evidence.facts
        if fact.grain == "period"
    ] == [
        (date(2026, 6, 2), date(2026, 7, 2)),
        (date(2026, 7, 3), date(2026, 7, 4)),
    ]
    assert [document.business_date for document in evidence.spend_documents] == [
        date(2026, 6, 2),
        date(2026, 7, 3),
    ]


def test_raw_fetch_normalizes_adapter_shaped_empty_cabinet(monkeypatch):
    fake = AdapterShapedAdsClient()
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.state == "ready"
    assert result.bundle["adverts"] == {"adverts": []}
    assert result.bundle["upd"] == [
        {"dateFrom": "2026-09-01", "dateTo": "2026-09-01", "payload": []}
    ]
    assert normalize_raw_advertising(PERIOD, result.bundle).facts == ()


def test_raw_fetch_normalizes_adapter_shaped_empty_fullstats(monkeypatch):
    class EmptyFullstatsClient(QueryAwareAdsClient):
        def _payload(self, request: WbApiRequest):
            if request.path == "/adv/v3/fullstats":
                return {"data": None}
            return super()._payload(request)

    fake = EmptyFullstatsClient(campaign_ids=[1001])
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.bundle["fullstats"][0]["payload"] == []
    assert normalize_raw_advertising(PERIOD, result.bundle).facts == ()


def test_raw_fetch_keeps_sibling_upd_mapping_unpublishable(monkeypatch):
    fake = SiblingDataAdsClient("/adv/v1/upd")
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.bundle["upd"][0]["payload"] == {
        "data": [],
        "error": "unexpected response",
    }
    with pytest.raises(AdvertisingNormalizationError, match="invalid upd payload"):
        normalize_raw_advertising(PERIOD, result.bundle)


def test_raw_fetch_keeps_sibling_adverts_mapping_unpublishable(monkeypatch):
    fake = SiblingDataAdsClient("/api/advert/v2/adverts")
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert result.bundle["adverts"] == {
        "data": [],
        "error": "unexpected response",
    }
    with pytest.raises(
        AdvertisingNormalizationError, match="invalid advertising campaigns"
    ):
        normalize_raw_advertising(PERIOD, result.bundle)


def test_raw_fetch_manifest_is_checksum_only_and_secret_free(monkeypatch):
    fake = QueryAwareAdsClient(campaign_ids=[1001])
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    result = raw_module.fetch_raw_advertising(PERIOD, wb_token="token-value")

    assert all(
        set(item)
        == {
            "endpoint",
            "request",
            "statusCode",
            "ok",
            "wbRequestId",
            "responseChecksum",
        }
        for item in result.bundle["manifest"]
    )
    assert all(
        len(item["responseChecksum"]) == 64 for item in result.bundle["manifest"]
    )
    assert "token-value" not in repr(result.bundle)
    assert not any(
        forbidden in key.lower()
        for item in result.bundle["manifest"]
        for key in item
        for forbidden in ("token", "authorization", "secret", "message", "error")
    )


def test_raw_response_checksum_ignores_nonsemantic_list_order():
    first = {"data": [{"id": 2, "values": [3, 1]}, {"id": 1}]}
    reordered = {"data": [{"id": 1}, {"values": [1, 3], "id": 2}]}
    changed = {"data": [{"id": 1}, {"values": [1, 4], "id": 2}]}

    assert raw_module._checksum(first) == raw_module._checksum(reordered)
    assert raw_module._checksum(first) != raw_module._checksum(changed)


def test_raw_fetch_requires_a_nonblank_token():
    with pytest.raises(ValueError, match="WB token is required"):
        raw_module.fetch_raw_advertising(PERIOD, wb_token="  ")


@pytest.mark.parametrize("payload", [float("nan"), object()])
def test_raw_fetch_rejects_non_json_success_payloads(monkeypatch, payload):
    class NonJsonAdsClient(QueryAwareAdsClient):
        def _payload(self, _request: WbApiRequest):
            return payload

    fake = NonJsonAdsClient()
    monkeypatch.setattr(raw_module, "build_wb_ads_client", lambda *_a, **_k: fake)

    with pytest.raises(
        raw_module.RawAdvertisingFetchError,
        match="invalid WB advertising response payload",
    ) as raised:
        raw_module.fetch_raw_advertising(PERIOD, wb_token="token")

    assert raised.value.__cause__ is None
    assert repr(payload) not in str(raised.value)
