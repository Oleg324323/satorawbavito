from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace

from celery import Celery
import httpx
import pytest

from ops import load_local_wb_readonly as readonly


@pytest.fixture
def live():
    from ops import local_wb_live
    return local_wb_live


@pytest.fixture
def account(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.infra import db
    from app.cabinet.orm import LkOrganizationRow, LkUserRow, LkUserWbTokenRow
    from app.platform.integrations.orm import MarketplaceAccountRow
    from app.repricer_cache.orm import WbRepricerGoodsCacheRow
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow

    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic-live.sqlite'}")
    factory = sessionmaker(engine)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    for model in (LkOrganizationRow, LkUserRow, LkUserWbTokenRow, MarketplaceAccountRow,
                  WbRepricerGoodsCacheRow, WbRepricerRuntimeStateRow):
        model.__table__.create(engine)
    state = {"assignments": {article: {"strategyId": "stockout_guard", "source": "manual"} for article in ("A", "B")}}
    with factory() as session, session.begin():
        session.add(LkOrganizationRow(organization_id=1, slug="synthetic", name="Synthetic"))
        session.add(LkUserRow(user_id="synthetic-user", organization_id=1, email="test@example.invalid",
                             password_hash="unused", full_name="Synthetic", permission_profile="admin", is_active=True))
        session.add(LkUserWbTokenRow(token_id=1, user_id="synthetic-user", organization_id=1,
                                   wb_token="synthetic-wb-live-token", token_masked="test"))
        session.add(MarketplaceAccountRow(marketplace_account_id=10, organization_id=1,
                                         marketplace="wb", external_account_id="synthetic-seller",
                                         status="connected", credential_ref="lk_user_wb_tokens:1"))
        session.add(WbRepricerRuntimeStateRow(organization_id=1, state_payload=state))
        session.add(WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000,
                    fetched_at=datetime.now(timezone.utc), goods_payload=[
                        {"nmID": 1001, "vendorCode": "A"}, {"nmID": 2002, "vendorCode": "B"}]))

    def save_state(update):
        with factory() as session, session.begin():
            row = session.get(WbRepricerRuntimeStateRow, 1)
            value = deepcopy(row.state_payload)
            update(value)
            row.state_payload = value

    yield SimpleNamespace(factory=factory, save_state=save_state, scope=(1, 10),
                          headers={"Authorization": "synthetic-wb-live-token"})
    engine.dispose()


@pytest.mark.parametrize("path,body", [
    ("/api/v2/upload/task", {"data": [{"nmID": 1001, "price": 2000, "discount": 10}]}),
    ("/api/v2/upload/task/size", {"data": [{"nmID": 2002, "sizeID": 3003, "price": 1900}]}),
])
def test_uploads_require_explicit_live_scope_and_durable_assignment(live, account, path, body):
    request = httpx.Request("POST", f"https://discounts-prices-api.wildberries.ru{path}", json=body, headers=account.headers)
    assert not readonly.allowed_request(request)
    assert not live.allowed_request(request)
    assert live.allowed_request(request, scope=account.scope)


@pytest.mark.parametrize("path", [
    "/api/v2/history/tasks", "/api/v2/history/goods/task",
    "/api/v2/buffer/tasks", "/api/v2/buffer/goods/task",
])
def test_live_transport_allows_upload_confirmation(live, path):
    assert live.allowed_request(httpx.Request("GET", f"https://discounts-prices-api.wildberries.ru{path}?uploadID=123"))


@pytest.mark.parametrize("method,url", [
    ("POST", "http://discounts-prices-api.wildberries.ru/api/v2/upload/task"),
    ("POST", "https://discounts-prices-api.wildberries.ru:444/api/v2/upload/task"),
    ("POST", "https://discounts-prices-api.wildberries.ru.example.com/api/v2/upload/task"),
    ("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task/club-discount"),
    ("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task/"),
    ("DELETE", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"),
    ("POST", "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/upload"),
    ("POST", "https://api.avito.ru/price/v1/items"),
])
def test_live_transport_keeps_unrelated_mutations_and_destinations_blocked(live, method, url):
    assert not live.allowed_request(httpx.Request(method, url))


def test_existing_read_endpoints_are_preserved(live):
    for method, host, path in readonly.ALLOWED:
        assert live.allowed_request(httpx.Request(method, f"https://{host}{path}"))


def test_final_transport_guard_does_not_follow_upload_redirects_or_leak_socket_scope(live, account, monkeypatch):
    for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (httpx.Client, "send")):
        monkeypatch.setattr(obj, name, getattr(obj, name))
    calls = []
    readonly.install_readonly_network_guard(
        lambda sock, address: calls.append(address), lambda sock, address: 0,
        request_allowed=lambda request: live.allowed_request(request, scope=account.scope), local_broker_port=59999,
    )

    def transport(request):
        with socket.socket() as sock:
            sock.connect(("synthetic-provider", 443))
        return httpx.Response(307, headers={"location": "https://example.invalid/upload"})

    # A rebuilt client still passes through the final guarded send method.
    for _ in range(2):
        with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            assert client.post("https://discounts-prices-api.wildberries.ru/api/v2/upload/task",
                               headers=account.headers, json={"data": [{"nmID": 1001, "price": 2000}]}).status_code == 307
            with pytest.raises(RuntimeError, match="LOCAL_READ_ENDPOINT_NOT_ALLOWED"):
                client.post("https://api.avito.ru/price/v1/items")
    with socket.socket() as sock:
        with pytest.raises(OSError):
            sock.connect(("synthetic-provider", 443))
        sock.connect(("127.0.0.1", 59999))
    assert calls == [("synthetic-provider", 443), ("synthetic-provider", 443), ("127.0.0.1", 59999)]


@pytest.mark.parametrize("wb_live", [False, True])
def test_only_authorized_organization_is_scheduled_in_live_mode(live, wb_live):
    with Celery("synthetic-local", broker="memory://") as app:
        assigned = {"task": "repricer.execute_assigned_all_orgs", "schedule": timedelta(minutes=5),
                    "kwargs": {"scenario": "complete"}, "options": {"queue": "vella.default"}}
        original = {"repricer-execute-assigned": assigned, "reviews": {"task": "reviews.sync_all_orgs"},
                    "avito": {"task": "avito.sync_returns_all_orgs"}}
        app.conf.update(task_routes={"unrelated": {"queue": "untouched"}}, beat_schedule=original)
        live.configure_local_celery(app, live_scope=(1, 10) if wb_live else None)
        assert app.amqp.router.route({}, "reports.refresh_budget_step")["queue"].name == live.REPORT_QUEUE
        assert app.amqp.router.route({}, "unrelated")["queue"].name == "untouched"
        if wb_live:
            assert list(app.conf.beat_schedule) == ["repricer-execute-assigned"]
            entry = app.conf.beat_schedule["repricer-execute-assigned"]
            assert entry["schedule"] == timedelta(minutes=5)
            assert entry["task"] == "repricer.execute_assigned_for_org"
            assert entry["kwargs"] == {"scenario": "complete", "organization_id": 1}
            assert app.amqp.router.route(entry["options"], entry["task"])["queue"].name == live.REPRICER_QUEUE
            assert app.amqp.router.route({}, "repricer.execute_assigned_for_org")["queue"].name == live.REPRICER_QUEUE
        else:
            assert app.conf.beat_schedule == original
            assert "repricer.execute_assigned_for_org" not in app.conf.task_routes


@pytest.mark.parametrize("change", ["clear", "manual", "warmup", "automation_off", "derived", "disabled"])
def test_final_post_rechecks_current_assignment_and_rejects_mixed_batches(live, account, change):
    request = httpx.Request("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task",
                           headers=account.headers, json={"data": [{"nmID": 1001}, {"nmID": 2002}]})
    assert live.allowed_request(request, scope=account.scope)

    def update(state):
        if change == "clear":
            state["assignments"].pop("B")
        elif change == "automation_off":
            state["skuSettingsOverrides"] = {"B": {"automationEnabled": False}}
        elif change == "derived":
            state["assignments"]["B"]["source"] = "derived"
        elif change == "disabled":
            state["assignments"]["B"]["strategyId"] = "cross_marketplace"
        else:
            state["skuMetaOverrides"] = {"B": {"status": change}}

    account.save_state(update)
    assert not live.allowed_request(request, scope=account.scope)


@pytest.mark.parametrize("kind", ["unknown_product", "missing_auth", "different_token", "different_scope", "bad_body"])
def test_unbound_or_unresolved_upload_is_denied(live, account, kind):
    body, headers, scope = {"data": [{"nmID": 1001}]}, account.headers, account.scope
    if kind == "unknown_product":
        body = {"data": [{"nmID": 9999}]}
    elif kind == "missing_auth":
        headers = {}
    elif kind == "different_token":
        headers = {"Authorization": "other-synthetic-token"}
    elif kind == "different_scope":
        scope = (2, 20)
    else:
        body = {"data": [{"nmID": True}]}
    request = httpx.Request("POST", "https://discounts-prices-api.wildberries.ru/api/v2/upload/task", headers=headers, json=body)
    assert not live.allowed_request(request, scope=scope)


def test_live_scope_uses_existing_verified_account_and_refuses_ambiguity(live, account):
    from app.platform.integrations.orm import MarketplaceAccountRow
    assert live.resolve_live_scope() == account.scope
    with account.factory() as session, session.begin():
        session.add(MarketplaceAccountRow(marketplace_account_id=20, organization_id=2,
                    marketplace="wb", external_account_id="other-synthetic-seller", status="connected"))
    with pytest.raises(RuntimeError, match="exactly one connected"):
        live.resolve_live_scope()
