"""Bounded XLSX copy of the Advertising tab, without provider requests."""
from datetime import date
from math import isfinite
from io import BytesIO
from typing import Literal
from zipfile import ZipFile

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.control_plane.auth import actor_from_request
from app.control_plane.store import assert_permission_or_audit
from app.modules.wb_reports.table_export import Cell, Header
from app.repricer_nomenclature_excel import build_xlsx, validate_xlsx_text


class AdsTableExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reportKind: Literal["ads"]
    sourceKind: Literal["loaded-ads"]
    dateFrom: date
    dateTo: date
    headers: list[Header] = Field(min_length=1, max_length=32)
    rows: list[list[Cell]] = Field(max_length=10000)
    sourceState: str = Field(min_length=1, max_length=128)
    warning: str = Field(max_length=4096)
    filterDescription: str = Field(max_length=4096)

    @model_validator(mode="after")
    def validate_table(self):
        if not 1 <= (self.dateTo - self.dateFrom).days + 1 <= 90:
            raise ValueError("Invalid selected period")
        for row in self.rows:
            if len(row) != len(self.headers):
                raise ValueError("Invalid row width")
        for value in [self.sourceState, self.warning, self.filterDescription, *self.headers,
                      *(cell for row in self.rows for cell in row)]:
            if isinstance(value, str):
                validate_xlsx_text(value)
            elif isinstance(value, float) and not isfinite(value):
                raise ValueError("Non-finite number")
        return self


async def export_ads_table(request: Request):
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.ads.export",
        object_type="wb_report", object_id="ads", reason="actor cannot export advertising")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 5 * 1024 * 1024:
            raise HTTPException(413, "Export body too large")
        body.extend(chunk)
    try:
        payload = AdsTableExport.model_validate_json(body)
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid Advertising table export") from None
    money = {"Заказы, ₽", "Продажи, ₽", "Расход", "Бюджет РК"}
    percentages = {"CTR", "CR", "ДРР", "ROMI"}
    counts = {"Показы", "Клики", "Корзины", "Заказы, шт.", "Продажи, шт."}
    content = build_xlsx([payload.headers, *payload.rows, [],
        ["Экспорт", "Копия таблицы рекламы; итоги товаров по кампаниям за период, нераспределённые расходы отдельно"],
        ["Организация", actor.organization_id], ["Период с", payload.dateFrom.isoformat()],
        ["Период по", payload.dateTo.isoformat()], ["Часовой пояс", "Europe/Moscow; обе границы включены"],
        ["Состояние источников", payload.sourceState], ["Ограничения", payload.warning],
        ["Фильтры и сортировка", payload.filterDescription], ["Экспортировано строк", len(payload.rows)],
        ["Единицы", "Деньги в рублях; проценты — доли с Excel-форматом %; пусто — нет данных"],
        ["Бюджет", "Текущий budget.total может повторяться у товаров одной РК; не суммировать"],
        ["Источники ID", "https://dev.wildberries.ru/docs/openapi/promotion — adverts.id/nm_settings.nm_id; fullstats.advertId/days.apps.nms.nmId"],
    ], sheet_name="Реклама", column_formats=[2 if label in money else 3 if label in percentages else 1 if label in counts else 0 for label in payload.headers], data_rows=len(payload.rows))
    # The shared exporter uses integer percentages. Refine only this workbook
    # to built-in Excel 0.00%, leaving other tabs' emitter/styles unchanged.
    formatted = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(formatted, "w") as target:
        for entry in source.infolist():
            value = source.read(entry.filename)
            if entry.filename == "xl/styles.xml":
                value = value.replace(b'<xf numFmtId="9" applyNumberFormat="1"/>', b'<xf numFmtId="10" applyNumberFormat="1"/>')
            target.writestr(entry, value)
    content = formatted.getvalue()
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="wb-ads-{payload.dateFrom}-{payload.dateTo}.xlsx"'})
