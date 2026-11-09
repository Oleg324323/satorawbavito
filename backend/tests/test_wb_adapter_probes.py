import pytest

from app.discovery.repricer_probes import _fields_present, run_read_prices_probe, run_task_history_probe, run_upload_lifecycle_probe
from app.wb_api.client import WbApiRequest, build_fake_client


@pytest.mark.parametrize(("value", "present"), [(None, False), (0, True), (False, True), ("", True)])
def test_probe_presence_rejects_null_but_preserves_non_null_values(value, present):
    fields = ["clubDiscount", "sizes[0].clubDiscountedPrice"]
    payload = {"data": {"listGoods": [{"clubDiscount": value, "sizes": [{"clubDiscountedPrice": value}]}]}}
    assert _fields_present(payload, fields) == (fields if present else [])


def test_read_prices_probe_confirms_price_fields_but_keeps_guard_blocked():
    response = run_read_prices_probe(build_fake_client("complete"), "FBBT_42")

    assert response.sourceStatus == "fresh"
    assert response.blockerIds == []
    assert response.results[0].sourceStatus == "fresh"
    assert {"clubDiscount", "sizes[0].clubDiscountedPrice"} <= set(response.results[0].confirmedFields)
    assert response.priceGuardPreview.canApply is False
    assert response.priceGuardPreview.freezeState == "requires_review"
    assert response.priceGuardPreview.blockerIds == []


def test_read_prices_probe_keeps_wb06_blocked_without_spp_or_buyer_price_fields():
    response = run_read_prices_probe(build_fake_client("missing_spp"), "FBBT_42")

    assert response.sourceStatus == "blocked"
    assert response.blockerIds == ["WB-06"]
    assert response.results[0].sourceStatus == "blocked"
    assert {"clubDiscount", "sizes[0].clubDiscountedPrice"} <= set(response.results[0].missingFields)
    assert response.priceGuardPreview.canApply is False
    assert response.priceGuardPreview.freezeState == "source_blocked"


def test_task_history_probe_keeps_wb22_blocked_without_row_errors():
    response = run_task_history_probe(build_fake_client("missing_task_errors"), "FBBT_42")

    assert response.sourceStatus == "blocked"
    assert "WB-22" in response.blockerIds
    blocked_rows = [result for result in response.results if result.sourceStatus == "blocked" and "errors" in result.missingFields]
    assert blocked_rows
    assert response.priceGuardPreview.canApply is False


def test_rate_limit_error_preserves_rate_limit_info_and_does_not_unblock_source():
    response = run_read_prices_probe(build_fake_client("rate_limited"), "FBBT_42")

    first = response.results[0]
    assert first.sourceStatus == "blocked"
    assert first.error is not None
    assert first.error.code == "rate_limited"
    assert first.error.retryable is True
    assert first.rateLimit is not None
    assert first.rateLimit.remaining == 0
    assert first.rateLimit.retryAfterSeconds == 1
    assert "WB-06" in response.blockerIds


def test_billing_error_is_operational_alert_not_retryable_generic_failure():
    response = run_read_prices_probe(build_fake_client("billing_error"), "FBBT_42")

    first = response.results[0]
    assert first.error is not None
    assert first.error.code == "billing_required"
    assert first.error.retryable is False
    assert first.error.operationalAlert is True


def test_server_error_is_retryable_but_keeps_price_guard_blocked():
    response = run_read_prices_probe(build_fake_client("server_error"), "FBBT_42")

    first = response.results[0]
    assert first.error is not None
    assert first.error.code == "wb_server_error"
    assert first.error.retryable is True
    assert response.priceGuardPreview.canApply is False
    assert "WB-06" in response.blockerIds


def test_fake_client_records_only_read_requests_for_probe_layer():
    client = build_fake_client("complete")
    run_read_prices_probe(client, "FBBT_42")
    run_task_history_probe(client, "FBBT_42")

    assert client.requests
    assert any(request.method == "POST" and request.path == "/api/v2/list/goods/filter" for request in client.requests)
    assert any(request.method == "GET" and request.path == "/api/v2/history/tasks" for request in client.requests)


def test_upload_lifecycle_probe_returns_automated_state_mapping():
    response = run_upload_lifecycle_probe(build_fake_client("complete"), "FBBT_42", upload_id=146567, max_polls=3)

    assert response.probeKind == "upload_lifecycle"
    assert response.lifecycleMap is not None
    assert response.lifecycleMap.uploadId == 146567
    assert response.lifecycleMap.observations
    assert response.lifecycleMap.proposedState in {"accepted", "processing", "queued", "requires_attention", "unknown"}
