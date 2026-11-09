"""WB-only calendar-year tax overrides; source facts and shared policies stay intact.

Unedited quarters keep the existing effective-dated policy. Merely opening the
profile (or saving identical initial values) must not rewrite historical tax.
"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from functools import wraps
from inspect import signature
from typing import Any

from sqlalchemy import JSON, cast, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import set_tenant_context
from app.platform.economics.policies import EconomicsService
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.period import MOSCOW
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow

SETTINGS_KEY = "wbQuarterlyTax"
_snapshot: ContextVar[tuple[int, dict[str, Any]] | None] = ContextVar("wb_tax_snapshot", default=None)


class QuarterlyTaxConflict(ValueError):
    pass


def basis_points(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Ставка должна быть числом от 0 до 100 с точностью до 0,01%")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Некорректная ставка налога") from exc
    if not number.is_finite() or not 0 <= number <= 100 or number * 100 != (number * 100).to_integral_value():
        raise ValueError("Ставка должна быть от 0 до 100 с точностью до 0,01%")
    return int(number * 100)


def settings_in_session(session: Session, organization_id: int) -> dict[str, Any]:
    set_tenant_context(session, organization_id)
    payload = session.scalar(select(WbRepricerAlgorithmSettingsRow.settings_payload).where(
        WbRepricerAlgorithmSettingsRow.organization_id == organization_id))
    return deepcopy(payload or {})


def settings_match(session: Session, previous: dict[str, Any]):
    column = WbRepricerAlgorithmSettingsRow.settings_payload
    # PostgreSQL JSON has no equality operator; JSONB compares semantic values.
    if session.get_bind().dialect.name == "postgresql":
        return cast(column, JSONB) == cast(literal(previous, type_=JSON), JSONB)
    return column == previous


def revision(settings: dict[str, Any]) -> str:
    schedule = settings.get(SETTINGS_KEY) or {}
    active = {year: {str(index): entry["basisPoints"][index] for index, enabled in enumerate(entry["configured"]) if enabled}
              for year, entry in schedule.items() if any(entry["configured"])}
    return sha256(json.dumps({"base": settings.get("taxPct"),
        "quarters": active, "currentProfileRate": planning_tax_pct(settings)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def wb_economics_revision(session: Session, organization_id: int, legacy_revision: int) -> int:
    settings = active_settings(session, organization_id)
    if not any(any(entry["configured"]) for entry in (settings.get(SETTINGS_KEY) or {}).values()):
        return legacy_revision
    # Preserve the existing integer API contract, within JavaScript's exact range.
    return int(sha256(f"{legacy_revision}:{revision(settings)}".encode()).hexdigest()[:13], 16)


def active_settings(session: Session, organization_id: int) -> dict[str, Any]:
    snapshot = _snapshot.get()
    return snapshot[1] if snapshot and snapshot[0] == organization_id else settings_in_session(session, organization_id)


def assert_current_snapshot(organization_id: int) -> str | None:
    snapshot = _snapshot.get()
    if snapshot is None or snapshot[0] != organization_id:
        return None
    # The same factory as the dated bridge; no fallback data, credentials or WB calls.
    from app.platform.economics.legacy_tax import get_session_factory
    with get_session_factory()() as session:
        current = revision(settings_in_session(session, organization_id))
    expected = revision(snapshot[1])
    if current != expected:
        raise QuarterlyTaxConflict("Налог изменился во время расчёта. Отчёт нужно пересчитать из БД.")
    return expected


def current_revision(organization_id: int) -> str:
    from app.platform.economics.legacy_tax import get_session_factory
    try:
        with get_session_factory()() as session:
            return revision(settings_in_session(session, organization_id))
    except SQLAlchemyError:
        return "unavailable"


def tax_consistent_report(function):
    """One settings snapshot for rows/totals/both weeks; superseded work fails closed."""
    parameters = signature(function, eval_str=True)

    @wraps(function)
    def guarded(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs).arguments
        if bound.get("report_id") not in {None, "abc", "pnl", "week-over-week", "rnp"}:
            return function(*args, **kwargs)
        organization_id = bound.get("organization_id")
        if organization_id is None and bound.get("actor") is not None:
            organization_id = bound["actor"].organization_id
        if organization_id is None and bound.get("self") is not None:
            organization_id = getattr(bound["self"], "organization_id", None)
        if organization_id is None and bound.get("request") is not None:
            from app.control_plane.auth import actor_from_request
            organization_id = actor_from_request(bound["request"]).organization_id
        existing = _snapshot.get()
        if organization_id is None or (existing is not None and existing[0] == organization_id):
            return function(*args, **kwargs)
        from app.platform.economics.legacy_tax import get_session_factory
        try:
            with get_session_factory()() as session:
                settings = settings_in_session(session, organization_id)
        except SQLAlchemyError:
            # Preserve existing explicit unavailable-tax behavior when the DB
            # itself is offline, rather than replacing it with a planning rate.
            return function(*args, **kwargs)
        token = _snapshot.set((organization_id, settings))
        try:
            try:
                result = function(*args, **kwargs)
                assert_current_snapshot(organization_id)
                return result
            except QuarterlyTaxConflict as exc:
                if bound.get("request") is not None:
                    from fastapi import HTTPException
                    raise HTTPException(409, str(exc)) from exc
                raise
        finally:
            _snapshot.reset(token)
    guarded.__signature__ = parameters
    return guarded


def year_view(settings: dict[str, Any], year: int) -> dict[str, Any]:
    if type(year) is not int or not 1900 <= year <= 9998:
        raise ValueError("Некорректный календарный год")
    saved = (settings.get(SETTINGS_KEY) or {}).get(str(year))
    initial = basis_points(settings.get("taxPct"))
    values = saved["basisPoints"] if saved else [initial] * 4
    return {"year": year, "rates": [value / 100 if value is not None else None for value in values],
        "configured": list(saved["configured"]) if saved else [False] * 4,
        "revision": revision(settings), "timezone": "Europe/Moscow"}


def save_year(session: Session, organization_id: int, *, year: int,
              rates: list[Any], expected_revision: str,
              confirmed_quarters: tuple[int, ...] = ()) -> dict[str, Any]:
    # Explicit operator confirmation is different from saving an unchanged form.
    # Regular callers retain the existing read-only initialization semantics.
    if any(type(index) is not int or index not in range(4) for index in confirmed_quarters):
        raise ValueError("Некорректный квартал подтверждения")
    previous = settings_in_session(session, organization_id)
    view = year_view(previous, year)
    if view["revision"] != expected_revision:
        raise QuarterlyTaxConflict("Настройки изменены в другом окне. Загрузите их заново.")
    if len(rates) != 4:
        raise ValueError("Нужны ставки всех четырёх кварталов")
    values = [basis_points(rate) for rate in rates]
    old_values = [basis_points(rate) for rate in view["rates"]]
    active = [configured or value != old or index in confirmed_quarters
              for index, (configured, value, old) in enumerate(
                  zip(view["configured"], values, old_values, strict=True))]
    if any(active[index] and value is None for index, value in enumerate(values)):
        raise ValueError("Укажите ставку изменённого квартала; 0% допустим")
    payload = deepcopy(previous)
    schedule = payload.setdefault(SETTINGS_KEY, {})
    entry = {"basisPoints": values, "configured": active}
    old_entry = schedule.get(str(year))
    # Idempotent saves do not invalidate caches or rewrite audit timestamps.
    if old_entry and all(old_entry[key] == value for key, value in entry.items()):
        return year_view(previous, year)
    entry["updatedAt"] = datetime.now(timezone.utc).isoformat()
    schedule[str(year)] = entry
    # Compare the entire JSON document: concurrent unrelated settings must not
    # be lost either. No memory/file fallback may claim this DB write succeeded.
    result = session.execute(update(WbRepricerAlgorithmSettingsRow).where(
        WbRepricerAlgorithmSettingsRow.organization_id == organization_id,
        settings_match(session, previous),
    ).values(settings_payload=payload, updated_at=datetime.now(timezone.utc)),
        execution_options={"synchronize_session": False})
    if result.rowcount != 1:
        if previous or session.scalar(select(WbRepricerAlgorithmSettingsRow.organization_id).where(
                WbRepricerAlgorithmSettingsRow.organization_id == organization_id)) is not None:
            raise QuarterlyTaxConflict("Настройки изменились. Загрузите их заново.")
        session.add(WbRepricerAlgorithmSettingsRow(organization_id=organization_id, settings_payload=payload))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise QuarterlyTaxConflict("Настройки созданы в другом окне. Загрузите их заново.") from exc
    return year_view(payload, year)


def selected_basis_points(settings: dict[str, Any], recognized_on: date | datetime) -> int | None:
    if isinstance(recognized_on, datetime):
        if recognized_on.tzinfo is None:
            raise ValueError("Tax recognition timestamp must be timezone-aware")
        day = recognized_on.astimezone(MOSCOW).date()
    else:
        day = recognized_on
    entry = (settings.get(SETTINGS_KEY) or {}).get(str(day.year))
    index = (day.month - 1) // 3
    if not entry or not entry["configured"][index]:
        return None
    return entry["basisPoints"][index]


def planning_tax_pct(settings: dict[str, Any], on: date | None = None) -> Any:
    """Planning uses today's business date, not the selected report's end date."""
    selected = selected_basis_points(settings, on or datetime.now(MOSCOW).date())
    return settings.get("taxPct") if selected is None else selected / 100


def wb_policies_for_points(session: Session, organization_id: int, points: list[tuple[int, datetime]]):
    policies = EconomicsService(session, organization_id).get_policies_for_points(points)
    settings = active_settings(session, organization_id)
    if not settings.get(SETTINGS_KEY):
        return policies
    valid_skus = set(session.scalars(select(CatalogSkuRow.catalog_sku_id).where(
        CatalogSkuRow.organization_id == organization_id,
        CatalogSkuRow.catalog_sku_id.in_({sku for sku, _instant in points}))).all())
    for key, policy in policies.items():
        # An unknown SKU must remain unknown; do not make an invalid mapping valid.
        if key[0] not in valid_skus:
            continue
        rate = selected_basis_points(settings, key[1])
        if rate is None:
            continue
        day = key[1].astimezone(MOSCOW).date()
        start = datetime.combine(date(day.year, ((day.month - 1) // 3) * 3 + 1, 1), time.min, MOSCOW)
        policies[key] = replace(policy, tax_basis_points=rate, tax_value_state="configured",
            tax_evidence_status="dated", tax_effective_from=start.astimezone(timezone.utc))
    return policies
