from __future__ import annotations

from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait
from io import BytesIO
import struct
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
from PIL import Image, UnidentifiedImageError

from app.avito.orders import AvitoOrderRow

HEADERS = [
    "№", "Фото", "Наименование", "Размер", "Цвет", "Количество, шт.",
    "Номер отправления", "Стикер", "Номер заказа", "ID товара Авито", "Пункт приема",
]
RETURN_HEADERS = ["Место получения возврата", "Срок получения", "Код возврата"]

CHAT_SIZE_REASONS = {
    "chat_response_invalid": "Авито вернуло некорректные данные чата",
    "chat_message_incomplete": "Сообщение слишком длинное: нужна проверка полного ответа",
    "chat_not_collected": "Чат не собран", "chat_unavailable": "Чат недоступен",
    "chat_collection_failed": "Ошибка сбора чата", "chat_identity_mismatch": "Связь чата с заказом не подтверждена",
    "chat_account_mismatch": "Аккаунт чата не совпадает", "message_author_unknown": "Автор сообщения не определён",
    "chat_customer_identity_missing": "Покупатель чата не определён",
    "chat_identity_missing": "Связь чата с заказом не подтверждена", "multiple_or_missing_channels": "Чат не определён",
    "multiple_orders_or_items": "Нельзя определить товар или заказ", "size_question_not_found": "Вопрос о размере не найден",
    "message_timestamp_missing": "Нет времени сообщения", "customer_reply_missing": "Ответ покупателя не найден",
    "ambiguous_reply": "Размер неоднозначен", "multiple_sizes": "Указано несколько размеров",
    "ambiguous_correction": "Изменение размера неоднозначно", "rejected_size": "Размер отклонён покупателем",
    "unrelated_numbers": "Нет явного размера одежды", "unsafe_reply": "Ответ требует ручной проверки",
    "openai_api_key_missing": "AI не настроен", "ai_request_or_response_failed": "Ошибка AI-разбора",
    "chat_history_incomplete": "История чата неполная: размер не найден",
    "ai_evidence_not_confirmed": "AI не подтвердил ответ покупателя", "ai_confirmation_missing": "Нет подтверждения AI",
}


def picking_size(item) -> str:
    if item.sizeMode == "chat_ai":
        if item.sizeState == "confirmed" and item.size:
            return item.size
        return "Проверить: " + CHAT_SIZE_REASONS.get(item.sizeReason or "", "Размер из чата не подтверждён")
    return (item.size or "Не получено") + ("\nПроверить: из объявления" if item.sources.get("size") in {"description", "description_fallback"} else "")


def picking_issues(order: AvitoOrderRow) -> list[str]:
    issues = []
    if not order.marketplaceId:
        issues.append("Номер заказа не получен")
    if not order.shipmentNumber or order.shipmentNumberState == "ambiguous":
        issues.append("Номер отправления не подтверждён")
    # A text identifier alone does not prove a printable transport label.
    if order.stickerNumberState != "confirmed" or not order.stickerLabelId:
        issues.append("Оригинал транспортной этикетки не получен")
    if not order.items:
        issues.append("Состав заказа не получен")
    for item in order.items:
        if not item.imageUrl:
            issues.append("Фото не получено")
        if not item.size or item.sources.get("size") in {"description", "description_fallback"} or (item.sizeMode == "chat_ai" and item.sizeState != "confirmed"):
            issues.append("Размер требует проверки")
        if not item.color or item.sources.get("color") in {"listing", "browser_unspecified"}:
            issues.append("Цвет требует проверки")
        if not item.itemId:
            issues.append("ID товара не получен")
    return list(dict.fromkeys(issues))


def _xlsx_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _col_ref(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(ref: str, value: Any, style: int = 2) -> str:
    text = "" if value is None else str(value)
    # Reject XML 1.0-invalid input rather than emit a corrupt workbook or drop text.
    if any(
        not (
            code in (9, 10, 13)
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        )
        for code in map(ord, text)
    ):
        raise ValueError("Invalid XML character in XLSX text")
    if not text:
        return f'<c r="{ref}" s="{style}"/>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'


def _row(row_index: int, values: list[Any], style: int = 2, *, image: bool = False, label: bool = False, returns: bool = False) -> str:
    cells = [_cell(f"{_col_ref(column)}{row_index}", value, 6 if not returns and column == 8 and row_index >= 2 else style) for column, value in enumerate(values, start=1)]
    height = ' ht="104" customHeight="1"' if label or image else ""
    return f'<row r="{row_index}"{height}>{"".join(cells)}</row>'


def _picking_rows(orders: list[AvitoOrderRow], *, returns: bool = False) -> list[tuple[list[Any], str | None]]:
    rows: list[tuple[list[Any], str | None]] = []
    for order in orders:
        for item in order.items:
            if item.quantity <= 0:
                continue
            values = [
                len(rows) + 1,
                "",
                item.title or "Не получено",
                picking_size(item),
                (item.color or "Не получено") + ("\nПроверить: из объявления" if item.sources.get("color") in {"listing", "browser_unspecified"} else ""),
                item.quantity,
                order.shipmentNumber or "Не собрано",
                order.stickerNumber if order.stickerNumberState == "confirmed" and order.stickerLabelId else "Этикетка не получена",
                order.marketplaceId or "Не получено",
                item.itemId or "Не получено",
                order.dropoffProvider or "Не собрано",
            ]
            if returns:
                values = values[:6] + values[8:10] + [getattr(order, field) or (
                    "Неоднозначные данные: нужна проверка" if order.returnFieldStates.get(field) == "ambiguous"
                    else "Ошибка сбора: повторите сбор возвратов" if order.returnFieldStates.get(field) == "collection_failed"
                    else "Не найдено в деталях возврата" if order.returnFieldStates.get(field) == "unavailable"
                    else "Не собрано: выполните сбор возвратов")
                    for field in ("returnPickupPlace", "returnPickupDeadline", "returnPickupCode")]
            rows.append((values, item.imageUrl))
    return rows


def _load_image(url: str) -> tuple[bytes, str] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        host = (parsed.hostname or "").lower()
        if host != "img.avito.st" and not host.endswith(".img.avito.st"):
            return None
    except ValueError:
        return None
    started = time.monotonic()
    # A single bounded retry for transient transport/server failures. Never
    # retry rate limits or access errors; do not turn exports into a sync job.
    for _attempt in range(2):
        try:
            with httpx.Client(timeout=httpx.Timeout(2, connect=1), follow_redirects=False) as client:
                with client.stream("GET", url) as response:
                    if response.status_code in {500, 502, 503, 504}:
                        continue
                    if response.status_code != 200:
                        return None
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                        return None
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000 or time.monotonic() - started > 5:
                            return None
            data = bytes(content)
            with Image.open(BytesIO(data)) as photo:
                if photo.format not in {'PNG', 'JPEG', 'WEBP'} or photo.width * photo.height > 16_000_000:
                    return None
                photo.verify()
            with Image.open(BytesIO(data)) as photo:
                if photo.format == 'WEBP':
                    # Excel does not reliably support WebP as an embedded image.
                    output = BytesIO()
                    photo.convert('RGB').save(output, format='JPEG', quality=92)
                    return output.getvalue(), 'jpeg'
                return data, 'png' if photo.format == 'PNG' else 'jpeg'
        except httpx.HTTPError:
            if time.monotonic() - started > 3:
                return None
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            return None
    return None


def _prefetch_images(urls, loader, *, budget_seconds: float = 6) -> dict:
    """A slow/unavailable CDN must not hold the entire workbook indefinitely."""
    unique = list(dict.fromkeys(url for url in urls if url))
    if not unique:
        return {}
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="avito-xlsx-photo")
    futures = {pool.submit(loader, url): url for url in unique}
    images = {}
    try:
        done, pending = wait(futures, timeout=budget_seconds)
        for future in pending:
            future.cancel()
        for future in done:
            try:
                images[futures[future]] = future.result()
            except Exception:
                images[futures[future]] = None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return images


def _photo_extent(data: bytes) -> tuple[int, int] | None:
    """Bounded dimensions inspection; keep original aspect ratio in Excel."""
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 255:
                break
            marker = data[pos + 1]
            if marker == 255:
                pos += 1
                continue
            length = int.from_bytes(data[pos + 2:pos + 4], "big")
            if length < 2 or pos + 2 + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 7:
                height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
                break
            pos += 2 + length
    if not (0 < width <= 20000 and 0 < height <= 20000):
        return None
    scale = 120 * 9525 / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def build_avito_orders_picking_xlsx(
    orders: list[AvitoOrderRow], *, date_from: date, as_of: str | None = None,
    returns: bool = False, image_loader: Callable[[str], tuple[bytes, str] | None] = _load_image,
    label_loader: Callable[[int], bytes | None] | None = None,
    warning: str | None = None,
) -> bytes:
    rows = _picking_rows(orders, returns=returns)
    loaded_images = _prefetch_images((url for _, url in rows), image_loader)
    headers = HEADERS[:6] + HEADERS[8:10] + RETURN_HEADERS if returns else HEADERS
    # The downloadable sheet starts with the table, without report banners.
    # Freshness information belongs to the application, not extra sheet rows.
    sheet_rows = [_row(1, headers, style=1)]
    media: dict[str, tuple[int, bytes, str]] = {}
    anchors: list[str] = []
    row_labels = [order.stickerLabelId if order.stickerNumberState == "confirmed" else None for order in orders for item in order.items if item.quantity > 0]
    for offset, (values, image_url) in enumerate(rows, start=2):
        if image_url and image_url not in media:
            loaded = loaded_images.get(image_url)
            if loaded and loaded[1] in {"png", "jpeg"} and _photo_extent(loaded[0]):
                media[image_url] = (len(media) + 1, loaded[0], loaded[1])
        image_id = media.get(image_url or "", (0, b"", ""))[0]
        if not image_id:
            values[1] = "Фото не получено"
        if image_id:
            cx, cy = _photo_extent(media[image_url][1])
            anchors.append(f'''<xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{offset - 1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="{cx}" cy="{cy}"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{offset}" name="Photo {offset}"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId{image_id}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>''')
        label_id = None if returns else row_labels[offset - 2]
        label_key = f"label:{label_id}"
        if label_id and label_loader and label_key not in media:
            data = label_loader(label_id)
            if data and _photo_extent(data):
                media[label_key] = (len(media) + 1, data, "png")
        label_image = media.get(label_key)
        if label_image:
            lid, data, _ = label_image
            width, height = struct.unpack(">II", data[16:24])
            # Keep the complete original PNG (including quiet zones), fit
            # proportionally, and leave room below for the readable number.
            scale = min(320 * 9525 / width, 96 * 9525 / height)
            cx, cy = round(width * scale), round(height * scale)
            anchors.append(f'''<xdr:oneCellAnchor><xdr:from><xdr:col>7</xdr:col><xdr:colOff>19050</xdr:colOff><xdr:row>{offset - 1}</xdr:row><xdr:rowOff>19050</xdr:rowOff></xdr:from><xdr:ext cx="{cx}" cy="{cy}"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{100000 + offset}" name="Transport barcode {offset}"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId{lid}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>''')
        elif label_id:
            values[7] = "Изображение этикетки недоступно"
        sheet_rows.append(_row(offset, values, style=2, image=bool(image_id or label_image), label=bool(label_image), returns=returns))

    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:{_col_ref(len(headers))}{len(rows) + 1}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>
    <col min="1" max="{len(headers)}" width="18" customWidth="1"/>
    <col min="1" max="1" width="6" customWidth="1"/>
    <col min="2" max="2" width="19" customWidth="1"/>
    <col min="3" max="3" width="36" customWidth="1"/>
    {'<col min="9" max="9" width="42" customWidth="1"/><col min="10" max="11" width="24" customWidth="1"/>' if returns else '<col min="8" max="8" width="47" customWidth="1"/>'}
  </cols>
  <sheetData>
    {"".join(sheet_rows)}
  </sheetData>
  <autoFilter ref="A1:{_col_ref(len(headers))}{len(rows) + 1}"/>
  <pageMargins left="0.25" right="0.25" top="0.4" bottom="0.4" header="0.2" footer="0.2"/>
  <pageSetup paperSize="9" orientation="landscape" scale="100"/>
  {'<drawing r:id="rId1"/>' if anchors else ''}
</worksheet>'''

    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="{'Возвраты' if returns else 'Лист подбора'}" sheetId="1" r:id="rId1"/></sheets>
  <definedNames><definedName name="_xlnm.Print_Titles" localSheetId="0">'{'Возвраты' if returns else 'Лист подбора'}'!$1:$1</definedName></definedNames>
</workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpeg" ContentType="image/jpeg"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  {'<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>' if anchors else ''}
</Types>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE7E6E6"/></patternFill></fill></fills>
  <borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="7">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
    <xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="bottom" wrapText="1"/></xf>
  </cellXfs>
</styleSheet>'''

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        parts = [
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", rels),
            ("xl/workbook.xml", workbook_xml),
            ("xl/_rels/workbook.xml.rels", workbook_rels),
            ("xl/worksheets/sheet1.xml", sheet_xml),
            ("xl/styles.xml", styles),
        ]
        if anchors:
            parts.extend([
                ("xl/worksheets/_rels/sheet1.xml.rels", '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>'''),
                ("xl/drawings/drawing1.xml", f'''<?xml version="1.0" encoding="UTF-8"?><xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">{"".join(anchors)}</xdr:wsDr>'''),
                ("xl/drawings/_rels/drawing1.xml.rels", f'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image{index}.{extension}"/>' for index, _data, extension in media.values())}</Relationships>'''),
            ])
            parts.extend((f"xl/media/image{index}.{extension}", data) for index, data, extension in media.values())
        for name, content in parts:
            # Fixed ZIP epoch keeps retries byte-stable within the same renderer runtime.
            member = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = ZIP_DEFLATED
            archive.writestr(member, content)
    return buffer.getvalue()
