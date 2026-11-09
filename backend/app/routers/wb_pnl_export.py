"""Export the loaded P&L table without requiring a canonical rollout.

No WB collection, profit recalculation or cross-period cache fallback happens
here. This is a bounded copy of the same financial rows shown to the user.
"""
from datetime import date
from math import isfinite
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.control_plane.auth import ActorContext
from app.modules.wb_reports.table_export import Cell, Header
from app.platform.finance.access import get_finance_actor, require_finance_read
from app.platform.period import Period
from app.repricer_nomenclature_excel import build_xlsx, validate_xlsx_text

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_BODY_BYTES = 5 * 1024 * 1024


class LoadedPnlTableExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reportKind: Literal["pnl"]
    sourceKind: Literal["loaded-pnl"]
    dateFrom: date
    dateTo: date
    sourceState: str = Field(min_length=1, max_length=128)
    warning: str = Field(max_length=4096)
    filterDescription: str = Field(max_length=4096)
    headers: list[Header] = Field(min_length=1, max_length=32)
    rows: list[list[Cell]] = Field(max_length=10_000)

    @model_validator(mode="after")
    def validate_table(self):
        Period(self.dateFrom, self.dateTo)
        for row in self.rows:
            if len(row) != len(self.headers):
                raise ValueError("Row width must match headers")
        for value in [self.sourceState, self.warning, self.filterDescription, *self.headers,
                      *(cell for row in self.rows for cell in row)]:
            if isinstance(value, str):
                validate_xlsx_text(value)
            elif isinstance(value, float) and not isfinite(value):
                raise ValueError("XLSX numbers must be finite")
        return self


async def export_pnl_table(request: Request, actor: Annotated[ActorContext, Depends(get_finance_actor)]) -> Response:
    require_finance_read(actor)
    # The actor's organization is authoritative; the client cannot supply an
    # organization/account ID or turn this into an export of another report.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Export body too large")
        body.extend(chunk)
    try:
        payload = LoadedPnlTableExport.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Invalid P&L table export") from None
    money = {"Выручка", "Себестоимость", "Комиссия", "Логистика", "Хранение",
             "Штрафы, ₽", "Платная приёмка, ₽", "Реклама", "Налог", "Прибыль"}
    content = build_xlsx([payload.headers, *payload.rows, [],
        ["Экспорт", "Копия загруженной таблицы P&L; не заверенный финансовый расчёт"],
        ["Организация", actor.organization_id],
        ["Период с", payload.dateFrom.isoformat()],
        ["Период по", payload.dateTo.isoformat()],
        ["Часовой пояс", "Europe/Moscow"],
        ["Деньги", "рубли; маржа в процентах"],
        ["Состояние источников", payload.sourceState],
        ["Ограничения источников", payload.warning],
        ["Фильтры", payload.filterDescription],
        ["Экспортировано строк", len(payload.rows)],
        ["Даты штрафов и приёмки", "rrDate, включительно; расходы без nmId не распределяются"],
    ], sheet_name="P&L", column_formats=[2 if header in money else 0 for header in payload.headers],
        data_rows=len(payload.rows))
    return Response(content, media_type=XLSX_MIME,
        headers={"Cache-Control": "no-store", "Content-Disposition":
                 f'attachment; filename="wb-pnl-{payload.dateFrom}-{payload.dateTo}.xlsx"'})
