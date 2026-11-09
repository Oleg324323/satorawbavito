from __future__ import annotations

import re
from pydantic import BaseModel, Field

from app.avito.orders import AvitoOrderItem, AvitoOrderRow, AvitoReturnMatch, ReturnMatchReason


class AvitoReturnCandidate(BaseModel):
    returnOrderId: str = Field(min_length=1)
    marketplaceId: str | None = None
    accountId: str | None = None
    accountName: str | None = None
    itemId: str | None = None
    lineIndex: int | None = None
    title: str = Field(min_length=1)
    sellerArticle: str | None = None
    size: str | None = None
    color: str | None = None
    imageUrl: str | None = None
    sources: dict[str, str | None] = Field(default_factory=dict)
    quantity: int = Field(default=1, ge=0)
    status: str = "on_return"
    returnStatus: str | None = None
    sourceUpdatedAt: str | None = None
    lastSeenAt: str | None = None
    returnItemId: int | None = None
    receivedQuantity: int = 0
    inspectedQuantity: int = 0
    reservedQuantity: int = 0
    sentQuantity: int = 0
    writtenOffQuantity: int = 0
    availableQuantity: int = 0


_COLOR_ALIASES = {
    "black": "black",
    "черныи": "black",
    "черный": "black",
    "черный": "black",
    "чёрный": "black",
    "черная": "black",
    "чёрная": "black",
    "черное": "black",
    "чёрное": "black",
    "white": "white",
    "белыи": "white",
    "белый": "white",
    "белый": "white",
    "белая": "white",
    "белое": "white",
    "milk": "milk",
    "молочныи": "milk",
    "молочный": "milk",
    "молочная": "milk",
    "молочное": "milk",
    "graphite": "graphite",
    "графит": "graphite",
    "графитовый": "graphite",
    "графитовая": "graphite",
}


def _normalized_text(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.U)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_color(value: str | None) -> str:
    normalized = _normalized_text(value)
    return _COLOR_ALIASES.get(normalized, normalized)


def _conflicts(left: str, right: str) -> bool:
    return bool(left and right and left != right)


def _return_candidate(row: AvitoOrderRow, item: AvitoOrderItem) -> AvitoReturnCandidate:
    return AvitoReturnCandidate(
        returnOrderId=row.orderId,
        marketplaceId=row.marketplaceId,
        accountId=row.accountId,
        accountName=row.accountName,
        itemId=item.itemId,
        lineIndex=item.lineIndex,
        title=item.title,
        sellerArticle=item.sellerArticle,
        size=item.size,
        color=item.color,
        imageUrl=item.imageUrl,
        sources=item.sources,
        quantity=item.quantity,
        status=row.status,
        returnStatus=row.returnStatus,
        sourceUpdatedAt=row.updatedAt,
    )


def extract_return_candidates(rows: list[AvitoOrderRow]) -> list[AvitoReturnCandidate]:
    candidates: list[AvitoReturnCandidate] = []
    for row in rows:
        if row.status != "on_return" and not row.returnStatus:
            continue
        for item in row.items:
            if not item.title:
                continue
            candidates.append(_return_candidate(row, item))
    return candidates


def _same_order(candidate: AvitoReturnCandidate, order: AvitoOrderRow) -> bool:
    return bool(candidate.returnOrderId and candidate.returnOrderId == order.orderId)


def _score_match(item: AvitoOrderItem, candidate: AvitoReturnCandidate) -> tuple[int, ReturnMatchReason] | None:
    item_article = _normalized_text(item.sellerArticle)
    candidate_article = _normalized_text(candidate.sellerArticle)
    item_title = _normalized_text(item.title)
    candidate_title = _normalized_text(candidate.title)
    item_size = _normalized_text(item.size)
    candidate_size = _normalized_text(candidate.size)
    item_color = _normalized_color(item.color)
    candidate_color = _normalized_color(candidate.color)

    if _conflicts(item_size, candidate_size) or _conflicts(item_color, candidate_color):
        return None
    size_equal = bool(item_size and candidate_size and item_size == candidate_size)
    color_equal = bool(item_color and candidate_color and item_color == candidate_color)
    color_missing = not item_color and not candidate_color
    article_equal = bool(item_article and candidate_article and item_article == candidate_article)
    title_equal = bool(item_title and candidate_title and item_title == candidate_title)

    if article_equal and size_equal and color_equal:
        return 100, "article_size_color"
    if title_equal and size_equal and color_equal:
        return 90, "title_size_color"
    if article_equal and size_equal and color_missing:
        return 82, "article_size_color_missing"
    if title_equal and size_equal and color_missing:
        return 72, "title_size_color_missing"
    return None


def _match_payload(candidate: AvitoReturnCandidate, *, score: int, reason: ReturnMatchReason) -> AvitoReturnMatch:
    return AvitoReturnMatch(
        returnOrderId=candidate.returnOrderId,
        marketplaceId=candidate.marketplaceId,
        itemId=candidate.itemId,
        title=candidate.title,
        sellerArticle=candidate.sellerArticle,
        size=candidate.size,
        color=candidate.color,
        imageUrl=candidate.imageUrl,
        quantity=candidate.quantity,
        score=score,
        reason=reason,
        status=candidate.status,
        returnStatus=candidate.returnStatus,
        lastSeenAt=candidate.lastSeenAt,
        returnItemId=candidate.returnItemId,
        availableQuantity=candidate.availableQuantity,
        receivedQuantity=candidate.receivedQuantity,
        inspectedQuantity=candidate.inspectedQuantity,
    )


def match_return_candidates(
    item: AvitoOrderItem,
    *,
    order: AvitoOrderRow,
    candidates: list[AvitoReturnCandidate],
    limit: int = 3,
) -> list[AvitoReturnMatch]:
    matches: list[AvitoReturnMatch] = []
    for candidate in candidates:
        if candidate.availableQuantity <= 0:
            continue
        if _same_order(candidate, order):
            continue
        scored = _score_match(item, candidate)
        if scored is None:
            continue
        score, reason = scored
        matches.append(_match_payload(candidate, score=score, reason=reason))
    return sorted(matches, key=lambda match: match.score, reverse=True)[: max(0, limit)]
