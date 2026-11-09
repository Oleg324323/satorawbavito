from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.platform.catalog import orm as _catalog_models  # noqa: F401


class CatalogCostVersionRow(Base):
    __tablename__ = "catalog_cost_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "source",
            "source_reference",
            name="uq_catalog_cost_versions_org_source_reference",
        ),
        UniqueConstraint(
            "organization_id", "cost_version_id", name="uq_catalog_cost_versions_org_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "catalog_sku_id"],
            ["catalog_skus.organization_id", "catalog_skus.catalog_sku_id"],
            name="fk_catalog_cost_versions_org_sku",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["iam_memberships.organization_id", "iam_memberships.membership_id"],
            name="fk_catalog_cost_versions_org_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supersedes_cost_version_id"],
            [
                "catalog_cost_versions.organization_id",
                "catalog_cost_versions.cost_version_id",
            ],
            name="fk_catalog_cost_versions_org_supersedes",
        ),
        CheckConstraint(
            "source_reference <> ''", name="ck_catalog_cost_versions_source_reference"
        ),
        CheckConstraint(
            "(value_state = 'configured' AND amount_kopecks >= 0) OR "
            "(value_state = 'assumed' AND amount_kopecks > 0) OR "
            "(value_state = 'missing' AND amount_kopecks IS NULL)",
            name="ck_catalog_cost_versions_value",
        ),
        CheckConstraint(
            "evidence_status IN ('dated', 'undated', 'period_end_fallback')",
            name="ck_catalog_cost_versions_evidence_status",
        ),
        Index(
            "ix_catalog_cost_versions_resolve",
            "organization_id",
            "catalog_sku_id",
            "effective_from",
            "created_at",
        ),
    )

    cost_version_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("lk_organizations.organization_id"), nullable=False
    )
    catalog_sku_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_kopecks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_state: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_cost_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_by_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrganizationEconomicsVersionRow(Base):
    __tablename__ = "organization_economics_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "source",
            "source_reference",
            name="uq_organization_economics_org_source_reference",
        ),
        UniqueConstraint(
            "organization_id",
            "organization_economics_version_id",
            name="uq_organization_economics_org_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["iam_memberships.organization_id", "iam_memberships.membership_id"],
            name="fk_organization_economics_org_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supersedes_organization_economics_version_id"],
            [
                "organization_economics_versions.organization_id",
                "organization_economics_versions.organization_economics_version_id",
            ],
            name="fk_organization_economics_org_supersedes",
        ),
        CheckConstraint(
            "(tax_basis_points IS NULL OR tax_basis_points BETWEEN 0 AND 10000) "
            "AND (other_expense_price_basis_points IS NULL OR "
            "other_expense_price_basis_points BETWEEN 0 AND 10000) "
            "AND (other_expense_per_sale_kopecks IS NULL OR "
            "other_expense_per_sale_kopecks >= 0)",
            name="ck_organization_economics_values",
        ),
        CheckConstraint(
            "(value_state IN ('configured', 'assumed') "
            "AND tax_basis_points IS NOT NULL "
            "AND other_expense_price_basis_points IS NOT NULL "
            "AND other_expense_per_sale_kopecks IS NOT NULL) "
            "OR (value_state = 'missing' "
            "AND tax_basis_points IS NULL "
            "AND other_expense_price_basis_points IS NULL "
            "AND other_expense_per_sale_kopecks IS NULL)",
            name="ck_organization_economics_state",
        ),
        CheckConstraint(
            "evidence_status IN ('dated', 'undated', 'period_end_fallback')",
            name="ck_organization_economics_evidence_status",
        ),
        CheckConstraint(
            "(tax_value_state IS NULL AND tax_evidence_status IS NULL) OR "
            "(tax_value_state IS NOT NULL AND tax_evidence_status IS NOT NULL "
            "AND tax_value_state = 'configured' AND tax_evidence_status = 'dated' "
            "AND tax_basis_points IS NOT NULL)",
            name="ck_organization_economics_tax_evidence",
        ),
        Index(
            "ix_organization_economics_resolve",
            "organization_id",
            "effective_from",
            "created_at",
        ),
    )

    organization_economics_version_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("lk_organizations.organization_id"), nullable=False
    )
    tax_basis_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_expense_price_basis_points: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    other_expense_per_sale_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    value_state: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_value_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tax_evidence_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    supersedes_organization_economics_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_by_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CatalogEconomicsOverrideVersionRow(Base):
    __tablename__ = "catalog_economics_override_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "source",
            "source_reference",
            name="uq_catalog_economics_override_org_source_reference",
        ),
        UniqueConstraint(
            "organization_id",
            "catalog_economics_override_version_id",
            name="uq_catalog_economics_override_org_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "catalog_sku_id"],
            ["catalog_skus.organization_id", "catalog_skus.catalog_sku_id"],
            name="fk_catalog_economics_override_org_sku",
        ),
        ForeignKeyConstraint(
            ["organization_id", "created_by_membership_id"],
            ["iam_memberships.organization_id", "iam_memberships.membership_id"],
            name="fk_catalog_economics_override_org_membership",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supersedes_catalog_economics_override_version_id"],
            [
                "catalog_economics_override_versions.organization_id",
                "catalog_economics_override_versions.catalog_economics_override_version_id",
            ],
            name="fk_catalog_economics_override_org_supersedes",
        ),
        CheckConstraint(
            "(tax_basis_points IS NULL OR tax_basis_points BETWEEN 0 AND 10000) "
            "AND (other_expense_price_basis_points IS NULL OR "
            "other_expense_price_basis_points BETWEEN 0 AND 10000) "
            "AND (other_expense_per_sale_kopecks IS NULL OR "
            "other_expense_per_sale_kopecks >= 0)",
            name="ck_catalog_economics_override_values",
        ),
        CheckConstraint(
            "value_state IN ('configured', 'assumed')",
            name="ck_catalog_economics_override_state",
        ),
        CheckConstraint(
            "evidence_status IN ('dated', 'undated', 'period_end_fallback')",
            name="ck_catalog_economics_override_evidence_status",
        ),
        Index(
            "ix_catalog_economics_override_resolve",
            "organization_id",
            "catalog_sku_id",
            "effective_from",
            "created_at",
        ),
    )

    catalog_economics_override_version_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("lk_organizations.organization_id"), nullable=False
    )
    catalog_sku_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_basis_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_expense_price_basis_points: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    other_expense_per_sale_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    value_state: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    supersedes_catalog_economics_override_version_id: Mapped[int | None] = (
        mapped_column(Integer, nullable=True)
    )
    created_by_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
