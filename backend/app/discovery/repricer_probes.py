from __future__ import annotations

from typing import Any, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field, model_validator

from app.wb_api.client import RateLimitInfo, WbApiClient, WbApiError, WbApiRequest, WbApiResponseEnvelope
from vella_wb_19_05.models import Confidence, PriceGuardResponse, SourceEvidence, SourceStatus
from vella_wb_19_05.stubs import build_price_guard_stub


ProbeKind = Literal["read_prices", "task_history", "upload_lifecycle"]
ProbeScenario = Literal["complete", "missing_spp", "missing_task_errors", "rate_limited", "billing_error", "server_error"]


class RepricerProbeRequest(BaseModel):
    scenario: ProbeScenario = "complete"
    articleId: str = "FBBT_42"
    uploadId: int = Field(default=146567, ge=1)
    maxPolls: int = Field(default=6, ge=1, le=30)


InternalApplyState = Literal["queued", "processing", "accepted", "requires_attention", "unknown"]


class UploadLifecycleObservation(BaseModel):
    pollIndex: int = Field(ge=1)
    source: Literal["buffer/tasks", "history/tasks"]
    wbStatus: int | None = None
    totalRows: int | None = None
    successRows: int | None = None
    responseStatusCode: int
    apiErrorCode: str | None = None


class UploadLifecycleMap(BaseModel):
    uploadId: int = Field(ge=1)
    observations: List[UploadLifecycleObservation]
    proposedState: InternalApplyState
    rationale: str = Field(min_length=1)
    needsAttention: bool
    missingEvidence: List[str]
    nextActions: List[str]


class ProbeCandidateResult(BaseModel):
    blockerId: str = Field(min_length=1)
    probeId: str = Field(min_length=1)
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1)
    sourceStatus: SourceStatus
    confidence: Confidence
    confirmedFields: List[str]
    missingFields: List[str]
    blockerIds: List[str]
    evidence: List[SourceEvidence]
    responseStatusCode: int
    error: Optional[WbApiError] = None
    rateLimit: Optional[RateLimitInfo] = None

    @model_validator(mode="after")
    def validate_blocked_probe(self) -> ProbeCandidateResult:
        if self.sourceStatus in {"blocked", "unknown"} and not self.blockerIds:
            raise ValueError("Blocked or unknown probe result must carry blocker IDs")
        return self


class RepricerProbeResponse(BaseModel):
    probeKind: ProbeKind
    sourceStatus: SourceStatus
    confidence: Confidence
    blockerIds: List[str]
    results: List[ProbeCandidateResult]
    priceGuardPreview: PriceGuardResponse
    lifecycleMap: UploadLifecycleMap | None = None

    @model_validator(mode="after")
    def validate_probe_response(self) -> RepricerProbeResponse:
        if self.priceGuardPreview.canApply:
            raise ValueError("Discovery probes must not produce applicable price guard")
        if self.sourceStatus in {"blocked", "unknown"} and not self.blockerIds:
            raise ValueError("Blocked or unknown probe response must carry blocker IDs")
        return self


def _first_item(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("listGoods", "goods", "tasks", "bufferGoods", "historyGoods", "quarantineGoods"):
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        return data

    for key in ("listGoods", "goods", "tasks", "bufferGoods", "historyGoods", "quarantineGoods"):
        value = payload.get(key)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return payload


def _has_field_path(item: dict[str, Any], field_path: str) -> bool:
    current: Any = item
    for raw_part in field_path.split("."):
        if "[" in raw_part and raw_part.endswith("]"):
            key, index_part = raw_part[:-1].split("[", 1)
            if not isinstance(current, dict):
                return False
            current = current.get(key)
            if not isinstance(current, list):
                return False
            try:
                index = int(index_part)
            except ValueError:
                return False
            if index < 0 or index >= len(current):
                return False
            current = current[index]
            continue

        if not isinstance(current, dict):
            return False
        if raw_part not in current:
            return False
        current = current[raw_part]
    return current is not None


def _fields_present(payload: object, required_fields: Sequence[str]) -> List[str]:
    item = _first_item(payload)
    if item is None:
        return []
    return [field for field in required_fields if _has_field_path(item, field)]


def _evidence(path: str, fields: List[str]) -> List[SourceEvidence]:
    return [
        SourceEvidence(
            sourceId=f"wb-probe:{path}",
            sourceType="mock",
            sourceName=f"Read-only WB probe {path}",
            lastSyncedAt=None,
            freshnessTtlMinutes=60,
            fieldsUsed=fields,
        )
    ]


def _probe_candidate(
    client: WbApiClient,
    blocker_id: str,
    probe_id: str,
    method: Literal["GET", "POST"],
    path: str,
    required_fields: Sequence[str],
    confidence_when_complete: Confidence,
    query: Optional[dict[str, int | str]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> ProbeCandidateResult:
    envelope = client.request(
        WbApiRequest(
            method=method,
            path=path,
            query=query or {},
            jsonBody=json_body,
        )
    )
    if not envelope.ok:
        error_blocker = blocker_id
        return ProbeCandidateResult(
            blockerId=blocker_id,
            probeId=probe_id,
            method=method,
            path=path,
            sourceStatus="blocked",
            confidence="blocked",
            confirmedFields=[],
            missingFields=list(required_fields),
            blockerIds=[error_blocker],
            evidence=[],
            responseStatusCode=envelope.statusCode,
            error=envelope.error,
            rateLimit=envelope.rateLimit,
        )

    confirmed = _fields_present(envelope.data, required_fields)
    missing = [field for field in required_fields if field not in confirmed]
    if missing:
        return ProbeCandidateResult(
            blockerId=blocker_id,
            probeId=probe_id,
            method=method,
            path=path,
            sourceStatus="blocked",
            confidence="blocked",
            confirmedFields=confirmed,
            missingFields=missing,
            blockerIds=[blocker_id],
            evidence=_evidence(path, confirmed),
            responseStatusCode=envelope.statusCode,
            rateLimit=envelope.rateLimit,
        )

    return ProbeCandidateResult(
        blockerId=blocker_id,
        probeId=probe_id,
        method=method,
        path=path,
        sourceStatus="fresh",
        confidence=confidence_when_complete,
        confirmedFields=confirmed,
        missingFields=[],
        blockerIds=[],
        evidence=_evidence(path, confirmed),
        responseStatusCode=envelope.statusCode,
        rateLimit=envelope.rateLimit,
    )


def _build_guard_preview(article_id: str, results: List[ProbeCandidateResult], unresolved: List[str]) -> PriceGuardResponse:
    guard = build_price_guard_stub(article_id)
    evidence: List[SourceEvidence] = []
    for result in results:
        evidence.extend(result.evidence)

    freeze_state = "source_blocked" if "WB-06" in unresolved else "requires_review"
    if "WB-06" in unresolved:
        blocked_reason = "SPP source is not confirmed"
    elif "WB-22" in unresolved:
        blocked_reason = "Price apply status/errors are not confirmed"
    else:
        blocked_reason = "Discovery mode keeps real WB mutations disabled"
    return guard.model_copy(
        update={
            "canApply": False,
            "blockedReason": blocked_reason,
            "freezeState": freeze_state,
            "sourceEvidence": evidence or guard.sourceEvidence,
            "blockerIds": unresolved,
        }
    )


def _extract_data_dict(envelope: WbApiResponseEnvelope) -> dict[str, Any]:
    if not isinstance(envelope.data, dict):
        return {}
    data = envelope.data.get("data")
    if isinstance(data, dict):
        return data
    return envelope.data


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _obs_from_state_call(poll_index: int, source: Literal["buffer/tasks", "history/tasks"], envelope: WbApiResponseEnvelope) -> UploadLifecycleObservation:
    payload = _extract_data_dict(envelope)
    return UploadLifecycleObservation(
        pollIndex=poll_index,
        source=source,
        wbStatus=_as_int(payload.get("status")),
        totalRows=_as_int(payload.get("overAllGoodsNumber")),
        successRows=_as_int(payload.get("successGoodsNumber")),
        responseStatusCode=envelope.statusCode,
        apiErrorCode=envelope.error.code if envelope.error else None,
    )


def _state_from_observations(observations: List[UploadLifecycleObservation], row_error_detected: bool) -> tuple[InternalApplyState, str, bool]:
    if any(item.apiErrorCode for item in observations):
        return (
            "requires_attention",
            "WB state endpoints returned API errors; upload lifecycle cannot be trusted for auto-apply.",
            True,
        )

    history_statuses = [item.wbStatus for item in observations if item.source == "history/tasks" and item.wbStatus is not None]
    buffer_statuses = [item.wbStatus for item in observations if item.source == "buffer/tasks" and item.wbStatus is not None]
    latest_history = history_statuses[-1] if history_statuses else None
    latest_buffer = buffer_statuses[-1] if buffer_statuses else None

    terminal_done = latest_history == 3
    latest_history_obs = next((item for item in reversed(observations) if item.source == "history/tasks"), None)
    if terminal_done and latest_history_obs and latest_history_obs.totalRows is not None and latest_history_obs.successRows is not None:
        if latest_history_obs.successRows >= latest_history_obs.totalRows and not row_error_detected:
            return ("accepted", "Processed state is terminal and all rows are successful.", False)
        return ("requires_attention", "Processed state is terminal but row counters or row errors indicate partial failure.", True)

    if latest_history is not None:
        return ("processing", "Processed state exists but is not terminal yet.", False)

    if latest_buffer is not None:
        if latest_buffer == 1:
            return ("queued", "Upload is still in buffer queue.", False)
        return ("processing", "Upload is visible in buffer but not finalized in history.", False)

    return ("unknown", "No recognizable WB status codes were observed for upload lifecycle.", True)


def run_read_prices_probe(client: WbApiClient, article_id: str = "FBBT_42") -> RepricerProbeResponse:
    required = [
        "nmID",
        "discount",
        "clubDiscount",
        "sizes[0].price",
        "sizes[0].discountedPrice",
        "sizes[0].clubDiscountedPrice",
    ]
    results = [
        _probe_candidate(
            client,
            "WB-06",
            "prices-filter-by-article",
            "POST",
            "/api/v2/list/goods/filter",
            required,
            "high",
            query={"limit": 1},
            json_body={"nmList": [1060579917]},
        ),
    ]
    wb06_confirmed = results[0].sourceStatus == "fresh"
    unresolved: list[str] = [] if wb06_confirmed else ["WB-06"]
    return RepricerProbeResponse(
        probeKind="read_prices",
        sourceStatus="fresh" if wb06_confirmed else "blocked",
        confidence="high" if wb06_confirmed else "blocked",
        blockerIds=unresolved,
        results=results,
        priceGuardPreview=_build_guard_preview(article_id, results, unresolved),
    )


def run_task_history_probe(client: WbApiClient, article_id: str = "FBBT_42") -> RepricerProbeResponse:
    upload_state_fields = [
        "uploadID",
        "status",
        "uploadDate",
        "activationDate",
        "overAllGoodsNumber",
        "successGoodsNumber",
    ]
    upload_goods_fields = [
        "nmID",
        "sizeID",
        "price",
        "discount",
        "errors",
    ]
    results = [
        _probe_candidate(
            client,
            "WB-22",
            "price-upload-buffer-state",
            "GET",
            "/api/v2/buffer/tasks",
            upload_state_fields,
            "high",
            query={"uploadID": 146567},
        ),
        _probe_candidate(
            client,
            "WB-22",
            "price-upload-buffer-details",
            "GET",
            "/api/v2/buffer/goods/task",
            upload_goods_fields,
            "high",
            query={"uploadID": 146567, "limit": 10, "offset": 0},
        ),
        _probe_candidate(
            client,
            "WB-22",
            "price-upload-history-state",
            "GET",
            "/api/v2/history/tasks",
            upload_state_fields,
            "high",
            query={"uploadID": 146567},
        ),
        _probe_candidate(
            client,
            "WB-22",
            "price-upload-history-details",
            "GET",
            "/api/v2/history/goods/task",
            upload_goods_fields,
            "high",
            query={"uploadID": 146567, "limit": 10, "offset": 0},
        ),
        _probe_candidate(
            client,
            "WB-22",
            "price-quarantine",
            "GET",
            "/api/v2/quarantine/goods",
            ["nmID", "sizeID", "reason", "price", "discount"],
            "medium",
            query={"limit": 10, "offset": 0},
        ),
    ]
    wb22_confirmed = all(result.sourceStatus == "fresh" for result in results)
    unresolved: list[str] = [] if wb22_confirmed else ["WB-22"]
    return RepricerProbeResponse(
        probeKind="task_history",
        sourceStatus="fresh" if wb22_confirmed else "blocked",
        confidence="high" if wb22_confirmed else "blocked",
        blockerIds=unresolved,
        results=results,
        priceGuardPreview=_build_guard_preview(article_id, results, unresolved),
    )


def run_upload_lifecycle_probe(
    client: WbApiClient,
    article_id: str = "FBBT_42",
    upload_id: int = 146567,
    max_polls: int = 6,
) -> RepricerProbeResponse:
    observations: List[UploadLifecycleObservation] = []

    for index in range(1, max_polls + 1):
        buffer_envelope = client.request(
            WbApiRequest(
                method="GET",
                path="/api/v2/buffer/tasks",
                query={"uploadID": upload_id},
            )
        )
        history_envelope = client.request(
            WbApiRequest(
                method="GET",
                path="/api/v2/history/tasks",
                query={"uploadID": upload_id},
            )
        )
        observations.append(_obs_from_state_call(index, "buffer/tasks", buffer_envelope))
        observations.append(_obs_from_state_call(index, "history/tasks", history_envelope))

        history_data = _extract_data_dict(history_envelope)
        history_status = _as_int(history_data.get("status"))
        if history_status == 3:
            break

    details_results = run_task_history_probe(client, article_id).results
    row_error_detected = any(
        result.sourceStatus != "fresh" and "errors" in result.missingFields
        for result in details_results
    )

    proposed_state, rationale, needs_attention = _state_from_observations(observations, row_error_detected)
    missing_evidence = []
    if not any(item.source == "history/tasks" and item.wbStatus == 3 for item in observations):
        missing_evidence.append("terminal_history_status")
    if row_error_detected:
        missing_evidence.append("row_error_shape")
    if not any(result.probeId == "price-quarantine" and result.sourceStatus == "fresh" for result in details_results):
        missing_evidence.append("quarantine_signal")

    unresolved: list[str] = [] if proposed_state == "accepted" and not needs_attention else ["WB-22"]
    source_status: SourceStatus = "fresh" if not unresolved else "blocked"
    confidence: Confidence = "high" if source_status == "fresh" else "blocked"

    lifecycle_map = UploadLifecycleMap(
        uploadId=upload_id,
        observations=observations,
        proposedState=proposed_state,
        rationale=rationale,
        needsAttention=needs_attention,
        missingEvidence=missing_evidence,
        nextActions=[
            "collect_real_buffer_and_history_payloads",
            "freeze_status_code_mapping_in_backend",
            "enable_apply_job_poller_after_audit_gate",
        ],
    )

    return RepricerProbeResponse(
        probeKind="upload_lifecycle",
        sourceStatus=source_status,
        confidence=confidence,
        blockerIds=unresolved,
        results=details_results,
        priceGuardPreview=_build_guard_preview(article_id, details_results, unresolved),
        lifecycleMap=lifecycle_map,
    )
