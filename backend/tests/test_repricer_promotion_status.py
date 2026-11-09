"""Participation evidence only: synthetic calendar responses, no WB writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import repricer_bff as bff
from app.repricer_cache.store import _slim_promotion
from app.repricer_promotion_status import MEMBERSHIP_VERSION, promotion_membership, promotion_refresh_metadata
from app.routers import wb_repricer_bff as router
from app.wb_repricer_filters import RepricerFilters, compile_filter

NOW = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)


def promotion(**patch):
    return {
        "id": "7", "name": "Synthetic promo", "type": "special", "wbType": "regular",
        "startDateTime": (NOW - timedelta(days=1)).isoformat(),
        "endDateTime": (NOW + timedelta(days=1)).isoformat(),
        "membershipVersion": MEMBERSHIP_VERSION, "membershipState": "complete",
        "membershipFetchedAt": NOW.isoformat(), "participantNmIds": [101], **patch,
    }


@pytest.mark.parametrize("patch", [
    {"fetchedAt": None},
    {"fetchedAt": (NOW - timedelta(hours=1)).isoformat()},
    {"promotions": [promotion(membershipFetchedAt=(NOW - timedelta(hours=1)).isoformat())]},
    {"promotions": [promotion(membershipVersion=0)]},
    {"promotions": [promotion(membershipState="partial")]},
])
def test_source_requires_one_refresh_for_missing_stale_or_incomplete_evidence(patch):
    source = {"fetchedAt": NOW.isoformat(), "promotions": [promotion()], **patch}
    assert promotion_refresh_metadata(source, now=NOW)["promotionsRefreshRequired"] is True


@pytest.mark.parametrize("rows", [[], [promotion()], [promotion(wbType="auto", membershipState="unsupported")]])
def test_fresh_empty_and_unsupported_auto_sources_do_not_cause_endless_refresh(rows):
    meta = promotion_refresh_metadata({"fetchedAt": NOW.isoformat(), "promotions": rows}, now=NOW)
    assert meta["promotionsRefreshRequired"] is False
    assert meta["promotionsRefreshAt"] == (NOW + timedelta(hours=1)).isoformat()


def test_refresh_deadline_precedes_a_promotion_boundary_or_membership_expiry():
    rows = [promotion(endDateTime=(NOW + timedelta(minutes=2)).isoformat())]
    meta = promotion_refresh_metadata({"fetchedAt": NOW.isoformat(), "promotions": rows}, now=NOW)
    assert meta["promotionsRefreshAt"] == (NOW + timedelta(minutes=2)).isoformat()


def test_background_refresh_reuses_fresh_source_without_wb_requests_or_waiting_for_other_sources(monkeypatch):
    now = datetime.now(timezone.utc)
    saved = {"fetchedAt": now.isoformat(), "promotions": [promotion(
        startDateTime=(now - timedelta(hours=1)).isoformat(),
        endDateTime=(now + timedelta(hours=1)).isoformat(), membershipFetchedAt=now.isoformat())]}
    monkeypatch.setattr(router, "_request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=9), None))
    monkeypatch.setattr(router, "get_source_cache", lambda *args, **kwargs: saved)
    monkeypatch.setattr(router, "_ensure_wb_sync_not_running", lambda _org: pytest.fail("Fresh source must not acquire collection slots"))
    monkeypatch.setattr(router, "list_promotions", lambda *args, **kwargs: pytest.fail("Fresh source must not contact WB"))
    result = router.refresh_sku_promotions(Request({"type": "http"}), scenario="complete", only_if_stale=True)
    assert result["cached"] is True
    assert result["count"] == 1


def test_missing_source_refresh_requires_durable_save(monkeypatch):
    monkeypatch.setattr(router, "_request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=9), None))
    monkeypatch.setattr(router, "get_source_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(router, "_ensure_wb_sync_not_running", lambda _org: None)
    monkeypatch.setattr(router, "list_promotions", lambda *args, **kwargs: [promotion()])
    monkeypatch.setattr(router, "_promotion_thresholds_cache", lambda _org: {})

    def unavailable(_org, _key, _payload, *, strict=False):
        assert strict is True
        raise RuntimeError("Synthetic storage unavailable")

    monkeypatch.setattr(router, "save_source_cache", unavailable)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        router.refresh_sku_promotions(Request({"type": "http"}), scenario="complete", only_if_stale=True)


@pytest.mark.parametrize("patch, expected", [
    ({}, "yes"),
    ({"status": "upcoming"}, "yes"),  # Cached status has fallen behind the timestamps.
    ({"startDateTime": (NOW + timedelta(minutes=1)).isoformat()}, "unknown"),
    ({"endDateTime": NOW.isoformat()}, "unknown"),
    ({"membershipFetchedAt": (NOW - timedelta(hours=1)).isoformat()}, "unknown"),
    ({"membershipFetchedAt": (NOW + timedelta(seconds=1)).isoformat()}, "unknown"),
    ({"membershipState": "partial"}, "unknown"),
    ({"membershipState": "unsupported", "wbType": "auto"}, "unknown"),
    ({"membershipFetchedAt": None}, "unknown"),
    ({"startDateTime": "2026-11-02"}, "unknown"),
    ({"participantNmIds": [True]}, "unknown"),
])
def test_only_fresh_current_product_evidence_is_positive(patch, expected):
    assert promotion_membership([promotion(**patch)], now=NOW).analytics(101)["promotionStatus"] == expected


def test_union_positive_wins_and_negative_has_explicit_regular_scope():
    rows = [promotion(participantNmIds=[]), promotion(id="8")]
    evidence = promotion_membership(rows, now=NOW)
    assert evidence.analytics(101)["promotionStatus"] == "yes"
    negative = evidence.analytics(102)
    assert negative["promotionStatus"] == "no"
    assert negative["promotionStatusText"] == "Нет в обычных"
    assert negative["promotionStatusScope"] == "regular"
    assert "Автоакции" in negative["promotionStatusReason"]
    assert evidence.analytics(None)["promotionStatus"] == "unknown"
    rows.append(promotion(id="9", wbType="auto", membershipState="unsupported"))
    assert promotion_membership(rows, now=NOW).analytics(101)["promotionStatus"] == "yes"
    assert promotion_membership(rows, now=NOW).analytics(102)["promotionStatus"] == "unknown"
    assert promotion_membership([], now=NOW).analytics(101)["promotionStatus"] == "unknown"


def test_excel_is_eligibility_not_current_membership_and_does_not_overwrite_api_evidence():
    legacy = {"id": "7", "type": "auto", "status": "active", "thresholdNmIds": [101]}
    imported = {"byPromotionId": {"7": {"importedAt": NOW.isoformat(), "thresholds": [
        {"nmId": 101, "wbStatus": "Не участвует", "planPriceKopecks": 100000},
    ]}}}
    merged = router._apply_promotion_threshold_cache([legacy], imported)
    assert merged[0]["thresholdStatusesByNmId"]["101"] == "Не участвует"
    assert promotion_membership(merged, now=NOW).analytics(101)["promotionStatus"] == "unknown"
    merged = router._apply_promotion_threshold_cache([promotion()], imported)
    assert merged[0]["thresholdNmIds"] == [101]
    assert promotion_membership(merged, now=NOW).analytics(101)["promotionStatus"] == "yes"


def calendar(monkeypatch, *, failure=None, auto=False):
    monkeypatch.setattr(bff, "_utc_now", lambda: NOW)
    monkeypatch.setattr(bff, "_PROMOTION_NOMENCLATURES_PAGE_SIZE", 2)
    monkeypatch.setattr(bff, "build_wb_promotions_client", lambda *args, **kwargs: object())
    monkeypatch.setattr(bff, "RateLimitedWbApiClient", lambda **kwargs: object())
    requests = []

    def request(_client, req):
        requests.append(req)
        if req.path.endswith("/details"):
            return {"promotions": [{"id": 7, "inPromoActionTotal": 3, "notInPromoActionTotal": 1}]}
        if req.path.endswith("/nomenclatures"):
            offset = req.query["offset"]
            if offset and failure:
                if failure == "schema":
                    return {}
                raise HTTPException(failure, "Synthetic unavailable page")
            # Documented shape, including a repeated item across page boundaries.
            return {"nomenclatures": [{"id": nm, "inAction": True, "planPrice": 1000}
                                      for nm in [101, 102, 101, 103][offset:offset + 2]]}
        assert req.path == "/api/v1/calendar/promotions"
        return {"promotions": [{"id": 7, "type": "auto" if auto else "regular",
                                "startDateTime": promotion()["startDateTime"], "endDateTime": promotion()["endDateTime"]}]}

    monkeypatch.setattr(bff, "_request_or_raise_calendar", request)
    return requests


def test_documented_schema_pagination_deduplication_and_parent_identity(monkeypatch):
    requests = calendar(monkeypatch)
    rows = bff.list_promotions()
    assert rows[0]["participantNmIds"] == [101, 102, 103]
    assert [req.query["offset"] for req in requests if req.path.endswith("/nomenclatures")] == [0, 2, 4]
    assert promotion_membership(rows, now=NOW).analytics(103)["promotionStatus"] == "yes"
    assert rows[0]["id"] == "7"
    assert rows[0]["thresholdNmIds"] == [101, 102, 103]


def test_auto_promotions_do_not_call_an_unsupported_endpoint(monkeypatch):
    requests = calendar(monkeypatch, auto=True)
    rows = bff.list_promotions()
    assert not any(req.path.endswith("/nomenclatures") for req in requests)
    assert rows[0]["participatingSkuCount"] == 3  # Aggregate remains available.
    assert promotion_membership(rows, now=NOW).analytics(101)["promotionStatus"] == "unknown"


@pytest.mark.parametrize("failure", [400, 429, 502, "schema"])
def test_interrupted_collection_preserves_the_saved_source(monkeypatch, failure):
    calendar(monkeypatch, failure=failure)
    saved = {"promotions": [promotion()]}
    before = deepcopy(saved)
    monkeypatch.setattr(router, "_request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=9), None))
    monkeypatch.setattr(router, "_ensure_wb_sync_not_running", lambda *_: None)
    monkeypatch.setattr(router, "get_source_cache", lambda *_args, **_kwargs: saved)
    monkeypatch.setattr(router, "save_source_cache", lambda *_args, **_kwargs: pytest.fail("Failed collection must not overwrite membership"))
    if failure == 400:
        with pytest.raises(HTTPException):
            router.refresh_sku_promotions(Request({"type": "http"}), scenario="complete")
    else:
        result = router.refresh_sku_promotions(Request({"type": "http"}), scenario="complete")
        assert result["partial"] is True
    assert saved == before


def test_full_slim_and_json_cache_roundtrip_preserves_membership_evidence():
    original = promotion()
    full = bff._promotions_for_cache([original])
    slim = json.loads(json.dumps([_slim_promotion(row) for row in full]))
    assert promotion_membership(slim, now=NOW).analytics(101) == promotion_membership(full, now=NOW).analytics(101)
    assert original == promotion()


def test_actual_sku_builder_uses_evidence_without_changing_the_price(monkeypatch):
    monkeypatch.setattr(bff, "_utc_now", lambda: NOW)
    row = bff._build_sku_row("Synthetic-101", nm_id=101, name="Synthetic", subject="", brand=None,
        chrt_ids=[], current_price_kopecks=100000, discounted_price_kopecks=90000, buyer_price_kopecks=None,
        promotions=[promotion()], use_demo_data=False, commission_tariffs_index={}, list_view=True)
    assert row["analytics"]["promotionStatus"] == "yes"
    assert row["meta"]["currentPriceKopecks"] == 90000


def test_unknown_is_not_a_confirmed_negative_filter_match():
    matches = compile_filter(RepricerFilters(promo="no"))
    assert matches({"analytics": {"promotionStatus": "no"}})
    assert not matches({"analytics": {"promotionStatus": "unknown"}})
    assert not matches({"analytics": {}})


def test_snapshot_expires_when_participation_evidence_expires(monkeypatch):
    monkeypatch.setattr(router, "get_source_cache", lambda *_args, **_kwargs: {
        "version": router.SKU_LIST_SNAPSHOT_VERSION, "promotionStatusExpiresAt": "2020-01-01T00:00:00Z",
    })
    assert router._load_repricer_sku_snapshot(9, "complete", period_suffix="30",
        include_promotions=False, include_content=False) is None


def test_snapshot_retains_next_promotion_boundary(monkeypatch):
    monkeypatch.setattr(router, "_page_finance_active", lambda: False)
    monkeypatch.setattr(router, "save_source_cache", lambda _org, _key, payload: payload)
    expiry = (NOW + timedelta(minutes=5)).isoformat()
    rows = [{"analytics": {"promotionStatusExpiresAt": expiry, "promotionStatus": "yes"}}]
    snapshot = router._save_repricer_list_snapshot(9, "synthetic_snapshot", rows, {})
    assert snapshot["promotionStatusExpiresAt"] == expiry
