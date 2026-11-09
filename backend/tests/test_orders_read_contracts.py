from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.modules.orders import OrderContractValidationError
from app.modules.production import (
    AssignmentCommand,
    OrderCommandConflict,
    validate_assignment_preconditions,
)
from app.orders.contracts import (
    AccountCoverage,
    CatalogResolution,
    DeadlineEvidence,
    OrderReadPage,
    OrderReadRow,
)
from tests.test_orders_ingestion import observation


def row():
    source = observation()
    return OrderReadRow(
        source,
        source.items[0].identity,
        1,
        CatalogResolution("unmapped", None, None, None, "synthetic-resolution-v1"),
        ("CATALOG_UNMAPPED",),
    )


def page(rows=None, **changes):
    result = OrderReadPage(
        101,
        (1001,),
        "synthetic-snapshot",
        "synthetic-watermark",
        datetime(2026, 9, 8, tzinfo=UTC),
        "partial",
        (row(),) if rows is None else rows,
        None,
        (
            AccountCoverage(
                1001,
                "avito-order-management",
                "synthetic-adapter-v1",
                "partial",
                "synthetic-source-snapshot",
                None,
                None,
            ),
        ),
    )
    return replace(result, **changes)


def test_unknown_and_partial_evidence_can_be_displayed_with_explicit_blockers():
    value = page()
    assert value.rows[0].readiness_blockers == ("CATALOG_UNMAPPED",)
    assert value.coverage_state == "partial"


def test_page_scope_excludes_wrong_account_and_org():
    with pytest.raises(OrderContractValidationError):
        page(organization_id=202)
    with pytest.raises(OrderContractValidationError):
        page(marketplace_account_ids=(1002,))


def test_page_rejects_duplicate_rows_and_mutable_collections():
    with pytest.raises(OrderContractValidationError):
        page(rows=(row(), row()))
    with pytest.raises(OrderContractValidationError):
        page(marketplace_account_ids=[1001])
    with pytest.raises(OrderContractValidationError):
        page(marketplace_account_ids=(1001, 1001))


def test_multi_item_order_has_separate_resolution_rows():
    first = row()
    second = replace(first, item_identity=first.observation.items[1].identity)
    assert len(page(rows=(first, second)).rows) == 2
    with pytest.raises(OrderContractValidationError):
        replace(first, item_identity=observation("synthetic-other").items[0].identity)


def test_unresolved_row_cannot_claim_empty_readiness_blockers():
    with pytest.raises(OrderContractValidationError):
        replace(row(), readiness_blockers=())


@pytest.mark.parametrize("state", ["unmapped", "ambiguous", "stale"])
def test_unresolved_resolution_does_not_claim_a_current_sku(state):
    with pytest.raises(OrderContractValidationError):
        CatalogResolution(state, None, None, 1, "synthetic-v1")


def test_resolved_and_manual_override_are_distinct_and_require_sku_evidence():
    resolved = CatalogResolution("resolved", 1, 2, 3, "synthetic-v1")
    manual = CatalogResolution("manual_override", None, None, 3, "synthetic-action-v1")
    assert resolved != manual
    with pytest.raises(OrderContractValidationError):
        replace(resolved, catalog_sku_id=None)
    with pytest.raises(OrderContractValidationError):
        replace(resolved, marketplace_product_id=None)


def command(**changes):
    return replace(
        AssignmentCommand(1, 2, "synthetic-idempotency", 3, "synthetic-reason"),
        **changes,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("work_item_id", True),
        ("expected_version", 0),
        ("catalog_sku_id", -1),
        ("idempotency_key", ""),
        ("reason", " "),
        ("reason", " synthetic-reason"),
    ],
)
def test_manual_assignment_requires_exact_valid_command_fields(field, value):
    with pytest.raises(OrderContractValidationError):
        command(**{field: value})


def test_exact_replay_wins_over_expected_version_check():
    assert validate_assignment_preconditions(command(), 3, command()) == "replay"


def test_new_command_requires_current_expected_version():
    assert validate_assignment_preconditions(command(), 2) == "new"
    with pytest.raises(OrderCommandConflict) as error:
        validate_assignment_preconditions(command(), 3)
    assert error.value.code == "VERSION_CONFLICT"


def test_same_key_changed_payload_conflicts_before_stale_check():
    with pytest.raises(OrderCommandConflict) as error:
        validate_assignment_preconditions(command(catalog_sku_id=4), 3, command())
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_unrelated_stored_command_is_not_accepted_as_replay():
    with pytest.raises(OrderContractValidationError):
        validate_assignment_preconditions(
            command(), 2, command(idempotency_key="synthetic-other")
        )


def test_command_body_cannot_supply_actor_or_organization():
    with pytest.raises(TypeError):
        AssignmentCommand(1, 2, "synthetic-key", 3, "synthetic-reason", actor_id=7)


def test_source_deadline_and_computed_deadline_keep_independent_provenance():
    now = datetime(2026, 9, 8, tzinfo=UTC)
    source = DeadlineEvidence(
        "ship_by", now, None, None, None, "Europe/Moscow", "synthetic-observation", now
    )
    assert source.computed_at is None
    assert replace(row(), deadlines=(source,)).deadlines[0].source_at == now
    with pytest.raises(OrderContractValidationError):
        replace(source, computed_at=now)
    calculated = replace(
        source, computed_at=now, rule_id="synthetic-rule", rule_version="synthetic-v1"
    )
    assert calculated.source_at == calculated.computed_at


def test_deadline_missing_evidence_or_invalid_timezone_is_rejected():
    now = datetime(2026, 9, 8, tzinfo=UTC)
    value = DeadlineEvidence(
        "ship_by", now, None, None, None, "Europe/Moscow", "synthetic-observation", now
    )
    for field, invalid in [
        ("source_at", None),
        ("timezone", "synthetic-zone"),
        ("evidence_source", ""),
        ("observed_at", now.replace(tzinfo=None)),
    ]:
        with pytest.raises(OrderContractValidationError):
            replace(value, **{field: invalid})


def test_page_requires_coverage_for_every_account_and_honest_aggregate_state():
    with pytest.raises(OrderContractValidationError):
        page(account_coverage=())
    with pytest.raises(OrderContractValidationError):
        page(coverage_state="complete")
    with pytest.raises(OrderContractValidationError):
        page(marketplace_account_ids=(1001, 1002))


def test_missing_account_coverage_has_no_snapshot_or_rows():
    coverage = AccountCoverage(
        1001,
        "avito-order-management",
        "synthetic-adapter-v1",
        "missing",
        None,
        None,
        None,
    )
    with pytest.raises(OrderContractValidationError):
        replace(coverage, source_snapshot="synthetic-snapshot")
    with pytest.raises(OrderContractValidationError):
        page(account_coverage=(coverage,), coverage_state="missing")


def test_coverage_bounds_are_paired_and_ordered():
    now = datetime(2026, 9, 8, tzinfo=UTC)
    original = page().account_coverage[0]
    with pytest.raises(OrderContractValidationError):
        replace(original, requested_from=now)
    with pytest.raises(OrderContractValidationError):
        replace(original, requested_from=now, requested_to=now.replace(day=7))


def test_coverage_repeated_local_hour_is_a_nonempty_instant_interval():
    from zoneinfo import ZoneInfo

    start = datetime(2024, 10, 27, 2, 30, tzinfo=ZoneInfo("Europe/Berlin"), fold=0)
    end = start.replace(fold=1)
    assert start.astimezone(UTC) < end.astimezone(UTC)
    value = replace(page().account_coverage[0], requested_from=start, requested_to=end)
    assert value.requested_from is start and value.requested_to is end


def test_coverage_rejects_reversed_instants_despite_increasing_local_clock():
    from zoneinfo import ZoneInfo

    start = datetime(2024, 10, 27, 2, 15, tzinfo=ZoneInfo("Europe/Berlin"), fold=1)
    end = start.replace(minute=45, fold=0)
    assert start.astimezone(UTC) > end.astimezone(UTC)
    with pytest.raises(OrderContractValidationError, match="interval"):
        replace(page().account_coverage[0], requested_from=start, requested_to=end)
