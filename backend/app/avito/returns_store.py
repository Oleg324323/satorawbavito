from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.avito.returns import AvitoReturnCandidate
from app.avito.returns_orm import AvitoReturnInventoryEventRow, AvitoReturnItemRow
from app.avito.orders_queue import PICKUP_RETURN_STATUSES, _timestamp
from app.infra.db import get_session_factory


def _run_db(db_fn):
    try:
        session_factory = get_session_factory()
        with session_factory() as session:
            return db_fn(session)
    except SQLAlchemyError:
        return None


def _norm_identity_part(value: str | None) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.U)
    return re.sub(r"\s+", " ", text).strip()


def return_candidate_identity_key(candidate: AvitoReturnCandidate) -> str:
    account = _norm_identity_part(candidate.accountId)
    item_identity = _norm_identity_part(candidate.itemId)
    if not item_identity:
        item_identity = "|".join(
            part
            for part in (
                _norm_identity_part(candidate.sellerArticle),
                _norm_identity_part(candidate.title),
                _norm_identity_part(candidate.size),
                _norm_identity_part(candidate.color),
            )
            if part
        )
    if candidate.lineIndex is not None:
        item_identity = f"{item_identity or 'item:none'}:line:{candidate.lineIndex}"
    return "|".join(
        part
        for part in (
            account or "account:none",
            _norm_identity_part(candidate.returnOrderId),
            item_identity or "item:none",
        )
        if part
    )


def _candidate_from_row(row: AvitoReturnItemRow) -> AvitoReturnCandidate:
    last_seen_at = row.last_seen_at.isoformat() if row.last_seen_at else None
    return AvitoReturnCandidate(
        returnOrderId=row.order_id,
        marketplaceId=row.marketplace_id,
        accountId=row.account_id,
        accountName=row.account_name,
        itemId=row.item_id,
        lineIndex=row.line_index,
        title=row.title,
        sellerArticle=row.seller_article,
        size=row.size,
        color=row.color,
        imageUrl=row.image_url,
        sources=(row.last_payload or {}).get("sources") or {},
        quantity=row.quantity,
        status=row.status,
        returnStatus=row.return_status,
        sourceUpdatedAt=row.source_updated_at,
        lastSeenAt=last_seen_at,
        returnItemId=row.return_item_id,
        receivedQuantity=row.received_quantity,
        inspectedQuantity=row.inspected_quantity,
        reservedQuantity=row.reserved_quantity,
        sentQuantity=row.sent_quantity,
        writtenOffQuantity=row.written_off_quantity,
        availableQuantity=max(0, row.inspected_quantity - row.reserved_quantity - row.sent_quantity - row.written_off_quantity),
    )


def _apply_candidate(row: AvitoReturnItemRow, candidate: AvitoReturnCandidate, *, now: datetime) -> None:
    row.account_id = candidate.accountId or row.account_id
    row.account_name = candidate.accountName or row.account_name
    row.order_id = candidate.returnOrderId
    row.marketplace_id = candidate.marketplaceId or row.marketplace_id
    row.item_id = candidate.itemId or row.item_id
    row.line_index = candidate.lineIndex if candidate.lineIndex is not None else row.line_index
    row.title = row.title or candidate.title
    row.seller_article = row.seller_article or candidate.sellerArticle
    row.size = row.size or candidate.size
    row.color = row.color or candidate.color
    row.image_url = row.image_url or candidate.imageUrl
    row.quantity = max(candidate.quantity, row.received_quantity)
    row.status = candidate.status
    row.return_status = candidate.returnStatus or row.return_status
    row.source_updated_at = candidate.sourceUpdatedAt or row.source_updated_at
    row.last_payload = candidate.model_dump(mode="json")
    row.last_seen_at = now
    row.updated_at = now


def upsert_return_candidates(organization_id: int, candidates: list[AvitoReturnCandidate]) -> dict[str, int]:
    now = datetime.now(timezone.utc)

    def _db(session: Session) -> dict[str, int]:
        inserted = 0
        updated = 0
        for candidate in candidates:
            identity_key = return_candidate_identity_key(candidate)
            row = session.scalar(
                select(AvitoReturnItemRow).where(
                    AvitoReturnItemRow.organization_id == organization_id,
                    AvitoReturnItemRow.identity_key == identity_key,
                )
            )
            if row is None:
                legacy = list(session.scalars(select(AvitoReturnItemRow).where(
                    AvitoReturnItemRow.organization_id == organization_id,
                    AvitoReturnItemRow.account_id == candidate.accountId,
                    AvitoReturnItemRow.order_id == candidate.returnOrderId,
                    or_(AvitoReturnItemRow.line_index.is_(None), AvitoReturnItemRow.line_index == candidate.lineIndex),
                )).all()) if candidate.lineIndex is not None else []
                compatible = [known for known in legacy if (
                    (not known.item_id or not candidate.itemId or known.item_id == candidate.itemId)
                    and (not known.size or not candidate.size or known.size == candidate.size)
                    and (not known.color or not candidate.color or known.color == candidate.color)
                )]
                if len(compatible) == 1:
                    row = compatible[0]
                    row.identity_key = identity_key
                    _apply_candidate(row, candidate, now=now)
                    updated += 1
                    continue
                row = AvitoReturnItemRow(
                    organization_id=organization_id,
                    identity_key=identity_key,
                    account_id=candidate.accountId,
                    account_name=candidate.accountName,
                    order_id=candidate.returnOrderId,
                    marketplace_id=candidate.marketplaceId,
                    item_id=candidate.itemId,
                    line_index=candidate.lineIndex,
                    title=candidate.title,
                    seller_article=candidate.sellerArticle,
                    size=candidate.size,
                    color=candidate.color,
                    image_url=candidate.imageUrl,
                    quantity=candidate.quantity,
                    status=candidate.status,
                    return_status=candidate.returnStatus,
                    source_updated_at=candidate.sourceUpdatedAt,
                    last_payload=candidate.model_dump(mode="json"),
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
                session.add(row)
                inserted += 1
            else:
                previous = _timestamp(row.source_updated_at)
                incoming = _timestamp(candidate.sourceUpdatedAt)
                if previous and (not incoming or incoming < previous):
                    continue
                _apply_candidate(row, candidate, now=now)
                updated += 1
        session.commit()
        return {"inserted": inserted, "updated": updated}

    result = _run_db(_db)
    if result is None:
        raise RuntimeError("AVITO_RETURN_STORE_UNAVAILABLE")
    return result


def enrich_existing_return_candidates(organization_id: int, candidates: list[AvitoReturnCandidate]) -> dict[str, int]:
    now = datetime.now(timezone.utc)

    def _matches_existing(row: AvitoReturnItemRow, candidate: AvitoReturnCandidate) -> bool:
        if _norm_identity_part(row.account_id) != _norm_identity_part(candidate.accountId):
            return False
        if _norm_identity_part(row.order_id) != _norm_identity_part(candidate.returnOrderId):
            return False
        if row.line_index is not None and row.line_index != candidate.lineIndex:
            return False
        if row.item_id and candidate.itemId:
            return _norm_identity_part(row.item_id) == _norm_identity_part(candidate.itemId)
        if row.seller_article and candidate.sellerArticle:
            return _norm_identity_part(row.seller_article) == _norm_identity_part(candidate.sellerArticle)
        return bool(row.title and candidate.title and _norm_identity_part(row.title) == _norm_identity_part(candidate.title))

    def _db(session: Session) -> dict[str, int]:
        updated = 0
        for candidate in candidates:
            row = session.scalar(
                select(AvitoReturnItemRow).where(
                    AvitoReturnItemRow.organization_id == organization_id,
                    AvitoReturnItemRow.identity_key == return_candidate_identity_key(candidate),
                )
            )
            if row is None:
                possible_rows = list(
                    session.scalars(
                        select(AvitoReturnItemRow).where(
                            AvitoReturnItemRow.organization_id == organization_id,
                            AvitoReturnItemRow.order_id == candidate.returnOrderId,
                        )
                    ).all()
                )
                row = next((possible for possible in possible_rows if _matches_existing(possible, candidate)), None)
            if row is None:
                continue
            changed = False
            if candidate.itemId and not row.item_id:
                row.item_id = candidate.itemId
                changed = True
            if candidate.title and (not row.title or row.title in {"Товар", "Товар Авито"}):
                row.title = candidate.title
                changed = True
            if candidate.sellerArticle and not row.seller_article and candidate.sources.get("sellerArticle") in {"order_row", "order_detail"}:
                row.seller_article = candidate.sellerArticle
                changed = True
            if candidate.size and not row.size and candidate.sources.get("size") in {"order_row", "order_detail"}:
                row.size = candidate.size
                changed = True
            if candidate.color and not row.color and candidate.sources.get("color") in {"order_row", "order_detail"}:
                row.color = candidate.color
                changed = True
            if candidate.imageUrl and not row.image_url and candidate.sources.get("imageUrl") in {"order_row", "order_detail"}:
                row.image_url = candidate.imageUrl
                changed = True
            if changed:
                payload = dict(row.last_payload or {})
                payload.update(candidate.model_dump(mode="json", exclude_none=True))
                row.last_payload = payload
                row.updated_at = now
                updated += 1
        session.commit()
        return {"updated": updated}

    return _run_db(_db) or {"updated": 0}


def list_active_return_candidates(organization_id: int, limit: int = 500) -> list[AvitoReturnCandidate]:
    safe_limit = max(1, min(2000, int(limit)))

    def _db(session: Session) -> list[AvitoReturnCandidate]:
        rows = list(
            session.scalars(
                select(AvitoReturnItemRow)
                .where(AvitoReturnItemRow.organization_id == organization_id)
                .order_by(AvitoReturnItemRow.last_seen_at.desc())
                .limit(safe_limit)
            ).all()
        )
        return [_candidate_from_row(row) for row in rows]

    return _run_db(_db) or []


def list_return_inventory(organization_id: int, *, account_id: str | None = None, page: int = 1, limit: int = 50) -> tuple[list[AvitoReturnCandidate], int]:
    def _db(session: Session) -> tuple[list[AvitoReturnCandidate], int]:
        query = select(AvitoReturnItemRow).where(AvitoReturnItemRow.organization_id == organization_id)
        if account_id:
            query = query.where(AvitoReturnItemRow.account_id == account_id)
        from sqlalchemy import func
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(AvitoReturnItemRow.last_seen_at.desc()).offset((page - 1) * limit).limit(limit)).all()
        return [_candidate_from_row(row) for row in rows], total

    result = _run_db(_db)
    if result is None:
        raise RuntimeError("AVITO_RETURN_STORE_UNAVAILABLE")
    return result


def get_return_inventory_item(organization_id: int, return_item_id: int) -> AvitoReturnCandidate | None:
    def _db(session: Session) -> AvitoReturnCandidate | None:
        row = session.scalar(select(AvitoReturnItemRow).where(
            AvitoReturnItemRow.organization_id == organization_id,
            AvitoReturnItemRow.return_item_id == return_item_id,
        ))
        return _candidate_from_row(row) if row else None

    return _run_db(_db)


def list_return_inventory_events(organization_id: int, return_item_id: int, *, page: int = 1, limit: int = 50) -> dict[str, Any]:
    with get_session_factory()() as session:
        item = session.scalar(select(AvitoReturnItemRow.return_item_id).where(
            AvitoReturnItemRow.organization_id == organization_id,
            AvitoReturnItemRow.return_item_id == return_item_id,
        ))
        if item is None:
            raise LookupError("RETURN_ITEM_NOT_FOUND")
        event = AvitoReturnInventoryEventRow
        scope = event.organization_id == organization_id, event.return_item_id == return_item_id
        total = session.scalar(select(func.count()).select_from(event).where(*scope)) or 0
        rows = session.scalars(select(event).where(*scope).order_by(event.event_id.desc()).offset((page - 1) * limit).limit(limit)).all()
        delta = case((event.action == "reserve", event.quantity), else_=-event.quantity)
        balances = session.execute(select(event.linked_order_id, func.sum(delta)).where(
            *scope, event.action.in_(("reserve", "release", "ship")),
        ).group_by(event.linked_order_id)).all()
        return {
            "events": [{
                "action": row.action, "quantity": row.quantity, "actorId": row.actor_id,
                "linkedOrderId": row.linked_order_id, "createdAt": row.created_at.isoformat(),
            } for row in rows],
            "reservations": [{"linkedOrderId": order_id, "quantity": int(quantity)} for order_id, quantity in balances if order_id and quantity > 0],
            "total": total, "page": page, "limit": limit,
        }


def _reserved_for_order(session: Session, return_item_id: int, linked_order_id: str) -> int:
    events = session.scalars(select(AvitoReturnInventoryEventRow).where(
        AvitoReturnInventoryEventRow.return_item_id == return_item_id,
        AvitoReturnInventoryEventRow.linked_order_id == linked_order_id,
        AvitoReturnInventoryEventRow.action.in_(("reserve", "release", "ship")),
    )).all()
    return sum(event.quantity * (1 if event.action == "reserve" else -1) for event in events)


def apply_return_operation(
    organization_id: int, return_item_id: int, *, operation_id: str, action: str,
    quantity: int, actor_id: str, linked_order_id: str | None = None,
) -> AvitoReturnCandidate:
    if action not in {"receive", "inspect", "reserve", "release", "ship", "write_off"} or quantity < 1:
        raise ValueError("INVALID_RETURN_OPERATION")
    if action in {"reserve", "release", "ship"} and not linked_order_id:
        raise ValueError("LINKED_ORDER_REQUIRED")
    if not operation_id or len(operation_id) > 128:
        raise ValueError("INVALID_OPERATION_ID")
    with get_session_factory()() as session:
        try:
            existing = session.scalar(select(AvitoReturnInventoryEventRow).where(
                AvitoReturnInventoryEventRow.organization_id == organization_id,
                AvitoReturnInventoryEventRow.operation_id == operation_id,
            ))
            if existing:
                if (existing.return_item_id, existing.action, existing.quantity, existing.linked_order_id) != (return_item_id, action, quantity, linked_order_id):
                    raise ValueError("OPERATION_ID_CONFLICT")
                row = session.scalar(select(AvitoReturnItemRow).where(
                    AvitoReturnItemRow.organization_id == organization_id, AvitoReturnItemRow.return_item_id == return_item_id,
                ))
                if row is None:
                    raise LookupError("RETURN_ITEM_NOT_FOUND")
                return _candidate_from_row(row)
            row = session.scalar(select(AvitoReturnItemRow).where(
                AvitoReturnItemRow.organization_id == organization_id,
                AvitoReturnItemRow.return_item_id == return_item_id,
            ).with_for_update())
            if row is None:
                raise LookupError("RETURN_ITEM_NOT_FOUND")
            if action == "receive" and (row.status != "on_return" or (row.return_status or "").lower() not in PICKUP_RETURN_STATUSES):
                raise ValueError("RETURN_NOT_READY_FOR_PICKUP")
            if action in {"release", "ship"} and _reserved_for_order(session, return_item_id, linked_order_id or "") < quantity:
                raise ValueError("RESERVATION_NOT_AVAILABLE")
            stock = AvitoReturnItemRow
            free = stock.inspected_quantity - stock.reserved_quantity - stock.sent_quantity - stock.written_off_quantity
            transitions = {
                "receive": (stock.quantity - stock.received_quantity >= quantity, {"received_quantity": stock.received_quantity + quantity}),
                "inspect": (stock.received_quantity - stock.inspected_quantity >= quantity, {"inspected_quantity": stock.inspected_quantity + quantity}),
                "reserve": (free >= quantity, {"reserved_quantity": stock.reserved_quantity + quantity}),
                "release": (stock.reserved_quantity >= quantity, {"reserved_quantity": stock.reserved_quantity - quantity}),
                "ship": (stock.reserved_quantity >= quantity, {"reserved_quantity": stock.reserved_quantity - quantity, "sent_quantity": stock.sent_quantity + quantity}),
                "write_off": (free >= quantity, {"written_off_quantity": stock.written_off_quantity + quantity}),
            }
            condition, values = transitions[action]
            changed = session.execute(update(stock).where(
                stock.organization_id == organization_id, stock.return_item_id == return_item_id, condition,
            ).values(**values))
            if changed.rowcount != 1:
                raise ValueError("RETURN_QUANTITY_UNAVAILABLE")
            session.add(AvitoReturnInventoryEventRow(
                organization_id=organization_id, return_item_id=return_item_id,
                operation_id=operation_id, action=action, quantity=quantity,
                actor_id=actor_id, linked_order_id=linked_order_id,
            ))
            session.commit()
            session.refresh(row)
            return _candidate_from_row(row)
        except IntegrityError:
            session.rollback()
            existing = session.scalar(select(AvitoReturnInventoryEventRow).where(
                AvitoReturnInventoryEventRow.organization_id == organization_id,
                AvitoReturnInventoryEventRow.operation_id == operation_id,
            ))
            if existing and existing.return_item_id == return_item_id and existing.action == action and existing.quantity == quantity and existing.linked_order_id == linked_order_id:
                row = session.get(AvitoReturnItemRow, return_item_id)
                if row and row.organization_id == organization_id:
                    return _candidate_from_row(row)
            raise


def return_pickup_remaining(organization_id: int, account_id: str | None = None) -> dict[tuple[str, str, int | None, str], int]:
    def _db(session: Session) -> dict[tuple[str, str, int | None, str], int]:
        query = select(AvitoReturnItemRow).where(AvitoReturnItemRow.organization_id == organization_id)
        if account_id:
            query = query.where(AvitoReturnItemRow.account_id == account_id)
        return {
            (row.account_id or "", row.order_id, row.line_index, row.item_id or ""): max(0, row.quantity - row.received_quantity)
            for row in session.scalars(query).all()
        }

    result = _run_db(_db)
    if result is None:
        raise RuntimeError("AVITO_RETURN_STORE_UNAVAILABLE")
    return result


def release_canceled_reservations(organization_id: int, canceled_order_ids: set[str]) -> int:
    if not canceled_order_ids:
        return 0

    def _db(session: Session) -> list[tuple[int, str, int]]:
        events = session.scalars(select(AvitoReturnInventoryEventRow).where(
            AvitoReturnInventoryEventRow.organization_id == organization_id,
            AvitoReturnInventoryEventRow.linked_order_id.in_(canceled_order_ids),
            AvitoReturnInventoryEventRow.action.in_(("reserve", "release", "ship")),
        )).all()
        balances: dict[tuple[int, str], int] = {}
        for event in events:
            key = event.return_item_id, event.linked_order_id or ""
            balances[key] = balances.get(key, 0) + event.quantity * (1 if event.action == "reserve" else -1)
        return [(item_id, order_id, quantity) for (item_id, order_id), quantity in balances.items() if quantity > 0]

    released = 0
    for item_id, order_id, quantity in _run_db(_db) or []:
        try:
            apply_return_operation(
                organization_id, item_id, operation_id=f"cancel:{order_id}:{item_id}",
                action="release", quantity=quantity, actor_id="system:avito_status", linked_order_id=order_id,
            )
            released += quantity
        except ValueError:
            continue
    return released
