"""Current WB content and native size prices, never period financial estimates."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ProductSize(WireModel):
    chrt_id: str = Field(alias="chrtId")
    tech_size: str | None = Field(alias="techSize")
    skus: list[str] | None
    skus_truncated: bool = Field(alias="skusTruncated")
    price_kopecks: str | None = Field(alias="priceKopecks")
    discounted_price_kopecks: str | None = Field(alias="discountedPriceKopecks")
    truncated_fields: list[str] = Field(alias="truncatedFields", default_factory=list)


class Product(WireModel):
    nm_id: str = Field(alias="nmId")
    vendor_code: str | None = Field(alias="vendorCode")
    title: str | None
    brand: str | None
    subject_id: str | None = Field(alias="subjectId")
    subject_name: str | None = Field(alias="subjectName")
    photo_url: str | None = Field(alias="photoUrl")
    content_updated_at: datetime | None = Field(alias="contentUpdatedAt")
    prices_updated_at: datetime | None = Field(alias="pricesUpdatedAt")
    sizes: list[ProductSize]
    sizes_truncated: bool = Field(alias="sizesTruncated")
    truncated_fields: list[str] = Field(alias="truncatedFields", default_factory=list)


class SourceState(WireModel):
    source: Literal["content", "prices"]
    state: Literal["idle", "queued", "running", "partial", "completed", "failed"]
    processed: int = Field(ge=0)
    updated_at: datetime | None = Field(alias="updatedAt")
    error_code: str | None = Field(alias="errorCode")


class ProductsPage(WireModel):
    marketplace_account_id: int = Field(alias="marketplaceAccountId", gt=0)
    items: list[Product]
    next_cursor: str | None = Field(alias="nextCursor")
    read_version: str = Field(alias="readVersion")
    readiness: Literal["empty", "partial", "ready", "error"]
    sources: list[SourceState]


class ProductsResponse(WireModel):
    data: ProductsPage


class ProductsQuery(WireModel):
    limit: int = Field(default=50, ge=1, le=200)
    sort: Literal["nmId", "vendorCode", "title", "brand"] = "nmId"
    direction: Literal["asc", "desc"] = "asc"
    q: str = Field(default="", max_length=100)
    brand: str | None = Field(default=None, max_length=200)
