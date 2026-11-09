from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.models import Base
from app.platform.identity import orm as _identity_models  # noqa: F401
from app.platform.integrations import orm as _integration_models  # noqa: F401


class CatalogSkuRow(Base):
    __tablename__ = "catalog_skus"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_catalog_skus_org_code"),
        UniqueConstraint("organization_id", "catalog_sku_id", name="uq_catalog_skus_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "manager_membership_id"],
            ["iam_memberships.organization_id", "iam_memberships.membership_id"],
            name="fk_catalog_skus_org_manager_membership",
        ),
    )

    catalog_sku_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("lk_organizations.organization_id"), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    print_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_membership_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketplaceProductRow(Base):
    __tablename__ = "marketplace_products"
    __table_args__ = (
        UniqueConstraint("organization_id", "marketplace_account_id", "marketplace_product_id", name="uq_orders_product_account"),
        UniqueConstraint(
            "organization_id",
            "marketplace_account_id",
            "external_product_id",
            name="uq_marketplace_products_org_account_external",
        ),
        UniqueConstraint("organization_id", "marketplace_product_id", name="uq_marketplace_products_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id"],
            ["marketplace_accounts.organization_id", "marketplace_accounts.marketplace_account_id"],
            name="fk_marketplace_products_org_account",
        ),
    )

    marketplace_product_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("lk_organizations.organization_id"), nullable=False)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seller_article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    product_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketplaceOfferRow(Base):
    __tablename__ = "marketplace_offers"
    __table_args__ = (
        UniqueConstraint("organization_id", "marketplace_account_id", "marketplace_product_id", "marketplace_offer_id", name="uq_orders_offer_product_account"),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id", "marketplace_product_id"],
            ["marketplace_products.organization_id", "marketplace_products.marketplace_account_id", "marketplace_products.marketplace_product_id"],
            name="fk_orders_offer_product_account",
        ),
        UniqueConstraint(
            "organization_id",
            "marketplace_product_id",
            "external_offer_key",
            name="uq_marketplace_offers_org_product_external",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_account_id"],
            ["marketplace_accounts.organization_id", "marketplace_accounts.marketplace_account_id"],
            name="fk_marketplace_offers_org_account",
        ),
        ForeignKeyConstraint(
            ["organization_id", "marketplace_product_id"],
            ["marketplace_products.organization_id", "marketplace_products.marketplace_product_id"],
            name="fk_marketplace_offers_org_product",
        ),
        ForeignKeyConstraint(
            ["organization_id", "catalog_sku_id"],
            ["catalog_skus.organization_id", "catalog_skus.catalog_sku_id"],
            name="fk_marketplace_offers_org_sku",
        ),
    )

    marketplace_offer_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("lk_organizations.organization_id"), nullable=False)
    marketplace_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    marketplace_product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    external_offer_key: Mapped[str] = mapped_column(String(128), nullable=False)
    catalog_sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offer_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
