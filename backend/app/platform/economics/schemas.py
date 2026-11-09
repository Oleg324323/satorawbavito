from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.contracts.envelopes import MoneyKopecks, UtcDateTime


CostState = Literal["configured", "assumed", "missing", "restricted"]
EvidenceState = Literal["dated", "undated", "period_end_fallback"]


class CostView(BaseModel):
    costVersionId: int | None = Field(default=None, ge=1)
    amountKopecks: MoneyKopecks | None = None
    valueState: CostState
    effectiveFrom: UtcDateTime | None = None
    source: str | None = None
    sourceReference: str | None = None
    evidenceStatus: EvidenceState | None = None
    supersedesCostVersionId: int | None = Field(default=None, ge=1)
    createdAt: UtcDateTime | None = None


class CostSetRequest(BaseModel):
    amountKopecks: MoneyKopecks | None = None
    valueState: Literal["configured", "assumed", "missing"]
    effectiveFrom: UtcDateTime
    sourceReference: str = Field(min_length=1, max_length=255)
    evidenceStatus: EvidenceState
    supersedesCostVersionId: int | None = Field(default=None, ge=1)


class CurrentCostSetRequest(BaseModel):
    amountKopecks: int = Field(strict=True, ge=0, le=2_147_483_647)
    expectedCostVersionId: int | None = Field(ge=1)
    sourceReference: str = Field(min_length=1, max_length=255, pattern=r".*\S.*")
