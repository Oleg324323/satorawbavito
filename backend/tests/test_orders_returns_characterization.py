"""Legacy behavior only: matching is neither allocation nor tenant authorization."""

import pytest

from app.avito.orders import AvitoOrderItem, AvitoOrderRow
from app.avito.returns import extract_return_candidates, match_return_candidates


def item(quantity=1):
    return AvitoOrderItem(
        itemId="000synthetic-listing",
        title="Synthetic item",
        sellerArticle="synthetic-article",
        size="M",
        color="white",
        quantity=quantity,
    )


def order(order_id="synthetic-return", account="synthetic-account-a", **kwargs):
    return AvitoOrderRow(
        orderId=order_id,
        accountId=account,
        status=kwargs.pop("status", "on_return"),
        items=kwargs.pop("items", [item()]),
        **kwargs,
    )


def test_order_level_partial_return_marker_extracts_every_item_without_unit_evidence():
    row = order(
        status="delivered", returnStatus="synthetic-partial", items=[item(2), item(3)]
    )
    before = row.model_dump()
    candidates = extract_return_candidates([row])
    assert [candidate.quantity for candidate in candidates] == [2, 3]
    assert [candidate.itemId for candidate in candidates] == [
        "000synthetic-listing"
    ] * 2
    assert row.model_dump() == before


@pytest.mark.parametrize("status", ["canceled", "in_dispute", "unknown", "on_return "])
def test_status_without_return_marker_does_not_create_return_candidate(status):
    assert extract_return_candidates([order(status=status)]) == []


def test_unknown_status_with_return_marker_still_creates_legacy_candidate():
    candidates = extract_return_candidates(
        [order(status="synthetic-unknown", returnStatus="synthetic-marker")]
    )
    assert candidates[0].status == "synthetic-unknown"


def test_matcher_can_match_different_accounts_without_authorization():
    target = order(
        "synthetic-target", account="synthetic-account-b", status="ready_to_ship"
    )
    candidates = extract_return_candidates([order()])
    assert (
        len(
            match_return_candidates(
                target.items[0], order=target, candidates=candidates
            )
        )
        == 1
    )


def test_same_external_order_id_is_excluded_even_across_accounts():
    target = order(account="synthetic-account-b", status="ready_to_ship")
    candidates = extract_return_candidates([order()])
    assert (
        match_return_candidates(target.items[0], order=target, candidates=candidates)
        == []
    )


def test_zero_quantity_candidate_is_still_matched_and_not_reserved():
    candidates = extract_return_candidates([order(items=[item(0)])])
    target = order("synthetic-target", status="ready_to_ship")
    first = match_return_candidates(
        target.items[0], order=target, candidates=candidates
    )
    second = match_return_candidates(
        target.items[0], order=target, candidates=candidates
    )
    assert len(first) == 1
    assert first[0].quantity == 0
    assert second == first


def test_equal_score_limit_depends_on_source_order():
    candidates = extract_return_candidates([order("synthetic-a"), order("synthetic-b")])
    target = order("synthetic-target", status="ready_to_ship")
    forward = match_return_candidates(
        target.items[0], order=target, candidates=candidates, limit=1
    )
    backward = match_return_candidates(
        target.items[0], order=target, candidates=list(reversed(candidates)), limit=1
    )
    assert forward[0].returnOrderId == "synthetic-a"
    assert backward[0].returnOrderId == "synthetic-b"
