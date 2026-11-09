from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app import repricer_bff
from app.wb_api.client import FakeWbApiClient, WbApiRequest, WbApiResponseEnvelope


def row(identity=1, **changes):
    return {
        "rrdId": identity,
        "nmId": 101,
        "docTypeName": "Продажа",
        "quantity": 1,
        "currency": "RUB",
        "retailAmount": "1",
        "retailPriceWithDisc": changes.get("retailAmount", "1"),
        "saleDt": "2026-08-17",
        "rrDate": "2026-08-17",
        **changes,
    }


def fetch(monkeypatch, responses):
    requests = []

    class Client:
        def request(self, request):
            requests.append(request)
            assert responses, "fetch did not terminate at the provider boundary"
            status, payload = responses.pop(0)
            return WbApiResponseEnvelope(
                request=request,
                statusCode=status,
                ok=True,
                data={"data": payload},
            )

    monkeypatch.setattr(
        repricer_bff, "build_wb_finance_client", lambda *a, **kw: Client()
    )
    monkeypatch.setattr(repricer_bff, "RateLimitedWbApiClient", lambda inner: inner)
    monkeypatch.setattr(repricer_bff, "_FINANCE_REPORT_MIN_INTERVAL_S", 0)
    result = repricer_bff.fetch_finance_report_aggregates(
        "complete",
        date_from=datetime(2026, 8, 17, tzinfo=timezone.utc),
        date_to=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )
    return result, [request.jsonBody["rrdId"] for request in requests]


@pytest.mark.parametrize("as_text", [False, True])
def test_short_pages_continue_until_204_without_rounding_large_ids(
    monkeypatch, as_text
):
    first = 9_007_199_254_740_992
    second = first + 1
    identities = [str(value) if as_text else value for value in (first, second)]
    result, cursors = fetch(
        monkeypatch,
        [(200, [row(identities[0])]), (200, [row(identities[1])]), (204, None)],
    )

    assert cursors == [0, first, second]
    assert result["rowsCount"] == 2
    assert result["aggregates"]["101"]["revenueGrossKopecks"] == 200
    assert [item["rrdId"] for item in result["rows"]] == identities


@pytest.mark.parametrize(
    "payload", [None, {}, {"data": "invalid"}, [None], [row(), False]]
)
def test_malformed_success_never_becomes_an_empty_or_partial_report(
    monkeypatch, payload
):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, payload)])
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    "identity",
    [None, 0, -1, True, 1.5, "bad", "1_0", "+10", " 10 ", "01", "١٠", "１０"],
)
def test_invalid_finance_identity_cannot_prove_complete_pagination(
    monkeypatch, identity
):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, [row(identity)]), (204, None)])
    assert error.value.status_code == 502


@pytest.mark.parametrize("last", [1, 2])
def test_nonadvancing_cursor_does_not_publish_a_partial_report(monkeypatch, last):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, [row(2)]), (200, [row(last)])])
    assert error.value.status_code == 502


def test_repeated_identity_with_changed_money_is_not_silently_discarded(monkeypatch):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, [row()]), (200, [row(retailAmount="2"), row(2)])])
    assert error.value.status_code == 502


@pytest.mark.parametrize("changed", [True, 1.0])
def test_duplicate_payloads_do_not_hide_different_json_types(monkeypatch, changed):
    with pytest.raises(HTTPException) as error:
        fetch(
            monkeypatch,
            [
                (200, [row(retailAmount=1)]),
                (200, [row(retailAmount=changed), row(2)]),
                (204, None),
            ],
        )
    assert error.value.status_code == 502


def test_unseen_identity_behind_the_requested_cursor_is_rejected(monkeypatch):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, [row(2)]), (200, [row(), row(3)]), (204, None)])
    assert error.value.status_code == 502


@pytest.mark.parametrize("status", [201, 202, 206])
def test_unexpected_success_status_cannot_certify_an_empty_report(monkeypatch, status):
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(status, [])])
    assert error.value.status_code == 502


def test_identical_overlap_is_deduplicated_when_cursor_advances(monkeypatch):
    result, cursors = fetch(
        monkeypatch, [(200, [row(), row(2)]), (200, [row(2), row(3)]), (204, None)]
    )
    assert cursors == [0, 2, 3]
    assert result["rowsCount"] == 3
    assert result["duplicateRowsSkipped"] == 1


@pytest.mark.parametrize("response", [(204, None), (200, [])])
def test_explicit_empty_response_is_a_complete_zero(monkeypatch, response):
    result, cursors = fetch(monkeypatch, [response])
    assert result["rowsCount"] == 0
    assert cursors == [0]


def test_fake_finance_provider_models_cursor_pages_and_204():
    path = "/api/finance/v1/sales-reports/detailed"
    client = FakeWbApiClient({path: {"data": [row(), row(2)]}})
    responses = [
        client.request(
            WbApiRequest(
                method="POST", path=path, jsonBody={"rrdId": cursor, "limit": 1}
            )
        )
        for cursor in (0, 1, 2)
    ]
    assert [response.statusCode for response in responses] == [200, 200, 204]
    assert [response.data["data"] for response in responses] == [[row()], [row(2)], []]
    assert client.fixtures[path]["data"] == [row(), row(2)]


def test_manual_finance_save_publishes_valid_batch_without_clock_name_error(monkeypatch):
    from app.routers import wb_repricer_bff as router

    provider = FakeWbApiClient({
        "/api/finance/v1/sales-reports/detailed": {"data": [row(retailAmount="12.34")]},
    })
    monkeypatch.setattr(repricer_bff, "build_wb_finance_client", lambda *a, **kw: provider)
    monkeypatch.setattr(router, "list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    saved = []

    def save(organization_id, key, payload):
        saved.append((organization_id, key, payload))
        return payload

    monkeypatch.setattr(router, "save_source_cache", save)
    payload, cached, goods_count, matched_count = router._save_finance_source_cache(
        1, scenario="complete", wb_token=None,
        range_start=datetime(2026, 8, 17, tzinfo=timezone.utc),
        range_end=datetime(2026, 8, 23, tzinfo=timezone.utc),
        resolved_period_days=7, period_suffix="2026-08-17_2026-08-23",
    )
    assert payload["aggregates"]["101"]["revenueGrossKopecks"] == 1234
    assert (goods_count, matched_count) == (1, 1)
    assert cached["canonicalSnapshot"] == {"state": "disabled"}
    assert len(saved) == 1
    assert saved[0][:2] == (1, "finance_2026-08-17_2026-08-23")
    assert saved[0][2]["aggregates"]["101"]["revenueGrossKopecks"] == 1234


def test_fake_finance_does_not_sleep_or_change_real_provider_pacing(monkeypatch):
    path = "/api/finance/v1/sales-reports/detailed"
    client = FakeWbApiClient({path: {"data": [row()]}})
    monkeypatch.setattr(
        repricer_bff, "build_wb_finance_client", lambda *a, **kw: client
    )
    monkeypatch.setattr(repricer_bff, "_finance_report_last_request_at", 1_000.0)
    monkeypatch.setattr(repricer_bff.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(
        repricer_bff.time, "sleep", lambda seconds: pytest.fail(f"fake slept {seconds}")
    )

    result = repricer_bff.fetch_finance_report_aggregates(
        "complete", date_from=datetime(2026, 8, 17, tzinfo=timezone.utc)
    )
    assert result["rowsCount"] == 1
    assert len(client.requests) == 2
    assert repricer_bff._finance_report_last_request_at == 1_000.0


@pytest.mark.parametrize("document", ["Продажа", "Возврат"])
@pytest.mark.parametrize("nm_id", [101, 0])
@pytest.mark.parametrize(
    "revenue",
    [
        {},
        {"retailAmount": None},
        {"retailAmount": ""},
        {"retailAmount": True},
        {"retailAmount": False},
        {"retailAmount": "bad"},
        {"retailAmount": "NaN"},
        {"retailAmount": "Infinity"},
        {"retailAmount": "1e308"},
        {"retail_amount": False},
    ],
)
def test_invalid_trade_revenue_never_publishes_partial_finance(
    monkeypatch, document, nm_id, revenue
):
    invalid = row(2, docTypeName=document, nmId=nm_id, saleDt="2026-08-17")
    invalid.pop("retailAmount")
    invalid.update(revenue)
    with pytest.raises(HTTPException) as error:
        fetch(monkeypatch, [(200, [row()]), (200, [invalid]), (204, None)])
    assert error.value.status_code == 502
    assert error.value.detail == "WB_FINANCE_INVALID_TRADE_REVENUE"


@pytest.mark.parametrize("field", ["retailAmount", "retail_amount"])
@pytest.mark.parametrize("document,sign", [("Продажа", 1), ("Возврат", -1)])
@pytest.mark.parametrize(
    "amount,kopecks", [(0, 0), ("0", 0), ("12,34", 1234), (-2.34, -234)]
)
def test_valid_trade_revenue_preserves_period_and_daily_amounts(
    monkeypatch, field, document, sign, amount, kopecks
):
    trade = row(docTypeName=document, saleDt="2026-08-17")
    trade.pop("retailAmount")
    trade[field] = amount
    trade["retailPriceWithDisc"] = amount
    result, cursors = fetch(monkeypatch, [(200, [trade]), (204, None)])
    for aggregate in (
        result["aggregates"]["101"],
        result["dailyAggregates"]["2026-08-17"]["101"],
    ):
        assert aggregate["revenueGrossKopecks"] == sign * kopecks
        assert aggregate["sellerRevenueMissingRows"] == 0
    assert cursors == [0, 1]


def test_fee_only_row_does_not_require_trade_revenue(monkeypatch):
    fee = row(docTypeName="", saleDt="2026-08-17", paidStorage="12.34")
    fee.pop("retailAmount")
    result, cursors = fetch(monkeypatch, [(200, [fee]), (204, None)])
    for aggregate in (
        result["aggregates"]["101"],
        result["dailyAggregates"]["2026-08-17"]["101"],
    ):
        assert aggregate["revenueGrossKopecks"] == 0
        assert aggregate["storageKopecks"] == 1234
        assert aggregate["sellerRevenueMissingRows"] == 0
    assert cursors == [0, 1]
