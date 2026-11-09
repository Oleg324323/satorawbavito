"""Production permission fences on an owned disposable PostgreSQL database."""
# Separate contexts expose commit and rollback boundaries.
# ruff: noqa: SIM117

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow, LkSessionRow
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from tests import test_publication_guard_postgres as guard_tests
from tests.test_publication_guard import api

cluster = guard_tests.cluster
pg_database = guard_tests.pg_database
pg_store = guard_tests.pg_store
data = guard_tests.data
arguments = guard_tests.arguments
proof = guard_tests.proof
count_proof = guard_tests.count_proof
wait_blocked = guard_tests.wait_blocked

READ = frozenset({"production:read"})
CREATE = frozenset({"production:read", "production:create"})
ASSIGN = frozenset({"production:read", "production:assign"})
OPERATIONS = (("read", READ), ("create", CREATE), ("assign", ASSIGN))


def production_arguments(d, provider, required_permissions):
    args = arguments(d, token=provider == "avito", read=True)
    args["required_permissions"] = required_permissions
    assert args["authorities"] == ()
    return args


def set_membership(d, *, role="custom", permissions=(), scope_mode="all", account_ids=()):
    with Session(d.engine) as session, session.begin():
        session.execute(
            update(IamMembershipRow)
            .where(IamMembershipRow.membership_id == d.org)
            .values(
                role=role,
                permissions=sorted(permissions),
                scope_mode=scope_mode,
                allowed_account_ids=list(account_ids),
            )
        )


@pytest.mark.parametrize("operation,required_permissions", OPERATIONS)
@pytest.mark.parametrize("provider", ["wb", "avito"])
@pytest.mark.parametrize("role", ["custom", "viewer", "admin"])
def test_explicit_membership_grants_allow_each_operation_and_exact_account_binding(
    data, operation, required_permissions, provider, role
):
    d = data
    set_membership(d, role=role, permissions=required_permissions)
    args = production_arguments(d, provider, required_permissions)

    with d.factory() as session, session.begin():
        guard = api().acquire_publication_guard(session, **args)
        guard.revalidate_before_write()
        proof(session, d)

    assert operation in {"read", "create", "assign"}
    assert count_proof(d) == 2


@pytest.mark.parametrize("operation,required_permissions", OPERATIONS)
@pytest.mark.parametrize(
    "role",
    [
        "viewer",
        "settings_editor",
        "price_sender",
        "finance_viewer",
        "admin",
        "custom",
        "owner",
        "production",
    ],
)
def test_profiles_and_legacy_production_alias_do_not_implicitly_grant_production(
    data, operation, required_permissions, role
):
    d = data
    set_membership(d, role=role)
    args = production_arguments(d, "wb", required_permissions)

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert operation in {"read", "create", "assign"}
    assert count_proof(d) == 0


@pytest.mark.parametrize(
    "required_permissions,granted_permissions",
    [
        (CREATE, READ),
        (ASSIGN, READ),
        (CREATE, ASSIGN),
        (ASSIGN, CREATE),
    ],
)
def test_read_only_and_wrong_write_operation_grants_deny(
    data, required_permissions, granted_permissions
):
    d = data
    set_membership(d, permissions=granted_permissions)
    args = production_arguments(d, "wb", required_permissions)

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert count_proof(d) == 0


def test_unknown_required_permission_is_not_implicitly_granted_or_reflected(data):
    d = data
    set_membership(d, permissions=CREATE)
    args = production_arguments(
        d, "wb", CREATE | frozenset({"unknown-secret-canary"})
    )

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ) as caught:
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert "unknown-secret-canary" not in repr(caught.value)
    assert count_proof(d) == 0


def test_same_organization_wrong_selected_account_denies(data):
    d = data
    set_membership(
        d,
        permissions=CREATE,
        scope_mode="selected",
        account_ids=(d.org + 100000,),
    )
    args = production_arguments(d, "wb", CREATE)

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert count_proof(d) == 0


def add_foreign_account(d):
    account_id = d.org + 300000
    with Session(d.engine) as session, session.begin():
        session.add(
            MarketplaceAccountRow(
                marketplace_account_id=account_id,
                organization_id=d.org + 200000,
                marketplace="wb",
                external_account_id=f"foreign-{d.org}",
                status="connected",
            )
        )
    return api().ExpectedAccountBinding(
        account_id, "wb", f"foreign-{d.org}", None
    )


@pytest.mark.parametrize("boundary", ["principal", "account"])
def test_different_organization_principal_or_account_denies(data, boundary):
    d = data
    set_membership(d, permissions=CREATE)
    foreign_account = add_foreign_account(d)
    args = production_arguments(d, "wb", CREATE)
    expected_code = "publication_access_denied"
    if boundary == "principal":
        args["principal"] = replace(
            args["principal"], organization_id=d.org + 200000
        )
        args["accounts"] = (foreign_account,)
    else:
        args["accounts"] = (foreign_account,)
        expected_code = "publication_binding_changed"

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match=f"^{expected_code}$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert count_proof(d) == 0


@pytest.mark.parametrize(
    "state,expected_code",
    [
        ("inactive_membership", "publication_access_denied"),
        ("revoked_session", "publication_access_denied"),
        ("expired_session", "publication_expired"),
    ],
)
def test_inactive_membership_and_invalid_sessions_deny(data, state, expected_code):
    d = data
    set_membership(d, permissions=CREATE)
    with Session(d.engine) as session, session.begin():
        if state == "inactive_membership":
            session.execute(
                update(IamMembershipRow)
                .where(IamMembershipRow.membership_id == d.org)
                .values(is_active=False)
            )
        elif state == "revoked_session":
            session.execute(
                update(LkSessionRow)
                .where(LkSessionRow.session_id == f"login-{d.org}")
                .values(revoked_at=datetime.now(UTC))
            )
        else:
            session.execute(
                update(LkSessionRow)
                .where(LkSessionRow.session_id == f"login-{d.org}")
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    args = production_arguments(d, "wb", CREATE)

    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match=f"^{expected_code}$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert count_proof(d) == 0


def test_stale_membership_identity_map_does_not_retain_explicit_grants(data):
    d = data
    set_membership(d, permissions=CREATE)
    args = production_arguments(d, "wb", CREATE)

    with d.factory() as session, session.begin():
        set_tenant_context(session, d.org)
        cached = session.get(IamMembershipRow, d.org)
        assert cached.permissions == sorted(CREATE)
        set_membership(d, permissions=READ)
        assert cached.permissions == sorted(CREATE)
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            api().acquire_publication_guard(session, **args)
        session.rollback()

    assert count_proof(d) == 0


def replay_existing_proof(session, d, args, disclosed):
    api().acquire_publication_guard(session, **args)
    rows = session.scalars(
        select(LkAuditEventRow.action)
        .where(
            LkAuditEventRow.organization_id == d.org,
            LkAuditEventRow.action.in_(
                ["synthetic.publication", "synthetic.proof"]
            ),
        )
        .order_by(LkAuditEventRow.action)
    ).all()
    disclosed.extend(rows)
    return tuple(rows)


@pytest.mark.parametrize("required_permissions", [CREATE, ASSIGN])
def test_synthetic_replay_rechecks_operation_grants_before_returning_existing_proof(
    data, required_permissions
):
    d = data
    set_membership(d, permissions=required_permissions)
    args = production_arguments(d, "wb", required_permissions)
    with d.factory() as session, session.begin():
        api().acquire_publication_guard(session, **args)
        proof(session, d)
    assert count_proof(d) == 2

    allowed = []
    with d.factory() as session, session.begin():
        result = replay_existing_proof(session, d, args, allowed)
    assert result == ("synthetic.proof", "synthetic.publication")
    assert allowed == list(result)

    set_membership(d, permissions=READ)
    denied = []
    with d.factory() as session, session.begin():
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            replay_existing_proof(session, d, args, denied)
        session.rollback()
    assert denied == []
    assert count_proof(d) == 2


@pytest.mark.parametrize("required_permissions", [CREATE, ASSIGN])
@pytest.mark.parametrize("removed", ["read", "command"])
@pytest.mark.parametrize("stage", ["before_acquire", "before_commit"])
def test_removing_read_or_command_grant_denies_and_rolls_back(
    data, required_permissions, removed, stage
):
    d = data
    set_membership(d, permissions=required_permissions)
    command = next(iter(required_permissions - READ))
    retained = required_permissions - ({"production:read"} if removed == "read" else {command})
    args = production_arguments(d, "wb", required_permissions)

    if stage == "before_acquire":
        set_membership(d, permissions=retained)
        with d.factory() as session, session.begin():
            with pytest.raises(
                api().PublicationGuardError, match="^publication_access_denied$"
            ):
                api().acquire_publication_guard(session, **args)
            session.rollback()
    else:
        with pytest.raises(
            api().PublicationGuardError, match="^publication_access_denied$"
        ):
            with d.factory() as session, session.begin():
                api().acquire_publication_guard(session, **args)
                proof(session, d)
                session.execute(
                    update(IamMembershipRow)
                    .where(IamMembershipRow.membership_id == d.org)
                    .values(permissions=sorted(retained))
                )

    assert count_proof(d) == 0


@pytest.mark.parametrize("required_permissions", [CREATE, ASSIGN])
@pytest.mark.parametrize("requester_first", [False, True])
def test_concurrent_command_grant_revocation_observes_both_lock_winners(
    data, required_permissions, requester_first
):
    d = data
    set_membership(d, permissions=required_permissions)
    args = production_arguments(d, "wb", required_permissions)
    started = Event()
    pids = {}

    def request():
        with d.factory() as session, session.begin():
            pids["requester"] = session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            api().acquire_publication_guard(session, **args)
            proof(session, d)

    def revoke():
        with Session(d.engine) as session, session.begin():
            pids["revoker"] = session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            session.execute(
                update(IamMembershipRow)
                .where(IamMembershipRow.membership_id == d.org)
                .values(permissions=sorted(READ))
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        if requester_first:
            with d.factory() as session, session.begin():
                pids["requester"] = session.scalar(text("SELECT pg_backend_pid()"))
                api().acquire_publication_guard(session, **args)
                future = pool.submit(revoke)
                assert started.wait(5)
                with d.engine.connect() as observer:
                    wait_blocked(observer, pids["revoker"], pids["requester"])
                assert not future.done()
                proof(session, d)
            future.result(timeout=8)
            assert count_proof(d) == 2
            with d.factory() as session, session.begin():
                with pytest.raises(
                    api().PublicationGuardError,
                    match="^publication_access_denied$",
                ):
                    api().acquire_publication_guard(session, **args)
                session.rollback()
            assert count_proof(d) == 2
        else:
            with Session(d.engine) as session, session.begin():
                pids["revoker"] = session.scalar(text("SELECT pg_backend_pid()"))
                session.execute(
                    update(IamMembershipRow)
                    .where(IamMembershipRow.membership_id == d.org)
                    .values(permissions=sorted(READ))
                )
                future = pool.submit(request)
                assert started.wait(5)
                with d.engine.connect() as observer:
                    wait_blocked(observer, pids["requester"], pids["revoker"])
            with pytest.raises(
                api().PublicationGuardError, match="^publication_access_denied$"
            ):
                future.result(timeout=8)
            assert count_proof(d) == 0
