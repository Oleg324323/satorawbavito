from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import escape
from io import BytesIO
from math import isfinite
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from app.promotion_excel import _find_column, _int_or_none, _money_kopecks, _normalize_header, _worksheet_rows


EXPORT_HEADERS = [
    "WorkspaceId (WorkspaceId)",
    "AccountId (AccountId)",
    "AccountName (AccountName)",
    "Предмет (SubjectName)",
    "Бренд (BrandName)",
    "Артикул МП (NmId)",
    "Арт. поставщика (SupplierArticle)",
    "SupplierProductId (SupplierProductId)",
    "Разрешить отриц маржу (AllowMinMargin)",
    "Валюта (CurrencyId)",
    "Себестоимость, ₽ (Настройка) (CostPrice)",
    "Мин. марж., ₽ (MinMarginAmount)",
    "Мин. марж. % (MinMarginPercent)",
    "Мин. цена, ₽ (MinPrice)",
    "Базовая марж., ₽ (MaxMarginAmount)",
    "Базовая марж. % (MaxMarginPercent)",
    "Базовая цена (МаксРЦ), ₽ (MaxPrice)",
    "Затраты от цены с СПП, % (TaxRate)",
    "Прочие расх., ₽ (PickPackCostAmount)",
    "Прочие расх. от Себест., % (PickPackCostPercent)",
    "Запретить отправку цен (Disabled)",
    "Стратегия (StrategyId)",
    "Прочие расх. от РРЦ, % (PromoCostPercent)",
    "Конк. тип отличия (CompeteDiffType)",
    "Конк. тип цен (CompetePriceType)",
    "Конк. отличие (CompeteDiffValue)",
    "План заказ, шт (OrdersPlanQty)",
    "План заказ, дн (OrdersPlanDays)",
    "План заказ, тип (OrdersPlanType)",
    "Мин допустимый остаток, шт (NormalStockQty)",
    "Цена до скидки (настр.) (PriceBeforeDiscount)",
    "Целевая скидка, % (TargetDiscount)",
    "Мин остаток конкурента, шт (MinCompeteStock)",
    "UseSpp (UseSpp)",
    "Выставить красивую цену (BeautyPriceEnabled)",
    "Режим красивой цены (BeautyPriceMode)",
    "Шаблон красивой цены (BeautyPriceTemplate)",
    "Макс изменение, ₽ (BeautyPriceMaxChangeAmount)",
    "Макс изменение в % (BeautyPriceMaxChangePercent)",
    "Хранение на одну продажу, ₽ (StorageCostAmount)",
    "Учет СПП+Кошелек (DiscountType)",
    "Тип красивой цены (BeautyPriceLevel)",
    "Уровень увелич. акц. цен  (TypeOfPromoPriceBoost)",
    "Защита от out of stock (UseOutOffStock)",
    "Целевая оборачиваемость (TargetTurnover)",
    "Затраты на продвижение, % (AdvertCostPercent)",
    "Учёт кошелька для конкурентного следования (CompeteWalletType)",
    "Остаток, шт. (StockTotal)",
    "Остаток FBS, шт (StockFbs)",
    "Остаток FBM, шт (StockFbm)",
    "NmId (NmId)",
]

_YELLOW_SETTING_COLUMNS: dict[str, tuple[str, str]] = {
    "allow_min_margin": ("allowNegativeMargin", "bool"),
    "cost": ("cogsKopecks", "money"),
    "min_margin_amount": ("minMarginKopecks", "money"),
    "min_margin_percent": ("minMarginPct", "number"),
    "p_min": ("pMinKopecks", "money"),
    "max_margin_amount": ("maxMarginKopecks", "money"),
    "max_margin_percent": ("maxMarginPct", "number"),
    "p_max": ("pMaxKopecks", "money"),
    "tax_rate": ("taxPct", "number"),
    "pick_pack_amount": ("otherExpensePerSaleKopecks", "money"),
    "pick_pack_percent": ("pickPackCostPercent", "number"),
    "promo_cost_percent": ("promoCostPercent", "number"),
    "compete_diff_type": ("competeDiffType", "raw"),
    "compete_price_type": ("competePriceType", "raw"),
    "compete_diff_value": ("competeDiffValue", "number"),
    "orders_plan_qty": ("ordersPlanQty", "int"),
    "orders_plan_days": ("ordersPlanDays", "int"),
    "orders_plan_type": ("ordersPlanType", "raw"),
    "normal_stock_qty": ("normalStockQty", "int"),
    "price_before_discount": ("rrpKopecks", "money"),
    "target_discount": ("targetDiscountPct", "number"),
    "min_compete_stock": ("minCompeteStock", "int"),
    "use_spp": ("useSpp", "bool"),
    "beauty_price_enabled": ("beautyPriceEnabled", "bool"),
    "beauty_price_mode": ("beautyPriceMode", "raw"),
    "beauty_price_template": ("beautyPriceTemplate", "raw"),
    "beauty_price_max_change_amount": ("beautyPriceMaxChangeKopecks", "money"),
    "beauty_price_max_change_percent": ("beautyPriceMaxChangePct", "number"),
    "storage_cost_amount": ("storageCostPerSaleKopecks", "money"),
    "discount_type": ("discountType", "raw"),
    "beauty_price_level": ("beautyPriceLevel", "raw"),
    "promo_price_boost_type": ("promoBoostType", "raw"),
    "use_out_of_stock": ("useOutOfStock", "bool"),
    "target_turnover": ("targetTurnover", "number"),
    "advert_cost_percent": ("advertCostPercent", "number"),
    "compete_wallet_type": ("competeWalletType", "raw"),
}

_STOCK_COLUMNS = {
    "stock_total": "stockTotal",
    "stock_fbs": "stockFbs",
    "stock_fbm": "stockFbm",
}


@dataclass(frozen=True)
class ParsedRepricerNomenclatureExcel:
    original_filename: str
    file_hash: str
    rows_total: int
    rows_parsed: int
    items: list[dict[str, Any]]
    status: str
    error_text: str | None = None


def _col_ref(index: int) -> str:
    value = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def validate_xlsx_text(value: str) -> str:
    if any(
        code not in (9, 10, 13)
        and not 0x20 <= code <= 0xD7FF
        and not 0xE000 <= code <= 0xFFFD
        and not 0x10000 <= code <= 0x10FFFF
        for code in map(ord, value)
    ):
        raise ValueError("Invalid XML character in XLSX text")
    return value


def _cell_xml(value: Any, cell_ref: str, style: int = 0, preserve_text: bool = False) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{cell_ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("XLSX numbers must be finite")
        return f'<c r="{cell_ref}"{style_attr}><v>{value}</v></c>'
    text = validate_xlsx_text(str(value))
    space = ' xml:space="preserve"' if preserve_text else ""
    return f'<c r="{cell_ref}" t="inlineStr"><is><t{space}>{escape(text)}</t></is></c>'


def build_xlsx(rows: list[list[Any]], *, sheet_name: str = "repricer", column_formats: list[int] | None = None, data_rows: int | None = None) -> bytes:
    if (
        not 1 <= len(sheet_name) <= 31
        or any(character in "[]:*?/\\" for character in sheet_name)
    ):
        raise ValueError("Invalid XLSX sheet name")
    validate_xlsx_text(sheet_name)
    sheet_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = [
            _cell_xml(value, f"{_col_ref(column_index)}{row_index}",
                      column_formats[column_index] if column_formats and 1 < row_index <= (data_rows or 0) + 1 and column_index < len(column_formats) else 0,
                      preserve_text=bool(column_formats))
            for column_index, value in enumerate(row)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    dimension = f"A1:{_col_ref(max((len(row) for row in rows), default=1) - 1)}{max(len(rows), 1)}"
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        + ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if column_formats else '')
        +
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        + (f'<autoFilter ref="A1:{_col_ref(len(column_formats)-1)}{(data_rows or 0)+1}"/>' if column_formats else '')
        +
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name, quote=True)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    if column_formats:
        workbook_rels = workbook_rels.replace('</Relationships>',
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        content_types = content_types.replace('</Types>',
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if column_formats:
            # 1=count, 2=RUB, 3=percentage stored as a fraction (0.28).
            archive.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00 &quot;₽&quot;"/></numFmts>'
                '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
                '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="1" applyNumberFormat="1"/><xf numFmtId="164" applyNumberFormat="1"/><xf numFmtId="9" applyNumberFormat="1"/></cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return buffer.getvalue()


def _rub(kopecks: Any) -> int | None:
    try:
        value = int(kopecks)
    except (TypeError, ValueError):
        return None
    return round(value / 100)


def _raw_setting(settings: dict[str, Any], key: str) -> Any:
    value = settings.get(key)
    return "" if value is None else value


def _disabled_value(settings: dict[str, Any]) -> int:
    return 0 if bool(settings.get("automationEnabled", True)) else 1


def build_repricer_nomenclature_xlsx(sku_rows: list[dict[str, Any]]) -> bytes:
    rows: list[list[Any]] = [EXPORT_HEADERS]
    for sku in sku_rows:
        meta = sku.get("meta") or {}
        settings = sku.get("settings") or {}
        analytics = sku.get("analytics") or {}
        strategy = sku.get("strategy") or {}
        assignment_source = str(strategy.get("assignmentSource") or meta.get("assignmentSource") or "")
        strategy_id = strategy.get("id") if assignment_source in {"manual", "xlsx"} else ""
        rows.append(
            [
                "",
                "",
                "",
                meta.get("subject"),
                meta.get("brand"),
                meta.get("nmId"),
                meta.get("articleId"),
                _raw_setting(settings, "supplierProductId"),
                1 if bool(settings.get("allowNegativeMargin")) else 0,
                _raw_setting(settings, "currencyId") or 643,
                _rub(settings.get("cogsKopecks")),
                _rub(settings.get("minMarginKopecks")),
                _raw_setting(settings, "minMarginPct"),
                _rub(settings.get("pMinKopecks") or settings.get("pminKopecks")),
                _rub(settings.get("maxMarginKopecks")),
                _raw_setting(settings, "maxMarginPct"),
                _rub(settings.get("pMaxKopecks")),
                _raw_setting(settings, "taxPct"),
                _rub(settings.get("otherExpensePerSaleKopecks")),
                _raw_setting(settings, "pickPackCostPercent"),
                _disabled_value(settings),
                strategy_id,
                _raw_setting(settings, "promoCostPercent") or _raw_setting(settings, "otherExpensePricePct"),
                _raw_setting(settings, "competeDiffType"),
                _raw_setting(settings, "competePriceType"),
                _raw_setting(settings, "competeDiffValue"),
                _raw_setting(settings, "ordersPlanQty"),
                _raw_setting(settings, "ordersPlanDays"),
                _raw_setting(settings, "ordersPlanType"),
                _raw_setting(settings, "normalStockQty"),
                _rub(settings.get("rrpKopecks")),
                _raw_setting(settings, "targetDiscountPct"),
                _raw_setting(settings, "minCompeteStock"),
                _raw_setting(settings, "useSpp"),
                _raw_setting(settings, "beautyPriceEnabled"),
                _raw_setting(settings, "beautyPriceMode"),
                _raw_setting(settings, "beautyPriceTemplate"),
                _rub(settings.get("beautyPriceMaxChangeKopecks")),
                _raw_setting(settings, "beautyPriceMaxChangePct"),
                _rub(settings.get("storageCostPerSaleKopecks")),
                _raw_setting(settings, "discountType"),
                _raw_setting(settings, "beautyPriceLevel"),
                _raw_setting(settings, "promoBoostType"),
                _raw_setting(settings, "useOutOfStock"),
                _raw_setting(settings, "targetTurnover"),
                _raw_setting(settings, "advertCostPercent"),
                _raw_setting(settings, "competeWalletType"),
                analytics.get("wbStockUnits"),
                _raw_setting(settings, "stockFbs"),
                _raw_setting(settings, "stockFbm"),
                meta.get("nmId"),
            ]
        )
    return build_xlsx(rows)


def _value(row: list[str], columns: dict[str, int | None], key: str) -> str:
    column = columns.get(key)
    if column is None or column >= len(row):
        return ""
    return row[column]


def _strategy_text(value: Any) -> str | None:
    text = str(value or "").replace("\u00a0", " ").strip()
    if not text:
        return None
    normalized = text.lower()
    if normalized in {"none", "null", "no_strategy", "нет", "не задана", "без стратегии", "-"}:
        return "none"
    return text


def _number_or_none(value: Any) -> float | None:
    text = str(value or "").replace("\u00a0", " ").replace(",", ".").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _bool_or_none(value: Any) -> bool | None:
    text = str(value or "").replace("\u00a0", " ").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "да", "истина", "вкл", "on"}:
        return True
    if text in {"0", "false", "no", "n", "нет", "ложь", "выкл", "off"}:
        return False
    number = _number_or_none(text)
    if number is not None:
        return bool(number)
    return None


def _raw_or_none(value: Any) -> Any | None:
    text = str(value or "").replace("\u00a0", " ").strip()
    if not text:
        return None
    number = _number_or_none(text)
    if number is not None and re.fullmatch(r"-?\d+(?:[.,]0+)?", text.replace(" ", "")):
        return int(number)
    return text


def _parse_setting_value(value: Any, value_type: str) -> Any | None:
    if value_type == "money":
        return _money_kopecks(value)
    if value_type == "int":
        return _int_or_none(value)
    if value_type == "number":
        return _number_or_none(value)
    if value_type == "bool":
        return _bool_or_none(value)
    return _raw_or_none(value)


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int | None]]:
    aliases = {
        "article_id": {"артпоставщика", "артикулпоставщика", "supplierarticle", "vendorcode", "артикулпродавца"},
        "nm_id": {"nmid", "артикулмп", "артикулмпnmid", "supsku"},
        "strategy": {"стратегия", "strategyid", "strategy", "typedstrategyid"},
        "strategy_name": {"стратегияназвание", "strategyname"},
        "cost": {"себестоимостьруб", "себестоимость", "costprice"},
        "allow_min_margin": {"allowminmargin", "разрешитьотрицмаржу"},
        "min_margin_amount": {"minmarginamount", "минмарж"},
        "min_margin_percent": {"minmarginpercent", "минмаржпроц"},
        "p_min": {"минценаруб", "минцена", "minprice"},
        "max_margin_amount": {"maxmarginamount", "базоваямарж"},
        "max_margin_percent": {"maxmarginpercent", "базоваямаржпроц"},
        "p_max": {"базоваяценамаксрцруб", "базоваяценамаксрц", "максрц", "maxprice"},
        "tax_rate": {"taxrate", "затратыотцены"},
        "pick_pack_amount": {"pickpackcostamount", "прочиерасх"},
        "pick_pack_percent": {"pickpackcostpercent", "прочиерасхотсебест"},
        "disabled": {"disabled", "запретитьотправкуцен"},
        "promo_cost_percent": {"promocostpercent", "прочиерасхотррц"},
        "compete_diff_type": {"competedifftype", "конктипотличия"},
        "compete_price_type": {"competepricetype", "конктипцен"},
        "compete_diff_value": {"competediffvalue", "конкотличие"},
        "orders_plan_qty": {"ordersplanqty", "планзаказшт"},
        "orders_plan_days": {"ordersplandays", "планзаказдн"},
        "orders_plan_type": {"ordersplantype", "планзаказтип"},
        "normal_stock_qty": {"normalstockqty", "миндопустимыйостаток"},
        "price_before_discount": {"pricebeforediscount", "ценадоскидки"},
        "target_discount": {"targetdiscount", "целеваяскидка"},
        "min_compete_stock": {"mincompetestock", "миностатокконкурента"},
        "use_spp": {"usespp"},
        "beauty_price_enabled": {"beautypriceenabled", "выставитькрасивуюцену"},
        "beauty_price_mode": {"beautypricemode", "режимкрасивойцены"},
        "beauty_price_template": {"beautypricetemplate", "шаблонкрасивойцены"},
        "beauty_price_max_change_amount": {"beautypricemaxchangeamount", "максизменение"},
        "beauty_price_max_change_percent": {"beautypricemaxchangepercent", "максизменениев"},
        "storage_cost_amount": {"storagecostamount", "хранениенаоднупродажу"},
        "discount_type": {"discounttype", "учетсппкошелек"},
        "beauty_price_level": {"beautypricelevel", "типкрасивойцены"},
        "promo_price_boost_type": {"typeofpromopriceboost", "уровеньувеличакццен"},
        "use_out_of_stock": {"useoutoffstock", "useoutofstock", "защитаотoutofstock"},
        "target_turnover": {"targetturnover", "целеваяоборачиваемость"},
        "advert_cost_percent": {"advertcostpercent", "затратынapродвижение", "затратынапродвижение"},
        "compete_wallet_type": {"competewallettype", "учеткошелькадляконкурентногоследования"},
        "stock_total": {"stocktotal"},
        "stock_fbs": {"stockfbs", "остатокfbs"},
        "stock_fbm": {"stockfbm", "остатокfbm"},
    }
    for index, row in enumerate(rows[:50]):
        headers = [_normalize_header(value) for value in row]
        columns = {key: _find_column(headers, values) for key, values in aliases.items()}
        if columns.get("article_id") is not None or columns.get("nm_id") is not None:
            if columns.get("strategy") is not None or columns.get("strategy_name") is not None or columns.get("p_min") is not None or columns.get("p_max") is not None:
                return index, columns
    return -1, {}


def parse_repricer_nomenclature_excel(content: bytes, filename: str) -> ParsedRepricerNomenclatureExcel:
    file_hash = hashlib.sha256(content).hexdigest()
    try:
        rows = _worksheet_rows(content)
    except Exception as exc:
        return ParsedRepricerNomenclatureExcel(filename, file_hash, 0, 0, [], "error", f"XLSX не распознан: {exc}")

    header_index, columns = _find_header(rows)
    if header_index < 0:
        return ParsedRepricerNomenclatureExcel(
            filename,
            file_hash,
            max(0, len(rows) - 1),
            0,
            [],
            "error",
            "Не найдены колонки артикула и изменяемых настроек",
        )

    items: list[dict[str, Any]] = []
    data_rows = rows[header_index + 1 :]
    for row in data_rows:
        article_id = str(_value(row, columns, "article_id") or "").strip()
        nm_id = _int_or_none(_value(row, columns, "nm_id"))
        strategy = _strategy_text(_value(row, columns, "strategy")) or _strategy_text(_value(row, columns, "strategy_name"))
        settings_patch: dict[str, Any] = {}
        stock_patch: dict[str, int] = {}
        for column_key, (target_key, value_type) in _YELLOW_SETTING_COLUMNS.items():
            value = _parse_setting_value(_value(row, columns, column_key), value_type)
            if value is not None:
                settings_patch[target_key] = value
                if target_key == "promoCostPercent":
                    settings_patch["otherExpensePricePct"] = value
        disabled = _bool_or_none(_value(row, columns, "disabled"))
        if disabled is not None:
            settings_patch["automationEnabled"] = not disabled
        for column_key, target_key in _STOCK_COLUMNS.items():
            value = _int_or_none(_value(row, columns, column_key))
            if value is not None:
                stock_patch[target_key] = value
        if not article_id and nm_id is None:
            continue
        if strategy is None and not settings_patch and not stock_patch:
            continue
        item = {
            "articleId": article_id or None,
            "nmId": nm_id,
            "strategyId": strategy,
            "settingsPatch": settings_patch,
            "stockPatch": stock_patch,
            "rawRow": {str(index): cell for index, cell in enumerate(row) if cell},
        }
        item.update(settings_patch)
        items.append(item)

    return ParsedRepricerNomenclatureExcel(
        original_filename=filename,
        file_hash=file_hash,
        rows_total=len(data_rows),
        rows_parsed=len(items),
        items=items,
        status="parsed" if items else "error",
        error_text=None if items else "В файле не найдено строк со стратегией или желтыми настройками",
    )
