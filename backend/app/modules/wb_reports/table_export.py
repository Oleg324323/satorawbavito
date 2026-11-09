from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.repricer_nomenclature_excel import build_xlsx, validate_xlsx_text

Header = Annotated[StrictStr, StringConstraints(min_length=1, max_length=128)]
CellText = Annotated[StrictStr, StringConstraints(max_length=4096)]
Cell = CellText | StrictInt | StrictFloat | None
ShortText = Annotated[StrictStr, StringConstraints(min_length=1, max_length=128)]


class ReportTableExportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["ready", "partial", "empty"]
    formulaVersion: ShortText
    financeFormulaVersion: ShortText
    financeSnapshotChecksum: ShortText
    financeObservedAt: AwareDatetime
    advertisingSnapshotChecksum: (
        Annotated[StrictStr, StringConstraints(max_length=128)] | None
    )
    costLedgerRevision: Annotated[StrictInt, Field(ge=0)]
    economicsRevision: Annotated[StrictInt, Field(ge=0)]
    blockerIds: Annotated[list[ShortText], Field(max_length=64)]
    filterDescription: Annotated[StrictStr, StringConstraints(max_length=4096)]

    @model_validator(mode="after")
    def validate_xml_text(self) -> ReportTableExportSource:
        for value in (
            self.formulaVersion,
            self.financeFormulaVersion,
            self.financeSnapshotChecksum,
            self.advertisingSnapshotChecksum,
            self.filterDescription,
            *self.blockerIds,
        ):
            if value is not None:
                validate_xlsx_text(value)
        return self


class ReportTableExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reportKind: Literal["abc", "pnl"]
    marketplaceAccountId: Annotated[StrictInt, Field(gt=0)]
    dateFrom: date
    dateTo: date
    headers: Annotated[list[Header], Field(min_length=1, max_length=32)]
    rows: Annotated[list[list[Cell]], Field(max_length=10_000)]
    source: ReportTableExportSource

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: list[str]) -> list[str]:
        for header in headers:
            validate_xlsx_text(header)
        return headers

    @model_validator(mode="after")
    def validate_rows(self) -> ReportTableExportRequest:
        width = len(self.headers)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("Each row must match the header width")
            for cell in row:
                if isinstance(cell, str):
                    validate_xlsx_text(cell)
                elif isinstance(cell, float) and not isfinite(cell):
                    raise ValueError("XLSX numbers must be finite")
        return self


def render_report_table_export(
    request: ReportTableExportRequest, *, organization_id: int
) -> bytes:
    source = request.source
    metadata: list[list[Cell]] = [
        ["Экспорт", "Копия загруженной таблицы; не заверенный финансовый расчёт"],
        ["Организация", organization_id],
        ["Аккаунт WB", request.marketplaceAccountId],
        ["Период с", request.dateFrom.isoformat()],
        ["Период по", request.dateTo.isoformat()],
        ["Часовой пояс", "Europe/Moscow"],
        ["Деньги", "рубли"],
        ["Состояние источника", source.state],
        ["Версия формулы", source.formulaVersion],
        ["Версия финансовой формулы", source.financeFormulaVersion],
        ["Контрольная сумма финансов", source.financeSnapshotChecksum],
        ["Финансы наблюдались", source.financeObservedAt.isoformat()],
        ["Контрольная сумма рекламы", source.advertisingSnapshotChecksum],
        ["Ревизия себестоимости", source.costLedgerRevision],
        ["Ревизия экономики", source.economicsRevision],
        ["Блокировки", ", ".join(source.blockerIds)],
        ["Фильтры", source.filterDescription],
        ["Экспортировано строк", len(request.rows)],
    ]
    pnl_fee_columns = request.reportKind == "pnl" and any(
        header in {"Штрафы, ₽", "Платная приёмка, ₽"} for header in request.headers)
    if pnl_fee_columns:
        metadata.append(["Даты штрафов и приёмки", "rrDate, включительно; расходы без nmId не распределяются"])
    return build_xlsx(
        [request.headers, *request.rows, [], *metadata],
        sheet_name="ABC" if request.reportKind == "abc" else "P&L",
        column_formats=[2 if header in {"Штрафы, ₽", "Платная приёмка, ₽"} else 0 for header in request.headers] if pnl_fee_columns else None,
        data_rows=len(request.rows) if pnl_fee_columns else None,
    )
