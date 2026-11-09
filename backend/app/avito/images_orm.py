"""Successful product photo downloads, isolated by tenant and exact URL hash."""
from datetime import datetime
from sqlalchemy import DateTime, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.models import Base


class AvitoProductImage(Base):
    __tablename__ = 'avito_product_images'
    __table_args__ = (UniqueConstraint('organization_id', 'url_hash', name='uq_avito_product_image_url'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    url_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    extension: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AvitoListingPhoto(Base):
    __tablename__ = 'avito_listing_photos'
    __table_args__ = (UniqueConstraint('organization_id', 'account_id', 'item_id', name='uq_avito_listing_photo'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[str] = mapped_column(String(128))
    item_id: Mapped[str] = mapped_column(String(128))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    thumbnail: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
