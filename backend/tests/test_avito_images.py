from io import BytesIO
from PIL import Image
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.avito import images_store
from app.avito.images_orm import AvitoProductImage
from app.avito.orders_picking_xlsx import _load_image


def test_product_photos_persist_and_work_offline_without_crossing_tenants(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    AvitoProductImage.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(images_store, 'get_session_factory', lambda: factory)
    buffer = BytesIO()
    Image.new('RGB', (400, 300), 'red').save(buffer, format='JPEG')
    photo = (buffer.getvalue(), 'jpeg')
    url = 'https://70.img.avito.st/test-photo.jpeg'
    calls = []
    def download(url):
        calls.append(url)
        return photo
    def offline(url):
        raise AssertionError('Cached photos must not call the CDN')
    try:
        assert images_store.load_product_image(1, url, download) == photo
        assert images_store.load_product_image(1, url, offline) == photo
        assert calls == [url]
        assert images_store.load_product_image(2, url, lambda _: None) is None
        assert images_store.load_product_image(1, url + '?new', lambda _: None) is None
        assert images_store.load_product_image(1, url + '?new', download) == photo
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(AvitoProductImage)) == 2
    finally:
        engine.dispose()


def test_image_download_retries_transport_failure_and_normalizes_webp(monkeypatch):
    import httpx
    buffer = BytesIO()
    Image.new('RGB', (400, 300), 'blue').save(buffer, format='WEBP')
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout('synthetic timeout')
        return httpx.Response(200, content=buffer.getvalue(), headers={'content-type': 'image/webp'})
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    photo = _load_image('https://70.img.avito.st/test.webp')
    assert len(calls) == 2 and photo[1] == 'jpeg'
    assert Image.open(BytesIO(photo[0])).size == (400, 300)


def test_photo_download_rejects_unsafe_hosts_and_does_not_retry_rate_limit(monkeypatch):
    import httpx
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429)
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert _load_image('https://127.0.0.1/private') is None
    assert not calls
    assert _load_image('https://70.img.avito.st/test.jpeg') is None
    assert len(calls) == 1
