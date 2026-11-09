"""Browser price authority and durable merges on disposable PostgreSQL only."""
import importlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from types import SimpleNamespace
import json
from pathlib import Path
import select as io_select
import shutil
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.cabinet.orm import LkOrganizationRow, LkSessionRow, LkUserRow, LkUserWbTokenRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.wb_api.client import RateLimitInfo, WbApiResponseEnvelope
from tests.test_empty_database_migrations import cluster, database  # noqa: F401

PREFIX = "/api/v1/wb/browser-prices"


def api():
    name = "app.routers.wb_browser_prices"
    assert importlib.util.find_spec(name) is not None, "browser price router is missing"
    return importlib.import_module(name)


def test_extension_routes_reject_regular_login_before_storage(monkeypatch):
    module = api()
    monkeypatch.setattr(module, "get_session_factory", lambda: pytest.fail("ordinary JWT reached extension storage"))
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)
    response = client.get(PREFIX + "/catalog", headers={"Authorization": "Bearer ordinary-login-token"})
    assert response.status_code == 401


@pytest.fixture
def data(database, monkeypatch):
    _, engine = database
    module = api()
    for model in (LkOrganizationRow, LkUserRow, LkSessionRow, LkUserWbTokenRow, IamMembershipRow,
                  MarketplaceAccountRow, WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow):
        model.__table__.create(engine)
    now = datetime.now(timezone.utc)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as session, session.begin():
        session.add_all([LkOrganizationRow(organization_id=org, slug=f"browser-{org}", name="Synthetic") for org in (1, 2)])
        session.flush()
        session.add(LkUserRow(user_id="issuer", organization_id=1, email="browser@example.invalid",
                             password_hash="unused", full_name="Synthetic", permission_profile="custom", is_active=True))
        session.flush()
        session.add_all([
            LkSessionRow(session_id="login", user_id="issuer", issued_at=now, last_seen_at=now,
                         expires_at=now + timedelta(hours=2)),
            IamMembershipRow(membership_id=1, organization_id=1, user_id="issuer", role="custom",
                             permissions=["integrations:write"], scope_mode="selected", allowed_account_ids=[10], is_active=True),
            MarketplaceAccountRow(marketplace_account_id=10, organization_id=1, marketplace="wb",
                                  external_account_id="synthetic-seller", credential_ref="synthetic-ref", status="connected"),
            MarketplaceAccountRow(marketplace_account_id=20, organization_id=2, marketplace="wb",
                                  external_account_id="other-seller", status="connected"),
            WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000, fetched_at=now,
                goods_payload=[{"vendorCode": "A", "nmID": 101, "sizes": [
                    {"sizeID": 1001, "discountedPrice": 1000}, {"sizeID": 1002, "discountedPrice": 1100}]},
                    {"vendorCode": "B", "nmID": 102, "sizes": [{"sizeID": 2001, "discountedPrice": 2000}]}]),
        ])
    actor = ActorContext("issuer", "issuer", 1, "custom", frozenset(), session_id="login")
    def resolve(request):
        if request.headers.get("authorization") != "Bearer synthetic-ui":
            raise HTTPException(401, "AUTH_REQUIRED")
        return actor
    monkeypatch.setattr(module, "actor_from_request", resolve)
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    app = FastAPI()
    app.include_router(module.router)
    return SimpleNamespace(client=TestClient(app, headers={"Authorization": "Bearer synthetic-ui"}),
                           factory=factory, engine=engine, now=now, module=module)


def connect(data):
    response = data.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True and body["token"].startswith("sat_wbp_")
    return body, {"Authorization": "Bearer " + body["token"]}


def test_actual_extension_worker_to_http_postgres_and_platform_state(data):
    issued, _ = connect(data)
    server = 'https://synthetic-vps.example'
    script = Path(__file__).resolve().parents[2] / 'wb-prices-extension/tests/http-roundtrip.mjs'
    node = shutil.which('node')
    assert node, 'Node is required for the extension/backend contract gate'
    with subprocess.Popen([node, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, bufsize=1) as child:
        def reply(value):
            child.stdin.write(json.dumps(value) + '\n'); child.stdin.flush()
        reply({'backendUrl': server, 'token': issued['token']})
        requests = 0
        try:
            for _ in range(20):
                assert io_select.select([child.stdout], [], [], 20)[0], 'Synthetic worker transport timed out'
                line = child.stdout.readline()
                assert line, 'Synthetic worker exited before completion'
                message = json.loads(line)
                if message['kind'] == 'done':
                    assert message['result']['ok']
                    assert message['result']['state']['backendUrl'] == server
                    assert message['result']['state']['accepted'] == 2
                    assert issued['token'] not in json.dumps(message)
                    break
                requests += 1
                url = message['url']
                if url.startswith('https://card.wb.ru/cards/v4/detail?'):
                    assert 'Authorization' not in message['headers']
                    reply({'status': 200, 'body': {'products': [
                        {'id': nm, 'supplierId': 777, 'sizes': [{'optionId': size,
                          'price': {'basic': 120000, 'product': price}}]}
                        for nm, size, price in [(101, 1001, 80000), (102, 2001, 160000)]]}})
                else:
                    assert url.startswith(server + PREFIX + '/')
                    response = data.client.request(message['method'], url[len(server):],
                        headers=message['headers'], content=message.get('body'))
                    reply({'status': response.status_code, 'body': response.json()})
            else:
                pytest.fail('Synthetic worker exceeded its bounded request budget')
            assert child.wait(timeout=5) == 0
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
    assert requests == 3  # CRM catalog, synthetic public WB batch, CRM snapshot.
    persisted = prices(data)
    assert persisted['items']['101:1001']['buyerPriceNoWalletKopecks'] == 80000
    assert persisted['items']['102:2001']['buyerPriceNoWalletKopecks'] == 160000
    # The same endpoint consumed by the real platform connection card reads DB.
    view = data.client.get(PREFIX + '/connection?marketplaceAccountId=10')
    assert view.status_code == 200 and view.json()['knownPrices'] == 2
    assert view.json()['lastObservedAt'] is not None


def catalog(data, headers):
    response = data.client.get(PREFIX + "/catalog", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def observation(data, nm=101, **changes):
    item = {"nmId": nm, "sizeId": 1001 if nm == 101 else 2001,
            "sellerPriceKopecks": 100000 if nm == 101 else 200000,
            "buyerPriceNoWalletKopecks": 80000, "buyerPriceWithWalletKopecks": None,
            "observedAt": datetime.now(timezone.utc).isoformat()}
    return {**item, **changes}


def submit(data, headers, revision, items):
    return data.client.post(PREFIX + "/snapshots", headers=headers,
                            json={"goodsRevision": revision, "items": items})


def prices(data):
    with data.factory() as session:
        row = session.scalar(select(WbRepricerSourceCacheRow).where(
            WbRepricerSourceCacheRow.organization_id == 1,
            WbRepricerSourceCacheRow.source_key == "wb_browser_prices"))
        return deepcopy(row.payload) if row else None


def test_connection_secret_is_once_scoped_and_bounded_by_login(data):
    body, headers = connect(data)
    status = data.client.get(PREFIX + "/connection?marketplaceAccountId=10").json()
    assert set(status) == {"marketplaceAccountId", "configured", "tokenPrefix", "expiresAt", "lastObservedAt", "knownPrices"}
    assert status["configured"] and status["knownPrices"] == 0 and status["lastObservedAt"] is None
    assert datetime.fromisoformat(status["expiresAt"]) == data.now + timedelta(hours=2)
    with data.factory() as session:
        payloads = session.scalars(select(WbRepricerSourceCacheRow.payload)).all()
    assert body["token"] not in str(payloads) and body["token"] not in str(status)
    refs = catalog(data, headers)
    assert refs["marketplaceAccountId"] == 10 and refs["total"] == 2 and refs["maxAgeSeconds"] == 1800
    assert [(i["nmId"], i["sizeId"], i["sellerPriceKopecks"]) for i in refs["items"]] == [(101, 1001, 100000), (102, 2001, 200000)]
    assert data.client.get(PREFIX + "/connection?marketplaceAccountId=20").status_code == 403


def test_account_discovery_filters_live_membership_and_org(data):
    response = data.client.get(PREFIX + "/accounts")
    assert response.status_code == 200
    assert response.json() == {"data": [{"marketplaceAccountId": 10, "provider": "wb",
        "externalAccountId": "synthetic-seller", "displayName": None, "status": "connected"}]}
    with data.factory() as session, session.begin():
        session.get(IamMembershipRow, 1).allowed_account_ids = []
    assert data.client.get(PREFIX + "/accounts").json() == {"data": []}
    with data.factory() as session, session.begin():
        session.get(LkUserRow, "issuer").is_active = False
    assert data.client.get(PREFIX + "/accounts").status_code == 403


@pytest.mark.parametrize("mutation", ["user", "user_org", "membership", "permission", "scope", "session", "expiry",
                                      "account_status", "provider", "external", "credential", "binding", "second_account"])
def test_token_rechecks_live_authority_and_account_binding(data, mutation):
    _, headers = connect(data)
    with data.factory() as session, session.begin():
        if mutation == "user": session.get(LkUserRow, "issuer").is_active = False
        elif mutation == "user_org": session.get(LkUserRow, "issuer").organization_id = 2
        elif mutation == "membership": session.get(IamMembershipRow, 1).is_active = False
        elif mutation == "permission": session.get(IamMembershipRow, 1).permissions = []
        elif mutation == "scope": session.get(IamMembershipRow, 1).allowed_account_ids = [20]
        elif mutation == "session": session.get(LkSessionRow, "login").revoked_at = data.now
        elif mutation == "expiry": session.get(LkSessionRow, "login").expires_at = data.now - timedelta(seconds=1)
        elif mutation == "second_account": session.add(MarketplaceAccountRow(organization_id=1, marketplace="wb", external_account_id="second", status="connected"))
        else:
            row = session.get(MarketplaceAccountRow, 10)
            field, value = {"account_status": ("status", "disconnected"), "provider": ("marketplace", "avito"),
                            "external": ("external_account_id", "replacement"), "credential": ("credential_ref", "replacement"),
                            "binding": ("ingestion_binding_version", 2)}[mutation]
            setattr(row, field, value)
    response = data.client.get(PREFIX + "/catalog", headers=headers)
    assert response.status_code in (401, 403, 409)
    assert not prices(data)["items"]


def test_rotation_and_revoke_reject_old_token_and_clear_observations(data):
    _, old = connect(data)
    ref = catalog(data, old)
    assert submit(data, old, ref["goodsRevision"], [observation(data)]).status_code == 200
    _, new = connect(data)
    assert data.client.get(PREFIX + "/catalog", headers=old).status_code == 401
    assert not prices(data)["items"]
    response = data.client.delete(PREFIX + "/connection?marketplaceAccountId=10")
    assert response.status_code == 200 and response.json()["configured"] is False
    assert data.client.get(PREFIX + "/catalog", headers=new).status_code == 401


def test_partial_batches_merge_duplicates_and_ignore_older_observations(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    first = observation(data, buyerPriceWithWalletKopecks=75000)
    assert submit(data, headers, revision, [first]).json()["accepted"] == 1
    assert submit(data, headers, revision, [observation(data, 102)]).json()["accepted"] == 1
    result = submit(data, headers, revision, [first, {**first, "observedAt": (data.now - timedelta(seconds=1)).isoformat()}])
    assert result.json()["accepted"] == 0 and result.json()["ignored"] == 2
    payload = prices(data)
    assert set(payload["items"]) == {"101:1001", "102:2001"}
    assert payload["marketplaceAccountId"] == 10 and payload["accountBindingVersion"] == 1
    assert payload["items"]["101:1001"]["buyerPriceWithWalletKopecks"] == 75000
    assert payload["items"]["101:1001"]["source"] == "wb_browser"
    assert payload["items"]["101:1001"]["sellerPriceObservedAt"] == data.now.isoformat()
    status = data.client.get(PREFIX + "/connection?marketplaceAccountId=10").json()
    assert status["knownPrices"] == 2 and status["lastObservedAt"] is not None
    updated = {**first, "observedAt": (data.now + timedelta(seconds=1)).isoformat(),
               "buyerPriceNoWalletKopecks": 78000, "buyerPriceWithWalletKopecks": 74000}
    assert submit(data, headers, revision, [updated]).json()["accepted"] == 1
    replaced = prices(data)
    assert set(replaced["items"]) == {"101:1001", "102:2001"}
    assert replaced["items"]["101:1001"]["buyerPriceNoWalletKopecks"] == 78000
    assert replaced["items"]["101:1001"]["buyerPriceWithWalletKopecks"] == 74000


def test_corrected_three_price_observation_replaces_db_cache_and_reaches_repricer_xlsx(data, monkeypatch):
    from app import repricer_bff as bff
    from app.wb_browser_prices import apply_browser_prices
    from app.repricer_table_export import COLUMNS, table_rows

    with data.factory() as session, session.begin():
        goods_row = session.scalar(select(WbRepricerGoodsCacheRow).where(WbRepricerGoodsCacheRow.organization_id == 1))
        goods = deepcopy(goods_row.goods_payload)
        goods[0]['sizes'][0].update(price=4300, discountedPrice=2064)
        goods_row.goods_payload = goods
    _, headers = connect(data)
    revision = catalog(data, headers)['goodsRevision']
    older = observation(data, sellerPriceKopecks=206400, buyerPriceNoWalletKopecks=206400,
        observedAt=(data.now - timedelta(seconds=2)).isoformat())
    assert submit(data, headers, revision, [older]).json()['accepted'] == 1
    corrected = {**older, 'buyerPriceNoWalletKopecks': 142600, 'buyerPriceWithWalletKopecks': 139700,
                 'originalPriceKopecks': 430000, 'observedAt': data.now.isoformat()}
    assert submit(data, headers, revision, [corrected]).json()['accepted'] == 1
    stored = prices(data)
    assert stored['items']['101:1001']['buyerPriceNoWalletKopecks'] == 142600
    assert submit(data, headers, revision, [older]).json()['ignored'] == 1
    with data.factory() as session:
        connection = session.scalar(select(WbRepricerSourceCacheRow).where(
            WbRepricerSourceCacheRow.organization_id == 1,
            WbRepricerSourceCacheRow.source_key == data.module.CONNECTION)).payload
    overlaid = apply_browser_prices(goods, connection, stored, now=data.now)
    monkeypatch.setattr(bff, '_demo_repricer_data_enabled', lambda _: False)
    row = bff.list_repricer_skus(cached_goods=overlaid[:1], cached_content_cards=[], cached_promotions=[],
        include_content=False, include_promotions=False, allow_commission_tariff_fetch=False)[0]
    assert row['meta']['currentPriceKopecks'] == 206400  # Seller strategy input is untouched.
    assert row['analytics']['basePriceKopecks'] == 430000
    assert row['analytics']['browserOriginalPriceKopecks'] == 430000
    assert row['analytics']['buyerPriceNoWalletKopecks'] == 142600
    assert row['analytics']['buyerPriceWithWalletKopecks'] == 139700
    exported = dict(zip([c[0] for c in COLUMNS], table_rows([row])[0]))
    assert exported['Цена до СПП'] == 1426
    assert exported['% СПП'] == pytest.approx(0.668)


@pytest.mark.parametrize("changes,status", [({"nmId": True}, 422), ({"sizeId": "1001"}, 422),
    ({"buyerPriceNoWalletKopecks": 1.5}, 422), ({"buyerPriceNoWalletKopecks": 0}, 422),
    ({"buyerPriceNoWalletKopecks": 100001}, 422), ({"buyerPriceWithWalletKopecks": 80001}, 422),
    ({"observedAt": "2026-09-16T12:00:00"}, 422), ({"observedAt": 1789550400}, 422),
    ({"source": "forged"}, 422), ({"sellerPriceObservedAt": "forged"}, 422),
    ({"nmId": 999}, 409), ({"sizeId": 1002}, 409), ({"sellerPriceKopecks": 110000}, 409)])
def test_snapshot_rejects_invalid_values_and_mismatched_reference(data, changes, status):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    response = submit(data, headers, revision, [observation(data, **changes)])
    assert response.status_code == status
    assert not prices(data)["items"]


@pytest.mark.parametrize("seconds", [-301, 31])
def test_snapshot_observation_time_is_bounded(data, seconds):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    item = observation(data, observedAt=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
    assert submit(data, headers, revision, [item]).status_code == 422


def test_changed_catalog_revision_and_stale_reference_are_rejected(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    with data.factory() as session, session.begin():
        row = session.scalar(select(WbRepricerGoodsCacheRow))
        row.fetched_at = data.now - timedelta(minutes=31)
    assert submit(data, headers, revision, [observation(data)]).status_code == 409
    assert not prices(data)["items"]


def test_saved_prices_remain_when_next_partial_batch_arrives(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    assert submit(data, headers, revision, [observation(data)]).status_code == 200
    with data.factory() as session, session.begin():
        row = session.scalar(select(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.source_key == "wb_browser_prices"))
        value = deepcopy(row.payload)
        value["items"]["101:1001"]["observedAt"] = (data.now - timedelta(minutes=31)).isoformat()
        row.payload = value
    assert submit(data, headers, revision, [observation(data, 102)]).status_code == 200
    assert set(prices(data)["items"]) == {"101:1001", "102:2001"}


def test_concurrent_partial_batches_do_not_lose_each_other(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    start = Barrier(2)
    def send(nm):
        start.wait(timeout=5)
        return submit(data, headers, revision, [observation(data, nm)]).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(send, (101, 102))) == [200, 200]
    assert set(prices(data)["items"]) == {"101:1001", "102:2001"}


def test_commit_error_never_acknowledges_or_persists_a_snapshot(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    def reject(_session):
        raise SQLAlchemyError("private-driver-message-must-not-leak")
    event.listen(data.factory, "before_commit", reject)
    try:
        response = submit(data, headers, revision, [observation(data)])
    finally:
        event.remove(data.factory, "before_commit", reject)
    assert response.status_code == 503 and "private-driver" not in response.text
    assert not prices(data)["items"]


def test_connection_commit_failure_never_returns_a_token(data):
    def reject(_session):
        raise SQLAlchemyError("private-driver-message")
    event.listen(data.factory, "before_commit", reject)
    try:
        response = data.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10})
    finally:
        event.remove(data.factory, "before_commit", reject)
    assert response.status_code == 503 and "sat_wbp_" not in response.text
    with data.factory() as session:
        assert session.scalars(select(WbRepricerSourceCacheRow)).all() == []


def test_token_lifetime_never_exceeds_thirty_days(data):
    with data.factory() as session, session.begin():
        session.get(LkSessionRow, "login").expires_at = data.now + timedelta(days=90)
    body, _ = connect(data)
    expiry = datetime.fromisoformat(body["expiresAt"])
    assert data.now + timedelta(days=30) <= expiry <= datetime.now(timezone.utc) + timedelta(days=30)


def test_login_expiring_during_lock_wait_cannot_issue_a_configured_token(data, monkeypatch):
    # Simulate wall clock advancing between the IAM check and the account lock.
    monkeypatch.setattr(data.module, "_now", lambda _session: data.now + timedelta(hours=3))
    response = data.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10})
    assert response.status_code == 403
    with data.factory() as session:
        assert session.scalars(select(WbRepricerSourceCacheRow)).all() == []


@pytest.mark.parametrize("kind", ["expired", "cross_org"])
def test_token_metadata_is_fail_closed(data, kind):
    _, headers = connect(data)
    with data.factory() as session, session.begin():
        for row in session.scalars(select(WbRepricerSourceCacheRow)).all():
            if row.source_key == "wb_browser_prices":
                continue
            payload = dict(row.payload)
            payload.update({"expiresAt": (data.now - timedelta(seconds=1)).isoformat()} if kind == "expired" else {"organizationId": 2})
            row.payload = payload
    assert data.client.get(PREFIX + "/catalog", headers=headers).status_code == 401


def test_unchanged_revision_cannot_extend_seller_reference_freshness(data, monkeypatch):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    future = data.now + timedelta(minutes=31)
    monkeypatch.setattr(data.module, "_now", lambda _session: future)
    result = submit(data, headers, revision, [observation(data, observedAt=future.isoformat())])
    assert result.status_code == 409 and not prices(data)["items"]


def test_new_revision_still_requires_exact_seller_price(data):
    _, headers = connect(data)
    old = catalog(data, headers)["goodsRevision"]
    with data.factory() as session, session.begin():
        row = session.scalar(select(WbRepricerGoodsCacheRow))
        goods = deepcopy(row.goods_payload)
        goods[0]["sizes"][0]["discountedPrice"] = 900
        row.goods_payload = goods
    new = catalog(data, headers)["goodsRevision"]
    assert old != new
    assert submit(data, headers, old, [observation(data)]).status_code == 409
    assert submit(data, headers, new, [observation(data)]).status_code == 409
    assert submit(data, headers, new, [observation(data, sellerPriceKopecks=90000)]).status_code == 200


def test_batch_limits_extra_fields_and_invalid_sibling_never_write(data):
    _, headers = connect(data)
    revision = catalog(data, headers)["goodsRevision"]
    assert submit(data, headers, revision, [observation(data)] * 101).status_code == 422
    result = data.client.post(PREFIX + "/snapshots", headers=headers,
        json={"goodsRevision": revision, "items": [observation(data)], "marketplaceAccountId": 20})
    assert result.status_code == 422
    assert submit(data, headers, revision, [observation(data), observation(data, 999)]).status_code == 409
    assert not prices(data)["items"]


def goods_pages(data):
    with data.factory() as session:
        return [(row.page_offset, row.page_limit, deepcopy(row.goods_payload), row.fetched_at)
                for row in session.scalars(select(WbRepricerGoodsCacheRow).where(
                    WbRepricerGoodsCacheRow.organization_id == 1).order_by(WbRepricerGoodsCacheRow.page_offset))]


@pytest.fixture
def refresh(data, monkeypatch):
    with data.factory() as session, session.begin():
        session.add(LkUserWbTokenRow(token_id=1, user_id="issuer", organization_id=1,
                    wb_token="synthetic-server-only-secret", token_masked="synthetic"))
        session.get(MarketplaceAccountRow, 10).credential_ref = "lk_user_wb_tokens:1"
        session.scalar(select(WbRepricerGoodsCacheRow)).fetched_at = data.now - timedelta(minutes=42)
    _, headers = connect(data)
    old = goods_pages(data)
    state = SimpleNamespace(headers=headers, old=old, requests=[], hook=None,
                            payload={"error": False, "data": {"listGoods": deepcopy(old[0][2])}},
                            status=200, retry=0, invalidations=[])
    class Provider:
        def request(self, request):
            state.requests.append(request)
            if state.hook:
                state.hook(request)
            return WbApiResponseEnvelope(request=request, statusCode=state.status, ok=state.status == 200,
                data=state.payload, rateLimit=RateLimitInfo(retryAfterSeconds=state.retry))
    def build(*args, **kwargs):
        assert kwargs["token_override"] == "synthetic-server-only-secret"
        return Provider()
    monkeypatch.setattr(data.module, "build_wb_client", build, raising=False)
    from app.repricer_cache import store
    monkeypatch.setattr(store, "_redis_delete", lambda *keys: state.invalidations.extend(keys))
    return state


def run_refresh(data, refresh):
    return data.client.post(PREFIX + "/catalog/refresh", headers=refresh.headers)


def test_catalog_refresh_commits_fresh_exact_first_size_without_other_sources(data, refresh):
    assert data.client.get(PREFIX + "/catalog", headers=refresh.headers).status_code == 409
    result = run_refresh(data, refresh)
    assert result.status_code == 200, result.text
    refs = catalog(data, refresh.headers)
    assert result.json() == {"ok": True, "goodsRevision": refs["goodsRevision"], "total": 2}
    assert [(row["nmId"], row["sizeId"]) for row in refs["items"]] == [(101, 1001), (102, 2001)]
    assert [(r.method, r.path, r.query, r.jsonBody) for r in refresh.requests] == [
        ("GET", "/api/v2/list/goods/filter", {"limit": 1000, "offset": 0}, None)]
    assert goods_pages(data)[0][3] > refresh.old[0][3]
    assert set(refresh.invalidations) == {"vella:repricer:goods-meta:1", "vella:repricer:goods-list:1"}
    assert run_refresh(data, refresh).status_code == 200 and len(refresh.requests) == 1
    assert "synthetic-server-only-secret" not in result.text
    with data.factory() as session:
        assert session.get(IamMembershipRow, 1).permissions == ["integrations:write"]
        assert {key for key in session.scalars(select(WbRepricerSourceCacheRow.source_key))
                if not key.startswith("wb_browser_prices_token_")} == {
                    "wb_browser_prices", "wb_browser_prices_connection"}


@pytest.mark.parametrize("status", [429, 503])
def test_catalog_refresh_failed_upstream_keeps_pages_and_persists_cooldown(data, refresh, status):
    refresh.status, refresh.retry = status, 180
    refresh.payload = {"private": "synthetic-server-only-secret"}
    response = run_refresh(data, refresh)
    assert response.status_code == status
    assert response.json()["detail"]["retryAfterSeconds"] >= 180
    assert response.headers["Retry-After"] == str(response.json()["detail"]["retryAfterSeconds"])
    assert "synthetic-server-only-secret" not in response.text
    assert goods_pages(data) == refresh.old and not refresh.invalidations
    assert run_refresh(data, refresh).status_code == 429 and len(refresh.requests) == 1


@pytest.mark.parametrize("mutation", ["session", "permission", "credential", "binding", "token", "rotation"])
def test_catalog_refresh_revalidates_authority_after_network(data, refresh, mutation):
    def during_network(_request):
        # This independent commit would time out if the HTTP ran under the
        # original live user/account/token transaction locks.
        if mutation == "rotation":
            connect(data)
            return
        with data.factory() as session, session.begin():
            if mutation == "session": session.get(LkSessionRow, "login").revoked_at = data.now
            elif mutation == "permission": session.get(IamMembershipRow, 1).permissions = []
            elif mutation == "token": session.get(LkUserWbTokenRow, 1).wb_token = "replacement-secret"
            elif mutation == "credential": session.get(MarketplaceAccountRow, 10).credential_ref = None
            elif mutation == "binding": session.get(MarketplaceAccountRow, 10).ingestion_binding_version = 2
    refresh.hook = during_network
    result = run_refresh(data, refresh)
    assert result.status_code in (401, 403), result.text
    assert goods_pages(data) == refresh.old and not refresh.invalidations


def test_catalog_refresh_preserves_newer_concurrent_goods(data, refresh):
    def during_network(_request):
        with data.factory() as session, session.begin():
            row = session.scalar(select(WbRepricerGoodsCacheRow))
            row.fetched_at = datetime.now(timezone.utc)
            goods = deepcopy(row.goods_payload)
            goods[0]["sizes"][0]["discountedPrice"] = 1234
            row.goods_payload = goods
    refresh.hook = during_network
    result = run_refresh(data, refresh)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "WB_BROWSER_GOODS_CHANGED"
    assert goods_pages(data)[0][2][0]["sizes"][0]["discountedPrice"] == 1234


def test_catalog_refresh_singleflight_keeps_network_outside_transactions(data, refresh):
    entered, release = Event(), Event()
    def during_network(_request):
        entered.set()
        assert release.wait(5)
    refresh.hook = during_network
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(run_refresh, data, refresh)
        assert entered.wait(5)
        try:
            second = run_refresh(data, refresh)
            assert second.status_code == 409
            assert second.json()["detail"]["code"] == "WB_BROWSER_REFRESH_BUSY"
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert len(refresh.requests) == 1


def test_catalog_refresh_second_page_failure_never_publishes_partial_catalog(data, refresh):
    first_page = [{"vendorCode": f"P-{nm}", "nmID": nm,
                   "sizes": [{"sizeID": nm, "discountedPrice": 1000}]} for nm in range(1, 1001)]
    def during_network(request):
        if request.query["offset"] == 0:
            refresh.payload = {"data": {"listGoods": first_page}}
        else:
            refresh.status = 503
    refresh.hook = during_network
    assert run_refresh(data, refresh).status_code == 503
    assert [request.query["offset"] for request in refresh.requests] == [0, 1000]
    assert goods_pages(data) == refresh.old and not refresh.invalidations


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"listGoods": [None]}},
    {"data": {"listGoods": [{"nmID": True}]}}, {"error": True, "data": {"listGoods": []}}])
def test_catalog_refresh_malformed_success_preserves_catalog(data, refresh, payload):
    refresh.payload = payload
    assert run_refresh(data, refresh).status_code == 503
    assert goods_pages(data) == refresh.old


def test_catalog_refresh_commit_failure_never_acknowledges_new_pages(data, refresh):
    def mark(session, *_):
        if any(isinstance(row, WbRepricerGoodsCacheRow) for row in session.new):
            session.info["refresh_test_pages_written"] = True
    def reject(session):
        if session.info.get("refresh_test_pages_written"):
            raise SQLAlchemyError("private-commit-error")
    event.listen(data.factory, "before_flush", mark)
    event.listen(data.factory, "before_commit", reject)
    try:
        result = run_refresh(data, refresh)
    finally:
        event.remove(data.factory, "before_flush", mark)
        event.remove(data.factory, "before_commit", reject)
    assert result.status_code == 503 and "private-commit" not in result.text
    assert goods_pages(data) == refresh.old and not refresh.invalidations


def test_catalog_refresh_page_cap_preserves_the_previous_complete_catalog(data, refresh, monkeypatch):
    monkeypatch.setattr(data.module, "REFRESH_MAX_PAGES", 2)
    def during_network(request):
        start = request.query["offset"] + 1
        refresh.payload = {"data": {"listGoods": [{"vendorCode": f"P-{nm}", "nmID": nm,
            "sizes": [{"sizeID": nm, "discountedPrice": 1000}]} for nm in range(start, start + 1000)]}}
    refresh.hook = during_network
    assert run_refresh(data, refresh).status_code == 503
    assert [request.query["offset"] for request in refresh.requests] == [0, 1000]
    assert goods_pages(data) == refresh.old


def test_catalog_refresh_rechecks_revocation_before_requesting_another_page(data, refresh):
    def during_network(_request):
        refresh.payload = {"data": {"listGoods": [{"vendorCode": f"P-{nm}", "nmID": nm,
            "sizes": [{"sizeID": nm, "discountedPrice": 1000}]} for nm in range(1, 1001)]}}
        with data.factory() as session, session.begin():
            session.get(LkSessionRow, "login").revoked_at = data.now
    refresh.hook = during_network
    assert run_refresh(data, refresh).status_code == 403
    assert len(refresh.requests) == 1 and goods_pages(data) == refresh.old


@pytest.mark.parametrize("missing", ["token", "binding", "login", "regular_jwt"])
def test_catalog_refresh_denies_before_network_without_current_authority(data, refresh, missing):
    with data.factory() as session, session.begin():
        if missing == "token": session.delete(session.get(LkUserWbTokenRow, 1))
        elif missing == "binding": session.get(MarketplaceAccountRow, 10).credential_ref = None
        elif missing == "login": session.get(LkSessionRow, "login").revoked_at = data.now
    if missing == "regular_jwt": refresh.headers = {"Authorization": "Bearer synthetic-ui"}
    assert run_refresh(data, refresh).status_code in (401, 403)
    assert not refresh.requests and goods_pages(data) == refresh.old


def test_catalog_refresh_publishes_all_pages_together_and_removes_old_pages(data, refresh):
    with data.factory() as session, session.begin():
        session.add(WbRepricerGoodsCacheRow(organization_id=1, page_offset=5000, page_limit=1000,
            fetched_at=data.now - timedelta(minutes=42), goods_payload=deepcopy(refresh.old[0][2])))
    def during_network(request):
        assert len(goods_pages(data)) == 2
        start, count = (1, 1000) if request.query["offset"] == 0 else (1001, 1)
        refresh.payload = {"data": {"listGoods": [{"vendorCode": f"P-{nm}", "nmID": nm,
            "sizes": [{"sizeID": nm, "discountedPrice": 1000}]} for nm in range(start, start + count)]}}
    refresh.hook = during_network
    result = run_refresh(data, refresh)
    assert result.status_code == 200 and result.json()["total"] == 1001
    assert [(offset, len(rows)) for offset, _, rows, _ in goods_pages(data)] == [(0, 1000), (1000, 1)]
    assert catalog(data, refresh.headers)["goodsRevision"] == result.json()["goodsRevision"]


def test_catalog_refresh_provider_setup_failure_is_safe_and_retryable(data, refresh, monkeypatch):
    def unavailable(**_kwargs):
        raise RuntimeError("private-driver-and-credential-message")
    monkeypatch.setattr(data.module, "build_wb_client", unavailable)
    response = run_refresh(data, refresh)
    assert response.status_code == 503 and "private-driver" not in response.text
    assert response.json()["detail"]["retryAfterSeconds"] >= 60
    assert not refresh.requests and goods_pages(data) == refresh.old


def test_catalog_refresh_does_not_sleep_past_bounded_provider_budget(data, refresh, monkeypatch):
    original = data.module.RateLimitedWbApiClient
    class WaitingQuota(original):
        def request(self, request):
            self._sleep(900)
            return super().request(request)
    monkeypatch.setattr(data.module, "RateLimitedWbApiClient", WaitingQuota)
    response = run_refresh(data, refresh)
    assert response.status_code == 503
    assert not refresh.requests and goods_pages(data) == refresh.old
