from copy import deepcopy
from datetime import date
import socket
from types import SimpleNamespace

import pytest

from app.wb_api import reports_sources_runtime as runtime
from app.wb_api.client import WbApiResponseEnvelope


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("stock parser test must not perform I/O")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(wb_api_mode="fake"))


class Pages:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return WbApiResponseEnvelope(request=request, ok=True, statusCode=200,
                                     data=next(self.payloads))


@pytest.mark.parametrize("invalid", [None, "unexpected", 42, {"unexpected": []},
                                     {"data": [None]}, {"data": [{"nmId": 3}, "bad"]}])
def test_invalid_page_is_not_a_successful_short_terminal_page(invalid):
    client = Pages([{"data": [{"nmId": 1}, {"nmId": 2}]}, invalid])
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        client, limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok is False
    assert envelope.error.code == "STOCK_REPORT_INVALID_PAGE"
    assert [row["nmId"] for row in rows] == [1, 2]
    assert [request.jsonBody["offset"] for request in client.requests] == [0, 2]


def test_annotation_does_not_mutate_provider_payload_for_checksum_evidence():
    raw = {"data": [{"nmId": 1, "quantity": 0}]}
    original = deepcopy(raw)
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        Pages([raw]), limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert rows == [{"nmId": 1, "quantity": 0, "stockType": "wb"}]
    assert raw == original


@pytest.mark.parametrize("wrapper", ["items", "rows", "report", "data"])
def test_supported_wrapped_empty_page_terminates_successfully(wrapper):
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        Pages([{"data": {wrapper: []}}]), limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert rows == []


def test_full_page_requires_next_page_and_preserves_omitted_quantity():
    client = Pages([{"data": [{"nmId": 1, "quantity": 0}, {"nmId": 2}]}, {"data": []}])
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        client, limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert len(client.requests) == 2
    assert rows[0]["quantity"] == 0
    assert "quantity" not in rows[1]


def test_existing_singleton_stock_row_shape_is_preserved():
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        Pages([{"data": {"nmId": 1, "quantity": 2}}]), limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert rows == [{"nmId": 1, "quantity": 2, "stockType": "wb"}]


def test_explicit_http_204_empty_response_remains_empty_success():
    class Empty:
        def request(self, request):
            return WbApiResponseEnvelope(request=request, ok=True, statusCode=204)
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        Empty(), limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert rows == []


def test_rate_limit_recovery_retries_same_page_without_repeating_accepted_rows():
    class Recovering:
        def __init__(self):
            self.offsets = []

        def request(self, request):
            self.offsets.append(request.jsonBody["offset"])
            if len(self.offsets) == 2:
                return WbApiResponseEnvelope(request=request, ok=False, statusCode=429)
            data = [{"nmId": 1}, {"nmId": 2}] if len(self.offsets) == 1 else [{"nmId": 3}]
            return WbApiResponseEnvelope(request=request, ok=True, statusCode=200, data={"data": data})
    client = Recovering()
    envelope, rows = runtime._load_stock_report_wb_warehouses(
        client, limit=2, sleeper=lambda _: None,
    )
    assert envelope.ok
    assert [row["nmId"] for row in rows] == [1, 2, 3]
    assert client.offsets == [0, 2, 2]


def test_report_consumer_marks_malformed_stock_run_partial(monkeypatch):
    class SyntheticClient:
        def request(self, request):
            data = []
            if request.path == "/api/analytics/v1/stocks-report/wb-warehouses":
                data = [{"nmId": 1, "quantity": 3}, {"nmId": 2, "quantity": 4}]
                if request.jsonBody["offset"]:
                    data = [None]
            return WbApiResponseEnvelope(request=request, ok=True, statusCode=200, data={"data": data})
    for name in ("build_wb_analytics_client", "build_wb_statistics_client", "build_wb_finance_client"):
        monkeypatch.setattr(runtime, name, lambda **kwargs: SyntheticClient())
    monkeypatch.setattr(runtime, "RateLimitedWbApiClient", lambda inner: inner)
    monkeypatch.setattr(runtime, "STOCK_REPORT_PAGE_LIMIT", 2)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    snapshot = runtime.build_wb_reports_sources_snapshot(date_from=date(2026, 9, 1), date_to=date(2026, 9, 2))
    assert snapshot.source_status == "partial"
    assert "WB-03" in snapshot.blocker_ids
    assert [(row.nm_id, row.quantity) for row in snapshot.stocks] == [(1, 3), (2, 4)]


@pytest.mark.parametrize("reordered", [False, True])
def test_repeated_full_page_is_partial_instead_of_duplicate_stock_or_infinite_fetch(reordered):
    first = [{"nmId": 1, "quantity": 2}, {"nmId": 2, "quantity": 3}]
    second = list(reversed(first)) if reordered else first
    client = Pages([{"data": first}, {"data": second}, {"data": []}])
    envelope, rows = runtime._load_stock_report_wb_warehouses(client, limit=2, sleeper=lambda _: None)
    assert envelope.ok is False
    assert envelope.error.code == "STOCK_REPORT_PAGINATION_STALLED"
    assert [row["nmId"] for row in rows] == [1, 2]
    assert [req.jsonBody["offset"] for req in client.requests] == [0, 2]


def test_oversized_page_is_not_accepted_then_skipped_by_smaller_offset():
    client = Pages([{"data": [{"nmId": 1}, {"nmId": 2}, {"nmId": 3}]}, {"data": []}])
    envelope, rows = runtime._load_stock_report_wb_warehouses(client, limit=2, sleeper=lambda _: None)
    assert envelope.ok is False
    assert envelope.error.code == "STOCK_REPORT_INVALID_PAGE"
    assert rows == []
    assert len(client.requests) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"])
def test_invalid_limit_fails_before_any_provider_request(limit):
    client = Pages([{"data": []}])
    with pytest.raises(ValueError):
        runtime._load_stock_report_wb_warehouses(client, limit=limit, sleeper=lambda _: None)
    assert client.requests == []


def test_nonconsecutive_page_repetition_is_detected_before_append():
    client=Pages([{"data":[{"nmId":1},{"nmId":2}]},
                  {"data":[{"nmId":3},{"nmId":4}]},
                  {"data":[{"nmId":2},{"nmId":1}]}])
    envelope,rows=runtime._load_stock_report_wb_warehouses(client,limit=2,sleeper=lambda _:None)
    assert envelope.error.code == "STOCK_REPORT_PAGINATION_STALLED"
    assert [row["nmId"] for row in rows] == [1,2,3,4]


def test_page_fingerprint_does_not_ignore_duplicate_multiplicity():
    client=Pages([{"data":[{"nmId":1},{"nmId":1},{"nmId":2}]},
                  {"data":[{"nmId":1},{"nmId":2},{"nmId":2}]}, {"data":[]}])
    envelope,rows=runtime._load_stock_report_wb_warehouses(client,limit=3,sleeper=lambda _:None)
    assert envelope.ok
    assert len(rows) == 6  # Not source-grain deduplication; these page multisets differ.


def test_non_json_full_page_fails_without_appending_or_printing_payload():
    client=Pages([{"data":[{"nmId":1,"quantity":float("nan")}]}])
    envelope,rows=runtime._load_stock_report_wb_warehouses(client,limit=1,sleeper=lambda _:None)
    assert envelope.error.code == "STOCK_REPORT_INVALID_PAGE"
    assert rows == []
    assert "quantity" not in envelope.error.message
