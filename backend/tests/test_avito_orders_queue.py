from app.avito.orders import AvitoOrderItem, AvitoOrderRow, AvitoOrdersBrowserOrder, AvitoOrdersBrowserSnapshot, AvitoOrdersFetchResult, LiveAvitoOrdersClient
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi import BackgroundTasks, Request

from app.avito.orders_picking_xlsx import build_avito_orders_picking_xlsx
from app.avito.orders_queue import fetch_full_queue, merge_queue_rows, select_queue
from app.routers import avito_orders
import pytest


@pytest.fixture(autouse=True)
def isolated_label_and_photo_store(monkeypatch):
    """Queue unit tests don't depend on a developer's database or credentials."""
    from app.avito import listing_photos
    monkeypatch.setattr(listing_photos, "photo_index", lambda _: {})
    monkeypatch.setattr(avito_orders, "enrich_labels", lambda *_: None)


def order(order_id: str, status: str, updated: str = "2026-09-30T10:00:00+00:00") -> AvitoOrderRow:
    return AvitoOrderRow(
        orderId=order_id, accountId="account-1", status=status, createdAt="2026-07-01T00:00:00+00:00",
        statusSource="avito_api", statusObservedAt=updated, sourceStatus="fresh",
        items=[AvitoOrderItem(itemId="item-1", title="Товар", size="M", color="чёрный")],
    )

def test_all_active_includes_returns_pending_confirmation_not_history():
    rows = [order(str(index), status) for index, status in enumerate(['ready_to_ship', 'in_transit', 'on_return', 'closed', 'canceled', 'on_confirmation', 'in_dispute'])]
    assert [row.status for row in select_queue(rows, 'active')] == ['ready_to_ship', 'in_transit', 'on_return']
    assert select_queue(rows, 'returns') == [rows[2]]
    rows[2].returnStatus = 'received'
    assert select_queue(rows, 'returns') == []
    assert rows[2] not in select_queue(rows, 'active')


@pytest.mark.parametrize('api_status,browser_status,observed,expected', [
    ('ready_to_ship', 'on_return', '2026-10-05T10:00:00Z', 'on_return'),
    ('in_transit', 'ready_to_ship', '2026-10-05T10:00:00Z', 'ready_to_ship'),
    ('on_return', 'ready_to_ship', '2026-09-29T10:00:00Z', 'on_return'),
    ('ready_to_ship', None, '2026-10-05T10:00:00Z', 'unknown'),
    ('ready_to_ship', 'on_return', None, 'unknown'),
])
def test_export_reconciles_observed_status_not_stale_api(api_status, browser_status, observed, expected, monkeypatch):
    cached = {'rows': [order('1', api_status).model_dump()]}
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt=observed, orders=[
        AvitoOrdersBrowserOrder(orderId='1', accountId='account-1', status=browser_status)])
    monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda _: (SimpleNamespace(organization_id=1), cached))
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: snapshot)
    exported = []
    monkeypatch.setattr(avito_orders, 'build_avito_orders_picking_xlsx', lambda rows, **kwargs: exported.append([r.orderId for r in rows]) or b'xlsx')
    for mode in ('ready_to_ship', 'returns'):
        avito_orders._queue_xlsx_response(Request({'type': 'http', 'headers': []}), mode=mode, account_id=None)
    assert exported == [['1'] if expected == 'ready_to_ship' else [], ['1'] if expected == 'on_return' else []]


def test_unknown_browser_status_does_not_default_to_ready():
    snapshot = AvitoOrdersBrowserSnapshot(orders=[AvitoOrdersBrowserOrder(orderId='1')])
    assert avito_orders._browser_snapshot_rows(snapshot, statuses=['ready_to_ship']) == []

@pytest.mark.parametrize('phase', ['received', 'completed', 'closed'])
@pytest.mark.parametrize('observed,newer', [('2026-10-08T12:00:00Z', True), ('2026-09-29T12:00:00Z', False)])
def test_terminal_browser_return_reconciliation_respects_observation_time(phase, observed, newer):
    row = order('returned', 'on_return')
    row.returnStatus = 'ready_for_pickup'
    row.returnStatusSource = 'avito_api'
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt=observed, returns=[
        AvitoOrdersBrowserOrder(orderId='returned', accountId='account-1', status='on_return', returnStatus=phase)])
    assert avito_orders._browser_snapshot_rows(snapshot, statuses=['on_return']) == []
    evidence = avito_orders._browser_snapshot_rows(snapshot, statuses=[])
    assert evidence[0].status == 'closed' and evidence[0].returnStatus == phase
    avito_orders._reconcile_export_statuses([row], evidence, None)
    assert row.status == ('closed' if newer else 'on_return')
    assert bool(select_queue([row], 'active')) is not newer
    assert bool(select_queue([row], 'returns')) is not newer
    assert bool(select_queue([row], 'history')) is newer
    assert snapshot.returns[0].status == 'on_return'

def test_queue_adds_browser_only_active_orders_reconciles_terminal_and_isolates_accounts(monkeypatch):
    actor = SimpleNamespace(organization_id=1)
    cached = {'complete': True, 'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat(),
              'rows': [order('finished', 'ready_to_ship').model_dump(mode='json'),
                       order('same-id', 'in_transit').model_dump(mode='json')]}
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt='2026-10-08T12:00:00Z', orders=[
        AvitoOrdersBrowserOrder(orderId=key, accountId=account, status=status)
        for key, account, status in [('browser-ready', 'account-1', 'ready_to_ship'),
                                     ('buyer-pickup', 'account-1', 'in_transit'),
                                     ('active-return', 'account-1', 'on_return'),
                                     ('finished', 'account-1', 'delivered'),
                                     ('unknown', 'account-1', 'unknown'),
                                     ('same-id', 'account-2', 'on_return')]])
    monkeypatch.setattr(avito_orders, '_orders_client_for_request', lambda _: (actor, None, None))
    monkeypatch.setattr(avito_orders, '_read_queue_cache', lambda *_: ('synthetic', cached))
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: snapshot)
    monkeypatch.setattr(avito_orders, '_enrich_orders_with_return_matches', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(avito_orders, '_pickup_unreceived', lambda rows, *_: rows)
    result = avito_orders.get_avito_orders_queue(Request({'type': 'http', 'headers': []}), BackgroundTasks(),
        mode='active', account_id='account-1', search='', history_from=None, page=1, limit=50, force_refresh=False)
    assert {row['orderId'] for row in result['rows']} == {'same-id', 'browser-ready', 'buyer-pickup', 'active-return'}
    assert next(row['status'] for row in result['rows'] if row['orderId'] == 'same-id') == 'in_transit'
    assert result['summary']['total'] == 4
    assert result['summary']['modeCounts']['history'] == 1


def test_returns_xlsx_has_only_return_columns_and_exact_identifiers():
    from xml.etree import ElementTree as ET
    row = order('00012345678901234567', 'on_return')
    row.marketplaceId = '00012345678901234567'
    row.items[0].itemId = '000987654'
    row.returnPickupPlace = 'Москва, ул. Тестовая, 2'
    row.returnPickupDeadline = '12.10.2026'
    row.returnPickupCode = '001234'
    row.shipmentNumber = 'unrelated-shipment'
    row.stickerLabelId = 99
    row.stickerNumberState = 'confirmed'
    def forbidden_label(_):
        pytest.fail('Returns must not read or embed labels')
    content = build_avito_orders_picking_xlsx([row], date_from=date.today(), returns=True,
        image_loader=lambda _: None, label_loader=forbidden_label)
    with ZipFile(BytesIO(content)) as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        headers = [el.text for el in sheet.findall('.//s:row[@r="1"]//s:t', ns)]
        assert len(headers) == 11
        assert 'Стикер' not in headers and 'Номер отправления' not in headers
        assert headers[6:] == ['Номер заказа', 'ID товара Авито', 'Место получения возврата', 'Срок получения', 'Код возврата']
        for ref, expected in [('G2', row.marketplaceId), ('H2', '000987654'), ('I2', row.returnPickupPlace), ('J2', '12.10.2026'), ('K2', '001234')]:
            cell = sheet.find(f'.//s:c[@r="{ref}"]', ns)
            assert cell.attrib['t'] == 'inlineStr' and cell.find('.//s:t', ns).text == expected
        assert sheet.find('s:autoFilter', ns).attrib['ref'] == 'A1:K2'
    regular = build_avito_orders_picking_xlsx([row], date_from=date.today(), image_loader=lambda _: None)
    with ZipFile(BytesIO(regular)) as archive:
        text = archive.read('xl/worksheets/sheet1.xml').decode()
        assert 'Стикер' in text and 'Номер отправления' in text


def test_export_status_does_not_cross_accounts():
    rows = [order('1', 'ready_to_ship')]
    browser = order('1', 'on_return', '2026-10-05T10:00:00Z')
    browser.accountId = 'other-account'
    avito_orders._reconcile_export_statuses(rows, [browser], None)
    assert rows[0].status == 'ready_to_ship' and len(rows) == 2

def test_reconcile_account_identity_requires_one_exact_account():
    api = order('api', 'ready_to_ship')
    api.accountId = None
    api.marketplaceId = '70000000532427280'
    browser = order('70000000532427280', 'ready_to_ship', '2026-10-08T10:00:00Z')
    avito_orders._reconcile_export_statuses([api], [browser], None)
    assert api.accountId == 'account-1'
    api.accountId = None
    other = browser.model_copy(update={'accountId': 'account-2'})
    avito_orders._reconcile_export_statuses([api], [browser, other], None)
    assert api.accountId is None

def test_label_collection_targets_only_outbound_ready_orders(monkeypatch):
    snapshot = AvitoOrdersBrowserSnapshot(orders=[
        AvitoOrdersBrowserOrder(orderId=status, accountId='account-1', status=status)
        for status in ['ready_to_ship', 'in_transit', 'on_return', 'closed']],
        returns=[AvitoOrdersBrowserOrder(orderId='return', accountId='account-1', status='on_return')])
    monkeypatch.setattr(avito_orders, '_label_organization', lambda _: 1)
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: snapshot)
    monkeypatch.setattr(avito_orders, 'get_source_cache', lambda *a, **kw: None)
    result = avito_orders.get_label_collection_context(Request({'type': 'http', 'headers': []}))
    assert result['total'] == 1
    assert [row['orderId'] for row in result['missing']] == ['ready_to_ship']


def test_returns_export_distinguishes_collection_failure_and_ambiguous_source():
    row = order('1', 'on_return')
    row.returnFieldStates = {'returnPickupPlace': 'unavailable', 'returnPickupDeadline': 'collection_failed', 'returnPickupCode': 'ambiguous'}
    content = build_avito_orders_picking_xlsx([row], date_from=date.today(), returns=True, image_loader=lambda _: None)
    with ZipFile(BytesIO(content)) as archive:
        text = archive.read('xl/worksheets/sheet1.xml').decode()
        assert 'Не найдено в деталях возврата' in text
        assert 'Ошибка сбора: повторите сбор возвратов' in text
        assert 'Неоднозначные данные: нужна проверка' in text


def test_export_cache_is_credential_scoped_and_never_requests_oauth(monkeypatch):
    from app.cabinet.store import AvitoCredentialsSecret
    actor = SimpleNamespace(organization_id=7, user_id='test-user')
    credentials = AvitoCredentialsSecret(client_id='synthetic-client', client_secret='synthetic-secret', cached_access_token=None, access_token_expires_at=None)
    monkeypatch.setattr(avito_orders, '_orders_credentials_for_request', lambda _: (actor, credentials))
    def forbidden(*args, **kwargs):
        raise AssertionError('Export must not request provider sync/OAuth')
    monkeypatch.setattr(avito_orders, 'resolve_user_avito_access_token', forbidden)
    monkeypatch.setattr(avito_orders, '_load_orders_queue', forbidden)
    expected = avito_orders.scoped_avito_cache_key(avito_orders.AVITO_ORDERS_QUEUE_KEY, 'synthetic-client\0synthetic-secret')
    reads = []
    def read(org, key, **kwargs):
        reads.append((org, key))
        return {'rows': [], 'complete': True}
    monkeypatch.setattr(avito_orders, 'get_source_cache', read)
    assert avito_orders._saved_queue_for_export(Request({'type': 'http', 'headers': []}))[1]['complete']
    assert reads == [(7, expected)]


def test_saved_export_works_during_refresh_outage_and_with_old_statuses(monkeypatch):
    rows = [order('waiting', 'ready_to_ship'), order('finished', 'delivered'), order('moving', 'in_transit')]
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: None)
    request = Request({'type': 'http', 'headers': []})
    for complete, timestamp in [(False, datetime.now(timezone.utc).isoformat()), (True, '2020-01-01T00:00:00+00:00'), (False, None)]:
        cached = {'rows': [row.model_dump(mode='json') for row in rows], 'complete': complete,
                  'lastSuccessfulRefresh': timestamp, 'error': {'code': 'rate_limited'}}
        monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda _: (SimpleNamespace(organization_id=1), cached))
        response = avito_orders._queue_xlsx_response(request, mode='ready_to_ship', account_id='account-1')
        assert response.status_code == 200
        with ZipFile(BytesIO(response.body)) as book:
            sheet = book.read('xl/worksheets/sheet1.xml').decode()
            assert 'актуальность статусов не подтверждена' not in sheet
            assert '<dimension ref="A1:K2"' in sheet
            assert 'Снимок:' not in sheet and 'Заказов:' not in sheet
        assert response.headers['content-disposition'].endswith('.xlsx"')


def test_export_without_any_saved_snapshot_is_explicit_error(monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda _: (SimpleNamespace(organization_id=1), {}))
    with pytest.raises(HTTPException) as exc:
        avito_orders._queue_xlsx_response(Request({'type': 'http', 'headers': []}), mode='ready_to_ship', account_id=None)
    assert exc.value.status_code == 404


def test_export_embeds_saved_order_photo_without_refetching_cdn(monkeypatch):
    from PIL import Image
    from app.avito import listing_photos

    row = order('saved-photo-order', 'ready_to_ship')
    row.items[0].imageUrl = 'https://70.img.avito.st/expired.jpg'
    cached = {'rows': [row.model_dump(mode='json')], 'complete': True}
    photo = BytesIO()
    Image.new('RGB', (60, 60), 'red').save(photo, format='JPEG')
    monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda _: (SimpleNamespace(organization_id=1), cached))
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: None)
    monkeypatch.setattr(avito_orders, 'enrich_labels', lambda *_: None)
    monkeypatch.setattr(listing_photos, 'photo_index', lambda _: {('account-1', 'item-1'): 17})
    monkeypatch.setattr(listing_photos, 'read_photo', lambda _org, _id: photo.getvalue())
    monkeypatch.setattr(avito_orders, '_load_image', lambda _: (_ for _ in ()).throw(AssertionError('CDN must not be fetched')))

    response = avito_orders._queue_xlsx_response(Request({'type': 'http', 'headers': []}), mode='ready_to_ship', account_id='account-1')
    with ZipFile(BytesIO(response.body)) as book:
        assert any(name.startswith('xl/media/image') for name in book.namelist())
        assert 'Фото не получено' not in book.read('xl/worksheets/sheet1.xml').decode()


def test_export_photo_fetching_has_one_shared_deadline():
    from threading import Event
    from time import monotonic
    from app.avito.orders_picking_xlsx import _prefetch_images
    release = Event()
    def slow(_url):
        release.wait(2)
        return None
    start = monotonic()
    try:
        assert _prefetch_images(['slow-a', 'slow-b'], slow, budget_seconds=0.01) == {}
        assert monotonic() - start < 0.5
    finally:
        release.set()


class Pages:
    def __init__(self, pages, total=None, by_id=None):
        self.pages = pages
        self.total = total
        self.by_id = by_id or {}
        self.requests = []

    def fetch_orders(self, request):
        self.requests.append(request)
        if request.ids:
            rows = [self.by_id[key] for key in request.ids if key in self.by_id]
            return AvitoOrdersFetchResult(status="synced", orders=rows, total=len(rows))
        value = self.pages[request.page - 1]
        return value if isinstance(value, AvitoOrdersFetchResult) else AvitoOrdersFetchResult(status="synced", orders=value, total=self.total or sum(len(page) for page in self.pages if isinstance(page, list)))


@pytest.mark.parametrize("blocked", [False, True])
def test_live_queue_reuses_and_closes_one_http_session_for_pages_and_exact_lookup(monkeypatch, blocked):
    import httpx
    sessions, requests = [], []
    def handler(request):
        requests.append(dict(request.url.params))
        if request.url.params.get("ids"):
            rows = [{"id": "old", "status": "closed"}]
        elif request.url.params["page"] == "1":
            rows = [{"id": f"new-{i}", "status": "ready_to_ship"} for i in range(20)]
        elif blocked:
            return httpx.Response(403, json={"error": "forbidden"})
        else:
            rows = [{"id": "last", "status": "ready_to_ship"}]
        return httpx.Response(200, json={"orders": rows, "total": 21})
    real_client = httpx.Client
    class Session(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)
            sessions.append(self)
    monkeypatch.setattr(httpx, "Client", Session)
    previous = [order("old", "ready_to_ship")]
    rows, complete, error = fetch_full_queue(LiveAvitoOrdersClient("test-token"), previous=previous, snapshot=None)
    assert len(sessions) == 1 and sessions[0].is_closed
    if blocked:
        assert not complete and error["code"] == "forbidden_scope" and rows == previous
        assert len(requests) == 2
    else:
        assert complete and error is None and len(rows) == 22
        assert requests[-1]["ids"] == "old"


def test_queue_fetches_old_active_order_across_pages_and_moves_status_without_duplicate():
    first = [order(f"order-{index}", "ready_to_ship") for index in range(20)]
    second = [order("old-order", "in_transit")]
    client = Pages([first, second])
    rows, complete, error = fetch_full_queue(client, previous=[order("old-order", "ready_to_ship", "2026-09-29T10:00:00+00:00")], snapshot=None)
    assert complete and error is None
    assert len(rows) == 21
    assert len(client.requests) == 2
    assert all(request.dateFrom is None for request in client.requests)
    assert "old-order" not in {row.orderId for row in select_queue(rows, "ready_to_ship")}
    assert "old-order" in {row.orderId for row in select_queue(rows, "in_transit")}


def test_stale_snapshot_cannot_restore_old_status_or_shipment_number():
    prior = order("order-1", "in_transit")
    prior.shipmentNumber = "009 888 7777"
    prior.shipmentNumberObservedAt = "2026-09-30T11:00:00+00:00"
    incoming = order("order-1", "ready_to_ship", "2026-09-29T10:00:00+00:00")
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt="2026-09-29T09:00:00+00:00", orders=[
        AvitoOrdersBrowserOrder(orderId="order-1", accountId="account-1", status="ready_to_ship", shipmentNumber="001 111 2222"),
    ])
    merged = merge_queue_rows([prior], [incoming], snapshot)
    assert merged[0].status == "in_transit"
    assert merged[0].shipmentNumber == "009 888 7777"


def test_current_listing_cannot_replace_ordered_variant_or_duplicate_item_id():
    prior = order("order-1", "ready_to_ship")
    prior.items = [
        AvitoOrderItem(itemId="item-1", lineIndex=0, title="Худи", size="M", color="чёрный"),
        AvitoOrderItem(itemId="item-1", lineIndex=1, title="Худи", size="L", color="белый"),
    ]
    current = order("order-1", "ready_to_ship")
    current.items = [item.model_copy(deep=True) for item in prior.items]
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt="2026-09-30T12:00:00Z", orders=[
        AvitoOrdersBrowserOrder(orderId="order-1", accountId="account-1", items=[
            {"itemId": "item-1", "size": "XL", "color": "красный", "imageUrl": "https://70.img.avito.st/new.jpg", "sources": {"size": "description", "color": "listing", "imageUrl": "listing"}},
        ]),
    ])
    rows = merge_queue_rows([prior], [current], snapshot)
    assert [(item.size, item.color, item.imageUrl) for item in rows[0].items] == [("M", "чёрный", None), ("L", "белый", None)]


def test_multiple_shipments_keep_previous_number_only_as_history():
    prior = order("order-1", "ready_to_ship")
    prior.shipmentNumber = "001 222 3333"
    prior.shipmentNumberState = "confirmed"
    incoming = LiveAvitoOrdersClient("test-token")._order_row({
        "id": "order-1", "accountId": "account-1", "status": "ready_to_ship",
        "shipments": [{"id": "first"}, {"id": "second"}],
    })
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt="2026-09-30T13:00:00Z", orders=[
        AvitoOrdersBrowserOrder(orderId="order-1", accountId="account-1", shipmentNumber="999 888 7777"),
    ])
    row = merge_queue_rows([prior], [incoming], snapshot)[0]
    assert row.shipmentNumber is None and row.shipmentNumberState == "ambiguous"
    assert row.shipmentNumberHistory[0]["number"] == "001 222 3333"


def test_incomplete_fetch_keeps_previous_rows_and_blocks_refresh():
    blocked = AvitoOrdersFetchResult(status="blocked", error={"code": "rate_limited", "message": "429", "retryable": True})
    rows, complete, error = fetch_full_queue(Pages([[order(f"new-{index}", "ready_to_ship") for index in range(20)], blocked], total=21), previous=[order("old", "ready_to_ship")], snapshot=None)
    assert not complete and error is not None
    assert [row.orderId for row in rows] == ["old"]


def test_known_active_order_is_rechecked_by_id_when_list_omits_it():
    old = order("old", "ready_to_ship", "2026-09-29T10:00:00+00:00")
    client = Pages([[]], by_id={"old": order("old", "in_transit")})
    rows, complete, error = fetch_full_queue(client, previous=[old], snapshot=None)
    assert complete and error is None
    assert client.requests[-1].ids == ["old"]
    assert [row.orderId for row in select_queue(rows, "in_transit")] == ["old"]
    assert select_queue(rows, "ready_to_ship") == []


@pytest.mark.parametrize('latest_status,mode', [('delivered', 'active'), ('closed', 'history')])
def test_unclosed_api_delivery_is_rechecked_when_missing_from_list(latest_status, mode):
    old = order('synthetic-pickup', 'delivered', '2026-09-29T10:00:00Z')
    client = Pages([[]], by_id={'synthetic-pickup': order('synthetic-pickup', latest_status)})
    rows, complete, error = fetch_full_queue(client, previous=[old], snapshot=None)
    assert complete and error is None
    assert client.requests[-1].ids == ['synthetic-pickup']
    assert [row.orderId for row in select_queue(rows, mode)] == ['synthetic-pickup']
    assert select_queue(rows, 'ready_to_ship') == []


def test_unverified_old_active_order_blocks_working_export():
    old = order("old", "ready_to_ship")
    rows, complete, error = fetch_full_queue(Pages([[]]), previous=[old], snapshot=None)
    assert not complete and error["code"] == "active_orders_unverified"
    assert rows == [old]


def test_unknown_and_return_phase_never_enter_working_exports():
    unknown = order("unknown", "mystery")
    returned = order("return", "on_return")
    returned.returnStatus = "started"
    pickup = order("pickup", "on_return")
    pickup.returnStatus = "ready_for_pickup"
    rows = merge_queue_rows([], [unknown, returned, pickup], None)
    assert [row.orderId for row in select_queue(rows, "return_pickup")] == ["pickup"]
    assert [row.orderId for row in select_queue(rows, "return_inbound")] == ["return"]
    assert [row.orderId for row in select_queue(rows, "review")] == ["unknown"]


def test_xlsx_preserves_shipment_text_and_embeds_safe_image():
    row = order("order-1", "ready_to_ship")
    row.shipmentNumber = "001 526 8946"
    row.items[0].imageUrl = "https://70.img.avito.st/photo.png"
    import base64
    image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGNcAAAAASUVORK5CYII=')
    content = build_avito_orders_picking_xlsx(
        [row], date_from=date(2026, 9, 30), as_of="2026-09-30T10:00:00Z",
        image_loader=lambda _url: (image, "png"),
    )
    with ZipFile(BytesIO(content)) as book:
        sheet = book.read("xl/worksheets/sheet1.xml").decode()
        assert 'r="G2" s="2" t="inlineStr"' in sheet
        assert "001 526 8946" in sheet
        assert "<drawing r:id=\"rId1\"/>" in sheet
        assert book.read("xl/media/image1.png") == image


def test_stale_queue_returns_existing_rows_and_refreshes_in_background(monkeypatch):
    cached = {
        "rows": [order("old", "ready_to_ship").model_dump(mode="json")],
        "complete": True,
        "lastSuccessfulRefresh": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }
    monkeypatch.setattr(avito_orders, "_orders_client_for_request", lambda _request: (SimpleNamespace(organization_id=1), object(), "test-token"))
    monkeypatch.setattr(avito_orders, "get_source_cache", lambda *_args, **_kwargs: cached)
    monkeypatch.setattr(avito_orders, "_enrich_orders_with_return_matches", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(avito_orders, "_pickup_unreceived", lambda rows, *_args: rows)
    monkeypatch.setattr(avito_orders, "_browser_snapshot_from_cache", lambda *_args: None)
    tasks = BackgroundTasks()
    response = avito_orders.get_avito_orders_queue(
        Request({"type": "http", "headers": []}), tasks,
        mode="active", account_id=None, search="", history_from=None, page=1, limit=50, force_refresh=False,
    )
    assert [row["orderId"] for row in response["rows"]] == ["old"]
    assert response["source"]["error"]["code"] == "refresh_in_progress"
    assert response["source"]["complete"] is False
    assert len(tasks.tasks) == 1


def test_api_delivered_is_active_until_closed_but_confirmed_buyer_handover_is_history():
    rows = [order('shipped', 'in_transit'), order('awaiting-buyer', 'delivered'),
            order('received', 'delivered'), order('closed', 'closed'), order('canceled', 'canceled')]
    rows[2].statusSource = 'avito_browser'
    assert [row.orderId for row in select_queue(rows, 'active')] == ['shipped', 'awaiting-buyer']
    assert [row.orderId for row in select_queue(rows, 'history')] == ['received', 'closed', 'canceled']
    assert select_queue([rows[1]], 'ready_to_ship') == []
    assert rows[1].status == 'delivered'  # filtering never changes marketplace facts


def test_api_delivered_buyer_pickup_is_visible_using_marketplace_alias_and_older_browser_snapshot(monkeypatch):
    row = order('synthetic-api-order', 'delivered', updated='2026-10-13T16:00:00Z')
    row.marketplaceId = 'synthetic-marketplace-order'
    row.accountId = None
    cached = {'complete': True, 'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat(),
              'rows': [row.model_dump(mode='json')]}
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt='2026-10-08T16:00:00Z', orders=[
        AvitoOrdersBrowserOrder(orderId='synthetic-marketplace-order', marketplaceId='synthetic-marketplace-order',
                               accountId='account-1', status='in_transit')])
    monkeypatch.setattr(avito_orders, '_orders_client_for_request', lambda _: (SimpleNamespace(organization_id=1), None, None))
    monkeypatch.setattr(avito_orders, '_read_queue_cache', lambda *_: ('synthetic', cached))
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: snapshot)
    monkeypatch.setattr(avito_orders, '_enrich_orders_with_return_matches', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(avito_orders, '_pickup_unreceived', lambda rows, *_: rows)
    result = avito_orders.get_avito_orders_queue(Request({'type': 'http', 'headers': []}), BackgroundTasks(),
        mode='active', account_id='account-1', search='', history_from=None, page=1, limit=50, force_refresh=False)
    assert [item['orderId'] for item in result['rows']] == ['synthetic-api-order']
    assert result['summary']['modeCounts']['active'] == 1
    assert result['summary']['modeCounts']['history'] == 0
    assert result['summary']['modeCounts']['ready_to_ship'] == 0
    assert cached['rows'][0]['status'] == 'delivered' and cached['rows'][0]['accountId'] is None


@pytest.mark.parametrize('observed', ['2026-10-08T12:00:00Z', '2026-10-14T12:00:00Z'])
def test_buyer_handover_remains_history_when_api_uses_same_delivered_status(observed):
    row = order('synthetic-received', 'delivered', updated='2026-10-13T12:00:00Z')
    browser = order('synthetic-received', 'delivered', updated=observed)
    browser.statusSource = 'avito_browser'
    avito_orders._reconcile_export_statuses([row], [browser], None)
    assert row.statusSource == 'avito_browser'
    assert select_queue([row], 'active') == []
    assert select_queue([row], 'history') == [row]


def test_new_snapshot_enriches_cached_queue_without_provider_refresh(monkeypatch):
    row = order('order-1', 'ready_to_ship')
    row.items[0].size = None
    row.items[0].color = None
    cached = {'rows': [row.model_dump(mode='json')], 'complete': True,
              'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat()}
    snapshot = AvitoOrdersBrowserSnapshot(capturedAt=datetime.now(timezone.utc).isoformat(), orders=[AvitoOrdersBrowserOrder(
        orderId='order-1', accountId='account-1', status='ready_to_ship', shipmentNumber='001 286 40390', shipmentNumberState='confirmed', items=[{
            'itemId': 'item-1', 'size': '48 (M)', 'color': 'Чёрный',
            'imageUrl': 'https://70.img.avito.st/synthetic.jpg',
            'sources': {'size': 'description', 'color': 'listing', 'imageUrl': 'listing'},
        }])])
    monkeypatch.setattr(avito_orders, '_orders_client_for_request', lambda _: (SimpleNamespace(organization_id=1), object(), 'test-token'))
    monkeypatch.setattr(avito_orders, 'get_source_cache', lambda *a, **kw: cached)
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda _: snapshot)
    monkeypatch.setattr(avito_orders, '_enrich_orders_with_return_matches', lambda *a, **kw: None)
    monkeypatch.setattr(avito_orders, '_pickup_unreceived', lambda rows, *a: rows)
    tasks = BackgroundTasks()
    response = avito_orders.get_avito_orders_queue(Request({'type': 'http', 'headers': []}), tasks,
        mode='active', account_id=None, search='', history_from=None, page=1, limit=50, force_refresh=False)
    item = response['rows'][0]['items'][0]
    assert response['rows'][0]['shipmentNumber'] == '001 286 40390'
    assert response['rows'][0]['shipmentNumberSource'] == 'browser_order_instruction'
    assert (item['size'], item['color'], item['imageUrl']) == ('48 (M)', 'Чёрный', 'https://70.img.avito.st/synthetic.jpg')
    assert item['sources']['size'] == 'description'
    assert not tasks.tasks
    assert cached['rows'][0]['items'][0]['size'] is None
    monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda *a: (SimpleNamespace(organization_id=1), cached))
    exported = []
    def capture_xlsx(rows, **kwargs):
        exported.extend(rows)
        return b'synthetic-xlsx'
    monkeypatch.setattr(avito_orders, 'build_avito_orders_picking_xlsx', capture_xlsx)
    export = avito_orders._queue_xlsx_response(
        Request({'type': 'http', 'headers': []}), mode='ready_to_ship', account_id=None)
    assert export.body == b'synthetic-xlsx'
    assert exported[0].items[0].model_dump(mode='json') == item
    assert exported[0].shipmentNumber == '001 286 40390'
    assert cached['rows'][0]['items'][0]['size'] is None


def test_listing_fallback_does_not_replace_known_variant_or_cross_account():
    row = order('order-1', 'ready_to_ship')
    row.items[0].sources = {'size': 'order_detail'}
    snapshot = AvitoOrdersBrowserSnapshot(orders=[AvitoOrdersBrowserOrder(
        orderId='order-1', accountId='account-1', items=[{
            'itemId': 'item-1', 'size': 'XL', 'color': 'белый', 'sources': {'size': 'description', 'color': 'listing'},
        }])])
    merged = merge_queue_rows([], [row], snapshot)[0]
    assert merged.items[0].size == 'M'
    assert merged.items[0].sources['size'] == 'order_detail'
    row.items[0].size = None
    snapshot.orders[0].accountId = 'another-account'
    assert merge_queue_rows([], [row], snapshot)[0].items[0].size is None


def test_legacy_collector_missing_provenance_keeps_fields_as_unconfirmed():
    row = order('order-1', 'ready_to_ship')
    row.items[0].color = None
    snapshot = AvitoOrdersBrowserSnapshot(orders=[AvitoOrdersBrowserOrder(orderId='order-1', accountId='account-1', items=[{
        'itemId': 'item-1', 'color': 'Чёрный', 'imageUrl': 'https://70.img.avito.st/synthetic.jpg',
    }])])
    merged = merge_queue_rows([], [row], snapshot)[0].items[0]
    assert merged.color == 'Чёрный' and merged.imageUrl
    assert merged.sources == {'color': 'browser_unspecified', 'imageUrl': 'browser_unspecified'}
    assert snapshot.orders[0].items[0].sources == {}
    row.items[0].imageUrl = None
    snapshot.orders[0].items[0].imageUrl = 'http://127.0.0.1/private'
    assert merge_queue_rows([], [row], snapshot)[0].items[0].imageUrl is None


def test_queue_cache_survives_token_rotation_without_crossing_credential_scope(monkeypatch):
    stored = {(1, avito_orders._queue_cache_key('old-token')): {'rows': [], 'complete': True}}
    monkeypatch.setattr(avito_orders, 'get_source_cache', lambda org, key, **kw: stored.get((org, key)))
    monkeypatch.setattr(avito_orders, 'save_source_cache', lambda org, key, value: stored.update({(org, key): value}))
    client = SimpleNamespace(queue_cache_key='synthetic-credential-generation-A')
    key, cached = avito_orders._read_queue_cache(1, 'old-token', client)
    assert cached['complete'] and key == client.queue_cache_key
    assert avito_orders._read_queue_cache(1, 'new-token', client)[1] == cached
    assert avito_orders._read_queue_cache(2, 'new-token', client)[1] == {}
    assert avito_orders._read_queue_cache(1, 'different-token', SimpleNamespace(queue_cache_key='generation-B'))[1] == {}


def test_preview_filters_all_ready_orders_and_never_marks_text_only_label_ready(monkeypatch):
    rows = [order('wanted', 'ready_to_ship'), order('other', 'ready_to_ship'), order('delivered', 'delivered')]
    cached = {'rows': [row.model_dump(mode='json') for row in rows], 'complete': True,
              'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(avito_orders, '_orders_client_for_request', lambda _: (SimpleNamespace(organization_id=1), object(), 'synthetic'))
    monkeypatch.setattr(avito_orders, '_read_queue_cache', lambda *a: ('key', cached))
    monkeypatch.setattr(avito_orders, '_saved_queue_for_export', lambda *a: (SimpleNamespace(organization_id=1), cached))
    monkeypatch.setattr(avito_orders, '_browser_snapshot_from_cache', lambda *a: None)
    request = Request({'type': 'http', 'headers': []})
    preview = avito_orders.get_avito_picking_preview(request, account_id='account-1', search='wanted')
    assert preview['orders'] == 1 and preview['ready'] == 0
    assert preview['excluded'][0]['orderId'] == 'wanted'
    assert 'Оригинал транспортной этикетки не получен' in preview['excluded'][0]['issues']
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        avito_orders._queue_xlsx_response(request, mode='ready_to_ship', account_id='account-1', search='wanted', ready_only=True)
    assert exc.value.status_code == 409


def test_xlsx_has_offline_image_freeze_filters_and_safe_text():
    from app.avito.orders_picking_xlsx import _photo_extent
    import struct
    assert _photo_extent(b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR' + struct.pack('>II', 400, 200)) == (1143000, 571500)
    assert _photo_extent(b'not-an-image') is None
    row = order('api-id', 'ready_to_ship')
    row.marketplaceId = '00000123456789012345'
    row.items[0].title = '=HYPERLINK("https://example.invalid")'
    row.items[0].sources = {'size': 'description', 'color': 'listing'}
    content = build_avito_orders_picking_xlsx([row], date_from=date.today(), image_loader=lambda _: None)
    with ZipFile(BytesIO(content)) as book:
        sheet = book.read('xl/worksheets/sheet1.xml').decode()
        assert '00000123456789012345' in sheet and 't="inlineStr"' in sheet
        assert '<f>' not in sheet and 'HYPERLINK' in sheet
        assert 'Проверить: из объявления' in sheet
        assert '<pane ySplit="1"' in sheet and '<autoFilter ref="A1:K2"' in sheet
        assert '<mergeCell' not in sheet
        assert b'$1:$1' in book.read('xl/workbook.xml')
        assert '_xlnm.Print_Titles' in book.read('xl/workbook.xml').decode()
