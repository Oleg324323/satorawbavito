from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.platform.integrations import orm as _integration_models  # noqa: F401


class WbFinanceSyncRunRow(Base):
    __tablename__ = "wb_finance_sync_runs"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "marketplace_account_id",
            "sync_run_id",
            name="uq_wb_finance_sync_runs_org_account_id",
        ),
        UniqueConstraint(
            "organization_id",
            "marketplace_account_id",
            "date_from",
            "date_to",
            "snapshot_checksum",
            name="uq_wb_finance_sync_runs_snapshot",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id"],
            [
                "marketplace_accounts.organization_id",
                "marketplace_accounts.marketplace_account_id",
            ],
            name="fk_wb_finance_sync_runs_org_account",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "parent_sync_run_id"],
            [
                "wb_finance_sync_runs.organization_id",
                "wb_finance_sync_runs.marketplace_account_id",
                "wb_finance_sync_runs.sync_run_id",
            ],
            name="fk_wb_finance_sync_runs_parent",
        ),
        CheckConstraint("date_from <= date_to", name="ck_wb_finance_sync_runs_period"),
        CheckConstraint(
            "parent_sync_run_id IS NULL OR parent_sync_run_id <> sync_run_id",
            name="ck_wb_finance_sync_runs_parent_not_self",
        ),
        Index(
            "ix_wb_finance_sync_runs_covering",
            "organization_id",
            "marketplace_account_id",
            "date_from",
            "date_to",
            "last_observed_at",
        ),
        Index(
            "ix_wb_finance_sync_runs_parent",
            "organization_id",
            "marketplace_account_id",
            "parent_sync_run_id",
        ),
    )

    sync_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("lk_organizations.organization_id"), nullable=False
    )
    marketplace_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_sync_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_materialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    is_rollup_materialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_pnl_rollup_materialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    is_daily_pnl_rollup_materialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WbFinanceOperationRow(Base):
    __tablename__ = "wb_finance_operations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "marketplace_account_id",
            "operation_id",
            name="uq_wb_finance_operations_org_account_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id"],
            [
                "marketplace_accounts.organization_id",
                "marketplace_accounts.marketplace_account_id",
            ],
            name="fk_wb_finance_operations_org_account",
        ),
        CheckConstraint(
            "report_type IN ('main', 'redemptions', 'unknown')",
            name="ck_wb_finance_operations_report_type",
        ),
        CheckConstraint(
            "operation_kind IN ('sale', 'return', 'correction', 'other')",
            name="ck_wb_finance_operations_kind",
        ),
        CheckConstraint("sign IN (-1, 0, 1)", name="ck_wb_finance_operations_sign"),
        Index(
            "ix_wb_finance_operations_product_date",
            "organization_id",
            "marketplace_account_id",
            "nm_id",
            "business_date",
        ),
        Index(
            "ix_wb_finance_operations_source_identity",
            "organization_id",
            "marketplace_account_id",
            "source_identity",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("lk_organizations.organization_id"), nullable=False
    )
    marketplace_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_identity: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    rrd_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    report_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    correction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_late_correction: Mapped[bool] = mapped_column(Boolean, nullable=False)
    nm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    seller_article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sign: Mapped[int] = mapped_column(Integer, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commission_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logistics_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acceptance_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    penalty_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deduction_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    additional_payment_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquiring_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payable_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    advertising_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compensation_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cashback_amount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_discount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_commission_change_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WbFinanceSyncRunOperationRow(Base):
    __tablename__ = "wb_finance_sync_run_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "sync_run_id"],
            [
                "wb_finance_sync_runs.organization_id",
                "wb_finance_sync_runs.marketplace_account_id",
                "wb_finance_sync_runs.sync_run_id",
            ],
            name="fk_wb_finance_membership_org_account_run",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "operation_id"],
            [
                "wb_finance_operations.organization_id",
                "wb_finance_operations.marketplace_account_id",
                "wb_finance_operations.operation_id",
            ],
            name="fk_wb_finance_membership_org_account_operation",
        ),
        Index(
            "ix_wb_finance_membership_operation",
            "organization_id",
            "marketplace_account_id",
            "operation_id",
        ),
    )

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


class WbFinanceSyncRunSkuRollupRow(Base):
    __tablename__ = "wb_finance_sync_run_sku_rollups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "sync_run_id"],
            [
                "wb_finance_sync_runs.organization_id",
                "wb_finance_sync_runs.marketplace_account_id",
                "wb_finance_sync_runs.sync_run_id",
            ],
            name="fk_wb_finance_rollups_org_account_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("nm_id >= 0", name="ck_wb_finance_rollups_nm_id"),
    )

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    main_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redemptions_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    late_correction_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sales_units: Mapped[int] = mapped_column(Integer, nullable=False)
    returns_units: Mapped[int] = mapped_column(Integer, nullable=False)
    net_units: Mapped[int] = mapped_column(Integer, nullable=False)


class WbFinanceSyncRunSkuPnlRollupRow(Base):
    __tablename__ = "wb_finance_sync_run_sku_pnl_rollups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "sync_run_id"],
            [
                "wb_finance_sync_runs.organization_id",
                "wb_finance_sync_runs.marketplace_account_id",
                "wb_finance_sync_runs.sync_run_id",
            ],
            name="fk_wb_finance_pnl_rollups_org_account_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("nm_id >= 0", name="ck_wb_finance_pnl_rollups_nm_id"),
    )

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sales_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    returns_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    main_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redemptions_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    late_correction_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sales_units: Mapped[int] = mapped_column(Integer, nullable=False)
    returns_units: Mapped[int] = mapped_column(Integer, nullable=False)
    net_units: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logistics_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acceptance_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    penalty_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deduction_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    additional_payment_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquiring_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payable_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    advertising_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compensation_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cashback_amount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_discount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_commission_change_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )


class WbFinanceSyncRunSkuDailyPnlRollupRow(Base):
    __tablename__ = "wb_finance_sync_run_sku_daily_pnl_rollups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "sync_run_id"],
            [
                "wb_finance_sync_runs.organization_id",
                "wb_finance_sync_runs.marketplace_account_id",
                "wb_finance_sync_runs.sync_run_id",
            ],
            name="fk_wb_finance_daily_pnl_rollups_org_account_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("nm_id >= 0", name="ck_wb_finance_daily_pnl_rollups_nm_id"),
    )

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sales_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    returns_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    main_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redemptions_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    late_correction_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_revenue_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sales_units: Mapped[int] = mapped_column(Integer, nullable=False)
    returns_units: Mapped[int] = mapped_column(Integer, nullable=False)
    net_units: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logistics_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acceptance_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    penalty_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deduction_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    additional_payment_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    acquiring_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payable_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    advertising_kopecks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compensation_kopecks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cashback_amount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_discount_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    cashback_commission_change_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
