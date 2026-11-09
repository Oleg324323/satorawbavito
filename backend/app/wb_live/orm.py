"""Minimal read mappings for the live projection; Alembic owns DDL."""
from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, DateTime, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class _Owner:
    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)

class WbLiveProductRow(_Owner, Base):
    __tablename__ = "wb_live_products"
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vendor_code: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    brand: Mapped[str | None] = mapped_column(String)
    subject_id: Mapped[int | None] = mapped_column(BigInteger)
    subject_name: Mapped[str | None] = mapped_column(String)
    photo_url: Mapped[str | None] = mapped_column(String)
    discount_pct: Mapped[int | None] = mapped_column(Integer)
    club_discount_pct: Mapped[int | None] = mapped_column(Integer)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prices_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class WbLiveProductSizeRow(_Owner, Base):
    __tablename__ = "wb_live_product_sizes"
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chrt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tech_size: Mapped[str | None] = mapped_column(String)
    skus: Mapped[list | None] = mapped_column(JSONB)
    price_kopecks: Mapped[int | None] = mapped_column(BigInteger)
    discounted_price_kopecks: Mapped[int | None] = mapped_column(BigInteger)
    club_price_kopecks: Mapped[int | None] = mapped_column(BigInteger)
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prices_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class WbLiveSyncJobRow(_Owner, Base):
    __tablename__ = "wb_live_sync_jobs"
    job_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    credential_id: Mapped[UUID] = mapped_column(Uuid)
    credential_generation: Mapped[int] = mapped_column(BigInteger)
    account_incarnation: Mapped[int] = mapped_column(BigInteger)
    external_account_id: Mapped[str] = mapped_column(String)
    credential_ref: Mapped[str | None] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String)
    membership_id: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class WbLiveSyncSourceRow(_Owner, Base):
    __tablename__ = "wb_live_sync_sources"
    job_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String)
    checkpoint: Mapped[dict] = mapped_column(JSONB)
    processed: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    attempt: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String)
