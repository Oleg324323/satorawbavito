"""Pure participant admission metadata; no fake T1 handle or database evidence."""

import ast
import inspect

import pytest

from app.orders import history_publication as publication
from app.orders.bindings import account_binding_checksum, serialize_account_bindings
from app.orders.history_bridge import decode_history_chunk
from app.platform.integrations.publication_guard import ExpectedAccountBinding
from tests.test_orders_history_bridge import page, row

ACCOUNT = ExpectedAccountBinding(2, "wb", "synthetic-history-account", None)


def metadata(*, empty=False):
    capture = page(count=0 if empty else 1)
    plan = decode_history_chunk(
        page=capture, first_ordinal=0, rows=() if empty else (row(),)
    )
    snapshot = f"wb-history-run-v1:{capture.job_id}:{capture.run_id}"
    value = {
        "organization_id": 1,
        "marketplace_account_id": 2,
        "marketplace": "wb",
        "source_kind": publication.SOURCE,
        "adapter_version": publication.ADAPTER,
        "mapping_version": "wb-statistics-status-v1",
        "source_contract_version": publication.SOURCE_CONTRACT,
        "state": "partial",
        "manifest_state": "partial",
        "payload_checksum": plan.input_checksum,
        "source_run_key": plan.source_run_key,
        "source_snapshot": snapshot,
        "requested_from": None,
        "requested_to": None,
        "page_count": 1,
        "order_count": len(plan.rows),
        "item_count": len(plan.rows),
        "expected_order_count": None,
        "account_binding_schema_version": 1,
        "account_binding_external_account_id": ACCOUNT.external_account_id,
        "account_binding_credential_ref": None,
        "account_binding_payload": serialize_account_bindings(1, (ACCOUNT,)),
        "account_binding_checksum": account_binding_checksum(1, (ACCOUNT,)),
    }
    return value, plan, snapshot


def test_replay_metadata_matches_exact_new_partial_contract():
    run, plan, snapshot = metadata()
    assert publication.is_history_partial_run(run)
    publication._validate_replay_run(run, plan, ACCOUNT, snapshot)


def test_empty_eof_replay_has_zero_counts_but_not_complete_coverage():
    run, plan, snapshot = metadata(empty=True)
    assert plan.rows == ()
    assert plan.first_ordinal == plan.next_ordinal == 0
    assert run["order_count"] == run["item_count"] == 0
    assert run["page_count"] == 1
    assert run["expected_order_count"] is None
    assert publication.is_history_partial_run(run)
    publication._validate_replay_run(run, plan, ACCOUNT, snapshot)
    run["item_count"] = 1
    with pytest.raises(ValueError, match="replay differs"):
        publication._validate_replay_run(run, plan, ACCOUNT, snapshot)


def test_initial_projection_requires_new_origin_without_earlier_evidence():
    assert publication._can_project_initial(
        comparison="new",
        parent_version=1,
        current_run_id=None,
        observation_run_id=7,
        run_id=7,
        has_other_observations=False,
        has_earlier_memberships=False,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_run_id": 6},
        {"has_earlier_memberships": True},
        {"has_other_observations": True},
        {"parent_version": 2},
        {"current_run_id": 6},
        {"comparison": "replay"},
    ],
)
def test_historical_or_current_evidence_cannot_become_initial_projection(changes):
    facts = {
        "comparison": "new",
        "parent_version": 1,
        "current_run_id": None,
        "observation_run_id": 7,
        "run_id": 7,
        "has_other_observations": False,
        "has_earlier_memberships": False,
    }
    facts.update(changes)
    assert not publication._can_project_initial(**facts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload_checksum", "f" * 64),
        ("source_contract_version", "orders-manifest-v1"),
        ("source_run_key", "different"),
        ("source_snapshot", "different"),
        ("marketplace_account_id", 3),
        ("organization_id", 3),
        ("marketplace", "avito"),
        ("source_kind", "wb-fulfillment"),
        ("adapter_version", "other-v1"),
        ("requested_from", "2026-09-09"),
        ("requested_to", "2026-09-10"),
        ("state", "complete"),
        ("manifest_state", "complete"),
        ("order_count", 0),
        ("item_count", 0),
        ("page_count", 2),
        ("expected_order_count", 1),
        ("account_binding_schema_version", None),
        ("account_binding_payload", b"{}"),
        ("account_binding_external_account_id", "other-synthetic-account"),
        ("account_binding_credential_ref", "other-synthetic-reference"),
        ("account_binding_checksum", "f" * 64),
        ("mapping_version", "other-v1"),
    ],
)
def test_replay_cannot_relabel_other_run_or_invent_complete_coverage(field, value):
    run, plan, snapshot = metadata()
    run[field] = value
    with pytest.raises(ValueError):
        publication._validate_replay_run(run, plan, ACCOUNT, snapshot)


def test_receiptless_legacy_partial_is_rejected_before_loading_t1_dependency():
    run, _, _ = metadata()
    run["source_contract_version"] = "legacy-partial"
    with pytest.raises(ValueError, match="source contract differs"):
        publication.require_history_projection_receipt(
            None, organization_id=1, marketplace_account_id=2, run=run
        )


def test_participant_does_not_create_guard_or_own_transaction():
    tree = ast.parse(inspect.getsource(publication))
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(fn, ast.Attribute)
        and fn.attr in {"commit", "rollback", "begin", "begin_nested"}
        for fn in calls
    )
    assert not any(
        isinstance(fn, ast.Name)
        and fn.id
        in {
            "acquire_publication_guard",
            "Session",
            "create_engine",
            "_persist_orders_manifest",
        }
        for fn in calls
    )


def test_history_initial_uses_only_genuine_restricted_parent_helper():
    tree = ast.parse(inspect.getsource(publication.persist_wb_history_chunk))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "set_parent"
        for node in calls
    )
    helpers = [
        node
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "apply_history_initial_parent"
    ]
    assert len(helpers) == 1
    assert {keyword.arg for keyword in helpers[0].keywords} == {
        "handle",
        "sync_run_id",
        "order_id",
    }


@pytest.mark.parametrize(
    "actual,expected,valid",
    [
        (0, 0, True),
        (1, 1, True),
        (1, 0, False),
        (0, 1, False),
        (True, 1, False),
        (-1, -1, False),
    ],
)
def test_scoped_decision_counts_must_match_local_chunk(actual, expected, valid):
    if valid:
        assert publication._validate_reconciliation_count(actual, expected) == actual
    else:
        with pytest.raises(ValueError, match="decision evidence differs"):
            publication._validate_reconciliation_count(actual, expected)
