"""Synthetic owner-only backfill; no real legacy files/state are read."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.modules.wb_repricing import (
    ApprovalConflictError,
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    build_action_key,
)
from app.modules.wb_repricing_postgres import (
    ApprovalTransaction,
    LegacyApprovalImportTransaction,
)
from app.modules.wb_repricing_repository import (
    ApprovalIdentityBlockedError,
    ApprovalRepositoryScope,
)
from tests import test_wb_repricing_postgres_repository as repository_tests

cluster = repository_tests.cluster
latest_db = repository_tests.latest_db


def historical(key, status="pending", *, account="42"):
    values = {
        "organization_id": 7,
        "marketplace_account_id": account,
        "approval_id": key.approval_id,
        "catalog_sku_id": "99",
        "nm_id": 2**70,
        "article_id": "Синтетика🚀",
        "recommended_price_kopecks": 2**72,
        "request_checksum": "a" * 64,
        "status": ApprovalStatus(status),
        "version": 2**80,
        "action_key": build_action_key(7, account, key.approval_id, "a" * 64),
        "created_at": datetime(2020, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2021, 1, 1, tzinfo=UTC),
    }
    if status in ("applying", "applied", "failed", "ambiguous"):
        values["claimed_by_membership_id"] = "77"
    if status == "applied":
        values["wb_upload_id"] = "synthetic-old-upload"
    if status in ("failed", "ambiguous"):
        values["safe_error_code"] = "LegacySafeCode"
    if status in ("rejected", "blocked"):
        values.update(decided_by_membership_id="77", reason_code="LegacyReason")
    return PriceApprovalSnapshot(**values)


def imported(repo, snapshot, **overrides):
    identities = {
        "catalog_sku_id": 99,
        "claimed_by_membership_id": 77 if snapshot.claimed_by_membership_id else None,
        "decided_by_membership_id": 77 if snapshot.decided_by_membership_id else None,
    }
    return repo.insert_snapshot(snapshot, **(identities | overrides))


@pytest.mark.parametrize("status", [item.value for item in ApprovalStatus])
def test_owner_import_preserves_snapshot_hash_price_identity_and_status(
    latest_db, status
):
    owner, runtime = latest_db
    key = ApprovalRepositoryScope(7, 42, "legacy-" + "x" * 5000 + uuid4().hex)
    original = historical(key, status)
    with Session(owner) as session, session.begin():
        assert (
            imported(LegacyApprovalImportTransaction(session, key), original)
            == original
        )
    with Session(owner) as session, session.begin():
        assert (
            imported(LegacyApprovalImportTransaction(session, key), original)
            == original
        )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == original
        assert repository_tests.audit_count(session, key) == 1
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, key).claim(
            original.version, repository_tests.ACTOR
        )


def test_runtime_cannot_import_even_well_scoped_synthetic_snapshot(latest_db):
    _, runtime = latest_db
    key = repository_tests.scope()
    with (
        pytest.raises(ApprovalValidationError),
        Session(runtime) as session,
        session.begin(),
    ):
        imported(LegacyApprovalImportTransaction(session, key), historical(key))
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None


def test_unresolved_legacy_account_is_blocked_not_rekeyed(latest_db):
    owner, runtime = latest_db
    key = repository_tests.scope()
    with (
        pytest.raises(ApprovalIdentityBlockedError),
        Session(owner) as session,
        session.begin(),
    ):
        imported(
            LegacyApprovalImportTransaction(session, key),
            historical(key, account="external-account"),
        )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None


def test_conflicting_legacy_replay_preserves_first_snapshot(latest_db):
    owner, runtime = latest_db
    key = repository_tests.scope()
    original = historical(key)
    with Session(owner) as session, session.begin():
        imported(LegacyApprovalImportTransaction(session, key), original)
    with (
        pytest.raises(ApprovalConflictError),
        Session(owner) as session,
        session.begin(),
    ):
        imported(
            LegacyApprovalImportTransaction(session, key),
            replace(original, recommended_price_kopecks=1),
        )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalog_sku_id", True),
        ("catalog_sku_id", "99"),
        ("catalog_sku_id", 0),
        ("claimed_by_membership_id", True),
        ("claimed_by_membership_id", "77"),
        ("claimed_by_membership_id", 0),
        ("claimed_by_membership_id", 78),
    ],
)
def test_backfill_requires_explicit_matching_internal_int_identities(
    latest_db, field, value
):
    owner, runtime = latest_db
    key = repository_tests.scope()
    with (
        pytest.raises(ApprovalIdentityBlockedError),
        Session(owner) as session,
        session.begin(),
    ):
        imported(
            LegacyApprovalImportTransaction(session, key),
            historical(key, "applying"),
            **{field: value},
        )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None


def test_synthetic_import_rollback_leaves_no_historical_rows(latest_db):
    owner, runtime = latest_db
    key = repository_tests.scope()
    with (
        pytest.raises(RuntimeError, match="synthetic rollback"),
        Session(owner) as session,
        session.begin(),
    ):
        imported(
            LegacyApprovalImportTransaction(session, key), historical(key, "applied")
        )
        raise RuntimeError("synthetic rollback")
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None
        assert repository_tests.audit_count(session, key) == 0
