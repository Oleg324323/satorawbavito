"""HTTP -> durable SQLite -> original PDF/photo -> downloadable offline XLSX.

Synthetic provider input; this is not a claim of a live Avito session test.
"""
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import zxingcpp
import pytest

from app.avito import labels_store, listing_photos
from app.avito.images_orm import AvitoListingPhoto
from app.avito.labels_orm import AvitoLabelDocument, AvitoTransportLabel
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.routers import avito_orders as orders, avito_listing_photos as photos
from tests.test_avito_labels import label_pdf


def test_snapshot_photos_multi_pdf_and_both_workbooks_persist_and_repeat(monkeypatch, tmp_path):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    for table in (WbRepricerSourceCacheRow.__table__, AvitoListingPhoto.__table__, AvitoLabelDocument.__table__, AvitoTransportLabel.__table__):
        table.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (store, labels_store, listing_photos):
        monkeypatch.setattr(module, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(store, '_redis_available', lambda: False)
    actor = SimpleNamespace(organization_id=1, user_id='qa')
    monkeypatch.setattr(orders, 'actor_from_request', lambda _: actor)
    monkeypatch.setattr(orders, 'has_permission', lambda *_: True)
    monkeypatch.setattr(orders, 'get_organization_avito_credentials_secret', lambda _: None)
    monkeypatch.setattr(orders, 'enrich_existing_return_candidates', lambda *_: None)
    monkeypatch.setattr(orders, '_saved_queue_for_export', lambda _: (actor, {}))
    app = FastAPI(); app.include_router(orders.router); app.include_router(photos.router)
    client = TestClient(app)
    snapshot = {'capturedAt': '2026-10-05T10:00:00Z', 'orders': [], 'returns': []}
    for index, section in enumerate(('orders', 'returns'), 1):
        snapshot[section].append({'orderId': f'70000000000000{index}', 'marketplaceId': f'70000000000000{index}',
            'accountId': 'qa-account', 'status': 'ready_to_ship' if section == 'orders' else 'on_return',
            'returnPickupPlace': 'Москва, ул. Тестовая, 2' if section == 'returns' else None,
            'returnPickupDeadline': '12.10.2026' if section == 'returns' else None,
            'returnPickupCode': '001234' if section == 'returns' else None,
            'shipmentNumber': f'001032864039{index}', 'shipmentNumberState': 'confirmed',
            'dropoffProvider': 'Яндекс Доставка' if section == 'orders' else None,
            'items': [{'itemId': str(index), 'lineIndex': 0, 'title': f'Test item {index}', 'quantity': 1,
                'size': 'M', 'color': 'black', 'imageUrl': f'https://b00.img.avito.st/{index}.jpg',
                'sources': {'size': 'order_detail', 'color': 'order_detail', 'imageUrl': 'order_detail'}}]})
    assert client.post('/api/v1/avito/orders/browser-snapshot', json=snapshot).status_code == 200
    context = client.get('/api/v1/avito/orders/labels/collection-context').json()
    # Only outbound orders need a new shipping label. Existing return labels
    # can still be imported and preserved without becoming collection targets.
    assert context['total'] == 1
    assert len(context['missing']) == 1
    assert context['missing'][0]['orderId'] == '700000000000001'
    assert context['missing'][0]['shipmentNumber'] == '0010328640391'
    photo = BytesIO(); Image.new('RGB', (200, 200), 'navy').save(photo, 'JPEG')
    for index in (1, 2):
        url = f'/api/v1/avito/repricer/photos/import?accountId=qa-account&itemId={index}'
        assert client.post(url, content=photo.getvalue(), headers={'Content-Type': 'image/jpeg'}).status_code == 200
    assert client.get('/api/v1/avito/repricer/photos/orders/collection-context').json()['missing'] == []
    pdf = label_pdf((('700000000000001', '0010328640391'), ('700000000000002', '0010328640392')), same_page=True)
    imported = client.post('/api/v1/avito/orders/labels/import?accountId=qa-account', content=pdf, headers={'Content-Type': 'application/pdf'})
    assert imported.status_code == 200
    assert imported.json()['labels'] == 2 and not imported.json()['warnings']
    assert client.get('/api/v1/avito/orders/labels/collection-context').json()['missing'] == []
    assert client.post('/api/v1/avito/orders/labels/import?accountId=qa-account', content=pdf, headers={'Content-Type': 'application/pdf'}).json()['duplicate']
    monkeypatch.setattr(orders, '_load_image', lambda *_: (_ for _ in ()).throw(AssertionError('Saved images must not use CDN')))
    for kind in ('picking', 'returns'):
        response = client.get(f'/api/v1/avito/orders/{kind}-list.xlsx')
        assert response.status_code == 200
        (tmp_path / f'{kind}.xlsx').write_bytes(response.content)
        with ZipFile(BytesIO(response.content)) as book:
            sheet = book.read('xl/worksheets/sheet1.xml').decode()
            assert 'Test item' in sheet and 'Фото не получено' not in sheet and 'Этикетка не получена' not in sheet
            if kind == 'picking':
                assert 'Пункт приема' in sheet and 'Яндекс Доставка' in sheet
            media = [name for name in book.namelist() if name.startswith('xl/media/')]
            assert len(media) == (2 if kind == 'picking' else 1)
            codes = [code.text for name in media for code in zxingcpp.read_barcodes(Image.open(BytesIO(book.read(name))))]
            assert codes == (['0010328640391'] if kind == 'picking' else [])
            if kind == 'returns':
                assert 'Стикер' not in sheet and 'Номер отправления' not in sheet
                assert 'Москва, ул. Тестовая, 2' in sheet and '12.10.2026' in sheet and '001234' in sheet
    engine.dispose()


@pytest.mark.parametrize('reply,question,expected', [
    ('Мне нужен 48', 'Здравствуйте! Какой размер вам нужен?', '48'),
    ('М оформляю', 'Здравствуйте! Посадка оверсайз', 'M'),
    ('Лучше M вместо S', 'Здравствуйте! Посадка оверсайз', 'M'),
    ('Здравствуйте 50-52', 'Укажите пожалуйста размер который нужен', '50-52'),
    ('L есть?', 'Здравствуйте! 180 грамм плотность. Дополнительных фото нет', 'L'),
    ('можно xl', 'Выберите подходящий размер', 'XL'),
    ('Хорошо, тогда размер L будет', 'Пропишите нужный размер', 'L'),
])
def test_bound_chat_rules_http_persistence_orders_and_xlsx(monkeypatch, reply, question, expected):
    from app.avito import returns_store
    from app.avito.returns_orm import AvitoReturnItemRow, AvitoReturnInventoryEventRow
    from app.avito.orders_ai import enrich_avito_orders_snapshot_with_ai
    from tests.test_avito_chat_size import snapshot, client as ai_client
    from xml.etree import ElementTree as ET
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    for table in (WbRepricerSourceCacheRow.__table__, AvitoListingPhoto.__table__, AvitoLabelDocument.__table__, AvitoTransportLabel.__table__, AvitoReturnItemRow.__table__, AvitoReturnInventoryEventRow.__table__):
        table.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (store, labels_store, listing_photos, returns_store):
        monkeypatch.setattr(module, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(store, '_redis_available', lambda: False)
    actor = SimpleNamespace(organization_id=19, user_id='synthetic')
    monkeypatch.setattr(orders, 'actor_from_request', lambda _: actor)
    monkeypatch.setattr(orders, 'has_permission', lambda *_: True)
    monkeypatch.setattr(orders, 'get_organization_avito_credentials_secret', lambda _: None)
    monkeypatch.setattr(orders, '_saved_queue_for_export', lambda _: (actor, {}))
    monkeypatch.setattr('app.avito.orders_ai.get_settings', lambda: pytest.fail('Size parsing accessed AI settings'))
    provider = ai_client(expected)
    monkeypatch.setattr(orders, 'enrich_avito_orders_snapshot_with_ai', lambda data, **kwargs: enrich_avito_orders_snapshot_with_ai(data, client=provider, **kwargs))
    app = FastAPI(); app.include_router(orders.router); http = TestClient(app)
    data = snapshot(reply).model_dump(mode='json'); data['capturedAt'] = '2026-10-07T10:10:00Z'
    order = data['orders'][0]
    order.update(orderId='70000000532427280', marketplaceId='70000000532427280', dropoffProvider='Яндекс Доставка')
    order['items'][0].update(itemId='7330973699', descriptionSize='46 (S)')
    # Exercise the actual extension chat loader/role mapper with synthetic Avito
    # responses before sending its evidence through the real ingestion route.
    import json, subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    item = data['orders'][0]['items'][0]
    evidence = item['chatEvidence']
    evidence.update(orderId=order['orderId'], itemId=item['itemId'])
    evidence['messages'][0]['text'] = question
    raw_messages = [{**message, 'authorId': evidence['sellerId'] if message['role'] == 'seller' else evidence['buyerId']} for message in evidence['messages']]
    # Native Avito timestamps and author hashes; these are synthetic messages,
    # not a re-read or replay of the customer's private conversation.
    for index, message in enumerate(raw_messages):
        message.pop('createdAt')
        message['createdAt'] = 1791315250794337500 + index * 60_000_000_000
    script = """import {readFileSync} from 'node:fs';
      await import('./avito-orders-extension/src/order-chat.js');
      const input=JSON.parse(readFileSync(0,'utf8'));
      const result=await globalThis.SatornaAvitoOrderChat.loadOrderChat([input.binding.channelId],
        async()=>({ok:true,status:200,json:async()=>({messages:input.messages})}),input.binding);
      process.stdout.write(JSON.stringify(result.chatEvidence));"""
    result = subprocess.run(['node', '--max-old-space-size=128', '--input-type=module', '-e', script], cwd=root,
        input=json.dumps({'binding': {key: value for key, value in evidence.items() if key != 'messages'}, 'messages': raw_messages}),
        capture_output=True, text=True, check=True)
    item['chatEvidence'] = json.loads(result.stdout)
    response = http.post('/api/v1/avito/orders/browser-snapshot', json=data)
    assert response.status_code == 200 and response.json()['browserSnapshot']['aiExtraction']['confirmedSizeCount'] == 1
    persisted = orders._browser_snapshot_from_cache(19)
    item = persisted.orders[0].items[0]
    assert item.size == expected and item.sizeState == 'confirmed' and item.sizeEvidence['reply'] == reply
    assert item.itemId == '7330973699' and persisted.orders[0].orderId == '70000000532427280'
    rows = orders._browser_snapshot_rows(persisted, statuses=['ready_to_ship'])
    normalized = rows[0]
    assert normalized.items[0].size == expected and normalized.items[0].sizeState == 'confirmed'
    assert normalized.dropoffProvider == 'Яндекс Доставка'
    from datetime import datetime, timezone
    monkeypatch.setattr(orders, '_orders_client_for_request', lambda _: (actor, None, None))
    monkeypatch.setattr(orders, '_read_queue_cache', lambda *_: ('synthetic', {
        'complete': True, 'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat(), 'rows': [],
    }))
    monkeypatch.setattr(orders, '_enrich_orders_with_return_matches', lambda *_args, **_kwargs: None)
    queue_response = http.get('/api/v1/avito/orders/queue?mode=active')
    assert queue_response.status_code == 200, queue_response.json()
    queue = queue_response.json()
    assert len(queue['rows']) == 1
    assert queue['rows'][0]['orderId'] == '70000000532427280'
    assert queue['rows'][0]['items'][0]['itemId'] == '7330973699'
    assert queue['rows'][0]['items'][0]['size'] == expected
    assert queue['rows'][0]['dropoffProvider'] == 'Яндекс Доставка'
    response = http.get('/api/v1/avito/orders/picking-list.xlsx')
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as book:
        sheet = ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        cells = {cell.get('r'): ''.join(cell.itertext()) for cell in sheet.findall('.//s:c', ns)}
        assert cells['D1'] == 'Размер' and cells['D2'] == expected
        assert cells['I1'] == 'Номер заказа' and cells['I2'] == '70000000532427280'
        assert cells['J1'] == 'ID товара Авито' and cells['J2'] == '7330973699'
        assert cells['K1'] == 'Пункт приема' and cells['K2'] == 'Яндекс Доставка'
        assert cells['G1'] == 'Номер отправления' and cells['H1'] == 'Стикер'
    # Export must not collect chat or call AI; a second org cannot read this proof.
    assert not provider.posts and orders._browser_snapshot_from_cache(20) is None
    for mode in ('none', 'description', None):
        forged = snapshot().model_dump(mode='json')
        forged['collector']['options']['sizeMode'] = mode
        fake = forged['orders'][0]['items'][0]
        fake.update(sizeMode='chat_ai', size='XL', sizeState='confirmed', sizeEvidence={'state': 'confirmed', 'method': 'chat_ai'}, sources={'size': 'chat_ai'})
        assert http.post('/api/v1/avito/orders/browser-snapshot', json=forged).status_code == 200
        saved = orders._browser_snapshot_from_cache(19).orders[0].items[0]
        assert saved.size is None and saved.sizeState != 'confirmed'
    engine.dispose()
