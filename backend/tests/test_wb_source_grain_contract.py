"""Synthetic normalized observations; no claim of provider parser coverage."""

from dataclasses import replace
from hashlib import sha256
from itertools import permutations
import json

import pytest

from app.modules.wb_source_revision import (
    RevisionEvidence, SourceContext, SourceRow, SourceRun, compare_runs,
)


def checksum(payload):
    # Test fixture encoding only, not a new canonical production serializer.
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def stock_run(run_id, observations, complete=True):
    return SourceRun(
        SourceContext(7, 42, "synthetic-stock", "v1", "nm/chrt/warehouse", "a" * 64),
        run_id, checksum(observations),
        tuple(SourceRow(tuple(identity), checksum(payload)) for identity, payload in observations),
        complete,
    )


ROWS = (
    (("nm:101", "chrt:201", "warehouse:301"), {"quantity": 5}),
    (("nm:101", "chrt:202", "warehouse:301"), {"quantity": 7}),
    (("nm:101", "chrt:201", "warehouse:302"), {"quantity": 11}),
)


@pytest.mark.parametrize("ordered", list(permutations(ROWS)))
def test_same_nm_different_sizes_and_warehouses_survive_replay(ordered):
    before, after = stock_run("before", ROWS), stock_run("after", ordered)
    assert len({row.identity for row in after.rows}) == 3
    diff = compare_runs(before, after)
    assert diff.classification == "exact_replay"
    assert diff.added == diff.removed == diff.changed == ()


@pytest.mark.parametrize("index", range(3))
def test_change_is_at_exact_offer_warehouse_not_whole_nm(index):
    changed = list(ROWS)
    changed[index] = (ROWS[index][0], {"quantity": 0})
    diff = compare_runs(stock_run("before", ROWS), stock_run("after", changed))
    assert diff.classification == "unexplained_change"
    assert diff.changed == (ROWS[index][0],)
    assert diff.added == diff.removed == ()


@pytest.mark.parametrize("payload", [{}, {"quantity": None}, {"quantity": 0}])
def test_omitted_null_and_zero_do_not_collapse_to_same_observation(payload):
    before = stock_run("before", ((ROWS[0][0], payload),))
    for other in ({}, {"quantity": None}, {"quantity": 0}):
        after = stock_run("after", ((ROWS[0][0], other),))
        diff = compare_runs(before, after)
        assert diff.classification == ("exact_replay" if payload == other else "unexplained_change")


@pytest.mark.parametrize("remaining", [(), ROWS[:1], ROWS[:2]])
def test_partial_page_deltas_stay_diagnostic_even_with_review_reference(remaining):
    before, after = stock_run("before", ROWS), stock_run("after", remaining, False)
    evidence = RevisionEvidence(before.context, before.run_id, before.manifest_checksum,
                                after.run_id, after.manifest_checksum, "synthetic:review")
    diff = compare_runs(before, after, evidence)
    assert diff.classification == "incomplete_run"
    assert diff.removed == tuple(sorted(identity for identity, _ in ROWS[len(remaining):]))
    assert before.complete is True
    assert len(before.rows) == 3


def test_closed_day_revision_keeps_identity_and_checksums_without_inventing_rrd_id():
    context = SourceContext(7, 42, "synthetic-finance", "v1", "rrdId", "a" * 64)
    old_hash, new_hash = checksum({"amount": 0}), checksum({"amount": 120800})
    before = SourceRun(context, "before", "b" * 64,
                       (SourceRow(("rrdId", "101"), old_hash),), True)
    after = replace(before, run_id="after", manifest_checksum="c" * 64,
                    rows=(SourceRow(("rrdId", "101"), new_hash),))
    assert compare_runs(before, after).classification == "unexplained_change"
    evidence = RevisionEvidence(context, "before", "b" * 64, "after", "c" * 64,
                                "synthetic:review-not-real-discrepancy-evidence")
    diff = compare_runs(before, after, evidence)
    assert diff.classification == "source_revision"
    assert diff.changed == (("rrdId", "101"),)
    assert diff.added == diff.removed == ()
    assert before.rows[0].payload_checksum == old_hash
    assert after.rows[0].payload_checksum == new_hash
    assert old_hash != new_hash
