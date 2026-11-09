"""Private preparticipant state comparison, not a substitute for genuine guards."""
import importlib

import pytest

from app.platform.integrations.user_orders_job_contract import OrdersJobError


def verify(before, after, *, replayed=False):
    module = importlib.import_module("app.platform.integrations.wb_history_projection_store")
    check = getattr(module, "_verify_parent_baseline", None)
    assert check is not None, "Preparticipant parent baseline comparison is missing"
    check(before, after, run_id=80, replayed=replayed)


def current(**changes):
    row = {"external_order_id": "srid-one", "order_id": 12, "version": 5,
        "last_seen_sync_run_id": 60, "prior_observation_id": 70,
        "history_pre_order_version": 5, "history_pre_sync_run_id": 60,
        "history_pre_observation_id": 70, "history_outcome": "semantic_replay"}
    row.update(changes)
    return row


BASE = (("srid-one", 12, 5, 60, 70),)


def test_unchanged_existing_parent_accepts_matching_derived_prestate():
    verify(BASE, (current(),))


@pytest.mark.parametrize("changes", [{"order_id": 13}, {"version": 6},
    {"last_seen_sync_run_id": 61}, {"prior_observation_id": 71},
    {"history_pre_order_version": 4}, {"history_pre_sync_run_id": 61},
    {"history_pre_observation_id": 71}, {"history_outcome": "initial_projection"}])
def test_changed_identity_current_or_derived_prestate_fails(changes):
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify(BASE, (current(**changes),))


def test_absent_parent_allows_only_real_initial_transition():
    verify((("srid-one", None, None, None, None),), (current(version=2,
        last_seen_sync_run_id=80, prior_observation_id=90, history_pre_order_version=1,
        history_pre_sync_run_id=None, history_pre_observation_id=None, history_outcome="initial_projection"),))


def test_existing_empty_identity_allows_derived_initial_transition_with_same_id():
    verify((("srid-one", 12, 1, None, None),), (current(version=2,
        last_seen_sync_run_id=80, prior_observation_id=90, history_pre_order_version=1,
        history_pre_sync_run_id=None, history_pre_observation_id=None, history_outcome="initial_projection"),))


@pytest.mark.parametrize("changes", [{"order_id": 13}, {"version": 3},
    {"last_seen_sync_run_id": 81}, {"prior_observation_id": None},
    {"history_pre_order_version": 2}, {"history_pre_sync_run_id": 60},
    {"history_pre_observation_id": 70}, {"history_outcome": "reconciliation_required"}])
def test_existing_empty_identity_rejects_noninitial_or_substituted_transition(changes):
    row = current(version=2, last_seen_sync_run_id=80, prior_observation_id=90,
        history_pre_order_version=1, history_pre_sync_run_id=None,
        history_pre_observation_id=None, history_outcome="initial_projection")
    row.update(changes)
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify((("srid-one", 12, 1, None, None),), (row,))


def test_existing_empty_identity_can_remain_unchanged_for_derived_reconciliation():
    verify((("srid-one", 12, 1, None, None),), (current(version=1,
        last_seen_sync_run_id=None, prior_observation_id=None, history_pre_order_version=1,
        history_pre_sync_run_id=None, history_pre_observation_id=None, history_outcome="reconciliation_required"),))


def test_existing_noninitial_version_cannot_reenter_initial_projection():
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify((("srid-one", 12, 2, None, None),), (current(version=3,
            last_seen_sync_run_id=80, prior_observation_id=90, history_pre_order_version=2,
            history_pre_sync_run_id=None, history_pre_observation_id=None, history_outcome="initial_projection"),))


def test_absent_parent_cannot_be_reclassified_as_existing_reconciliation():
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify((("srid-one", None, None, None, None),), (current(history_outcome="reconciliation_required"),))


@pytest.mark.parametrize("rows", [(), (current(), current()), (current(external_order_id="other"),)])
def test_missing_duplicate_or_substituted_identity_fails(rows):
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify(BASE, rows)


def test_committed_replay_preserves_current_state_without_recomputing_old_decision():
    verify(BASE, (current(history_pre_order_version=1, history_pre_sync_run_id=None,
        history_pre_observation_id=None, history_outcome="initial_projection"),), replayed=True)


def test_committed_replay_cannot_change_current_parent():
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        verify(BASE, (current(version=6),), replayed=True)


def test_empty_eof_has_no_parent_state():
    verify((), ())
