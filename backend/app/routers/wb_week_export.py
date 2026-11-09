"""Bounded export of the Week page's already-loaded, filtered table."""
from datetime import date, timedelta
from math import isfinite

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.control_plane.auth import actor_from_request
from app.control_plane.store import assert_permission_or_audit
from app.modules.wb_reports.table_export import Header, Cell
from app.repricer_nomenclature_excel import build_xlsx, validate_xlsx_text


class WeekTableExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dateFrom: date
    dateTo: date
    sourceState: str = Field(max_length=128)
    warning: str = Field(default="", max_length=4096)
    headers: list[Header] = Field(min_length=1, max_length=32)
    rows: list[list[Cell]] = Field(max_length=10_000)

    @model_validator(mode="after")
    def validate_table(self):
        if not 1 <= (self.dateTo - self.dateFrom).days + 1 <= 90:
            raise ValueError("Invalid selected period")
        for row in self.rows:
            if len(row) != len(self.headers):
                raise ValueError("Row width must match headers")
        for value in [self.sourceState, self.warning, *self.headers, *(cell for row in self.rows for cell in row)]:
            if isinstance(value, str):
                validate_xlsx_text(value)
            elif isinstance(value, float) and not isfinite(value):
                raise ValueError("XLSX numbers must be finite")
        return self


async def export_week_table(request: Request) -> Response:
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.week.export",
                               object_type="wb_report", object_id="week-over-week", reason="actor cannot export week report")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 5 * 1024 * 1024:
            raise HTTPException(413, "Export body too large")
        body.extend(chunk)
    try:
        payload = WeekTableExport.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Invalid Week table export") from None
    days = (payload.dateTo - payload.dateFrom).days + 1
    previous_to = payload.dateFrom - timedelta(days=1)
    previous_from = previous_to - timedelta(days=days - 1)
    content = build_xlsx([payload.headers, *payload.rows, [],
        ["Экспорт", "Копия загруженной таблицы; не заверенный финансовый расчёт"],
        ["Организация", actor.organization_id],
        ["Выбранный период", f"{payload.dateFrom} — {payload.dateTo} ({days} дн.)"],
        ["Предыдущий период", f"{previous_from} — {previous_to} ({days} дн.)"],
        ["Состояние источников", payload.sourceState],
        ["Ограничения источников", payload.warning],
        ["История остатков", "Последние 7 дней до конца выбранного периода, по сохранённым снимкам"],
    ], sheet_name="Week-to-week")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Cache-Control": "no-store", "Content-Disposition":
                             f'attachment; filename="wb-week-{payload.dateFrom}-{payload.dateTo}.xlsx"'})
