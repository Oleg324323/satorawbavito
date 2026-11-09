"""Decode original labels, never manufacture a barcode from an order number.

Each spatial label region must contain exactly one explicitly labelled order ID
and one decoded barcode, with its human-readable value also present in that
region. Ambiguous/scanned-without-text documents are retained, not guessed.
"""
from dataclasses import dataclass
from contextlib import closing
from io import BytesIO
import re
import threading
import pypdfium2 as pdfium
import zxingcpp

MAX_PDF_BYTES = 12_000_000
_PDF_LOCK = threading.Lock()  # PDFium is not thread safe.
_ORDER = re.compile(r"(?:Номер\s+заказа|Order\s+(?:number|no\.?))\s*[:№#]?\s*([0-9][0-9 \u00a0\u202f\t]{5,50}[0-9])", re.I)


def identity(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def _order_number(text_page, match) -> str:
    """PDF text can concatenate the nearby barcode caption on the same line.
    Stop at the spatial gap between the order number and the next text block.
    """
    digits, previous = [], None
    for offset, char in enumerate(match.group(1)):
        if not char.isdigit():
            continue
        index = pdfium.raw.FPDFText_GetCharIndexFromTextIndex(text_page.raw, match.start(1) + offset)
        if index < 0:
            return ""
        box = text_page.get_charbox(index)
        if previous and box[0] - previous[2] > max(18, (box[3] - box[1]) * 1.8, (previous[3] - previous[1]) * 1.8):
            break
        digits.append(char)
        previous = box
    return ''.join(digits) if 6 <= len(digits) <= 24 else ""


@dataclass
class DecodedLabel:
    order_number: str
    barcode: str
    barcode_type: str
    png: bytes
    page: int


def decode_pdf(data: bytes) -> tuple[list[DecodedLabel], list[str]]:
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
        raise ValueError("AVITO_LABEL_INVALID_PDF")
    labels, warnings = [], []
    with _PDF_LOCK, pdfium.PdfDocument(data) as doc:
        if not 0 < len(doc) <= 100:
            raise ValueError("AVITO_LABEL_PAGE_LIMIT")
        pixel_budget = 0
        for page_index in range(len(doc)):
            with closing(doc[page_index]) as page:
                width, height = page.get_size()
                scale = 3
                pixel_budget += width * height * scale * scale
                if width <= 0 or height <= 0 or width * height * 9 > 16_000_000 or pixel_budget > 200_000_000:
                    raise ValueError("AVITO_LABEL_RENDER_LIMIT")
                with closing(page.get_textpage()) as text_page:
                    text = text_page.get_text_range()
                    anchors = []
                    for match in _ORDER.finditer(text):
                        # PDFium may insert/remove characters when extracting
                        # text. Convert text index to its internal char index.
                        char_index = pdfium.raw.FPDFText_GetCharIndexFromTextIndex(text_page.raw, match.start())
                        if char_index < 0:
                            continue
                        box = text_page.get_charbox(char_index)
                        number = _order_number(text_page, match)
                        if number:
                            anchors.append((number, box[0], height - box[3]))
                    if not anchors:
                        warnings.append(f"page_{page_index + 1}:order_number_not_readable")
                        continue
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().copy()
                    finally:
                        bitmap.close()
                    codes = [code for code in zxingcpp.read_barcodes(image) if code.valid and code.format in
                             {zxingcpp.BarcodeFormat.Code128, zxingcpp.BarcodeFormat.Code39, zxingcpp.BarcodeFormat.ITF}]
                    # Rows/columns of labels: split at the next label's anchor
                    # with a margin above its heading, not by PDF page order.
                    ys = sorted({round(a[2] / 20) * 20 for a in anchors})
                    for number, x, y in anchors:
                        same_row = [a for a in anchors if abs(a[2] - y) < 20]
                        xs = sorted({a[1] for a in same_row})
                        left = 0 if x == xs[0] else x - 15
                        right = min((v - 15 for v in xs if v > x), default=width)
                        row_y = round(y / 20) * 20
                        top = 0 if row_y == ys[0] else y - 55
                        bottom = min((v - 55 for v in ys if v > row_y), default=height)
                        region = (left * scale, top * scale, right * scale, bottom * scale)
                        candidates = []
                        for code in codes:
                            p = code.position
                            cx = (p.top_left.x + p.bottom_right.x) / 2
                            cy = (p.top_left.y + p.bottom_right.y) / 2
                            if region[0] <= cx < region[2] and region[1] <= cy < region[3]:
                                candidates.append(code)
                        if len(candidates) != 1 or sum(a[0] == number for a in anchors) != 1:
                            warnings.append(f"page_{page_index + 1}:ambiguous_label")
                            continue
                        code = candidates[0]
                        region_text = text_page.get_text_bounded(left, height - bottom, right, height - top)
                        if not code.text or len(code.text) > 256 or identity(code.text) not in identity(region_text):
                            warnings.append(f"page_{page_index + 1}:barcode_text_mismatch")
                            continue
                        p = code.position
                        px = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
                        py = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
                        crop = image.crop((max(0, min(px) - 40), max(0, min(py) - 16), min(image.width, max(px) + 40), min(image.height, max(py) + 16)))
                        # Check the very image that will be embedded in Excel.
                        if not any(c.text == code.text for c in zxingcpp.read_barcodes(crop)):
                            warnings.append(f"page_{page_index + 1}:crop_not_decodable")
                            continue
                        buffer = BytesIO()
                        crop.save(buffer, format="PNG")
                        labels.append(DecodedLabel(number, code.text, str(code.format).split(".")[-1].replace(" ", "").upper(), buffer.getvalue(), page_index + 1))
    return labels, warnings
