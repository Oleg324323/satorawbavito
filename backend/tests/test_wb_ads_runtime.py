from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import app.wb_api.ads_runtime as ads_runtime
from app.wb_api.ads_runtime import build_ads_attribution_snapshot
from app.wb_api.client import FakeWbApiClient


def test_budget_rate_limit_stops_remaining_requests_and_preserves_partial_snapshot(monkeypatch):
    fake = FakeWbApiClient(fixtures={
        '/adv/v1/promotion/count': {'adverts': [{'type': 8, 'status': 9, 'advert_list': [11, 12, 13]}]},
        '/adv/v1/balance': {}, '/adv/v1/upd': [], '/api/advert/v2/adverts': [],
        '/adv/v3/fullstats': [{'advertId': 11, 'sum': 100, 'views': 20}],
    }, errors={'/adv/v1/budget': 429})
    monkeypatch.setattr(ads_runtime, 'build_wb_ads_client', lambda *a, **kw: fake)
    snapshot = build_ads_attribution_snapshot(date_from=date(2026, 7, 1), date_to=date(2026, 7, 1), group_by='campaign')
    assert snapshot.source_status == 'partial'
    assert len([r for r in fake.requests if r.path == '/adv/v1/budget']) == 1
    assert snapshot.rows[0].budget_total_kopecks is None


def test_ads_snapshot_falls_back_to_upd_when_fullstats_has_no_campaigns(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [
                    {
                        "type": 8,
                        "status": 9,
                        "advert_list": [22161678],
                    }
                ]
            },
            "/adv/v1/balance": {"balance": 5000.0, "net": 0.0, "bonus": 0.0},
            "/adv/v1/upd": [
                {
                    "updNum": "UPD-22161678-1",
                    "updTime": "2026-07-01T12:00:00+03:00",
                    "updSum": 1332.5,
                    "advertId": 22161678,
                    "campName": "Search campaign from UPD",
                    "advertType": 8,
                    "paymentType": "cpm",
                    "advertStatus": 9,
                }
            ],
            "/api/advert/v2/adverts": [
                {
                    "advertId": 22161678,
                    "settings": {"name": "Search campaign from metadata", "payment_type": "cpm"},
                    "status": 9,
                    "type": 8,
                    "nm_settings": [{"nm_id": 123456}],
                }
            ],
            "/adv/v1/budget": {"cash": 100.0, "netting": 0.0, "total": 100.0},
            "/adv/v3/fullstats": {"error": True, "errorText": "no campaign stats"},
        }
    )
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)

    snapshot = build_ads_attribution_snapshot(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 1),
        group_by="campaign",
    )

    assert snapshot.source_status == "partial"
    assert snapshot.totals["ad_spend_kopecks"] == 133250
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert row.campaign_id == "22161678"
    assert row.campaign_name == "Search campaign from metadata"
    assert row.ad_spend_kopecks == 133250
    assert row.budget_total_kopecks == 10000


def test_ads_snapshot_batches_fullstats_over_more_than_fifty_campaigns(monkeypatch):
    advert_ids = list(range(37000000, 37000051))
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [
                    {
                        "type": 8,
                        "status": 9,
                        "advert_list": [{"advertId": advert_id} for advert_id in advert_ids],
                    }
                ]
            },
            "/adv/v1/balance": {"balance": 0.0, "net": 0.0, "bonus": 0.0},
            "/adv/v1/upd": [],
            "/api/advert/v2/adverts": [],
            "/adv/v1/budget": {"cash": 0.0, "netting": 0.0, "total": 0.0},
            "/adv/v3/fullstats": [],
        }
    )
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)

    build_ads_attribution_snapshot(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 1),
        group_by="campaign",
    )

    fullstats_requests = [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    assert len(fullstats_requests) == 2
    assert fullstats_requests[0].query["ids"] == ",".join(str(advert_id) for advert_id in advert_ids[:50])
    assert fullstats_requests[1].query["ids"] == str(advert_ids[50])


def test_ads_snapshot_can_skip_budgets_with_monotonic_bounded_progress(monkeypatch):
    advert_ids = [37000000, 37000001]
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [{"type": 8, "status": 9, "advert_list": advert_ids}]
            },
            "/adv/v1/balance": {"balance": 0.0, "net": 0.0, "bonus": 0.0},
            "/adv/v1/upd": [],
            "/api/advert/v2/adverts": [],
            "/adv/v3/fullstats": [],
        }
    )
    progress: list[int] = []
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)

    build_ads_attribution_snapshot(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 7),
        group_by="sku",
        include_budgets=False,
        progress_callback=lambda _stage, _label, percent: progress.append(percent),
        progress_start=88,
        progress_end=96,
    )

    assert not any(request.path == "/adv/v1/budget" for request in fake.requests)
    assert progress == sorted(progress)
    assert progress[0] == 88
    assert progress[-1] == 96


def test_ads_snapshot_exposes_blocked_source_error_details(monkeypatch):
    fake = FakeWbApiClient(
        errors={"/adv/v1/promotion/count": 429},
        headers={
            "/adv/v1/promotion/count": {
                "X-Ratelimit-Limit": "3",
                "X-Ratelimit-Remaining": "0",
                "X-Ratelimit-Retry": "20",
                "X-Ratelimit-Reset": "60",
                "X-Request-Id": "wb-rate-limit-1",
            }
        },
    )
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)

    snapshot = build_ads_attribution_snapshot(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 1),
        group_by="sku",
    )

    source = snapshot.diagnostics["sources"][0]
    assert snapshot.source_status == "blocked"
    assert source["sourceId"] == "wb-ads-promotion-count"
    assert source["endpoint"] == "GET /adv/v1/promotion/count"
    assert source["status"] == "blocked"
    assert source["statusCode"] == 429
    assert source["errorCode"] == "rate_limited"
    assert source["message"] == "Fake WB error 429"
    assert source["requestId"] == "wb-rate-limit-1"
    assert source["rateLimit"]["retryAfterSeconds"] == 20


def test_ads_snapshot_splits_upd_and_fullstats_date_windows_to_one_month(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [{"type": 8, "status": 9, "advert_list": [22161678]}]
            },
            "/adv/v1/balance": {"balance": 0.0, "net": 0.0, "bonus": 0.0},
            "/adv/v1/upd": [],
            "/api/advert/v2/adverts": [],
            "/adv/v1/budget": {"cash": 0.0, "netting": 0.0, "total": 0.0},
            "/adv/v3/fullstats": [],
        }
    )
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)
    monkeypatch.setattr(ads_runtime, "time", SimpleNamespace(sleep=lambda _seconds: None, monotonic=lambda: 1_000.0), raising=False)

    build_ads_attribution_snapshot(
        date_from=date(2026, 6, 2),
        date_to=date(2026, 7, 4),
        group_by="sku",
    )

    upd_requests = [request for request in fake.requests if request.path == "/adv/v1/upd"]
    fullstats_requests = [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    assert [request.query for request in upd_requests] == [
        {"from": "2026-06-02", "to": "2026-07-02"},
        {"from": "2026-07-03", "to": "2026-07-04"},
    ]
    assert [(request.query["beginDate"], request.query["endDate"]) for request in fullstats_requests] == [
        ("2026-06-02", "2026-07-02"),
        ("2026-07-03", "2026-07-04"),
    ]


def test_ads_snapshot_retries_429_adverts_chunk_after_retry_after(monkeypatch):
    class RetryAdvertsClient(FakeWbApiClient):
        def __init__(self):
            super().__init__(
                fixtures={
                    "/adv/v1/promotion/count": {
                        "adverts": [{"type": 8, "status": 9, "advert_list": [22161678]}]
                    },
                    "/adv/v1/balance": {"balance": 0.0, "net": 0.0, "bonus": 0.0},
                    "/adv/v1/upd": [],
                    "/adv/v1/budget": {"cash": 0.0, "netting": 0.0, "total": 0.0},
                    "/adv/v3/fullstats": [],
                }
            )
            self.adverts_attempts = 0

        def request(self, request):
            if request.path == "/api/advert/v2/adverts":
                self.requests.append(request)
                self.adverts_attempts += 1
                if self.adverts_attempts == 1:
                    return FakeWbApiClient(
                        errors={"/api/advert/v2/adverts": 429},
                        headers={"/api/advert/v2/adverts": {"X-Ratelimit-Retry": "1"}},
                    ).request(request)
                return FakeWbApiClient(fixtures={"/api/advert/v2/adverts": []}).request(request)
            return super().request(request)

    sleeps: list[float] = []
    fake = RetryAdvertsClient()
    monkeypatch.setattr("app.wb_api.ads_runtime.build_wb_ads_client", lambda *args, **kwargs: fake)
    monkeypatch.setattr(ads_runtime, "time", SimpleNamespace(sleep=sleeps.append, monotonic=lambda: 1_000.0), raising=False)

    snapshot = build_ads_attribution_snapshot(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 1),
        group_by="sku",
    )

    assert fake.adverts_attempts == 2
    assert sleeps == [1.0]
    assert snapshot.diagnostics["sources"][3]["status"] == "fresh"
