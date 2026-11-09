from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from app.cabinet.orm import LkOrganizationRow
from app.avito.returns_orm import AvitoReturnInventoryEventRow, AvitoReturnItemRow
from app.avito.returns import AvitoReturnCandidate
from app.avito.returns_store import apply_return_operation, list_return_inventory_events, upsert_return_candidates


def test_unavailable_store_is_not_reported_as_empty(monkeypatch):
    import app.avito.returns_store as store

    monkeypatch.setattr(store, "_run_db", lambda _query: None)
    with pytest.raises(RuntimeError, match="AVITO_RETURN_STORE_UNAVAILABLE"):
        store.list_return_inventory(1)
    with pytest.raises(RuntimeError, match="AVITO_RETURN_STORE_UNAVAILABLE"):
        store.return_pickup_remaining(1)


def test_partial_return_inventory_is_durable_and_reservation_is_idempotent(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    LkOrganizationRow.__table__.create(engine)
    AvitoReturnItemRow.__table__.create(engine)
    AvitoReturnInventoryEventRow.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr("app.avito.returns_store.get_session_factory", lambda: factory)
    with factory() as session:
        session.add(LkOrganizationRow(organization_id=1, slug="demo", name="Demo"))
        session.add(AvitoReturnItemRow(
            organization_id=1, identity_key="account|return|item", account_id="account", order_id="return",
            item_id="item", title="Товар", quantity=2, status="on_return", return_status="ready_for_pickup",
            last_payload={},
        ))
        session.commit()
        item_id = session.scalar(select(AvitoReturnItemRow.return_item_id))

    def act(operation_id, action, quantity=1, linked_order_id=None):
        return apply_return_operation(
            1, item_id, operation_id=operation_id, action=action, quantity=quantity,
            actor_id="user:manager", linked_order_id=linked_order_id,
        )

    received = act("receive-1", "receive")
    assert received.receivedQuantity == 1 and received.availableQuantity == 0
    inspected = act("inspect-1", "inspect")
    assert inspected.availableQuantity == 1
    reserved = act("reserve-1", "reserve", linked_order_id="new-order")
    assert reserved.availableQuantity == 0
    assert list_return_inventory_events(1, item_id)["reservations"] == [{"linkedOrderId": "new-order", "quantity": 1}]
    assert act("reserve-1", "reserve", linked_order_id="new-order").reservedQuantity == 1
    try:
        act("reserve-2", "reserve", linked_order_id="other-order")
    except ValueError as exc:
        assert str(exc) == "RETURN_QUANTITY_UNAVAILABLE"
    else:
        raise AssertionError("double reservation was accepted")
    assert act("release-1", "release", linked_order_id="new-order").availableQuantity == 1
    with factory() as session:
        assert len(session.scalars(select(AvitoReturnInventoryEventRow)).all()) == 4
    history = list_return_inventory_events(1, item_id)
    assert history["total"] == 4 and history["reservations"] == []
    assert history["events"][0]["actorId"] == "user:manager"
    try:
        list_return_inventory_events(2, item_id)
    except LookupError:
        pass
    else:
        raise AssertionError("another organization accessed return history")


def test_same_listing_with_two_variants_keeps_separate_return_stock(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    LkOrganizationRow.__table__.create(engine)
    AvitoReturnItemRow.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr("app.avito.returns_store.get_session_factory", lambda: factory)
    with factory() as session:
        session.add(LkOrganizationRow(organization_id=1, slug="demo", name="Demo"))
        session.commit()
    variants = [
        AvitoReturnCandidate(returnOrderId="return-1", accountId="account", itemId="item-1", lineIndex=index,
                             title="Худи", size=size, color=color, quantity=1)
        for index, size, color in ((0, "M", "чёрный"), (1, "L", "белый"))
    ]
    assert upsert_return_candidates(1, variants)["inserted"] == 2
    with factory() as session:
        rows = session.scalars(select(AvitoReturnItemRow).order_by(AvitoReturnItemRow.line_index)).all()
        assert [(row.line_index, row.size, row.color) for row in rows] == [(0, "M", "чёрный"), (1, "L", "белый")]

    partial = AvitoReturnCandidate(returnOrderId="return-2", accountId="account", lineIndex=0,
                                   title="Худи", size="M", color="чёрный", quantity=1)
    assert upsert_return_candidates(1, [partial])["inserted"] == 1
    with factory() as session:
        original_id = session.scalar(select(AvitoReturnItemRow.return_item_id).where(AvitoReturnItemRow.order_id == "return-2"))
    result = upsert_return_candidates(1, [partial.model_copy(update={"itemId": "item-2"})])
    assert result == {"inserted": 0, "updated": 1}
    with factory() as session:
        saved = session.scalars(select(AvitoReturnItemRow).where(AvitoReturnItemRow.order_id == "return-2")).all()
        assert len(saved) == 1 and saved[0].return_item_id == original_id and saved[0].item_id == "item-2"
