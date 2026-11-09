"""Read-only participation evidence for the Repricer column.

Price thresholds and the legacy promotion helpers used by pricing are separate.
Import time alone cannot establish when an Excel membership status was observed.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


MEMBERSHIP_VERSION = 1
# Match the hourly freshness policy for recent WB source observations.
MEMBERSHIP_TTL = timedelta(hours=1)


def promotion_refresh_metadata(source: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Schedule one source refresh, including when a saved view crosses its TTL."""
    now = now or datetime.now(timezone.utc)
    fetched = timestamp(source.get("fetchedAt"))
    promotions = source.get("promotions")
    required = fetched is None or not timedelta(0) <= now - fetched < MEMBERSHIP_TTL
    boundaries = [fetched + MEMBERSHIP_TTL] if fetched is not None else []
    if not isinstance(promotions, list):
        required = True
        promotions = []
    for promotion in promotions:
        if not isinstance(promotion, dict) or promotion.get("membershipVersion") != MEMBERSHIP_VERSION:
            required = True
            continue
        start, end = timestamp(promotion.get("startDateTime")), timestamp(promotion.get("endDateTime"))
        if start is None or end is None or start >= end:
            required = True
            continue
        if start > now:
            boundaries.append(start)
        if not start <= now < end or str(promotion.get("wbType") or promotion.get("type") or "").lower() != "regular":
            continue
        observed = timestamp(promotion.get("membershipFetchedAt"))
        ids = promotion.get("participantNmIds")
        if (observed is None or not timedelta(0) <= now - observed < MEMBERSHIP_TTL
                or promotion.get("membershipState") != "complete" or not isinstance(ids, list)
                or any(product_id(value) is None for value in ids)):
            required = True
        if observed is not None:
            boundaries.append(observed + MEMBERSHIP_TTL)
        boundaries.append(end)
    return {
        "promotionsRefreshRequired": required,
        "promotionsRefreshAt": min(boundaries).isoformat() if boundaries else None,
    }


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def product_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


@dataclass
class PromotionMembership:
    participants: dict[int, dict[str, Any]]
    negative_known: bool
    reason: str
    observed_at: str | None
    expires_at: str | None

    def analytics(self, nm_id: int | None) -> dict[str, Any]:
        nm_id = product_id(nm_id)
        promotion = self.participants.get(nm_id)
        status = "yes" if promotion else "no" if self.negative_known and nm_id else "unknown"
        name = str(promotion.get("name") or f"Акция {promotion['id']}") if promotion else None
        return {
            "promotionStatus": status,
            "promotionStatusText": name if promotion else "Нет в обычных" if status == "no" else "Нет данных",
            "promotionName": name,
            "promotionId": promotion.get("id") if promotion else None,
            "promotionStatusScope": "regular",
            "promotionStatusSource": "wb_calendar" if status != "unknown" else None,
            "promotionStatusReason": (
                "Участие подтверждено календарём WB в действующей обычной акции."
                if promotion else "Не удалось сопоставить товар с артикулом WB (nmID)."
                if nm_id is None else self.reason
            ),
            "promotionStatusObservedAt": promotion.get("membershipFetchedAt") if promotion else self.observed_at,
            "promotionStatusExpiresAt": self.expires_at,
        }


def promotion_membership(promotions: list[dict[str, Any]], *, now: datetime | None = None) -> PromotionMembership:
    now = now or datetime.now(timezone.utc)
    participants: dict[int, dict[str, Any]] = {}
    regular_count = 0
    complete = bool(promotions)
    unsupported = False
    observed: list[datetime] = []
    boundaries: list[datetime] = []
    for promotion in promotions:
        start = timestamp(promotion.get("startDateTime"))
        end = timestamp(promotion.get("endDateTime"))
        if start is None or end is None or start >= end:
            complete = False
            continue
        if start > now:
            boundaries.append(start)
            continue
        if end <= now:
            continue
        boundaries.append(end)
        if str(promotion.get("wbType") or promotion.get("type") or "").lower() != "regular":
            unsupported = True
            complete = False
            continue
        regular_count += 1
        fetched = timestamp(promotion.get("membershipFetchedAt"))
        ids = promotion.get("participantNmIds")
        fresh = fetched is not None and timedelta(0) <= now - fetched < MEMBERSHIP_TTL
        if (promotion.get("membershipVersion") != MEMBERSHIP_VERSION
                or promotion.get("membershipState") != "complete" or not fresh
                or not isinstance(ids, list) or any(product_id(value) is None for value in ids)):
            complete = False
            continue
        observed.append(fetched)
        boundaries.append(fetched + MEMBERSHIP_TTL)
        for nm_id in ids:
            participants.setdefault(product_id(nm_id), promotion)
    negative_known = complete and regular_count > 0
    reason = (
        "Не участвует в проверенных действующих обычных акциях. Автоакции этим методом API не проверяются."
        if negative_known else
        "API не предоставляет состав участников автоакций. Участие товара не подтверждено."
        if unsupported else
        "Нет полных актуальных данных об участии в действующих акциях. Требуется обновление источника акций."
    )
    return PromotionMembership(
        participants, negative_known, reason,
        min(observed).isoformat() if observed else None,
        min(boundaries).isoformat() if boundaries else None,
    )
