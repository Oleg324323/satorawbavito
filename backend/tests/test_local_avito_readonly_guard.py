import importlib.util
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "local_avito_readonly", Path(__file__).parents[1] / "ops/local_avito_readonly.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("path", [
    "/core/v1/accounts/self", "/core/v1/items", "/core/v1/accounts/123/items/456/",
    "/order-management/1/orders", "/ratings/v1/reviews", "/ratings/v1/info",
    "/messenger/v2/accounts/123/chats",
    "/messenger/v3/accounts/123/chats/u2i~synthetic_123-abc/messages/",
])
def test_reads(path):
    assert module.allowed_request(httpx.Request("GET", "https://api.avito.ru" + path))


def test_auth_and_stats():
    assert module.allowed_request(httpx.Request("POST", "https://api.avito.ru/token",
        content="grant_type=client_credentials&client_id=synthetic&client_secret=synthetic"))
    assert module.allowed_request(httpx.Request("POST", "https://api.avito.ru/stats/v2/accounts/123/items", json={}))
    assert not module.allowed_request(httpx.Request("POST", "https://api.avito.ru/token", content="grant_type=unknown"))

def test_only_scoped_nonstored_avito_ai_requests_allowed():
    body = {'store': False, 'text': {'format': {'name': 'avito_order_item_extraction', 'type': 'json_schema', 'strict': True}}}
    assert module.allowed_request(httpx.Request('POST', 'https://api.openai.com/v1/responses', json=body))
    for url in ['http://api.openai.com/v1/responses', 'https://api.openai.com/v1/files', 'https://api.openai.com/v1/responses?x=1', 'https://api.openai.com.evil.test/v1/responses']:
        assert not module.allowed_request(httpx.Request('POST', url, json=body))
    assert not module.allowed_request(httpx.Request('POST', 'https://api.openai.com/v1/responses', json={**body, 'store': True}))
    assert not module.allowed_request(httpx.Request('GET', 'https://api.openai.com/v1/responses', json=body))


def test_photo_reads_have_no_credentials_and_no_arbitrary_hosts():
    assert module.allowed_request(httpx.Request('GET', 'https://70.img.avito.st/synthetic.jpg'))
    for headers in [{'Authorization': 'Bearer synthetic'}, {'Cookie': 'synthetic=1'}]:
        assert not module.allowed_request(httpx.Request('GET', 'https://70.img.avito.st/synthetic.jpg', headers=headers))
    for url in ['https://70.img.avito.st.evil.test/x', 'http://70.img.avito.st/x', 'https://127.0.0.1/x']:
        assert not module.allowed_request(httpx.Request('GET', url))
    assert not module.allowed_request(httpx.Request('POST', 'https://70.img.avito.st/x'))


@pytest.mark.parametrize("method,url", [
    ("POST", "https://api.avito.ru/messenger/v1/accounts/123/chats/abc/messages"),
    ("POST", "https://api.avito.ru/messenger/v1/accounts/123/chats/abc/read"),
    ("POST", "https://api.avito.ru/messenger/v3/accounts/123/chats/u2i~synthetic/messages/"),
    ("GET", "https://api.avito.ru/messenger/v3/accounts/123/chats/abc/extra/messages/"),
    ("POST", "https://api.avito.ru/ratings/v1/answers"),
    ("POST", "https://api.avito.ru/order-management/1/orders"),
    ("DELETE", "https://api.avito.ru/core/v1/items/123"),
    ("GET", "http://api.avito.ru/core/v1/accounts/self"),
    ("GET", "https://api.avito.ru:444/core/v1/accounts/self"),
    ("GET", "https://example.com/core/v1/accounts/self"),
    ("GET", "https://api.avito.ru/core/v1/items/123/delete"),
])
def test_mutations_and_other_destinations_denied(method, url):
    assert not module.allowed_request(httpx.Request(method, url))
