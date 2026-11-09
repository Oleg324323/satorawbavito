from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.cash_flow.store import claim_jobs as claim_cash_flow_jobs
from app.cash_flow.store import get_or_create_job as persist_cash_flow_job
from app.cash_flow.store import update_job_result
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission


router = APIRouter(tags=["1c-cash-flow"])

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "var" / "1c_cash_flow_logs.json"
DEFAULT_JOBS_PATH = ROOT / "var" / "1c_cash_flow_jobs.json"
JOBS_PATH = DEFAULT_JOBS_PATH
MAX_LOG_ITEMS = 500
MAX_1C_JOBS_PER_POLL = 3
PROCESSING_RETRY_AFTER_SECONDS = 10 * 60
NON_OPERATIONAL_EXPENSE_ARTICLE_MARKERS = (
    "возврат кредита",
    "дивиденды",
    "лизинг",
    "перемещение между кассами",
    "перемещение между счетами",
    "оплата поставщикам_гур",
    "оплата поставщикам_дур",
    "оплата поставщикам_козлов",
    "оплата поставщикам_печ",
    "оплата поставщикам_султан",
    "оплата поставщикам_ух",
)


class OneCCashFlowAccepted(BaseModel):
    status: str = "ok"
    id: str
    receivedAt: str


class OneCCashFlowLogItem(BaseModel):
    id: str
    receivedAt: str
    source: str = "1c"
    payload: dict[str, Any] = Field(default_factory=dict)
    payloadKeys: list[str] = Field(default_factory=list)
    clientHost: str | None = None
    userAgent: str | None = None


class OneCCashFlowLogsResponse(BaseModel):
    total: int = Field(ge=0)
    items: list[OneCCashFlowLogItem]


class OneCJobItem(BaseModel):
    job_id: str
    type: str = "cash_flow"
    period_from: str
    period_to: str


class OneCJobsResponse(BaseModel):
    jobs: list[OneCJobItem]


class OneCNextJobResponse(BaseModel):
    has_job: bool
    job_id: str | None = None
    type: str | None = None
    period_from: str | None = None
    period_to: str | None = None


def _read_logs() -> list[dict[str, Any]]:
    try:
        raw = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _write_logs(items: list[dict[str, Any]]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(
        json.dumps(items[:MAX_LOG_ITEMS], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_jobs_state() -> dict[str, Any]:
    try:
        raw = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"jobs": []}
    jobs = raw.get("jobs") if isinstance(raw, dict) else None
    return {"jobs": jobs if isinstance(jobs, list) else []}


def _write_jobs_state(state: dict[str, Any]) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _processing_job_is_stale(job: dict[str, Any]) -> bool:
    if job.get("status") != "processing":
        return False
    started = _parse_iso(job.get("startedAt"))
    if started is None:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() >= PROCESSING_RETRY_AFTER_SECONDS


def _job_public_id(job: dict[str, Any]) -> str:
    return str(job.get("id") or "")


def _same_period(job: dict[str, Any], organization_id: int, period_from: date, period_to: date) -> bool:
    return (
        int(job.get("organizationId") or 0) == organization_id
        and str(job.get("periodFrom") or "") == period_from.isoformat()
        and str(job.get("periodTo") or "") == period_to.isoformat()
    )


def _amount_to_kopecks(row: dict[str, Any]) -> int:
    for key in ("amountKopecks", "amount_kopecks", "amountKop", "amount_kop"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return int(round(value))
    for key in ("amountRub", "amount_rub", "amount", "sum", "value"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            return int(round(value * 100))
        if isinstance(value, str):
            normalized = value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            try:
                return int(round(float(normalized) * 100))
            except ValueError:
                continue
    return 0


def _cash_flow_source_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, forced_type in (("rows", None), ("items", None), ("income", "income"), ("expense", "expense")):
        source = payload.get(key)
        if not isinstance(source, list):
            continue
        for raw in source:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if forced_type and not item.get("type"):
                item["type"] = forced_type
            rows.append(item)
    return rows


def _is_operational_expense(article: str, flow_type: str) -> bool:
    if flow_type != "expense":
        return False
    normalized = article.replace("\u00a0", " ").strip().casefold()
    if not normalized:
        return False
    return not any(marker in normalized for marker in NON_OPERATIONAL_EXPENSE_ARTICLE_MARKERS)


def _normalize_cash_flow_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(_cash_flow_source_rows(payload)):
        flow_type = str(raw.get("type") or raw.get("flowType") or raw.get("kind") or "").lower().strip()
        if flow_type not in {"income", "expense"}:
            amount = _amount_to_kopecks(raw)
            flow_type = "income" if amount < 0 else "expense"
        amount_kopecks = abs(_amount_to_kopecks(raw))
        article = str(raw.get("article") or raw.get("name") or raw.get("category") or raw.get("description") or f"Строка {index + 1}").strip()
        operational_expense = _is_operational_expense(article, flow_type)
        result.append(
            {
                "type": flow_type,
                "article": article,
                "amountKopecks": amount_kopecks,
                "operationalExpense": operational_expense,
                "raw": raw,
            }
        )
    return result


def _balance_to_kopecks(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return int(round(value * 100))
        if isinstance(value, str):
            normalized = value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            try:
                return int(round(float(normalized) * 100))
            except ValueError:
                continue
    return None


def _cash_flow_balances_payload(payload: dict[str, Any]) -> dict[str, Any]:
    balances = payload.get("balances")
    if isinstance(balances, dict):
        return balances
    if isinstance(balances, list):
        for item in balances:
            if isinstance(item, dict):
                return item
    return payload


def _cash_flow_data_from_job(job: dict[str, Any]) -> dict[str, Any]:
    rows = job.get("rows") if isinstance(job.get("rows"), list) else []
    balances = job.get("balances") if isinstance(job.get("balances"), dict) else {}
    total_income = sum(int(row.get("amountKopecks") or 0) for row in rows if row.get("type") == "income")
    total_expense = sum(int(row.get("amountKopecks") or 0) for row in rows if row.get("type") == "expense")
    operational_expense = sum(int(row.get("amountKopecks") or 0) for row in rows if row.get("operationalExpense") is True)
    return {
        "job_id": _job_public_id(job),
        "status": job.get("status"),
        "period_from": job.get("periodFrom"),
        "period_to": job.get("periodTo"),
        "completed_at": job.get("completedAt"),
        "rows": rows,
        "balances": balances,
        "totals": {
            "incomeKopecks": total_income,
            "expenseKopecks": total_expense,
            "operationalExpenseKopecks": operational_expense,
            "netKopecks": total_income - total_expense,
        },
    }


def get_or_create_cash_flow_job(
    *,
    organization_id: int,
    period_from: date,
    period_to: date,
    requested_by: str | None,
) -> dict[str, Any]:
    def fallback_get_or_create() -> dict[str, Any]:
        state = _read_jobs_state()
        jobs = state["jobs"]
        for current in jobs:
            if _same_period(current, organization_id, period_from, period_to) and current.get("status") != "failed":
                return current
        job_id = f"cf_{uuid4().hex[:12]}"
        current = {
            "id": job_id,
            "organizationId": organization_id,
            "type": "cash_flow",
            "periodFrom": period_from.isoformat(),
            "periodTo": period_to.isoformat(),
            "status": "pending",
            "attempts": 0,
            "createdAt": _utc_now(),
            "startedAt": None,
            "completedAt": None,
            "error": None,
            "requestedBy": requested_by,
            "rows": [],
            "balances": {},
        }
        jobs.insert(0, current)
        _write_jobs_state(state)
        return current

    job = persist_cash_flow_job(
        organization_id=organization_id,
        period_from=period_from,
        period_to=period_to,
        requested_by=requested_by,
        fallback_only=JOBS_PATH != DEFAULT_JOBS_PATH,
        fallback_get_or_create=fallback_get_or_create,
    )
    if job.get("status") == "completed":
        return {"status": "ready", "data": _cash_flow_data_from_job(job)}
    return {"status": job.get("status") or "pending", "job_id": _job_public_id(job)}


def get_cash_flow_for_period(
    *,
    organization_id: int,
    period_from: date,
    period_to: date,
    requested_by: str | None = None,
) -> dict[str, Any]:
    if not get_settings().one_c_enabled:
        return {"status": "disabled"}
    return get_or_create_cash_flow_job(
        organization_id=organization_id,
        period_from=period_from,
        period_to=period_to,
        requested_by=requested_by,
    )


def _claim_jobs(limit: int) -> list[dict[str, Any]]:
    safe_limit = max(1, min(MAX_1C_JOBS_PER_POLL, int(limit or MAX_1C_JOBS_PER_POLL)))

    def fallback_claim() -> list[dict[str, Any]]:
        state = _read_jobs_state()
        selected: list[dict[str, Any]] = []
        now = _utc_now()
        for current in state["jobs"]:
            if current.get("status") != "pending" and not _processing_job_is_stale(current):
                continue
            current["status"] = "processing"
            current["startedAt"] = now
            current["attempts"] = int(current.get("attempts") or 0) + 1
            selected.append(current)
            if len(selected) >= safe_limit:
                break
        if selected:
            _write_jobs_state(state)
        return selected

    return claim_cash_flow_jobs(
        limit=safe_limit,
        retry_after_seconds=PROCESSING_RETRY_AFTER_SECONDS,
        fallback_only=JOBS_PATH != DEFAULT_JOBS_PATH,
        fallback_claim=fallback_claim,
    )


def _require_1c_token(request: Request) -> None:
    expected = f"Bearer {get_settings().one_c_cash_flow_token}"
    authorization = request.headers.get("authorization") or ""
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/api/1c/cash-flow", response_model=OneCCashFlowAccepted)
async def receive_1c_cash_flow(request: Request) -> OneCCashFlowAccepted:
    _require_1c_token(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")

    received_at = datetime.now(timezone.utc).isoformat()
    item = {
        "id": uuid4().hex,
        "receivedAt": received_at,
        "source": "1c",
        "payload": payload,
        "payloadKeys": sorted(str(key) for key in payload.keys()),
        "clientHost": request.client.host if request.client else None,
        "userAgent": request.headers.get("user-agent"),
    }
    _write_logs([item, *_read_logs()])
    return OneCCashFlowAccepted(id=item["id"], receivedAt=received_at)


@router.get("/api/1c/cash-flow/logs", response_model=OneCCashFlowLogsResponse)
def get_1c_cash_flow_logs(request: Request, limit: int = 50) -> OneCCashFlowLogsResponse:
    actor = actor_from_request(request)
    if not has_permission(actor, "finance:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:finance:read")
    safe_limit = max(1, min(200, int(limit or 50)))
    logs = _read_logs()
    return OneCCashFlowLogsResponse(total=len(logs), items=logs[:safe_limit])


@router.get("/api/1c/jobs", response_model=OneCJobsResponse)
def get_1c_jobs(request: Request, limit: int = MAX_1C_JOBS_PER_POLL) -> OneCJobsResponse:
    _require_1c_token(request)
    selected = _claim_jobs(limit)
    return OneCJobsResponse(
        jobs=[
            OneCJobItem(
                job_id=_job_public_id(job),
                period_from=str(job.get("periodFrom")),
                period_to=str(job.get("periodTo")),
            )
            for job in selected
        ]
    )


@router.get("/api/1c/jobs/next", response_model=OneCNextJobResponse)
def get_1c_next_job(request: Request) -> OneCNextJobResponse:
    _require_1c_token(request)
    selected = _claim_jobs(1)
    if not selected:
        return OneCNextJobResponse(has_job=False)
    job = selected[0]
    return OneCNextJobResponse(
        has_job=True,
        job_id=_job_public_id(job),
        type=str(job.get("type") or "cash_flow"),
        period_from=str(job.get("periodFrom")),
        period_to=str(job.get("periodTo")),
    )


@router.post("/api/1c/jobs/{job_id}/result", response_model=dict[str, Any])
async def post_1c_job_result(request: Request, job_id: str) -> dict[str, Any]:
    _require_1c_token(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON body must be an object")
    failed = payload.get("status") == "failed" or bool(payload.get("error"))
    error = str(payload.get("error") or "1C job failed") if failed else None
    rows = [] if failed else _normalize_cash_flow_rows(payload)
    balances_payload = _cash_flow_balances_payload(payload)
    raw_balances = {
        "openingBalanceKopecks": _balance_to_kopecks(balances_payload, "openingBalance", "opening_balance", "openingBalanceRub"),
        "turnoverKopecks": _balance_to_kopecks(balances_payload, "turnover", "turnoverRub"),
        "closingBalanceKopecks": _balance_to_kopecks(balances_payload, "closingBalance", "closing_balance", "closingBalanceRub"),
    }
    balances = {} if failed else {key: value for key, value in raw_balances.items() if value is not None}

    def fallback_update() -> dict[str, Any] | None:
        state = _read_jobs_state()
        current = next((item for item in state["jobs"] if item.get("id") == job_id), None)
        if current is None:
            return None
        current["status"] = "failed" if failed else "completed"
        current["completedAt"] = _utc_now()
        current["error"] = error
        current["rows"] = rows
        current["balances"] = balances
        current["rawResult"] = payload
        _write_jobs_state(state)
        return current

    job = update_job_result(
        job_id=job_id,
        status="failed" if failed else "completed",
        error=error,
        rows=rows,
        balances=balances,
        raw_result=payload,
        fallback_only=JOBS_PATH != DEFAULT_JOBS_PATH,
        fallback_update=fallback_update,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    if failed:
        return {"status": "failed", "job_id": job_id, "error": job["error"], "completedAt": job["completedAt"]}

    log_item = {
        "id": uuid4().hex,
        "receivedAt": job["completedAt"],
        "source": "1c_job_result",
        "payload": payload,
        "payloadKeys": sorted(str(key) for key in payload.keys()),
        "clientHost": request.client.host if request.client else None,
        "userAgent": request.headers.get("user-agent"),
    }
    _write_logs([log_item, *_read_logs()])
    return {"status": "ok", "job_id": job_id, "rows": len(rows), "completedAt": job["completedAt"]}
