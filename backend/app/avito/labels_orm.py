"""Immutable, tenant-scoped originals and decoded transport labels."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.infra.models import Base


class AvitoLabelDocument(Base):
    __tablename__ = "avito_label_documents"
    __table_args__ = (UniqueConstraint("organization_id", "account_id", "sha256", name="uq_avito_label_document_hash"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[str] = mapped_column(String(128), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AvitoTransportLabel(Base):
    __tablename__ = "avito_transport_labels"
    __table_args__ = (UniqueConstraint("document_id", "page", "order_number", "barcode", name="uq_avito_label_binding"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[int] = mapped_column(ForeignKey("avito_label_documents.id"))
    order_number: Mapped[str] = mapped_column(String(128), index=True)
    page: Mapped[int] = mapped_column(Integer)
    barcode: Mapped[str] = mapped_column(String(256))
    barcode_type: Mapped[str] = mapped_column(String(32))
    barcode_png: Mapped[bytes] = mapped_column(LargeBinary)
