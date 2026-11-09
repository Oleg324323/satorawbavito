from io import BytesIO
from datetime import date
from zipfile import ZipFile

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas
from reportlab.graphics.barcode.code128 import Code128
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import zxingcpp

from app.avito.labels_pdf import decode_pdf
from app.avito import labels_store
from app.avito.labels_orm import AvitoLabelDocument, AvitoTransportLabel
from app.avito.orders import AvitoOrderRow, AvitoOrderItem
from app.avito.orders_picking_xlsx import build_avito_orders_picking_xlsx


def label_pdf(rows=(("700000000000001", "0010328640390"),), *, same_page=False):
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(600, 800))
    for index, (order, barcode) in enumerate(rows):
        y = 660 - (index * 350 if same_page else 0)
        canvas.drawString(25, y, f"Order number: {order}")
        code = Code128(barcode, barHeight=70, barWidth=1.5, humanReadable=True)
        code.drawOn(canvas, 280, y - 5)
        if not same_page:
            canvas.showPage()
    canvas.save()
    return buffer.getvalue()


@pytest.fixture
def label_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    AvitoLabelDocument.__table__.create(engine)
    AvitoTransportLabel.__table__.create(engine)
    monkeypatch.setattr(labels_store, "get_session_factory", lambda: sessionmaker(engine, expire_on_commit=False))
    yield
    engine.dispose()


@pytest.mark.parametrize("same_page", [False, True])
def test_pdf_decodes_and_binds_each_original_label(same_page):
    rows = (("700000000000001", "0010328640390"), ("700000000000002", "0010328721895"))
    labels, warnings = decode_pdf(label_pdf(rows, same_page=same_page))
    assert not warnings
    assert [(label.order_number, label.barcode) for label in labels] == list(rows)
    for label in labels:
        assert label.barcode_type == 'CODE128'
        assert zxingcpp.read_barcodes(Image.open(BytesIO(label.png)))[0].text == label.barcode


def test_invalid_pdf_rejected():
    with pytest.raises(ValueError):
        decode_pdf(b"<html>Avito login</html>")


def test_russian_heading_grouped_order_number_and_grid():
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font = next((p for p in [Path('/System/Library/Fonts/Supplemental/Arial.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.is_file()), None)
    if font is None:
        pytest.skip('Cyrillic system font required for the Russian label fixture')
    pdfmetrics.registerFont(TTFont('AvitoTestVera', str(font)))
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(1200, 800))
    for column, value in enumerate(('0001234567890', '0009876543210')):
        x = 25 + column * 600
        canvas.setFont('AvitoTestVera', 12)
        canvas.drawString(x, 650, 'Номер заказа')
        canvas.drawString(x, 625, f'700 000 000 000 00{column + 1}')
        Code128(value, barHeight=70, barWidth=1.5, humanReadable=True).drawOn(canvas, x + 270, 630)
    canvas.save()
    labels, warnings = decode_pdf(buffer.getvalue())
    assert not warnings
    assert [(label.order_number, label.barcode) for label in labels] == [
        ('700000000000001', '0001234567890'), ('700000000000002', '0009876543210')]


def test_text_without_bars_retains_pdf_but_does_not_invent_sticker(label_db):
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(40, 600, 'Order number: 700000000000001')
    canvas.drawString(40, 550, '0010328640390')
    canvas.save()
    result = labels_store.save_pdf(1, 'a', buffer.getvalue())
    assert result['labels'] == 0 and result['warnings']
    assert labels_store.read_artifact(1, result['documentId'], original=True) == buffer.getvalue()


def test_original_deduplicated_scoped_and_xlsx_offline_barcode(label_db):
    pdf = label_pdf()
    saved = labels_store.save_pdf(1, "account-a", pdf)
    repeated = labels_store.save_pdf(1, "account-a", pdf)
    assert repeated["duplicate"] and repeated["documentId"] == saved["documentId"]
    assert labels_store.read_artifact(1, saved["documentId"], original=True) == pdf
    assert labels_store.read_artifact(2, saved["documentId"], original=True) is None
    row = AvitoOrderRow(orderId="api-id", marketplaceId="700000000000001", accountId="account-a", items=[AvitoOrderItem(itemId="001", title="Synthetic test", quantity=1)])
    other = row.model_copy(deep=True, update={"accountId": "account-b"})
    labels_store.enrich_labels([row, other], 1)
    assert row.stickerNumber == "0010328640390"
    assert row.stickerNumberState == "confirmed"
    assert other.stickerLabelId is None
    content = build_avito_orders_picking_xlsx([row], date_from=date.today(), image_loader=lambda _: None,
        label_loader=lambda label_id: labels_store.read_artifact(1, label_id))
    with ZipFile(BytesIO(content)) as archive:
        images = [archive.read(name) for name in archive.namelist() if name.startswith("xl/media/")]
        assert len(images) == 1
        assert zxingcpp.read_barcodes(Image.open(BytesIO(images[0])))[0].text == "0010328640390"
        assert b"0010328640390" in archive.read("xl/worksheets/sheet1.xml")
        assert b"<xdr:col>7</xdr:col>" in archive.read("xl/drawings/drawing1.xml")
        from xml.etree import ElementTree as ET
        ns = {'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}
        drawing = ET.fromstring(archive.read('xl/drawings/drawing1.xml'))
        extent = drawing.find('xdr:oneCellAnchor/xdr:ext', ns)
        width, height = int(extent.attrib['cx']), int(extent.attrib['cy'])
        original = Image.open(BytesIO(images[0]))
        assert width / height == pytest.approx(original.width / original.height, rel=1e-5)
        assert width <= 320 * 9525 and height <= 96 * 9525
        assert b'ht="104"' in archive.read('xl/worksheets/sheet1.xml')
        assert b'width="47"' in archive.read('xl/worksheets/sheet1.xml')
        assert b'srcRect' not in archive.read('xl/drawings/drawing1.xml')


def test_duplicate_order_alias_not_assigned(label_db):
    labels_store.save_pdf(1, "a", label_pdf())
    rows = [AvitoOrderRow(orderId=str(i), jobNumber="700000000000001", accountId="a") for i in range(2)]
    labels_store.enrich_labels(rows, 1)
    assert all(row.stickerLabelId is None for row in rows)


def test_versions_and_multiple_shipments_preserve_originals(label_db):
    original = labels_store.save_pdf(1, 'a', label_pdf())
    newer = labels_store.save_pdf(1, 'a', label_pdf((("700000000000001", "0009876543210"),)))
    row = AvitoOrderRow(orderId='api-1', jobNumber='700000000000001', accountId='a')
    labels_store.enrich_labels([row], 1)
    assert row.stickerNumber == '0009876543210'
    assert labels_store.read_artifact(1, original['documentId'], original=True)
    assert labels_store.read_artifact(1, newer['documentId'], original=True)
    labels_store.save_pdf(1, 'a', label_pdf((("700000000000001", "0009876543210"), ("700000000000001", "0009876543220"))))
    row = row.model_copy(update={'stickerNumber': None, 'stickerLabelId': None})
    labels_store.enrich_labels([row], 1)
    assert row.stickerNumberState == 'ambiguous'
    assert row.stickerLabelId is None


def test_import_and_protected_artifacts_http(label_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import avito_orders
    app = FastAPI()
    app.include_router(avito_orders.router)
    monkeypatch.setattr(avito_orders, '_resolve_extension_token_organization', lambda token: {'sat_avito_TEST_A': 1, 'sat_avito_TEST_B': 2}.get(token))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer sat_avito_TEST_A', 'Content-Type': 'application/pdf'}
    pdf = label_pdf()
    response = client.post('/api/v1/avito/orders/labels/import?accountId=a', headers=headers, content=pdf)
    assert response.status_code == 200
    assert response.json()['labels'] == 1
    doc_id = response.json()['documentId']
    row = AvitoOrderRow(orderId='api', accountId='a', jobNumber='700000000000001')
    labels_store.enrich_labels([row], 1)
    path = f'/api/v1/avito/orders/labels/{row.stickerLabelId}/barcode.png'
    png = client.get(path, headers=headers)
    assert png.status_code == 200
    assert zxingcpp.read_barcodes(Image.open(BytesIO(png.content)))[0].text == row.stickerNumber
    assert client.get(path, headers={'Authorization': 'Bearer sat_avito_TEST_B'}).status_code == 404
    assert client.get(path, headers={'Authorization': 'Bearer sat_avito_INVALID'}).status_code == 401
    original = client.get(f'/api/v1/avito/orders/label-documents/{doc_id}.pdf', headers=headers)
    assert original.content == pdf
    assert original.headers['cache-control'] == 'private, no-store'
    assert client.post('/api/v1/avito/orders/labels/import', headers=headers, content=b'<html>').status_code == 422
