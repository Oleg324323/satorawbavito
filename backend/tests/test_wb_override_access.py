"""Real existing guard/catalog rows; no override DDL, receipts or provider I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.modules.wb_repricing_override_access import (
    OverrideMappingUnresolvedError,
    acquire_override_read_guard,
    acquire_override_replace_guard,
)
from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_wb_repricing_approval_commands as commands_tests
from tests.test_marketplace_credential_fetch_postgres import wait_blocked
from tests.test_wb_repricing_override_command import command

cluster = commands_tests.cluster
latest_db = commands_tests.latest_db
principal = commands_tests.principal


@pytest.fixture
def mapped(latest_db):
    owner, runtime = latest_db
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO marketplace_products(marketplace_product_id,organization_id,marketplace_account_id,external_product_id) VALUES(501,7,42,'synthetic-map')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) VALUES(601,7,42,501,'synthetic-offer',99)"
            )
        )
    try:
        yield owner, runtime
    finally:
        with owner.begin() as connection:
            connection.execute(
                text("DELETE FROM marketplace_offers WHERE marketplace_offer_id=601")
            )
            connection.execute(
                text(
                    "DELETE FROM marketplace_products WHERE marketplace_product_id=501"
                )
            )
            connection.execute(
                text(
                    "UPDATE iam_memberships SET role='admin',permissions='[]'::jsonb,scope_mode='all',allowed_account_ids='[]'::jsonb,is_active=true WHERE membership_id=77"
                )
            )


def acquire(operation, session, principal, **changes):
    args = {"principal": principal, "binding": commands_tests.binding(), **changes}
    if operation == "read":
        return acquire_override_read_guard(
            session, organization_id=7, marketplace_account_id=42, **args
        )
    return acquire_override_replace_guard(session, change=command(), **args)


@pytest.mark.parametrize(
    "permissions",
    [(), ("settings:read",), ("settings:write",), ("settings:read", "settings:write")],
)
@pytest.mark.parametrize("operation", ["read", "replace"])
def test_actual_permission_matrix_before_any_mapping_lookup(
    mapped, principal, permissions, operation
):
    owner, runtime = mapped
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET role='custom',permissions=CAST(:permissions AS jsonb) WHERE membership_id=77"
            ),
            {"permissions": json.dumps(permissions)},
        )
    allowed = "settings:read" in permissions and (
        operation == "read" or "settings:write" in permissions
    )
    mapping_reads = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "marketplace_offers" in statement:
            mapping_reads.append(statement)

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        if allowed:
            with Session(runtime) as session, session.begin():
                acquire(operation, session, principal).revalidate_before_write()
        else:
            with (
                pytest.raises(PublicationGuardError),
                Session(runtime) as session,
                session.begin(),
            ):
                acquire(operation, session, principal)
            assert mapping_reads == []
    finally:
        event.remove(runtime, "before_cursor_execute", observe)
    assert bool(mapping_reads) == (allowed and operation == "replace")


@pytest.mark.parametrize(
    "change", ["actor", "org", "account", "binding", "inactive", "revoked"]
)
def test_invalid_authority_is_denied_before_mapping(mapped, principal, change):
    owner, runtime = mapped
    args = {}
    if change == "actor":
        principal = replace(principal, membership_id=78)
    elif change == "org":
        principal = replace(principal, organization_id=8)
    elif change == "account":
        args["binding"] = replace(commands_tests.binding(), marketplace_account_id=43)
    elif change == "binding":
        args["binding"] = replace(
            commands_tests.binding(), external_account_id="different"
        )
    elif change == "inactive":
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE iam_memberships SET is_active=false WHERE membership_id=77"
                )
            )
    else:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:session"
                ),
                {"session": principal.session_id},
            )
    mapping_reads = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "marketplace_offers" in statement:
            mapping_reads.append(statement)

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        with (
            pytest.raises(PublicationGuardError),
            Session(runtime) as session,
            session.begin(),
        ):
            acquire("replace", session, principal, **args)
        assert mapping_reads == []
    finally:
        event.remove(runtime, "before_cursor_execute", observe)


def test_missing_mapping_blocks_replace_but_not_historical_read(mapped, principal):
    owner, runtime = mapped
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=601"
            )
        )
    with (
        pytest.raises(OverrideMappingUnresolvedError, match="override_mapping_unresolved"),
        Session(runtime) as session,
        session.begin(),
    ):
        acquire("replace", session, principal)
    with Session(runtime) as session, session.begin():
        acquire("read", session, principal)


def test_same_org_other_account_mapping_does_not_resolve_requested_scope(
    mapped, principal
):
    owner, runtime = mapped
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=601"
            )
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_products(marketplace_product_id,organization_id,marketplace_account_id,external_product_id) VALUES(502,7,43,'other')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) VALUES(602,7,43,502,'other',99)"
            )
        )
    try:
        with (
            pytest.raises(OverrideMappingUnresolvedError, match="override_mapping_unresolved"),
            Session(runtime) as session,
            session.begin(),
        ):
            acquire("replace", session, principal)
    finally:
        with owner.begin() as connection:
            connection.execute(
                text("DELETE FROM marketplace_offers WHERE marketplace_offer_id=602")
            )
            connection.execute(
                text(
                    "DELETE FROM marketplace_products WHERE marketplace_product_id=502"
                )
            )


def test_mapping_share_lock_survives_until_caller_commit(mapped, principal):
    owner, runtime = mapped
    started = Event()
    pids = {}

    def remove_mapping():
        with owner.begin() as connection:
            pids["writer"] = connection.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            connection.execute(
                text(
                    "UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=601"
                )
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(runtime) as session, session.begin():
            guard = acquire("replace", session, principal)
            reader = session.scalar(text("SELECT pg_backend_pid()"))
            future = pool.submit(remove_mapping)
            assert started.wait(5)
            with owner.connect() as observer:
                wait_blocked(observer, pids["writer"], reader)
            assert not future.done()
            guard.revalidate_before_write()
        future.result(timeout=5)
    with (
        pytest.raises(OverrideMappingUnresolvedError, match="override_mapping_unresolved"),
        Session(runtime) as session,
        session.begin(),
    ):
        acquire("replace", session, principal)


@pytest.mark.parametrize("blocked_on", ["mapping", "membership"])
def test_changed_scope_after_actual_lock_wait_fails_closed(
    mapped, principal, blocked_on
):
    owner, runtime = mapped
    started = Event()
    pids = {}
    mapping_queries = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "marketplace_offers" in statement:
            mapping_queries.append(statement)
        if (blocked_on == "mapping" and "marketplace_offers" in statement) or (
            blocked_on == "membership"
            and "iam_memberships" in statement
            and "FOR SHARE" in statement
        ):
            pids["reader"] = connection.connection.driver_connection.info.backend_pid
            started.set()

    def try_access():
        with Session(runtime) as session, session.begin():
            acquire("replace", session, principal)

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with owner.begin() as connection:
                writer = connection.scalar(text("SELECT pg_backend_pid()"))
                if blocked_on == "mapping":
                    connection.execute(
                        text(
                            "UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=601"
                        )
                    )
                else:
                    connection.execute(
                        text(
                            "UPDATE iam_memberships SET role='custom',permissions='[\"settings:read\"]'::jsonb WHERE membership_id=77"
                        )
                    )
                future = pool.submit(try_access)
                assert started.wait(5)
                with owner.connect() as observer:
                    wait_blocked(observer, pids["reader"], writer)
            expected_error = (
                OverrideMappingUnresolvedError
                if blocked_on == "mapping"
                else PublicationGuardError
            )
            with pytest.raises(expected_error):
                future.result(timeout=5)
        if blocked_on == "membership":
            assert mapping_queries == []
    finally:
        event.remove(runtime, "before_cursor_execute", observe)
