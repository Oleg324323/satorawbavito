from __future__ import annotations

from datetime import datetime, timezone
from time import sleep
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.wb_api.client import WbApiClient, WbApiRequest
from app.wb_api.price_units import kopecks_to_wb_price_rubles
from app.repricer_price_safety import price_baseline, price_write_permission


ApplyState = Literal["blocked", "queued", "processing", "accepted", "requires_attention", "local_applied"]
SourceStatus = Literal["fresh", "partial", "blocked"]
ApplyMode = Literal["dry_run", "local_mock", "wb_api"]
StatusLabel = Literal["processing", "success", "canceled", "partial_errors", "all_failed", "unknown", "local_only"]


class PriceApplyRowRequest(BaseModel):
    nmId: int = Field(gt=0)
    priceKopecks: int = Field(gt=0)
    discountPct: int = Field(ge=0, le=99)
    sizeId: int | None = Field(default=None, gt=0)
    minPriceKopecks: int | None = Field(default=None, gt=0)
    baseline: dict[str, Any] | None = None


class PriceApplyRequest(BaseModel):
    scenario: str = "complete"
    dryRun: bool = False
    pollMaxAttempts: int = Field(default=6, ge=1, le=60)
    pollDelaySeconds: float = Field(default=0.0, ge=0.0, le=5.0)
    skipStatusPoll: bool = False
    rows: list[PriceApplyRowRequest] = Field(min_length=1)


class PriceApplyRowError(BaseModel):
    nmId: int | None = None
    sizeId: int | None = None
    wbStatus: int | None = None
    errorText: str


class PriceApplyResponse(BaseModel):
    sourceStatus: SourceStatus
    applyState: ApplyState
    blockerIds: list[str]
    wbUploadId: int | None = None
    wbStatus: int | None = None
    statusLabel: StatusLabel
    rowErrors: list[PriceApplyRowError] = Field(default_factory=list)
    pollAttempts: int = Field(ge=0)
    observedAt: datetime
    notes: list[str] = Field(default_factory=list)
    applyMode: ApplyMode = "wb_api"
    wbMutationSent: bool = False
    priceConfirmed: bool = False
    confirmedGoods: list[dict[str, Any]] = Field(default_factory=list)


def _status_label(value: int | None) -> Literal["processing", "success", "canceled", "partial_errors", "all_failed", "unknown"]:
    mapping = {
        1: "processing",
        3: "success",
        4: "canceled",
        5: "partial_errors",
        6: "all_failed",
    }
    return mapping.get(value, "unknown")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _build_upload_payload(rows: list[PriceApplyRowRequest]) -> dict[str, Any]:
    payload_rows: list[dict[str, Any]] = []
    for row in rows:
        price_rubles = kopecks_to_wb_price_rubles(row.priceKopecks)
        if price_rubles is None:
            continue
        item: dict[str, Any] = {"nmID": row.nmId, "price": price_rubles}
        if row.sizeId is not None:
            item["sizeID"] = row.sizeId
        else:
            item["discount"] = row.discountPct
        if row.minPriceKopecks is not None and row.sizeId is None:
            min_price_rubles = kopecks_to_wb_price_rubles(row.minPriceKopecks)
            if min_price_rubles is not None:
                item["minPrice"] = min_price_rubles
        payload_rows.append(item)
    return {"data": payload_rows}


def run_price_apply(
    client: WbApiClient,
    payload: PriceApplyRequest,
    real_apply_enabled: bool,
    local_apply_enabled: bool = False,
) -> PriceApplyResponse:
    now = datetime.now(timezone.utc)
    if payload.dryRun:
        return PriceApplyResponse(
            sourceStatus="partial",
            applyState="queued",
            blockerIds=[],
            wbUploadId=None,
            wbStatus=None,
            statusLabel="unknown",
            rowErrors=[],
            pollAttempts=0,
            observedAt=now,
            notes=["Dry-run mode: WB upload is skipped"],
            applyMode="dry_run",
            wbMutationSent=False,
        )

    if not real_apply_enabled:
        if local_apply_enabled:
            return PriceApplyResponse(
                sourceStatus="partial",
                applyState="local_applied",
                blockerIds=[],
                wbUploadId=None,
                wbStatus=None,
                statusLabel="local_only",
                rowErrors=[],
                pollAttempts=0,
                observedAt=now,
                notes=["Local repricer test mode: WB upload is skipped and price is applied only to local runtime state"],
                applyMode="local_mock",
                wbMutationSent=False,
            )
        return PriceApplyResponse(
            sourceStatus="blocked",
            applyState="blocked",
            blockerIds=[],
            wbUploadId=None,
            wbStatus=None,
            statusLabel="unknown",
            rowErrors=[],
            pollAttempts=0,
            observedAt=now,
            notes=["Real price apply is disabled by VELLA_REAL_PRICE_APPLY_ENABLED=false"],
            applyMode="wb_api",
            wbMutationSent=False,
        )

    permission_error = price_write_permission(client)
    if permission_error:
        return PriceApplyResponse(sourceStatus="blocked", applyState="blocked", blockerIds=[permission_error],
            wbStatus=403, statusLabel="unknown", pollAttempts=0, observedAt=now,
            notes=["Configure a WB Prices and Discounts read/write token through the secure profile; no write was sent"])
    if len({(row.nmId, row.sizeId) for row in payload.rows}) != len(payload.rows) or len(payload.rows) > 1000:
        return PriceApplyResponse(sourceStatus="blocked", applyState="blocked", blockerIds=["mixed_or_duplicate_price_targets"],
            statusLabel="unknown", pollAttempts=0, observedAt=now)
    sized = [row.sizeId is not None for row in payload.rows]
    if any(sized) and not all(sized):
        return PriceApplyResponse(sourceStatus="blocked", applyState="blocked", blockerIds=["mixed_price_modes"],
            statusLabel="unknown", pollAttempts=0, observedAt=now)
    for row in payload.rows:
        baseline = row.baseline
        if row.sizeId is not None and (not baseline or baseline.get("nmId") != row.nmId
                or baseline.get("editableSizePrice") is not True
                or row.sizeId not in {size["sizeId"] for size in baseline["sizes"]}
                or row.discountPct != baseline.get("discountPct")):
            return PriceApplyResponse(sourceStatus="blocked", applyState="blocked", blockerIds=["size_price_baseline_required"],
                statusLabel="unknown", pollAttempts=0, observedAt=now)
    upload = client.request(
        WbApiRequest(
            method="POST",
            path="/api/v2/upload/task/size" if all(sized) else "/api/v2/upload/task",
            jsonBody=_build_upload_payload(payload.rows),
        )
    )
    if not upload.ok:
        error_code = upload.error.code if upload.error else "unknown_error"
        blocker_ids = ["wb_rate_limited"] if upload.statusCode == 429 or error_code == "rate_limited" else [error_code]
        # Transport diagnostics may contain credentials/headers; expose codes only.
        notes = [f"Upload request failed: {error_code}"]
        if "wb_rate_limited" in blocker_ids:
            retry_after = upload.rateLimit.retryAfterSeconds if upload.rateLimit is not None else None
            notes = [
                "WB price upload rate limit reached"
                + (f"; retry after {retry_after}s" if retry_after is not None else "")
            ]
        return PriceApplyResponse(
            sourceStatus="blocked",
            applyState="blocked" if "wb_rate_limited" in blocker_ids else "requires_attention",
            blockerIds=blocker_ids,
            wbUploadId=None,
            wbStatus=upload.statusCode,
            statusLabel="unknown",
            rowErrors=[],
            pollAttempts=0,
            observedAt=now,
            notes=notes,
            applyMode="wb_api",
            wbMutationSent=upload.statusCode >= 500,
        )

    upload_data = _extract_data(upload.data)
    upload_id = _as_int(upload_data.get("id")) or _as_int(upload_data.get("uploadID"))
    if upload_id is None:
        return PriceApplyResponse(
            sourceStatus="blocked",
            applyState="requires_attention",
            blockerIds=[],
            wbUploadId=None,
            wbStatus=None,
            statusLabel="unknown",
            rowErrors=[],
            pollAttempts=0,
            observedAt=now,
            notes=["WB upload response does not contain upload id"],
            applyMode="wb_api",
            wbMutationSent=True,
        )

    return poll_price_apply(client, payload, upload_id)


def poll_price_apply(client: WbApiClient, payload: PriceApplyRequest, upload_id: int) -> PriceApplyResponse:
    """Resume an existing upload. This function never sends a price POST."""
    now = datetime.now(timezone.utc)

    if payload.skipStatusPoll:
        return PriceApplyResponse(
            sourceStatus="partial",
            applyState="processing",
            blockerIds=[],
            wbUploadId=upload_id,
            wbStatus=None,
            statusLabel="processing",
            rowErrors=[],
            pollAttempts=0,
            observedAt=datetime.now(timezone.utc),
            notes=["WB accepted the price upload; status check is deferred"],
            applyMode="wb_api",
            wbMutationSent=True,
        )

    attempts = 0
    last_status: int | None = None
    status_rate_limited = False
    for _ in range(payload.pollMaxAttempts):
        attempts += 1
        state = client.request(
            WbApiRequest(
                method="GET",
                path="/api/v2/history/tasks",
                query={"uploadID": upload_id},
            )
        )
        if not state.ok:
            status_rate_limited = state.statusCode == 429 or (state.error is not None and state.error.code == "rate_limited")
            break
        state_data = _extract_data(state.data)
        last_status = _as_int(state_data.get("status"))
        if last_status in {3, 4, 5, 6}:
            break
        if payload.pollDelaySeconds > 0:
            sleep(payload.pollDelaySeconds)

    row_errors: list[PriceApplyRowError] = []
    history_rows = []
    details_complete = False
    if not status_rate_limited and last_status in {3, 4, 5, 6}:
        for offset in range(0, 10000, 1000):
            details = client.request(
                WbApiRequest(
                    method="GET",
                    path="/api/v2/history/goods/task",
                    query={"uploadID": upload_id, "limit": 1000, "offset": offset},
                )
            )
            if not details.ok:
                status_rate_limited = details.statusCode == 429 or (details.error is not None and details.error.code == "rate_limited")
                break
            details_data = _extract_data(details.data)
            page = details_data.get("historyGoods") if isinstance(details_data.get("historyGoods"), list) else []
            history_rows.extend(page)
            for row in page:
                if not isinstance(row, dict):
                    continue
                error_text = None
                errors = row.get("errors")
                if isinstance(errors, list) and errors:
                    error_text = str(errors[0])
                elif isinstance(row.get("errorText"), str) and row.get("errorText"):
                    error_text = row.get("errorText")
                if error_text:
                    row_errors.append(
                        PriceApplyRowError(
                            nmId=_as_int(row.get("nmID")),
                            sizeId=_as_int(row.get("sizeID")),
                            wbStatus=_as_int(row.get("status")),
                            errorText=error_text,
                        )
                    )
            if len(page) < 1000:
                details_complete = True
                break

    if status_rate_limited:
        return PriceApplyResponse(
            sourceStatus="partial",
            applyState="processing",
            blockerIds=["wb_status_rate_limited"],
            wbUploadId=upload_id,
            wbStatus=429,
            statusLabel="processing",
            rowErrors=[],
            pollAttempts=attempts,
            observedAt=datetime.now(timezone.utc),
            notes=[
                "WB accepted the price upload, but status check is temporarily rate-limited; verify upload later",
                f"uploadID {upload_id}",
            ],
            applyMode="wb_api",
            wbMutationSent=True,
        )

    label = _status_label(last_status)
    if label in {"success", "partial_errors"}:
        seen = {(_as_int(row.get("nmID")), _as_int(row.get("sizeID"))) for row in history_rows if isinstance(row, dict)}
        expected = {(row.nmId, row.sizeId) for row in payload.rows}
        # Whole-product uploads can have size-specific detail rows; require at
        # least one detail for each nmID plus complete price/size read-back.
        covered = all((nm, size) in seen if size is not None else any(item[0] == nm for item in seen) for nm, size in expected)
        if not details_complete or not covered:
            return PriceApplyResponse(sourceStatus="partial", applyState="processing", blockerIds=["wb_upload_details_incomplete"],
                wbUploadId=upload_id, wbStatus=last_status, statusLabel="processing", rowErrors=row_errors,
                pollAttempts=attempts, observedAt=datetime.now(timezone.utc), wbMutationSent=True)
        failed_nm = {item.nmId for item in row_errors}
        successful_rows = [row for row in payload.rows if None not in failed_nm and row.nmId not in failed_nm]
        goods, verification_error = _read_back_prices(client, successful_rows) if successful_rows else ([], None)
        if verification_error:
            return PriceApplyResponse(sourceStatus="partial", applyState="processing",
                blockerIds=[verification_error], wbUploadId=upload_id, wbStatus=last_status,
                statusLabel="processing", pollAttempts=attempts, observedAt=datetime.now(timezone.utc),
                notes=["WB upload completed; the intended product price is not confirmed yet"], wbMutationSent=True)
        complete = len(successful_rows) == len(payload.rows) and not row_errors
        return PriceApplyResponse(
            sourceStatus="fresh" if complete else "partial",
            applyState="accepted" if complete else "requires_attention",
            blockerIds=[],
            wbUploadId=upload_id,
            wbStatus=last_status,
            statusLabel=label,
            rowErrors=row_errors,
            pollAttempts=attempts,
            observedAt=datetime.now(timezone.utc),
            notes=["WB upload lifecycle completed successfully"],
            applyMode="wb_api",
            wbMutationSent=True,
            priceConfirmed=complete,
            confirmedGoods=goods,
        )

    apply_state: ApplyState = "processing" if label == "processing" else "requires_attention"
    return PriceApplyResponse(
        sourceStatus="partial",
        applyState=apply_state,
        blockerIds=[],
        wbUploadId=upload_id,
        wbStatus=last_status,
        statusLabel=label,
        rowErrors=row_errors,
        pollAttempts=attempts,
        observedAt=datetime.now(timezone.utc),
        notes=["Upload finished with non-success status or row-level errors"],
        applyMode="wb_api",
        wbMutationSent=True,
    )


def _read_back_prices(client: WbApiClient, rows: list[PriceApplyRowRequest]) -> tuple[list[dict[str, Any]], str | None]:
    """One batched, explicitly selected read; never an account-wide catalog fetch."""
    response = client.request(WbApiRequest(method="POST", path="/api/v2/list/goods/filter",
        jsonBody={"nmList": sorted({row.nmId for row in rows})}))
    if not response.ok:
        return [], "wb_readback_rate_limited" if response.statusCode == 429 else "wb_readback_unavailable"
    data = response.data.get("data", response.data) if isinstance(response.data, dict) else {}
    goods = data.get("listGoods")
    nm_ids = {row.nmId for row in rows}
    if not isinstance(goods, list) or len(goods) != len(nm_ids):
        return [], "wb_readback_incomplete"
    by_nm = {good.get("nmID"): good for good in goods if isinstance(good, dict)}
    if len(by_nm) != len(nm_ids) or set(by_nm) != nm_ids:
        return [], "wb_readback_identity_mismatch"
    for row in rows:
        actual = price_baseline(by_nm[row.nmId])
        if actual is None or actual["discountPct"] != row.discountPct:
            return [], "wb_readback_discount_mismatch"
        before = row.baseline
        if before is not None:
            if actual["articleId"] != before["articleId"] or actual["clubDiscountPct"] != before["clubDiscountPct"]:
                return [], "wb_readback_identity_mismatch"
            if {size["sizeId"] for size in actual["sizes"]} != {size["sizeId"] for size in before["sizes"]}:
                return [], "wb_readback_sizes_mismatch"
        wanted_base = kopecks_to_wb_price_rubles(row.priceKopecks) * 100
        wanted_seller = wanted_base * (100 - row.discountPct) // 100
        affected = {value.sizeId for value in rows if value.nmId == row.nmId}
        if row.sizeId is not None and row.sizeId not in {size["sizeId"] for size in actual["sizes"]}:
            return [], "wb_readback_sizes_mismatch"
        for size in actual["sizes"]:
            if row.sizeId is None or size["sizeId"] == row.sizeId:
                if size["basePriceKopecks"] != wanted_base or size["sellerPriceKopecks"] != wanted_seller:
                    return [], "wb_readback_price_mismatch"
            elif before is not None and size["sizeId"] not in affected and size not in before["sizes"]:
                return [], "wb_readback_other_size_changed"
    return goods, None
