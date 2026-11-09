from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.avito.orders import AvitoOrderItem, AvitoOrderRow
from app.avito.returns import extract_return_candidates, match_return_candidates
from app.cabinet.store import AvitoCredentialsSecret
from app.control_plane.auth import ActorContext
from app.main import create_app


def test_extracts_on_return_items_as_reusable_candidates():
    rows = [
        AvitoOrderRow(
            orderId="ret_1",
            marketplaceId="7001",
            accountId="acc_1",
            accountName="Bless T",
            status="on_return",
            returnStatus="started",
            updatedAt="2026-08-07T09:00:00+00:00",
            items=[
                AvitoOrderItem(
                    itemId="item_1",
                    title="Футболка белая Принт 42",
                    sellerArticle="FBBT_42",
                    size="M",
                    color="белая",
                    quantity=1,
                )
            ],
        )
    ]

    candidates = extract_return_candidates(rows)

    assert len(candidates) == 1
    assert candidates[0].returnOrderId == "ret_1"
    assert candidates[0].marketplaceId == "7001"
    assert candidates[0].accountId == "acc_1"
    assert candidates[0].accountName == "Bless T"
    assert candidates[0].sellerArticle == "FBBT_42"
    assert candidates[0].size == "M"
    assert candidates[0].color == "белая"
    assert candidates[0].quantity == 1
    assert candidates[0].status == "on_return"
    assert candidates[0].returnStatus == "started"
    assert candidates[0].sourceUpdatedAt == "2026-08-07T09:00:00+00:00"


def test_matches_return_candidate_by_article_size_and_color():
    order = AvitoOrderRow(
        orderId="new_1",
        status="ready_to_ship",
        items=[
            AvitoOrderItem(
                title="Футболка белая Принт 42",
                sellerArticle="FBBT_42",
                size="M",
                color="белый",
            )
        ],
    )
    candidates = extract_return_candidates(
        [
            AvitoOrderRow(
                orderId="ret_1",
                marketplaceId="7001",
                status="on_return",
                items=[
                    AvitoOrderItem(
                        itemId="item_1",
                        title="Футболка белая Принт 42",
                        sellerArticle="FBBT_42",
                        size="M",
                        color="white",
                    )
                ],
            )
        ]
    )
    assert match_return_candidates(order.items[0], order=order, candidates=candidates) == []
    candidates[0].availableQuantity = 1

    matches = match_return_candidates(order.items[0], order=order, candidates=candidates)

    assert matches[0].returnOrderId == "ret_1"
    assert matches[0].score == 100
    assert matches[0].reason == "article_size_color"


def test_does_not_match_when_size_or_color_conflicts():
    order = AvitoOrderRow(
        orderId="new_1",
        status="ready_to_ship",
        items=[AvitoOrderItem(title="Худи", sellerArticle="HCBT_17", size="L", color="черный")],
    )
    candidates = extract_return_candidates(
        [
            AvitoOrderRow(
                orderId="ret_1",
                status="on_return",
                items=[AvitoOrderItem(title="Худи", sellerArticle="HCBT_17", size="XL", color="черный")],
            ),
            AvitoOrderRow(
                orderId="ret_2",
                status="on_return",
                items=[AvitoOrderItem(title="Худи", sellerArticle="HCBT_17", size="L", color="белый")],
            ),
        ]
    )

    assert match_return_candidates(order.items[0], order=order, candidates=candidates) == []


def test_matches_by_title_size_color_when_article_missing():
    order = AvitoOrderRow(
        orderId="new_1",
        status="ready_to_ship",
        items=[AvitoOrderItem(title="Лонгслив Vintage Stars", size="S", color="графит")],
    )
    candidates = extract_return_candidates(
        [
            AvitoOrderRow(
                orderId="ret_1",
                status="on_return",
                items=[AvitoOrderItem(title="лонгслив vintage stars", size="S", color="graphite")],
            )
        ]
    )
    candidates[0].availableQuantity = 1

    matches = match_return_candidates(order.items[0], order=order, candidates=candidates)

    assert matches[0].score == 90
    assert matches[0].reason == "title_size_color"


def test_upserts_return_candidates_without_duplicates(monkeypatch, tmp_path):
    import app.cabinet.orm  # noqa: F401
    import app.avito.returns_store as store
    from app.avito.returns_orm import AvitoReturnItemRow
    from app.infra.models import Base

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'returns.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)

    candidates = extract_return_candidates(
        [
            AvitoOrderRow(
                orderId="ret_1",
                marketplaceId="7001",
                accountId="acc_1",
                accountName="Bless T",
                status="on_return",
                returnStatus="started",
                items=[AvitoOrderItem(itemId="item_1", title="Футболка белая Принт 42", sellerArticle="FBBT_42", size="M", color="белая")],
            )
        ]
    )

    first = store.upsert_return_candidates(organization_id=1, candidates=candidates)
    second = store.upsert_return_candidates(organization_id=1, candidates=candidates)
    candidates[0].sourceUpdatedAt = "2026-09-30T11:00:00Z"
    candidates[0].returnStatus = "ready_for_pickup"
    store.upsert_return_candidates(organization_id=1, candidates=candidates)
    stale = candidates[0].model_copy(update={"sourceUpdatedAt": "2026-09-30T13:00:00+03:00", "returnStatus": "started"})
    store.upsert_return_candidates(organization_id=1, candidates=[stale])
    rows = store.list_active_return_candidates(organization_id=1)

    assert first == {"inserted": 1, "updated": 0}
    assert second == {"inserted": 0, "updated": 1}
    assert len(rows) == 1
    assert rows[0].returnOrderId == "ret_1"
    assert rows[0].sellerArticle == "FBBT_42"
    assert rows[0].returnStatus == "ready_for_pickup"

    with factory() as session:
        assert session.query(AvitoReturnItemRow).count() == 1


def test_sync_returns_for_org_fetches_on_return_orders_and_persists_candidates(monkeypatch):
    import app.avito.returns_tasks as tasks
    import app.routers.avito_orders as orders_router
    from app.avito.orders import AvitoOrdersFetchRequest, AvitoOrdersFetchResult

    requests: list[AvitoOrdersFetchRequest] = []
    saved: list[tuple[int, list[str]]] = []
    statuses: list[tuple[int, str, dict]] = []
    cache: dict[tuple[int, str], dict] = {}

    class ReturnsOrdersClient:
        def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
            requests.append(request)
            return AvitoOrdersFetchResult(
                status="synced",
                total=1,
                orders=[
                    AvitoOrderRow(
                        orderId="ret_1",
                        marketplaceId="7001",
                        status="on_return",
                        returnStatus="started",
                        items=[
                            AvitoOrderItem(
                                itemId="item_1",
                                title="Футболка белая Принт 42",
                                sellerArticle="FBBT_42",
                                size="M",
                                color="белая",
                            )
                        ],
                    )
                ],
            )

    monkeypatch.setattr(
        tasks,
        "get_organization_avito_credentials_secret",
        lambda _organization_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr(tasks, "resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr(tasks, "build_avito_orders_client", lambda **_kwargs: ReturnsOrdersClient())
    monkeypatch.setattr(orders_router, "get_source_cache", lambda organization_id, key, **_kwargs: cache.get((organization_id, key)))
    monkeypatch.setattr(orders_router, "save_source_cache", lambda organization_id, key, payload: cache.__setitem__((organization_id, key), payload))
    monkeypatch.setattr(orders_router, "upsert_return_candidates", lambda organization_id, candidates: saved.append((organization_id, [item.returnOrderId for item in candidates])) or {"inserted": len(candidates), "updated": 0})
    monkeypatch.setattr(tasks, "save_source_cache", lambda organization_id, source_key, payload: statuses.append((organization_id, source_key, payload)) or payload)

    result = tasks.sync_returns_for_org.run(1, force=True)

    assert result["organizationId"] == 1
    assert result["syncedCount"] == 1
    assert requests[0].statuses == []
    assert saved == [(1, ["ret_1"])]
    assert statuses[-1][1] == "avito_returns_sync_status"
    assert statuses[-1][2]["state"] == "completed"


def test_returns_sync_settings_endpoint_persists_interval_and_manual_run(monkeypatch):
    import app.avito.returns_tasks as tasks
    import app.routers.avito_orders as router

    source_cache: dict[tuple[int, str], dict] = {}

    class ManualSyncTask:
        @staticmethod
        def delay(organization_id: int, scenario: str, force: bool):
            assert organization_id == 1
            assert scenario == "manual"
            assert force is True

            class Result:
                id = "task-return-sync-1"

            return Result()

    monkeypatch.setattr(
        router,
        "actor_from_request",
        lambda _request: ActorContext(
            actor_id="u1",
            user_id="u1",
            organization_id=1,
            permission_profile="admin",
            permissions=frozenset({"cabinet:read", "integrations:write", "sync:run"}),
        ),
    )
    monkeypatch.setattr(
        router,
        "has_permission",
        lambda _actor, permission: permission in {"cabinet:read", "integrations:write", "sync:run"},
    )
    monkeypatch.setattr(router, "list_active_return_candidates", lambda _organization_id: [])
    monkeypatch.setattr(router, "get_source_cache", lambda organization_id, source_key, slim=False: source_cache.get((organization_id, source_key)))
    monkeypatch.setattr(router, "save_source_cache", lambda organization_id, source_key, payload: source_cache.setdefault((organization_id, source_key), dict(payload)) or payload)
    monkeypatch.setattr(tasks, "get_source_cache", lambda organization_id, source_key, slim=False: source_cache.get((organization_id, source_key)))
    monkeypatch.setattr(tasks, "save_source_cache", lambda organization_id, source_key, payload: source_cache.__setitem__((organization_id, source_key), dict(payload)) or payload)
    monkeypatch.setattr(tasks, "sync_returns_for_org", ManualSyncTask)

    api = TestClient(create_app())

    saved = api.put("/api/v1/avito/orders/returns-sync", json={"enabled": True, "intervalMinutes": 30, "periodDays": 45})

    assert saved.status_code == 200
    assert saved.json()["intervalMinutes"] == 30
    assert saved.json()["periodDays"] == 45

    launched = api.post("/api/v1/avito/orders/returns-sync/run")

    assert launched.status_code == 200
    assert launched.json()["taskId"] == "task-return-sync-1"
    assert launched.json()["status"]["state"] == "queued"
