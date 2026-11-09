"""Physical management roots with real guards, synthetic keys and fake WB I/O."""

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow, LkSessionRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_management import (
    CredentialManagementError,
    CredentialManagementService,
)
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.security.marketplace_credentials import CredentialKeyring
from tests import test_marketplace_credential_fetch_postgres as fetch_tests
from tests import test_publication_guard_postgres as guard_tests
from tests.test_credential_maintenance_inert_postgres import cluster, pg_database  # noqa: F401

pg_store = fetch_tests.pg_store
data = guard_tests.data
TOKEN = "synthetic-management-acceptance-token"


@pytest.fixture
def management(data):
    d = data
    with Session(d.engine) as session, session.begin():
        session.execute(update(IamMembershipRow).where(
            IamMembershipRow.membership_id == d.org).values(permissions=["integrations:write"]))
        session.execute(update(MarketplaceAccountRow).where(
            MarketplaceAccountRow.marketplace_account_id == d.org).values(status="disconnected"))
    d.actor = ActorContext(actor_id=f"owner-{d.org}", user_id=f"owner-{d.org}",
                           organization_id=d.org, permission_profile="custom",
                           permissions=frozenset({"integrations:write"}), session_id=f"login-{d.org}")
    d.inputs = dict(authenticated_actor=d.actor, marketplace_account_id=d.org,
                    provider="wb", credential_kind="wb_api")
    d.verifications = []

    def verify(token):
        d.verifications.append(token)
        return f"legacy-{d.org}"

    d.verify = verify
    return d


def service(d, *, factory=None, verifier=None):
    return CredentialManagementService(
        session_factory=factory or d.factory,
        keyring_loader=lambda: CredentialKeyring(current_key_version=7, keys={7: b"a" * 32}),
        wb_seller_verifier=verifier or d.verify,
    )


def persisted(d):
    """Independent physical readback, including atomic credential audit rows."""
    with Session(d.engine) as session:
        rows = session.execute(select(
            MarketplaceAccountCredentialRow.credential_id,
            MarketplaceAccountCredentialRow.generation,
            MarketplaceAccountCredentialRow.revoked_at,
            MarketplaceAccountCredentialRow.revocation_reason_code,
        ).where(MarketplaceAccountCredentialRow.organization_id == d.org).order_by(
            MarketplaceAccountCredentialRow.generation)).all()
        audits = session.execute(select(LkAuditEventRow.action, LkAuditEventRow.object_id).where(
            LkAuditEventRow.organization_id == d.org,
            LkAuditEventRow.action.like("integration.marketplace_credential.%"),
        ).order_by(LkAuditEventRow.action, LkAuditEventRow.object_id)).all()
    return rows, audits


def test_management_disconnected_put_and_revoke_commit_with_audit(management):
    d = management
    manager = service(d)
    before_rows, before_audits = persisted(d)
    result = manager.put(**d.inputs, plaintext={"token": TOKEN})
    rows, audits = persisted(d)
    assert len(rows) == len(before_rows) + 1
    assert rows[-1][:2] == (result.credential_id, result.generation)
    assert rows[-1][2:] == (None, None)
    assert rows[-2][2] is not None and rows[-2][3] == "credential_replaced"
    assert len(audits) == len(before_audits) + 1
    assert ("integration.marketplace_credential.put", str(result.credential_id)) in audits
    assert manager.status(**d.inputs).credential_id == result.credential_id
    revoked = manager.revoke(**d.inputs, reason_code="operator_revoked")
    rows, audits = persisted(d)
    assert revoked.credential_id == result.credential_id and revoked.revoked_at is not None
    assert rows[-1][2:] == (revoked.revoked_at, "operator_revoked")
    assert ("integration.marketplace_credential.revoke", str(result.credential_id)) in audits
    assert len(audits) == len(before_audits) + 2
    assert d.verifications == [TOKEN]


@pytest.mark.parametrize("mutation, code", [
    ("membership", "credential_management_access_denied"),
    ("session", "credential_management_access_denied"),
    ("account", "credential_account_identity_mismatch"),
])
def test_management_rechecks_committed_revocation_after_wb_verification(management, mutation, code):
    d = management
    before = persisted(d)

    def verify(token):
        d.verify(token)
        # This independent commit would time out if snapshot locks survived I/O.
        with Session(d.engine) as session, session.begin():
            if mutation == "membership":
                session.execute(update(IamMembershipRow).where(
                    IamMembershipRow.membership_id == d.org).values(permissions=[]))
            elif mutation == "session":
                session.execute(update(LkSessionRow).where(
                    LkSessionRow.session_id == d.actor.session_id).values(revoked_at=func.clock_timestamp()))
            else:
                session.execute(update(MarketplaceAccountRow).where(
                    MarketplaceAccountRow.marketplace_account_id == d.org).values(external_account_id="replacement"))
        return f"legacy-{d.org}"

    with pytest.raises(CredentialManagementError, match=f"^{code}$"):
        service(d, verifier=verify).put(**d.inputs, plaintext={"token": TOKEN})
    assert d.verifications == [TOKEN]
    assert persisted(d) == before


def test_management_flush_failure_rolls_back_replacement_and_audit(management):
    d = management
    before = persisted(d)
    failed = []

    def factory():
        session = d.factory()

        def reject_audit(session, _flush_context, _instances):
            if any(isinstance(row, LkAuditEventRow) for row in session.new):
                failed.append(True)
                raise RuntimeError("synthetic-audit-failure")

        event.listen(session, "before_flush", reject_audit)
        return session

    with pytest.raises(CredentialManagementError, match="^credential_persistence_failed$"):
        service(d, factory=factory).put(**d.inputs, plaintext={"token": TOKEN})
    assert failed == [True]
    assert persisted(d) == before


def test_management_after_commit_failure_requires_readback_without_retry(management):
    d = management
    before_rows, before_audits = persisted(d)
    roots = []

    def factory():
        session = d.factory()
        roots.append(session)
        if len(roots) == 2:  # put root, after the successful snapshot root
            def fail_after_commit(_session):
                raise RuntimeError("synthetic-after-commit-failure")

            event.listen(session, "after_commit", fail_after_commit)
        return session

    with pytest.raises(CredentialManagementError, match="^credential_management_readback_required$"):
        service(d, factory=factory).put(**d.inputs, plaintext={"token": TOKEN})
    rows, audits = persisted(d)
    assert len(roots) == 2 and d.verifications == [TOKEN]
    assert len(rows) == len(before_rows) + 1
    assert rows[-1][1:] == (before_rows[-1][1] + 1, None, None)
    assert len(audits) == len(before_audits) + 1
    assert service(d).status(**d.inputs).credential_id == rows[-1][0]
