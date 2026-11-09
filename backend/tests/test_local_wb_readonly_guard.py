import importlib.util
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "local_wb_readonly", Path(__file__).parents[1] / "ops/load_local_wb_readonly.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("method,host,path", sorted(module.ALLOWED))
def test_explicit_reads_allowed(method, host, path):
    assert module.allowed_request(httpx.Request(method, f"https://{host}{path}"))


@pytest.mark.parametrize("method,url", [
    ("POST", "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/upload"),
    ("POST", "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions"),
    ("GET", "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/upload"),
    ("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"),
    ("DELETE", "https://content-api.wildberries.ru/content/v2/get/cards/list"),
    ("GET", "https://example.com/api/v2/list/goods/filter"),
    ("GET", "http://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"),
    ("GET", "https://discounts-prices-api.wildberries.ru:444/api/v2/list/goods/filter"),
])
def test_writes_and_unapproved_destinations_denied(method, url):
    assert not module.allowed_request(httpx.Request(method, url))


def test_guard_blocks_sockets_outside_read_and_resets_after_error(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    calls = []
    module.install_readonly_network_guard(lambda sock, address: calls.append(address), lambda sock, address: 0)
    with socket.socket() as sock:
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", 1))
    def transport(request):
        with socket.socket() as sock:
            sock.connect(("test-only", 443))
        return httpx.Response(302, headers={"location": "https://example.com"})
    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        assert client.get("https://common-api.wildberries.ru/api/v1/seller-info").status_code == 302
        with pytest.raises(RuntimeError):
            client.post("https://discounts-prices-api.wildberries.ru/api/v2/upload/task")
    assert calls == [("test-only", 443)]
    with socket.socket() as sock:
        with pytest.raises(OSError):
            sock.connect(("127.0.0.1", 1))


def test_local_report_broker_does_not_allow_other_socket_destinations(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', socket.socket.connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', socket.socket.connect_ex)
    monkeypatch.setattr(httpx.Client, 'send', httpx.Client.send)
    calls = []
    module.install_readonly_network_guard(lambda sock, address: calls.append(address), lambda sock, address: 0, local_broker_port=59999)
    with socket.socket() as sock:
        sock.connect(('127.0.0.1', 59999))
        for address in [('127.0.0.1', 6379), ('example.com', 59999), ('example.com', 443)]:
            with pytest.raises(OSError):
                sock.connect(address)
    assert calls == [('127.0.0.1', 59999)]
