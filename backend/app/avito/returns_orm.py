from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base


class AvitoReturnItemRow(Base):
    __tablename__ = "avito_return_items"
    __table_args__ = (
        UniqueConstraint("organization_id", "identity_key", name="uq_avito_return_items_org_identity"),
    )

    return_item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(Integer, ForeignKey("lk_organizations.organization_id"), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    marketplace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    line_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    seller_article: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    inspected_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sent_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    written_off_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="on_return")
    return_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AvitoReturnInventoryEventRow(Base):
    __tablename__ = "avito_return_inventory_events"
    __table_args__ = (UniqueConstraint("organization_id", "operation_id", name="uq_avito_return_inventory_org_operation"),)

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(Integer, ForeignKey("lk_organizations.organization_id"), nullable=False, index=True)
    return_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("avito_return_items.return_item_id"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    linked_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
