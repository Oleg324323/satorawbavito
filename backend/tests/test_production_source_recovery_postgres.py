"""Actual read-only recovery over existing synthetic complete source fixtures."""

from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.orders import production_service as service
from tests import test_production_service as production

cluster = production.cluster
read_db = production.read_db
prepared = production.prepared
authority = production.authority
case = production.case


def lookup(case, *, version=None, item=None):
    with Session(case[1]) as session:
        return service.read_production_work_item_by_source(
            session,
            principal=case[2],
            account=production.ACCOUNT,
            order_item_id=case[3].order_item_id if item is None else item,
            expected_source_item_version=case[3].version
            if version is None
            else version,
        )


def test_recovery_returns_committed_id_without_mutation_and_read_permission_only(case):
    created = production.create(case)
    assert lookup(case).work_item_id == created.work_item_id
    production.assign(
        case,
        production.AssignmentCommand(
            created.work_item_id,
            1,
            "synthetic-recovery",
            case[4][0],
            "synthetic reason",
        ),
    )
    with case[0].begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET permissions='[\"production:read\"]' "
                "WHERE membership_id=:id"
            ),
            {"id": case[2].membership_id},
        )
        audit_before = connection.scalar(text("SELECT count(*) FROM lk_audit_events"))
    for _ in range(2):
        result = lookup(case)
        assert result.work_item_id == created.work_item_id
        assert result.source_item_version == case[3].version
        assert result.order_item_id == case[3].order_item_id
        assert (
            result.marketplace_account_id == production.ACCOUNT.marketplace_account_id
        )
        assert result.version == 2 and result.catalog_sku_id == case[4][0]
    assert production.counts(case[0], created.work_item_id) == (1, 1, 1)
    with case[0].connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM lk_audit_events"))
            == audit_before
        )


def test_absent_recovery_is_not_found_and_does_not_create(case):
    with pytest.raises(service.ProductionServiceError, match="PRODUCTION_NOT_FOUND"):
        lookup(case)
    with case[0].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM production_work_items WHERE order_item_id=:id"
                ),
                {"id": case[3].order_item_id},
            )
            == 0
        )


@pytest.mark.parametrize("change", ["requested", "current"])
def test_recovery_mismatched_captured_or_current_source_is_conflict_not_absence(
    case, change
):
    created = production.create(case)
    if change == "current":
        with case[0].begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_order_items SET version=version+1 WHERE order_item_id=:id"
                ),
                {"id": case[3].order_item_id},
            )
    with pytest.raises(
        service.ProductionServiceError, match="PRODUCTION_SOURCE_CHANGED"
    ):
        lookup(case, version=case[3].version + 1 if change == "requested" else None)
    assert production.counts(case[0], created.work_item_id) == (1, 0, 0)


def test_recovery_rechecks_revoked_authority(case):
    production.create(case)
    with case[0].begin() as connection:
        connection.execute(
            text(
                "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
            ),
            {"id": case[2].session_id},
        )
    with pytest.raises(service.ProductionServiceError, match="PRODUCTION_DENIED"):
        lookup(case)


def test_recovery_denies_other_account_before_revealing_source(case):
    production.create(case)
    with (
        Session(case[1]) as session,
        pytest.raises(service.ProductionServiceError, match="PRODUCTION_DENIED"),
    ):
        service.read_production_work_item_by_source(
            session,
            principal=case[2],
            account=replace(production.ACCOUNT, marketplace_account_id=91102),
            order_item_id=case[3].order_item_id,
            expected_source_item_version=case[3].version,
        )
