from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import threading
import time
from typing import Any, Callable

from fastapi import HTTPException

from vella_wb_19_05.models import Confidence, SourceEvidence, SourceStatus, utc_now

from app.config import get_settings
from app.wb_api.client import (
    RateLimitedWbApiClient,
    WbApiError,
    WbApiRequest,
    WbApiResponseEnvelope,
    build_wb_analytics_client,
    build_wb_finance_client,
    build_wb_statistics_client,
)


# WB Statistics applies one seller-wide quota to orders and sales.
# Digest runs in Celery, so it is safe and necessary to serialize these calls.
STATISTICS_REPORT_MIN_INTERVAL_SECONDS = 60.0
STOCK_REPORT_PAGE_DELAY_SECONDS = 20.0
STOCK_REPORT_MIN_INTERVAL_SECONDS = 65.0
STOCK_REPORT_MAX_ATTEMPTS = 4
STOCK_REPORT_PAGE_LIMIT = 250_000
_statistics_report_lock = threading.Lock()
_statistics_report_last_request_at = 0.0
_stock_report_lock = threading.Lock()
_stock_report_last_request_at = 0.0


def _request_statistics_report(
    client: Any,
    request: WbApiRequest,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> WbApiResponseEnvelope:
    from app.wb_api.report_reads import active_report_read
    if active_report_read.get() is not None:
        return client.request(request)
    global _statistics_report_last_request_at
    is_real = get_settings().wb_api_mode == "real"
    for attempt in range(3):
        if is_real:
            with _statistics_report_lock:
                now = clock()
                wait_seconds = max(0.0, STATISTICS_REPORT_MIN_INTERVAL_SECONDS - (now - _statistics_report_last_request_at))
                if wait_seconds:
                    sleeper(wait_seconds)
                _statistics_report_last_request_at = now + wait_seconds
        envelope = client.request(request)
        if envelope.ok or envelope.statusCode != 429 or not is_real or attempt == 2:
            return envelope
        sleeper(STATISTICS_REPORT_MIN_INTERVAL_SECONDS)
    return envelope


@dataclass(frozen=True)
class WbStockRow:
    nm_id: int
    chrt_id: int | None
    warehouse_id: int | None
    warehouse_name: str | None
    region_name: str | None
    quantity: int
    in_way_to_client: int
    in_way_from_client: int
    available_units: int
    was_out_of_stock: bool
    stock_type: str = "wb"
    total_stock_units: int | None = None


@dataclass(frozen=True)
class WbFinancialRow:
    rrd_id: int
    nm_id: int
    srid: str | None
    retail_amount_kopecks: int
    seller_payout_kopecks: int
    commission_percent: float | None
    commission_kopecks: int
    logistics_kopecks: int
    storage_kopecks: int | None = None
    acceptance_kopecks: int | None = None
    penalty_kopecks: int | None = None


@dataclass(frozen=True)
class WbFinanceReconciliation:
    report_id: int | None
    retail_amount_kopecks: int
    for_pay_kopecks: int
    delivery_service_kopecks: int
    paid_storage_kopecks: int
    paid_acceptance_kopecks: int
    penalty_kopecks: int
    report_ids: tuple[int, ...] = ()
    currency: str = "RUB"


@dataclass(frozen=True)
class WbReportsSourcesSnapshot:
    source_status: SourceStatus
    confidence: Confidence
    blocker_ids: list[str]
    source_evidence: list[SourceEvidence]
    orders: list[dict[str, Any]]
    sales: list[dict[str, Any]]
    stocks: list[WbStockRow]
    financial_rows: list[WbFinancialRow]
    finance_reconciliation: WbFinanceReconciliation | None
    revenue_by_nm_kopecks: dict[int, int]
    seller_payout_by_nm_kopecks: dict[int, int]
    commission_cost_by_nm_kopecks: dict[int, int]
    logistics_cost_by_nm_kopecks: dict[int, int]
    penalties_cost_by_nm_kopecks: dict[int, int]
    acceptance_cost_by_nm_kopecks: dict[int, int]
    storage_cost_by_nm_kopecks: dict[int, int]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return int(float(normalized))
        except ValueError:
            return None
    return None


def _rub_to_kopecks(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return int(round(float(normalized) * 100))
        except ValueError:
            return 0
    return 0


def _finance_logistics_kopecks(item: dict[str, Any]) -> int:
    for key in ("delivery_service", "deliveryService", "delivery_rub", "deliveryRub"):
        value = _rub_to_kopecks(item.get(key))
        if value:
            return value
    return 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _payload(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    return data.get("data", data)


def _payload_dict(data: Any) -> dict[str, Any]:
    node = _payload(data)
    if isinstance(node, dict):
        return node
    return {}


def _extract_items(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        for key in ("items", "data", "rows", "report"):
            candidate = node.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return [node]
    return []


def _extract_task_id(data: Any) -> str | None:
    payload = _payload_dict(data)
    for key in ("taskId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in ("taskId", "id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _task_is_done(data: Any) -> bool:
    payload = _payload_dict(data)
    status = payload.get("status")
    if isinstance(status, str):
        return status.lower() in {"done", "success", "completed"}
    return bool(status)


def _warehouse_remains_to_stock_rows(
    warehouse_rows: list[dict[str, Any]],
    *,
    fallback_rows: list[dict[str, Any]],
) -> list[WbStockRow]:
    fallback_index: dict[tuple[int, str], dict[str, Any]] = {}
    for row in fallback_rows:
        nm_id = _as_int(row.get("nmId"))
        warehouse_name = str(row.get("warehouseName") or "").strip()
        if nm_id is None or not warehouse_name:
            continue
        fallback_index[(nm_id, warehouse_name.lower())] = row

    stocks: list[WbStockRow] = []
    for row in warehouse_rows:
        nm_id = _as_int(row.get("nmId"))
        if nm_id is None:
            continue
        warehouse_items = row.get("warehouses")
        if not isinstance(warehouse_items, list):
            continue

        in_way_to_client = 0
        in_way_from_client = 0
        actual_warehouses = []
        for warehouse in warehouse_items:
            if not isinstance(warehouse, dict):
                continue
            warehouse_name = str(warehouse.get("warehouseName") or "").strip()
            quantity = _as_int(warehouse.get("quantity")) or 0
            normalized_name = warehouse_name.lower()
            if normalized_name == "в пути до получателей":
                in_way_to_client += quantity
                continue
            if normalized_name == "в пути возвраты на склад wb":
                in_way_from_client += quantity
                continue
            actual_warehouses.append((warehouse_name, quantity))

        if not actual_warehouses:
            actual_warehouses.append(("all_wb_warehouses", 0))

        for index, (warehouse_name, quantity) in enumerate(actual_warehouses):
            fallback = fallback_index.get((nm_id, warehouse_name.lower()), {})
            row_in_way_to_client = in_way_to_client if index == 0 else 0
            row_in_way_from_client = in_way_from_client if index == 0 else 0
            available = quantity + row_in_way_from_client
            stocks.append(
                WbStockRow(
                    nm_id=nm_id,
                    chrt_id=_as_int(fallback.get("chrtId")),
                    warehouse_id=_as_int(fallback.get("warehouseId")),
                    warehouse_name=warehouse_name,
                    region_name=str(fallback.get("regionName")) if fallback.get("regionName") is not None else None,
                    quantity=quantity,
                    in_way_to_client=row_in_way_to_client,
                    in_way_from_client=row_in_way_from_client,
                    available_units=available,
                    was_out_of_stock=available <= 0,
                    stock_type="wb",
                )
            )
    return stocks


def _request_async_report_rows(
    client: RateLimitedWbApiClient,
    *,
    create_path: str,
    status_path_template: str,
    download_path_template: str,
    query: dict[str, Any] | None = None,
) -> tuple[WbApiResponseEnvelope, list[dict[str, Any]], str | None]:
    create_envelope = client.request(
        WbApiRequest(
            method="GET",
            path=create_path,
            query=query or {},
        )
    )
    task_id = _extract_task_id(create_envelope.data) if create_envelope.ok else None

    if not isinstance(task_id, str) or not task_id:
        return create_envelope, [], None

    status_envelope = client.request(
        WbApiRequest(
            method="GET",
            path=status_path_template.format(task_id=task_id),
        )
    )
    if not status_envelope.ok:
        return status_envelope, [], task_id
    if not _task_is_done(status_envelope.data):
        return status_envelope, [], task_id

    download_envelope = client.request(
        WbApiRequest(
            method="GET",
            path=download_path_template.format(task_id=task_id),
        )
    )
    if not download_envelope.ok:
        return download_envelope, [], task_id

    return download_envelope, _extract_items(_payload(download_envelope.data)), task_id


def _load_stock_report_wb_warehouses(
    analytics_client: RateLimitedWbApiClient,
    *,
    stock_type: str = "wb",
    limit: int | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> tuple[WbApiResponseEnvelope, list[dict[str, Any]]]:
    global _stock_report_last_request_at
    page_limit = STOCK_REPORT_PAGE_LIMIT if limit is None else limit
    if type(page_limit) is not int or page_limit <= 0:
        raise ValueError("stock report page limit must be a positive integer")
    offset = 0
    rows: list[dict[str, Any]] = []
    accepted_full_pages: set[bytes] = set()
    final_envelope: WbApiResponseEnvelope | None = None
    sleep_fn = sleeper or time.sleep

    while True:
        if offset > 0:
            sleep_fn(STOCK_REPORT_PAGE_DELAY_SECONDS)
        request = WbApiRequest(
            method="POST",
            path="/api/analytics/v1/stocks-report/wb-warehouses",
            jsonBody={"limit": page_limit, "offset": offset, "stockType": stock_type},
        )
        envelope: WbApiResponseEnvelope | None = None
        for attempt in range(STOCK_REPORT_MAX_ATTEMPTS):
            if get_settings().wb_api_mode == "real":
                with _stock_report_lock:
                    now = time.monotonic()
                    wait_seconds = max(0.0, STOCK_REPORT_MIN_INTERVAL_SECONDS - (now - _stock_report_last_request_at))
                    if wait_seconds:
                        sleep_fn(wait_seconds)
                    _stock_report_last_request_at = time.monotonic()
            envelope = analytics_client.request(request)
            if envelope.ok or envelope.statusCode != 429 or attempt >= STOCK_REPORT_MAX_ATTEMPTS - 1:
                break
            retry_after = (
                float(envelope.rateLimit.retryAfterSeconds)
                if envelope.rateLimit and envelope.rateLimit.retryAfterSeconds is not None
                else STOCK_REPORT_MIN_INTERVAL_SECONDS
            )
            sleep_fn(max(retry_after, STOCK_REPORT_MIN_INTERVAL_SECONDS) + 1.0)
        if envelope is None:
            break
        final_envelope = envelope
        if not envelope.ok:
            return envelope, rows

        node = _payload(envelope.data)
        if envelope.statusCode == 204 and node is None:
            node = []
        if isinstance(node, dict):
            for key in ("items", "data", "rows", "report"):
                if key in node:
                    node = node[key]
                    break
            else:
                node = [node] if "nmId" in node or "nmID" in node else None
        # Filtering malformed elements would shorten a full page and falsely
        # satisfy the terminal-page condition. Reject the whole page instead.
        if (not isinstance(node, list) or len(node) > page_limit
                or any(not isinstance(item, dict) for item in node)):
            return envelope.model_copy(update={
                "ok": False,
                "error": WbApiError(
                    statusCode=envelope.statusCode,
                    code="STOCK_REPORT_INVALID_PAGE",
                    message="Stock report page has an invalid response shape",
                    retryable=False,
                ),
            }), rows
        page = node
        if len(page) == page_limit:
            # An offset endpoint repeating a full page has not demonstrated
            # progress. Compare row multisets, retaining duplicate multiplicity
            # and ignoring only ordering/key ordering. Never double-count it.
            try:
                row_hashes = sorted(hashlib.sha256(json.dumps(
                    item, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).digest() for item in page)
            except (TypeError, ValueError):
                return envelope.model_copy(update={
                    "ok": False,
                    "error": WbApiError(statusCode=envelope.statusCode,
                        code="STOCK_REPORT_INVALID_PAGE",
                        message="Stock report page is not valid JSON data", retryable=False),
                }), rows
            signature = hashlib.sha256(b"".join(row_hashes)).digest()
            if signature in accepted_full_pages:
                return envelope.model_copy(update={
                    "ok": False,
                    "error": WbApiError(statusCode=envelope.statusCode,
                        code="STOCK_REPORT_PAGINATION_STALLED",
                        message="Stock report repeated an accepted full page", retryable=False),
                }), rows
            accepted_full_pages.add(signature)
        # Preserve raw provider payload for checksums/provenance.
        rows.extend({"stockType": stock_type, **item} for item in page)
        if len(page) < page_limit:
            break
        offset += page_limit

    return final_envelope, rows


def _load_realization_rows(
    finance_client: RateLimitedWbApiClient,
    *,
    date_from: date,
    date_to: date,
    request_finance: Callable[[WbApiRequest], WbApiResponseEnvelope] | None = None,
) -> tuple[WbApiResponseEnvelope, list[WbFinancialRow]]:
    from app.repricer_bff import fetch_finance_report_aggregates
    from app.wb_api.financial_reports import DETAIL_PATH, is_merchandise, money_kopecks

    final_envelope: WbApiResponseEnvelope | None = None
    class Capture:
        inner = getattr(finance_client, "inner", None)

        def request(self, request):
            nonlocal final_envelope
            final_envelope = (request_finance or finance_client.request)(request)
            return final_envelope

    try:
        payload = fetch_finance_report_aggregates(
            "complete", date_from=datetime.combine(date_from, datetime.min.time()),
            date_to=datetime.combine(date_to, datetime.min.time()), _client=Capture())
    except HTTPException as exc:
        if final_envelope is not None and not final_envelope.ok:
            return final_envelope, []
        return WbApiResponseEnvelope(request=WbApiRequest(method="POST", path=DETAIL_PATH),
            statusCode=exc.status_code, ok=False, error=WbApiError(
                statusCode=exc.status_code, code="WB_FINANCE_INVALID_RESPONSE",
                message="Financial report is unavailable or incomplete", retryable=False)), []

    rows = []
    for item in payload["rows"]:
        nm_id = _as_int(item.get("nmId"))
        nm_id = nm_id if nm_id and nm_id > 0 else 0
        if str(item.get("currency") or "").upper() not in {"RUB", "RUR", "643"}:
            return WbApiResponseEnvelope(request=final_envelope.request, statusCode=502, ok=False,
                error=WbApiError(statusCode=502, code="WB_FINANCE_CURRENCY_UNAVAILABLE",
                    message="Financial currency cannot be represented as RUB", retryable=False)), []
        doc = str(item.get("docTypeName") or "").strip().casefold()
        sign = -1 if doc == "возврат" else 1
        trade = is_merchandise(item)
        amounts = [money_kopecks(item.get(key)) for key in ("retailAmount", "forPay", "ppvzSalesCommission", "deliveryService")]
        if any(value is None for value in amounts):
            return WbApiResponseEnvelope(request=final_envelope.request, statusCode=502, ok=False,
                error=WbApiError(statusCode=502, code="WB_FINANCE_FIELDS_INCOMPLETE",
                    message="Required financial amounts are unavailable", retryable=False)), []
        revenue, payable, commission, logistics = amounts
        rows.append(WbFinancialRow(
            rrd_id=int(item["rrdId"]), nm_id=nm_id, srid=item.get("srid"),
            retail_amount_kopecks=sign * revenue if trade else 0,
            seller_payout_kopecks=sign * payable,
            commission_percent=_as_float(item.get("commissionPercent")),
            commission_kopecks=sign * (revenue - payable - (money_kopecks(item.get("acquiringFee")) or 0)) if trade else 0,
            logistics_kopecks=logistics,
            storage_kopecks=money_kopecks(item.get("paidStorage")),
            acceptance_kopecks=money_kopecks(item.get("paidAcceptance")),
            penalty_kopecks=money_kopecks(item.get("penalty"))))
    return final_envelope, rows


def _append_result_failure(
    envelope: WbApiResponseEnvelope,
    blocker_ids: list[str],
) -> None:
    if envelope.ok:
        return
    if "WB-03" not in blocker_ids:
        blocker_ids.append("WB-03")


def _evidence(source_id: str, source_name: str, fields: list[str]) -> SourceEvidence:
    return SourceEvidence(
        sourceId=source_id,
        sourceType="wb_api",
        sourceName=source_name,
        lastSyncedAt=utc_now(),
        freshnessTtlMinutes=60,
        fieldsUsed=fields,
    )


def load_operational_expense_drilldowns(analytics_client, date_from, date_to):
    """Optional Analytics drill-down, never a settlement P&L source."""
    blocker_ids, source_evidence = [], []
    penalties_envelope = analytics_client.request(
        WbApiRequest(
            method="GET",
            path="/api/analytics/v1/measurement-penalties",
            query={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
        )
    )
    _append_result_failure(penalties_envelope, blocker_ids)
    penalties_cost_by_nm_kopecks: dict[int, int] = {}
    for row in _extract_items(_payload(penalties_envelope.data)):
        nm_id = _as_int(row.get("nmId") or row.get("nmID"))
        if nm_id is None:
            continue
        penalties_cost_by_nm_kopecks[nm_id] = penalties_cost_by_nm_kopecks.get(nm_id, 0) + _rub_to_kopecks(
            row.get("penaltyAmount")
        ) + _rub_to_kopecks(row.get("retentionAmount"))
    if penalties_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-analytics-measurement-penalties",
                "WB Analytics /api/analytics/v1/measurement-penalties",
                ["nmId", "penaltyAmount", "retentionAmount"],
            )
        )

    acceptance_cost_by_nm_kopecks: dict[int, int] = {}
    acceptance_task = analytics_client.request(
        WbApiRequest(
            method="GET",
            path="/api/v1/acceptance_report",
            query={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
        )
    )
    _append_result_failure(acceptance_task, blocker_ids)
    acceptance_task_id = None
    if acceptance_task.ok:
        acceptance_task_id = _extract_task_id(acceptance_task.data)
    if isinstance(acceptance_task_id, str) and acceptance_task_id:
        acceptance_status = analytics_client.request(
            WbApiRequest(
                method="GET",
                path=f"/api/v1/acceptance_report/tasks/{acceptance_task_id}/status",
            )
        )
        _append_result_failure(acceptance_status, blocker_ids)
        acceptance_download = analytics_client.request(
            WbApiRequest(
                method="GET",
                path=f"/api/v1/acceptance_report/tasks/{acceptance_task_id}/download",
            )
        )
        _append_result_failure(acceptance_download, blocker_ids)
        for row in _extract_items(_payload(acceptance_download.data)):
            nm_id = _as_int(row.get("nmID") or row.get("nmId"))
            if nm_id is None:
                continue
            acceptance_cost_by_nm_kopecks[nm_id] = acceptance_cost_by_nm_kopecks.get(nm_id, 0) + _rub_to_kopecks(row.get("total"))
    if acceptance_task.ok:
        source_evidence.append(
            _evidence(
                "wb-analytics-acceptance-report",
                "WB Analytics /api/v1/acceptance_report",
                ["nmID", "total", "taskId", "status"],
            )
        )

    storage_cost_by_nm_kopecks: dict[int, int] = {}
    paid_storage_task = analytics_client.request(
        WbApiRequest(
            method="GET",
            path="/api/v1/paid_storage",
            query={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()},
        )
    )
    _append_result_failure(paid_storage_task, blocker_ids)
    storage_task_id = None
    if paid_storage_task.ok:
        storage_task_id = _extract_task_id(paid_storage_task.data)
    if isinstance(storage_task_id, str) and storage_task_id:
        storage_status = analytics_client.request(
            WbApiRequest(
                method="GET",
                path=f"/api/v1/paid_storage/tasks/{storage_task_id}/status",
            )
        )
        _append_result_failure(storage_status, blocker_ids)
        storage_download = analytics_client.request(
            WbApiRequest(
                method="GET",
                path=f"/api/v1/paid_storage/tasks/{storage_task_id}/download",
            )
        )
        _append_result_failure(storage_download, blocker_ids)
        for row in _extract_items(_payload(storage_download.data)):
            nm_id = _as_int(row.get("nmId") or row.get("nmID"))
            if nm_id is None:
                continue
            storage_cost_by_nm_kopecks[nm_id] = storage_cost_by_nm_kopecks.get(nm_id, 0) + _rub_to_kopecks(
                row.get("warehousePrice")
            )
    if paid_storage_task.ok:
        source_evidence.append(
            _evidence(
                "wb-analytics-paid-storage-report",
                "WB Analytics /api/v1/paid_storage",
                ["nmId", "warehousePrice", "taskId", "status"],
            )
        )
    return dict(penalties=penalties_cost_by_nm_kopecks,
                acceptance=acceptance_cost_by_nm_kopecks,
                storage=storage_cost_by_nm_kopecks,
                blockerIds=blocker_ids, sourceEvidence=source_evidence)


def build_wb_reports_sources_snapshot(
    *,
    date_from: date,
    date_to: date,
    scenario: str = "complete",
    wb_token: str | None = None,
    progress_callback: Any | None = None,
) -> WbReportsSourcesSnapshot:
    def progress(stage: str, label: str, percent: int) -> None:
        if progress_callback:
            progress_callback(stage, label, percent)
    if get_settings().wb_api_mode == "real" and not wb_token:
        return WbReportsSourcesSnapshot(
            source_status="blocked",
            confidence="blocked",
            blocker_ids=["WB_TOKEN_REQUIRED"],
            source_evidence=[
                _evidence(
                    "wb-reports-runtime-token",
                    "WB reports runtime requires the current cabinet WB token",
                    ["orders", "sales", "stocks", "finance_reports"],
                )
            ],
            orders=[],
            sales=[],
            stocks=[],
            financial_rows=[],
            finance_reconciliation=None,
            revenue_by_nm_kopecks={},
            seller_payout_by_nm_kopecks={},
            commission_cost_by_nm_kopecks={},
            logistics_cost_by_nm_kopecks={},
            penalties_cost_by_nm_kopecks={},
            acceptance_cost_by_nm_kopecks={},
            storage_cost_by_nm_kopecks={},
        )

    statistics_client = RateLimitedWbApiClient(inner=build_wb_statistics_client(scenario=scenario, token_override=wb_token))
    finance_client = RateLimitedWbApiClient(inner=build_wb_finance_client(scenario=scenario, token_override=wb_token))
    analytics_client = RateLimitedWbApiClient(inner=build_wb_analytics_client(scenario=scenario, token_override=wb_token))

    blocker_ids: list[str] = []
    source_evidence: list[SourceEvidence] = []

    progress("orders", "Загружаем заказы и продажи WB", 15)
    orders_envelope = _request_statistics_report(
        statistics_client,
        WbApiRequest(
            method="GET",
            path="/api/v1/supplier/orders",
            query={"dateFrom": f"{date_from.isoformat()}T00:00:00"},
        )
    )
    _append_result_failure(orders_envelope, blocker_ids)
    orders = _extract_items(_payload(orders_envelope.data))
    if orders_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-statistics-orders",
                "WB Statistics /api/v1/supplier/orders",
                ["nmId", "warehouseName", "regionName", "finishedPrice", "spp", "srid", "lastChangeDate"],
            )
        )

    progress("sales", "Загружаем продажи WB: соблюдаем лимит WB", 22)
    sales_envelope = _request_statistics_report(
        statistics_client,
        WbApiRequest(
            method="GET",
            path="/api/v1/supplier/sales",
            query={"dateFrom": f"{date_from.isoformat()}T00:00:00"},
        )
    )
    _append_result_failure(sales_envelope, blocker_ids)
    sales = _extract_items(_payload(sales_envelope.data))
    if sales_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-statistics-sales",
                "WB Statistics /api/v1/supplier/sales",
                ["nmId", "finishedPrice", "forPay", "srid", "saleID", "lastChangeDate", "isReturn"],
            )
        )

    progress("finance", "Сверяем финансовые отчёты WB", 30)
    realization_envelope, financial_rows = _load_realization_rows(
        finance_client,
        date_from=date_from,
        date_to=date_to,
    )
    _append_result_failure(realization_envelope, blocker_ids)
    revenue_by_nm_kopecks: dict[int, int] = {}
    seller_payout_by_nm_kopecks: dict[int, int] = {}
    commission_cost_by_nm_kopecks: dict[int, int] = {}
    logistics_cost_by_nm_kopecks: dict[int, int] = {}
    for row in financial_rows:
        revenue_by_nm_kopecks[row.nm_id] = revenue_by_nm_kopecks.get(row.nm_id, 0) + row.retail_amount_kopecks
        seller_payout_by_nm_kopecks[row.nm_id] = seller_payout_by_nm_kopecks.get(row.nm_id, 0) + row.seller_payout_kopecks
        commission_cost_by_nm_kopecks[row.nm_id] = commission_cost_by_nm_kopecks.get(row.nm_id, 0) + row.commission_kopecks
        logistics_cost_by_nm_kopecks[row.nm_id] = logistics_cost_by_nm_kopecks.get(row.nm_id, 0) + row.logistics_kopecks
    if realization_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-finance-realization-details",
                "WB Finance /api/finance/v1/sales-reports/detailed",
                ["rrdId", "nmId", "srid", "rrDate", "retailAmount", "commissionPercent", "ppvzSalesCommission", "forPay", "deliveryService", "paidStorage", "paidAcceptance", "penalty"],
            )
        )

    finance_reconciliation: WbFinanceReconciliation | None = None
    from app.wb_api.financial_reports import fetch_report_list, summarize_reports
    try:
        finance_report_items = fetch_report_list(finance_client, date_from, date_to, period="daily")
        groups = summarize_reports(finance_report_items)
        finance_row = groups.get("RUB")
        keys = ("retailAmountSum", "forPaySum", "deliveryServiceSum", "paidStorageSum", "paidAcceptanceSum", "penaltySum")
        if finance_row and len(groups) == 1 and all(finance_row["totals"][key] is not None for key in keys):
            ids = tuple(finance_row["reportIds"])
            totals = finance_row["totals"]
            finance_reconciliation = WbFinanceReconciliation(
                report_id=ids[0] if len(ids) == 1 else None, report_ids=ids,
                retail_amount_kopecks=totals["retailAmountSum"], for_pay_kopecks=totals["forPaySum"],
                delivery_service_kopecks=totals["deliveryServiceSum"], paid_storage_kopecks=totals["paidStorageSum"],
                paid_acceptance_kopecks=totals["paidAcceptanceSum"], penalty_kopecks=totals["penaltySum"])
        elif groups:
            blocker_ids.append("WB_FINANCE_RECONCILIATION_INCOMPLETE")
        source_evidence.append(
            _evidence(
                "wb-finance-sales-reports-list",
                "WB Finance /api/finance/v1/sales-reports/list",
                ["reportId", "retailAmountSum", "forPaySum", "deliveryServiceSum", "paidStorageSum", "paidAcceptanceSum", "penaltySum"],
            )
        )
    except HTTPException as exc:
        blocker_ids.append(f"WB_FINANCE_REPORT_LIST_{exc.status_code}")

    progress("stocks", "Загружаем остатки и склады WB", 55)
    fallback_stocks_envelope, fallback_stock_items = _load_stock_report_wb_warehouses(analytics_client, stock_type="wb")
    _append_result_failure(fallback_stocks_envelope, blocker_ids)

    warehouse_remains_envelope, warehouse_remains_items, _warehouse_task_id = _request_async_report_rows(
        analytics_client,
        create_path="/api/v1/warehouse_remains",
        status_path_template="/api/v1/warehouse_remains/tasks/{task_id}/status",
        download_path_template="/api/v1/warehouse_remains/tasks/{task_id}/download",
    )
    _append_result_failure(warehouse_remains_envelope, blocker_ids)
    stocks = _warehouse_remains_to_stock_rows(
        warehouse_remains_items,
        fallback_rows=fallback_stock_items,
    )
    if not stocks and fallback_stock_items:
        for item in fallback_stock_items:
            nm_id = _as_int(item.get("nmId"))
            if nm_id is None:
                continue
            quantity = _as_int(item.get("quantity")) or 0
            in_way_from_client = _as_int(item.get("inWayFromClient")) or 0
            in_way_to_client = _as_int(item.get("inWayToClient")) or 0
            available = quantity + in_way_from_client
            stocks.append(
                WbStockRow(
                    nm_id=nm_id,
                    chrt_id=_as_int(item.get("chrtId")),
                    warehouse_id=_as_int(item.get("warehouseId")),
                    warehouse_name=str(item.get("warehouseName")) if item.get("warehouseName") is not None else None,
                    region_name=str(item.get("regionName")) if item.get("regionName") is not None else None,
                    quantity=quantity,
                    in_way_to_client=in_way_to_client,
                    in_way_from_client=in_way_from_client,
                    available_units=available,
                    was_out_of_stock=available <= 0,
                    stock_type=str(item.get("stockType") or "wb"),
                )
            )
    if warehouse_remains_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-analytics-warehouse-remains",
                "WB Analytics /api/v1/warehouse_remains",
                ["nmId", "warehouses[].warehouseName", "warehouses[].quantity"],
            )
        )
    if fallback_stocks_envelope.ok:
        source_evidence.append(
            _evidence(
                "wb-analytics-stocks-report",
                "WB Analytics /api/analytics/v1/stocks-report/wb-warehouses",
                ["nmId", "chrtId", "warehouseId", "warehouseName", "regionName", "quantity", "inWayToClient", "inWayFromClient"],
            )
        )
    progress("costs", "Читаем расходы из финансовых операций WB", 70)
    def financial_costs(field):
        totals, unknown = {}, set()
        for row in financial_rows:
            value = getattr(row, field)
            if value is None:
                unknown.add(row.nm_id)
            else:
                totals[row.nm_id] = totals.get(row.nm_id, 0) + value
        if unknown:
            blocker_ids.append("WB_FINANCE_EXPENSE_FIELDS_INCOMPLETE")
        return {nm: amount for nm, amount in totals.items() if nm not in unknown}
    penalties_cost_by_nm_kopecks = financial_costs("penalty_kopecks")
    acceptance_cost_by_nm_kopecks = financial_costs("acceptance_kopecks")
    storage_cost_by_nm_kopecks = financial_costs("storage_kopecks")

    if not source_evidence and blocker_ids:
        source_status = "blocked"
        confidence = "blocked"
    elif blocker_ids:
        source_status: SourceStatus = "partial"
        confidence: Confidence = "medium"
    else:
        source_status = "fresh"
        confidence = "high"

    return WbReportsSourcesSnapshot(
        source_status=source_status,
        confidence=confidence,
        blocker_ids=sorted(set(blocker_ids)),
        source_evidence=source_evidence,
        orders=orders,
        sales=sales,
        stocks=stocks,
        financial_rows=financial_rows,
        finance_reconciliation=finance_reconciliation,
        revenue_by_nm_kopecks=revenue_by_nm_kopecks,
        seller_payout_by_nm_kopecks=seller_payout_by_nm_kopecks,
        commission_cost_by_nm_kopecks=commission_cost_by_nm_kopecks,
        logistics_cost_by_nm_kopecks=logistics_cost_by_nm_kopecks,
        penalties_cost_by_nm_kopecks=penalties_cost_by_nm_kopecks,
        acceptance_cost_by_nm_kopecks=acceptance_cost_by_nm_kopecks,
        storage_cost_by_nm_kopecks=storage_cost_by_nm_kopecks,
    )
