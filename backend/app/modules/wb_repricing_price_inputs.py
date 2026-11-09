"""Pure current-buyer input guard; no legacy wiring or storage implementation.

The adapter owns provenance, complete-run proof, and observed-time semantics.
An eligible input alone is never permission to send a price: approval, economics,
SPP and all other existing guards still apply. TTL is supplied by source policy.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Literal


class PriceKind(str, Enum):
    seller_current = "seller_current"
    buyer_current = "buyer_current"
    buyer_historical = "buyer_historical"
    club = "club"
    wallet = "wallet"


def _identity(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("positive internal identity required")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")


@dataclass(frozen=True, slots=True)
class PriceObservation:
    organization_id: int
    marketplace_account_id: int
    nm_id: int
    offer_id: int | None
    snapshot_id: str
    request_checksum: str
    kind: PriceKind
    amount_kopecks: int | None
    observed_at: datetime | None
    complete: bool

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.marketplace_account_id, self.nm_id):
            _identity(value)
        if self.offer_id is not None:
            _identity(self.offer_id)
        if type(self.snapshot_id) is not str or not self.snapshot_id.strip() or self.snapshot_id.strip() != self.snapshot_id:
            raise ValueError("exact nonblank snapshot identity required")
        if (type(self.request_checksum) is not str or len(self.request_checksum) != 64
                or any(char not in "0123456789abcdef" for char in self.request_checksum)):
            raise ValueError("lowercase SHA-256 request checksum required")
        if not isinstance(self.kind, PriceKind) or type(self.complete) is not bool:
            raise ValueError("explicit price kind and completeness required")
        if self.amount_kopecks is not None and (type(self.amount_kopecks) is not int or self.amount_kopecks < 0):
            raise ValueError("nonnegative integer kopecks or missing required")
        if self.observed_at is not None:
            _aware(self.observed_at)


@dataclass(frozen=True, slots=True)
class BuyerPriceAssessment:
    observation: PriceObservation
    state: Literal["fresh", "stale", "missing", "blocked"]
    expires_at: datetime | None
    reason_code: str | None

    @property
    def usable_for_repricing(self) -> bool:
        return self.state == "fresh"


def assess_current_buyer_price(
    observation: PriceObservation, *, organization_id: int, marketplace_account_id: int,
    nm_id: int, offer_id: int | None, now: datetime, ttl_seconds: int,
) -> BuyerPriceAssessment:
    """Assess one observation without changing timestamps, units or provenance.

    ``offer_id`` is the optional source-native WB offer/chrt identity, not a
    CatalogSku ID and not an nmId-derived size. Equality of the complete grain is
    mandatory. Unknown time remains None, never epoch or read time.
    """
    if not isinstance(observation, PriceObservation):
        raise ValueError("price observation required")
    for value in (organization_id, marketplace_account_id, nm_id):
        _identity(value)
    if offer_id is not None:
        _identity(offer_id)
    if (organization_id, marketplace_account_id, nm_id, offer_id) != (
        observation.organization_id, observation.marketplace_account_id,
        observation.nm_id, observation.offer_id,
    ):
        raise ValueError("price observation scope or grain mismatch")
    _aware(now)
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise ValueError("positive integer TTL seconds required")
    try:
        # Compare instants and elapsed TTL, not DST-sensitive wall-clock times.
        # Retain the original immutable observation for provenance.
        now = now.astimezone(UTC)
        observed_at = (
            observation.observed_at.astimezone(UTC)
            if observation.observed_at is not None else None
        )
        expires_at = observed_at + timedelta(seconds=ttl_seconds) if observed_at is not None else None
    except OverflowError as exc:
        raise ValueError("observation time or TTL exceeds supported UTC range") from exc

    reason = None
    state = "blocked"
    if observation.kind is not PriceKind.buyer_current:
        reason = "PRICE_KIND_NOT_CURRENT_BUYER"
    elif not observation.complete:
        reason = "PRICE_SNAPSHOT_INCOMPLETE"
    elif observation.amount_kopecks is None:
        state, reason = "missing", "PRICE_VALUE_MISSING"
    elif observation.amount_kopecks == 0:
        reason = "PRICE_VALUE_NOT_POSITIVE"
    elif observed_at is None:
        reason = "PRICE_OBSERVATION_TIME_UNKNOWN"
    elif observed_at > now:
        reason = "PRICE_OBSERVATION_IN_FUTURE"
    elif now >= expires_at:
        state, reason = "stale", "PRICE_OBSERVATION_EXPIRED"
    else:
        state = "fresh"
    return BuyerPriceAssessment(observation, state, expires_at, reason)
