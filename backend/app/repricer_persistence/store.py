from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
from threading import RLock
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import logging
import os
import uuid

from sqlalchemy import BigInteger, DateTime, case, cast, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import get_session_factory
from app.repricer_persistence.orm import (
    WbRepricerChangelogRow,
    WbRepricerExecutionRunRow,
    WbRepricerAlgorithmSettingsRow,
    WbRepricerRuntimeStateRow,
)

logger = logging.getLogger(__name__)

_MEMORY_RUNTIME_STATE: dict[int, dict[str, Any]] = {}
_MEMORY_ALGORITHM_SETTINGS: dict[int, dict[str, Any]] = {}
_HYDRATED_BASELINE: ContextVar[tuple[int, dict, dict] | None] = ContextVar("repricer_hydrated_baseline", default=None)
_BFF_STATE_LOCK = RLock()


def _serialized_bff_state(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _BFF_STATE_LOCK:
            return function(*args, **kwargs)
    return locked


def _merge_changed_values(current: dict, baseline: dict, proposed: dict) -> dict:
    """Apply this request's delta without replacing a concurrent product edit."""
    merged = deepcopy(current)
    for key in baseline.keys() | proposed.keys():
        if key in baseline and key in proposed and baseline[key] == proposed[key]:
            continue
        if key not in proposed:
            merged.pop(key, None)
        elif isinstance(proposed[key], dict) and isinstance(baseline.get(key), dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_changed_values(merged[key], baseline[key], proposed[key])
        else:
            merged[key] = deepcopy(proposed[key])
    return merged
_STATE_FILE = Path(os.getenv("VELLA_REPRICER_STATE_FILE", "var/vella_repricer_runtime_state.json"))


def _runtime_state_defaults(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    state = dict(payload or {})
    state.setdefault("assignments", {})
    state.setdefault("skuMetaOverrides", {})
    state.setdefault("skuSettingsOverrides", {})
    state.setdefault("cogsHistory", {})
    state.setdefault("liquidationActive", {})
    state.setdefault("liquidationHistory", [])
    state.setdefault("negativeMarginConfirmations", {})
    state.setdefault("skuGroups", {})
    state.setdefault("skuAuditEvents", {})
    state.setdefault("pendingPriceApprovals", {})
    return state


def _load_file_state() -> dict[str, Any]:
    try:
        if not _STATE_FILE.exists():
            return {}
        with _STATE_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load repricer runtime fallback file")
        return {}


def _save_file_state(payload: dict[str, Any]) -> bool:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = _STATE_FILE.with_name(f"{_STATE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        temp_path.replace(_STATE_FILE)
        return True
    except OSError:
        logger.exception("Failed to save repricer runtime fallback file")
        return False


def _load_file_runtime_state(organization_id: int) -> dict[str, Any] | None:
    payload = _load_file_state().get(str(organization_id))
    if not isinstance(payload, dict):
        return None
    runtime = payload.get("runtimeState")
    return _runtime_state_defaults(runtime) if isinstance(runtime, dict) else None


def _load_file_algorithm_settings(organization_id: int) -> dict[str, Any] | None:
    payload = _load_file_state().get(str(organization_id))
    if not isinstance(payload, dict):
        return None
    settings = payload.get("algorithmSettings")
    return dict(settings) if isinstance(settings, dict) else None


def _save_file_runtime_state(organization_id: int, runtime_state: dict[str, Any]) -> bool:
    payload = _load_file_state()
    org_payload = payload.get(str(organization_id))
    if not isinstance(org_payload, dict):
        org_payload = {}
    org_payload["runtimeState"] = _runtime_state_defaults(runtime_state)
    org_payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
    payload[str(organization_id)] = org_payload
    return _save_file_state(payload)


def _save_file_algorithm_settings(organization_id: int, settings_payload: dict[str, Any]) -> bool:
    payload = _load_file_state()
    org_payload = payload.get(str(organization_id))
    if not isinstance(org_payload, dict):
        org_payload = {}
    org_payload["algorithmSettings"] = dict(settings_payload or {})
    org_payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
    payload[str(organization_id)] = org_payload
    return _save_file_state(payload)


def _run_db(db_fn):
    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            return db_fn(session)
    except SQLAlchemyError:
        logger.exception("Database operation failed in repricer persistence")
        return None


def list_organization_ids() -> list[int]:
    def _db(session: Session) -> list[int]:
        from app.cabinet.orm import LkOrganizationRow

        return [int(value) for value in session.scalars(select(LkOrganizationRow.organization_id).order_by(LkOrganizationRow.organization_id)).all()]

    return _run_db(_db) or []


def load_runtime_state(organization_id: int) -> dict[str, Any] | None:
    def _db(session: Session) -> dict[str, Any] | None:
        row = session.get(WbRepricerRuntimeStateRow, organization_id)
        if row is None:
            return None
        return _runtime_state_defaults(row.state_payload)

    payload = _run_db(_db)
    if payload is not None:
        return _runtime_state_defaults(payload)
    file_payload = _load_file_runtime_state(organization_id)
    if file_payload is not None:
        _MEMORY_RUNTIME_STATE[organization_id] = deepcopy(file_payload)
        return file_payload
    memory_payload = _MEMORY_RUNTIME_STATE.get(organization_id)
    return deepcopy(memory_payload) if memory_payload is not None else None


def load_algorithm_settings(organization_id: int) -> dict[str, Any] | None:
    def _db(session: Session) -> dict[str, Any] | None:
        row = session.get(WbRepricerAlgorithmSettingsRow, organization_id)
        if row is None:
            return None
        return dict(row.settings_payload or {})

    payload = _run_db(_db)
    if payload is not None:
        return payload
    file_payload = _load_file_algorithm_settings(organization_id)
    if file_payload is not None:
        _MEMORY_ALGORITHM_SETTINGS[organization_id] = deepcopy(file_payload)
        return file_payload
    memory_payload = _MEMORY_ALGORITHM_SETTINGS.get(organization_id)
    return deepcopy(memory_payload) if memory_payload is not None else None


def save_algorithm_settings(organization_id: int, settings_payload: dict[str, Any], *, baseline: dict | None = None) -> bool:
    payload = dict(settings_payload or {})
    # Only the quarterly-tax endpoint owns this subdocument. A stale worker or
    # legacy profile form must never replace newly saved quarterly rates.
    previous = load_algorithm_settings(organization_id) or {}
    payload.pop("wbQuarterlyTax", None)
    if "wbQuarterlyTax" in previous:
        payload["wbQuarterlyTax"] = deepcopy(previous["wbQuarterlyTax"])

    def _db(session: Session) -> bool:
        nonlocal payload
        row = session.scalar(select(WbRepricerAlgorithmSettingsRow).where(
            WbRepricerAlgorithmSettingsRow.organization_id == organization_id).with_for_update())
        if row is not None and baseline is not None:
            payload = _merge_changed_values(row.settings_payload or {}, baseline, payload)
        if row is not None and "wbQuarterlyTax" in (row.settings_payload or {}):
            payload["wbQuarterlyTax"] = deepcopy(row.settings_payload["wbQuarterlyTax"])
        if row is None:
            row = WbRepricerAlgorithmSettingsRow(organization_id=organization_id, settings_payload=payload)
            session.add(row)
        else:
            from app.platform.economics.wb_quarterly_tax import settings_match
            result = session.execute(update(WbRepricerAlgorithmSettingsRow).where(
                WbRepricerAlgorithmSettingsRow.organization_id == organization_id,
                settings_match(session, row.settings_payload or {}),
            ).values(settings_payload=payload), execution_options={"synchronize_session": False})
            if result.rowcount != 1:
                session.rollback()
                return False
        session.commit()
        return True

    db_saved = _run_db(_db)
    _MEMORY_ALGORITHM_SETTINGS[organization_id] = deepcopy(payload)
    file_saved = _save_file_algorithm_settings(organization_id, payload)
    return bool(db_saved) or file_saved


def save_runtime_state(organization_id: int, state_payload: dict[str, Any], *, preserve_pending: bool = False, baseline: dict | None = None) -> bool:
    payload = _runtime_state_defaults(state_payload)

    def _db(session: Session) -> bool:
        nonlocal payload
        if preserve_pending and session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = session.scalar(select(WbRepricerRuntimeStateRow).where(
            WbRepricerRuntimeStateRow.organization_id == organization_id).with_for_update())
        if row is None:
            row = WbRepricerRuntimeStateRow(organization_id=organization_id, state_payload=payload)
            session.add(row)
        else:
            if baseline is not None:
                payload = _merge_changed_values(row.state_payload or {}, baseline, payload)
            if preserve_pending:
                payload["pendingPriceApprovals"] = deepcopy((row.state_payload or {}).get("pendingPriceApprovals") or {})
            row.state_payload = payload
        session.commit()
        return True

    db_saved = _run_db(_db)
    _MEMORY_RUNTIME_STATE[organization_id] = deepcopy(payload)
    file_saved = _save_file_runtime_state(organization_id, payload)
    return bool(db_saved) or file_saved


def append_execution_run(
    *,
    organization_id: int,
    run_id: str,
    trigger: str,
    report_payload: dict[str, Any],
) -> bool:
    def _db(session: Session) -> bool:
        session.add(
            WbRepricerExecutionRunRow(
                run_id=run_id,
                organization_id=organization_id,
                trigger=trigger,
                report_payload=report_payload,
            )
        )
        session.commit()
        return True

    return bool(_run_db(_db))


def list_execution_runs(
    *,
    organization_id: int,
    trigger: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    def _db(session: Session) -> list[dict[str, Any]]:
        query = (
            select(WbRepricerExecutionRunRow)
            .where(WbRepricerExecutionRunRow.organization_id == organization_id)
            .where(~WbRepricerExecutionRunRow.run_id.startswith("px"))
            .order_by(WbRepricerExecutionRunRow.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
        if trigger:
            query = query.where(WbRepricerExecutionRunRow.trigger == trigger)
        rows = session.scalars(query).all()
        return [
            {
                "runId": row.run_id,
                "organizationId": row.organization_id,
                "trigger": row.trigger,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "report": dict(row.report_payload or {}),
            }
            for row in rows
        ]

    return _run_db(_db) or []


def upsert_pending_price_approvals(
    *,
    organization_id: int,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not approvals:
        return []
    from app.repricer_dispatch_store import mutate_pending

    def mutate(pending):
        now = datetime.now(timezone.utc).isoformat()
        saved = []
        for approval in approvals:
            existing_id = next((key for key, item in pending.items() if isinstance(item, dict)
                and item.get("status", "pending") in {"pending", "applying", "sent", "needs_attention"}
                and item.get("articleId") == approval.get("articleId")
                and item.get("recommendedPriceKopecks") == approval.get("recommendedPriceKopecks")
                and (item.get("draftRequest") or {}).get("nightWindowKey") == (approval.get("draftRequest") or {}).get("nightWindowKey")), None)
            if existing_id is not None:
                saved.append(deepcopy(pending[existing_id]))
                continue
            approval_id = str(approval.get("approvalId") or approval.get("draftId") or f"price-apr-{uuid.uuid4().hex}")
            if approval_id in pending:
                saved.append(deepcopy(pending[approval_id]))
                continue
            item = {**dict(approval), "approvalId": approval_id, "status": "pending",
                "createdAt": approval.get("createdAt") or now, "updatedAt": now}
            pending[approval_id] = item
            saved.append(deepcopy(item))
        return saved
    return mutate_pending(organization_id, mutate)


def list_pending_price_approvals(
    *,
    organization_id: int,
    limit: int = 20,
    include_closed: bool = False,
    include_inflight: bool = False,
) -> list[dict[str, Any]]:
    if include_inflight:
        from app.repricer_dispatch_store import mutate_pending, DispatchPersistenceError
        try:
            mutate_pending(organization_id, lambda pending: None)
        except DispatchPersistenceError:
            pass  # Read last persisted status; never grant a fallback send claim.
    state = load_runtime_state(organization_id) or {}
    pending = state.get("pendingPriceApprovals") or {}
    if not isinstance(pending, dict):
        return []
    items = [dict(item) for item in pending.values() if isinstance(item, dict)]
    if not include_closed:
        visible = {"pending", "applying", "sent", "needs_attention"} if include_inflight else {"pending"}
        items = [item for item in items if str(item.get("status") or "pending") in visible]
    items.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
    return items[: max(1, min(limit, 100))]


def get_pending_price_approval(*, organization_id: int, approval_id: str) -> dict[str, Any] | None:
    state = load_runtime_state(organization_id) or {}
    pending = state.get("pendingPriceApprovals") or {}
    if not isinstance(pending, dict):
        return None
    payload = pending.get(approval_id)
    return dict(payload) if isinstance(payload, dict) else None


def update_pending_price_approval(
    *,
    organization_id: int,
    approval_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    updated = update_pending_price_approvals(
        organization_id=organization_id,
        patches={approval_id: patch},
    )
    return updated.get(approval_id)


def update_pending_price_approvals(
    *,
    organization_id: int,
    patches: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not patches:
        return {}
    from app.repricer_dispatch_store import mutate_pending

    def mutate(pending):
        now = datetime.now(timezone.utc).isoformat()
        updated = {}
        for approval_id, patch in patches.items():
            current = pending.get(approval_id)
            if not isinstance(current, dict):
                continue
            previous, target = current.get("status", "pending"), patch.get("status")
            if patch.get("_expectedStatus") and previous != patch["_expectedStatus"]:
                continue
            if patch.get("draftId") and current.get("claimedDraftId") and patch["draftId"] != current["claimedDraftId"]:
                continue
            if target and ((previous in {"applied", "rejected"} and target != previous)
                           or (previous in {"applying", "sent", "needs_attention"} and target in {"pending", "rejected"})):
                continue
            item = {**current, **{key: value for key, value in patch.items() if not key.startswith("_expected")}, "approvalId": approval_id, "updatedAt": now}
            pending[approval_id] = item
            updated[approval_id] = deepcopy(item)
        return updated
    return mutate_pending(organization_id, mutate)


def append_changelog_entry(*, organization_id: int, entry: dict[str, Any]) -> bool:
    def _db(session: Session) -> bool:
        session.add(
            WbRepricerChangelogRow(
                organization_id=organization_id,
                entry_id=str(entry.get("id") or entry.get("articleId")),
                article_id=str(entry.get("articleId") or ""),
                entry_payload=entry,
            )
        )
        session.commit()
        return True

    return bool(_run_db(_db))


def _sqlite_changelog_timestamp_microseconds(raw):
    """Order ISO datetime JSON by instant without SQLite's millisecond rounding.

    SQLite CAST(... AS DATETIME) is numeric affinity, not a datetime cast.
    Parse whole seconds with SQLite (including the offset), then add the ISO
    fractional part as an integer to retain Python datetime's six-digit precision.
    """
    tail = func.substr(raw, 20)
    plus, minus, zulu = func.instr(tail, "+"), func.instr(tail, "-"), func.instr(tail, "Z")
    zone_at = case((plus > 0, plus), (minus > 0, minus), (zulu > 0, zulu), else_=func.length(tail) + 1)
    offset = case((plus > 0, func.substr(tail, plus)), (minus > 0, func.substr(tail, minus)), else_="")
    seconds = cast(func.strftime("%s", func.substr(raw, 1, 19).concat(offset)), BigInteger)
    digits = func.substr(raw, 21, zone_at - 2)
    fraction = case((func.substr(raw, 20, 1) == ".", cast(func.substr(digits.concat("000000"), 1, 6), BigInteger)), else_=0)
    return seconds * 1_000_000 + fraction


def list_changelog_page(
    *,
    organization_id: int,
    article_id: str | None = None,
    triggers: list[str] | None = None,
    from_date: datetime | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    def _db(session: Session) -> dict[str, Any]:
        raw_timestamp = WbRepricerChangelogRow.entry_payload["timestamp"].as_string()
        sqlite = session.get_bind().dialect.name == "sqlite"
        timestamp = (_sqlite_changelog_timestamp_microseconds(raw_timestamp) if sqlite
                     else cast(raw_timestamp, DateTime(timezone=True)))
        query = (
            select(WbRepricerChangelogRow)
            .where(WbRepricerChangelogRow.organization_id == organization_id)
        )
        if article_id:
            query = query.where(WbRepricerChangelogRow.article_id == article_id)
        if triggers:
            query = query.where(WbRepricerChangelogRow.entry_payload["trigger"].as_string().in_(triggers))
        if from_date is not None:
            boundary = from_date
            if sqlite:
                instant = from_date.replace(tzinfo=timezone.utc) if from_date.tzinfo is None else from_date.astimezone(timezone.utc)
                delta = instant - datetime(1970, 1, 1, tzinfo=timezone.utc)
                boundary = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
            query = query.where(timestamp >= boundary)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(timestamp.desc(), WbRepricerChangelogRow.row_id.desc())
                               .offset((max(1, page) - 1) * max(1, limit)).limit(max(1, limit))).all()
        return {"items": [dict(row.entry_payload or {}) for row in rows], "total": total}

    return _run_db(_db) or {"items": [], "total": 0}


@_serialized_bff_state
def hydrate_repricer_bff_state(organization_id: int, repricer_bff_module: Any) -> None:
    payload = load_runtime_state(organization_id) or {}
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.update(payload.get("assignments") or {})
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.SKU_META_OVERRIDES.update(payload.get("skuMetaOverrides") or {})
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(payload.get("skuSettingsOverrides") or {})
    repricer_bff_module.LIQUIDATION_ACTIVE.clear()
    repricer_bff_module.LIQUIDATION_ACTIVE.update(payload.get("liquidationActive") or {})
    repricer_bff_module.LIQUIDATION_HISTORY.clear()
    repricer_bff_module.LIQUIDATION_HISTORY.extend(payload.get("liquidationHistory") or [])
    repricer_bff_module.NEGATIVE_MARGIN_CONFIRMATIONS.clear()
    repricer_bff_module.NEGATIVE_MARGIN_CONFIRMATIONS.update(payload.get("negativeMarginConfirmations") or {})
    if "REPRICER_SKU_GROUPS" in repricer_bff_module.__dict__:
        repricer_bff_module.REPRICER_SKU_GROUPS.clear()
        repricer_bff_module.REPRICER_SKU_GROUPS.update(payload.get("skuGroups") or {})
    if "SKU_AUDIT_EVENTS" in repricer_bff_module.__dict__:
        repricer_bff_module.SKU_AUDIT_EVENTS.clear()
        repricer_bff_module.SKU_AUDIT_EVENTS.update(payload.get("skuAuditEvents") or {})
    default_algorithm_settings = deepcopy(
        getattr(repricer_bff_module, "DEFAULT_ALGORITHM_SETTINGS", {})
    )
    algorithm_settings = dict(default_algorithm_settings)
    stored_algorithm_settings = load_algorithm_settings(organization_id) or {}
    if not stored_algorithm_settings:
        stored_algorithm_settings = dict(payload.get("algorithmSettings") or {})
        if stored_algorithm_settings:
            save_algorithm_settings(organization_id, stored_algorithm_settings)
    algorithm_settings.update(stored_algorithm_settings or {})
    if not stored_algorithm_settings.get("nightMedianDefaultAllSkusVersion"):
        algorithm_settings["nightMedianEnabled"] = True
        algorithm_settings["nightMedianAutoEnableAllSkus"] = True
        algorithm_settings["nightMedianGlobal"] = True
        algorithm_settings["nightMedianDefaultAllSkusVersion"] = 1
        save_algorithm_settings(organization_id, algorithm_settings)
    elif "nightMedianAutoEnableAllSkus" not in algorithm_settings:
        algorithm_settings["nightMedianAutoEnableAllSkus"] = bool(algorithm_settings.get("nightMedianGlobal", True))
    if "ALGORITHM_SETTINGS_STATE" in repricer_bff_module.__dict__:
        current = getattr(repricer_bff_module, "ALGORITHM_SETTINGS_STATE", None)
        if isinstance(current, dict):
            current.clear()
            current.update(algorithm_settings)
        else:
            repricer_bff_module.ALGORITHM_SETTINGS_STATE = algorithm_settings
    _HYDRATED_BASELINE.set((organization_id, deepcopy(payload), deepcopy(algorithm_settings)))
    repricer_bff_module._HYDRATED_ORGANIZATION_ID = organization_id


@_serialized_bff_state
def flush_repricer_bff_state(organization_id: int, repricer_bff_module: Any) -> bool:
    owner = getattr(repricer_bff_module, "_HYDRATED_ORGANIZATION_ID", None)
    if owner is not None and owner != organization_id:
        return False
    hydrated = _HYDRATED_BASELINE.get()
    runtime_baseline = hydrated[1] if hydrated and hydrated[0] == organization_id else None
    algorithm_baseline = hydrated[2] if hydrated and hydrated[0] == organization_id else None
    previous_payload = load_runtime_state(organization_id) or {}
    previous_algorithm = load_algorithm_settings(organization_id) or {}
    previous_settings = previous_payload.get("skuSettingsOverrides") or {}
    current_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    cogs_history = deepcopy(previous_payload.get("cogsHistory") or {})
    changed_at = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).isoformat()
    for article_id in set(previous_settings) | set(current_settings):
        old = (previous_settings.get(article_id) or {}).get("cogsKopecks")
        new = (current_settings.get(article_id) or {}).get("cogsKopecks")
        if old is None and new is None:
            continue
        history = cogs_history.setdefault(article_id, [])
        if not history and old is not None:
            history.append({"effectiveFrom": changed_at, "cogsKopecks": old, "source": "observed_snapshot"})
        if old == new:
            continue
        history.append({"effectiveFrom": changed_at, "cogsKopecks": new, "source": "platform_settings"})
    payload = {
        "assignments": dict(repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS),
        "skuMetaOverrides": dict(repricer_bff_module.SKU_META_OVERRIDES),
        "skuSettingsOverrides": current_settings,
        "cogsHistory": cogs_history,
        "liquidationActive": dict(repricer_bff_module.LIQUIDATION_ACTIVE),
        "liquidationHistory": list(repricer_bff_module.LIQUIDATION_HISTORY),
        "negativeMarginConfirmations": dict(repricer_bff_module.NEGATIVE_MARGIN_CONFIRMATIONS),
        "skuGroups": dict(getattr(repricer_bff_module, "REPRICER_SKU_GROUPS", {})),
        "skuAuditEvents": deepcopy(getattr(repricer_bff_module, "SKU_AUDIT_EVENTS", {})),
        "pendingPriceApprovals": dict(previous_payload.get("pendingPriceApprovals") or {}),
        "flushedAt": datetime.now(timezone.utc).isoformat(),
    }
    algorithm = dict(getattr(repricer_bff_module, "ALGORITHM_SETTINGS_STATE", {}))
    previous_garments = previous_algorithm.get("cogsByGarmentRub") or {}
    current_garments = algorithm.get("cogsByGarmentRub") or {}
    garment_history = deepcopy(previous_algorithm.get("cogsByGarmentHistory") or algorithm.get("cogsByGarmentHistory") or {})
    for garment in set(previous_garments) | set(current_garments):
        old = previous_garments.get(garment)
        new = current_garments.get(garment)
        history = garment_history.setdefault(garment, [])
        if not history and old is not None:
            history.append({"effectiveFrom": changed_at, "cogsRub": old, "source": "observed_snapshot"})
        if old == new:
            continue
        history.append({"effectiveFrom": changed_at, "cogsRub": new, "source": "platform_settings"})
    algorithm["cogsByGarmentHistory"] = garment_history
    algorithm_saved = save_algorithm_settings(organization_id, algorithm, baseline=algorithm_baseline)
    runtime_saved = save_runtime_state(organization_id, payload, preserve_pending=True, baseline=runtime_baseline)
    return bool(runtime_saved and algorithm_saved)
