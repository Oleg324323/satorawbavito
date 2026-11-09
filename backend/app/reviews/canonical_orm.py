"""Typed runtime mappings; install only via Alembic (RLS/triggers are mandatory).

These column mappings are not a replacement schema or a create_all bootstrap.
No relationship cascade or legacy review mapping is introduced.
Keep their registry private: importing a canonical consumer must not enroll
PostgreSQL-only tables in the legacy application's Base.metadata/create_all.
Alembic remains the sole owner of the complete physical Review schema.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Identity,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _CanonicalReviewBase(DeclarativeBase):
    """Isolated runtime mappings, never an application schema bootstrap."""


class ReviewOwnerColumns:
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    marketplace: Mapped[str] = mapped_column(String(16), nullable=False)


class CanonicalReviewRunRow(ReviewOwnerColumns, _CanonicalReviewBase):
    __tablename__ = "review_sync_runs_v2"

    sync_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_run_id: Mapped[str | None] = mapped_column(Text)
    source_run_id_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    run_sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    request_checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    completeness: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_count: Mapped[int] = mapped_column(BigInteger)
    manifest_checksum: Mapped[str | None] = mapped_column(String(64))
    coverage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    coverage_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    error_code: Mapped[str | None] = mapped_column(String(64))
    account_binding_schema_version: Mapped[int | None] = mapped_column(SmallInteger)
    account_binding_external_account_id: Mapped[str | None] = mapped_column(Text)
    account_binding_credential_ref: Mapped[str | None] = mapped_column(Text)
    account_binding_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    account_binding_checksum: Mapped[str | None] = mapped_column(Text)


class CanonicalReviewFactRow(ReviewOwnerColumns, _CanonicalReviewBase):
    __tablename__ = "review_facts"

    review_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    external_review_id: Mapped[str | None] = mapped_column(Text)
    external_review_id_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    current_observation_id: Mapped[UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(BigInteger)
    last_source_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    last_source_run_sequence: Mapped[int | None] = mapped_column(BigInteger)
    source_order_state: Mapped[str] = mapped_column(String(16))
    ambiguous_observation_id: Mapped[UUID | None] = mapped_column(Uuid)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CanonicalReviewObservationRow(ReviewOwnerColumns, _CanonicalReviewBase):
    __tablename__ = "review_observations"

    observation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    review_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger)
    source_run_id: Mapped[UUID] = mapped_column(Uuid)
    external_product_id: Mapped[str | None] = mapped_column(Text)
    external_product_id_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    text: Mapped[str | None] = mapped_column(Text)
    text_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    answered: Mapped[bool] = mapped_column(Boolean)
    can_answer: Mapped[bool | None] = mapped_column(Boolean)
    source_status: Mapped[str | None] = mapped_column(Text)
    source_status_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_schema_version: Mapped[str | None] = mapped_column(Text)
    source_schema_version_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    normalization_version: Mapped[str | None] = mapped_column(Text)
    normalization_version_utf8: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_checksum: Mapped[str] = mapped_column(String(64))


class CanonicalReviewRunItemRow(_CanonicalReviewBase):
    __tablename__ = "review_sync_run_items"

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(16), primary_key=True)
    sync_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    review_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(Uuid)
    content_checksum: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
