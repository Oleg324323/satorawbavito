from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.modules.wb_repricing_price_inputs import (
    PriceKind, PriceObservation, assess_current_buyer_price,
)


AT = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def observation(**changes):
    values = dict(organization_id=7, marketplace_account_id=42, nm_id=101,
                  offer_id=None, snapshot_id="synthetic-snapshot", request_checksum="a" * 64,
                  kind=PriceKind.buyer_current, amount_kopecks=123450,
                  observed_at=AT, complete=True)
    values.update(changes)
    return PriceObservation(**values)


def assess(value, **changes):
    args = dict(organization_id=7, marketplace_account_id=42, nm_id=101,
                offer_id=None, now=AT, ttl_seconds=3600)
    args.update(changes)
    return assess_current_buyer_price(value, **args)


def test_repeated_reads_do_not_refresh_source_observation_or_expiry():
    source = observation()
    first = assess(source, now=AT + timedelta(seconds=3599))
    expired = assess(source, now=AT + timedelta(seconds=3600))
    assert first.state == "fresh" and first.usable_for_repricing
    assert expired.state == "stale" and not expired.usable_for_repricing
    assert first.observation is expired.observation is source
    assert first.expires_at == expired.expires_at == AT + timedelta(hours=1)
    assert source.observed_at == AT


def test_unknown_observation_time_stays_unknown_and_blocked():
    result = assess(observation(observed_at=None))
    assert result.state == "blocked"
    assert result.expires_at is None
    assert result.reason_code == "PRICE_OBSERVATION_TIME_UNKNOWN"
    assert not result.usable_for_repricing


@pytest.mark.parametrize("kind", [PriceKind.seller_current, PriceKind.buyer_historical, PriceKind.club, PriceKind.wallet])
def test_other_price_planes_never_substitute_current_buyer_price(kind):
    result = assess(observation(kind=kind))
    assert result.state == "blocked"
    assert result.reason_code == "PRICE_KIND_NOT_CURRENT_BUYER"
    assert not result.usable_for_repricing


@pytest.mark.parametrize("field,value", [("organization_id", 8), ("marketplace_account_id", 43),
                                         ("nm_id", 102), ("offer_id", 99)])
def test_scope_or_grain_mismatch_is_rejected(field, value):
    with pytest.raises(ValueError):
        assess(observation(), **{field: value})


def test_product_observation_is_not_projected_to_an_offer():
    with pytest.raises(ValueError):
        assess(observation(), offer_id=101)
    result = assess(observation(offer_id=99), offer_id=99)
    assert result.usable_for_repricing


def test_partial_snapshot_cannot_be_used_even_with_fresh_timestamp():
    result = assess(observation(complete=False))
    assert result.state == "blocked"
    assert result.reason_code == "PRICE_SNAPSHOT_INCOMPLETE"
    assert not result.usable_for_repricing


def test_missing_and_explicit_zero_are_preserved_as_different_observations():
    missing = assess(observation(amount_kopecks=None))
    zero = assess(observation(amount_kopecks=0))
    assert missing.state == "missing" and missing.observation.amount_kopecks is None
    assert zero.state == "blocked" and zero.observation.amount_kopecks == 0
    assert not missing.usable_for_repricing and not zero.usable_for_repricing


@pytest.mark.parametrize("amount", [True, -1, 1.5, "100"])
def test_money_rejects_non_integer_or_negative_inputs(amount):
    with pytest.raises(ValueError):
        observation(amount_kopecks=amount)


@pytest.mark.parametrize("identity", [True, 0, -1, "7", 7.0])
def test_identity_requires_internal_integer(identity):
    with pytest.raises(ValueError):
        observation(organization_id=identity)
    with pytest.raises(ValueError):
        assess(observation(), organization_id=identity)


def test_future_observation_cannot_be_fresh():
    result = assess(observation(observed_at=AT + timedelta(seconds=1)))
    assert not result.usable_for_repricing
    assert result.reason_code == "PRICE_OBSERVATION_IN_FUTURE"


def test_naive_times_are_rejected():
    with pytest.raises(ValueError):
        observation(observed_at=AT.replace(tzinfo=None))
    with pytest.raises(ValueError):
        assess(observation(), now=AT.replace(tzinfo=None))


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
def test_ttl_requires_explicit_positive_integer_seconds(ttl):
    with pytest.raises(ValueError):
        assess(observation(), ttl_seconds=ttl)


@pytest.mark.parametrize("changes", [{"request_checksum": "A" * 64}, {"snapshot_id": " "},
                                     {"complete": 1}, {"kind": "buyer_current"}])
def test_malformed_provenance_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(observation(), **changes)


def test_fall_back_ttl_is_elapsed_seconds_not_repeated_wall_hour():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    source = observation(observed_at=datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0))
    now = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    result = assess(source, now=now, ttl_seconds=3600)
    assert result.state == "stale"
    assert not result.usable_for_repricing
    assert result.expires_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_repeated_hour_future_observation_is_not_fresh():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    source = observation(observed_at=datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1))
    result = assess(source, now=datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0))
    assert result.reason_code == "PRICE_OBSERVATION_IN_FUTURE"
    assert not result.usable_for_repricing


def test_spring_forward_does_not_expire_before_elapsed_ttl():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    source = observation(observed_at=datetime(2026, 3, 8, 1, 30, tzinfo=zone))
    result = assess(source, now=datetime(2026, 3, 8, 3, 0, tzinfo=zone))
    assert result.state == "fresh"
    assert result.expires_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize("field", ["observed_at", "now"])
def test_unrepresentable_utc_boundary_fails_closed(field):
    edge = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ValueError, match="supported UTC range"):
        if field == "observed_at":
            assess(observation(observed_at=edge))
        else:
            assess(observation(), now=edge)


def test_equivalent_offset_instants_preserve_observation_and_ttl():
    original = AT.astimezone(timezone(timedelta(hours=3)))
    source = observation(observed_at=original)
    result = assess(source, now=AT + timedelta(seconds=3599))
    assert result.state == "fresh"
    assert result.observation is source
    assert source.observed_at is original
    assert result.expires_at == AT + timedelta(hours=1)
