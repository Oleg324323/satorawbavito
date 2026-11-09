"""Committed override service on actual 0070, synthetic identities only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from threading import Event, Lock
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError

from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_sku_override_schema as schema
from tests import test_wb_repricing_approval_commands as auth
from tests.test_wb_repricing_override_command import command

cluster = schema.cluster
db = schema.db


@pytest.fixture
def context(db):
    owner, runtime = db
    sku = 1000 + uuid4().int % 1_000_000_000
    login = uuid4().hex
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) "
                "VALUES(:sku,7,:code)"
            ),
            {"sku": sku, "code": f"synthetic-{sku}"},
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_products(marketplace_product_id,organization_id,"
                "marketplace_account_id,external_product_id) VALUES(:sku,7,42,:external)"
            ),
            {"sku": sku, "external": f"synthetic-{sku}"},
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,"
                "marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) "
                "VALUES(:sku,7,42,:sku,:external,:sku)"
            ),
            {"sku": sku, "external": f"synthetic-{sku}"},
        )
        connection.execute(
            text(
                "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) "
                "VALUES(:login,'repricer77',clock_timestamp(),clock_timestamp(),"
                "clock_timestamp()+interval '1 hour')"
            ),
            {"login": login},
        )
    principal = auth.UserSessionPrincipal(7, "repricer77", 77, login)
    change = replace(command(), catalog_sku_id=sku, command_id=str(uuid4()))
    try:
        yield owner, runtime, principal, change
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE iam_memberships SET role='admin',permissions='[]'::jsonb,is_active=true,scope_mode='all',allowed_account_ids='[]'::jsonb WHERE membership_id=77"
                )
            )


def test_first_replace_returns_only_committed_full_revision(context):
    owner, runtime, principal, change = context
    # Actual accepted predecessor/schema bootstrap must succeed before API RED.
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    result = service.replace(change, principal, auth.binding(), now=now)
    assert result.change == change
    assert result.revision == 1 and result.parent_revision is None
    assert result.created_at == now
    assert result.canonical_request_bytes == change.canonical_bytes(max_bytes=8192)
    assert result.request_checksum == change.checksum(max_bytes=8192)
    with owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT version FROM wb_repricing_sku_override_heads "
                    "WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                ),
                {"sku": change.catalog_sku_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_repricing_sku_override_audit WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                ),
                {"sku": change.catalog_sku_id},
            ).scalar_one()
            == 1
        )


def test_exact_historical_replay_survives_new_revision_and_removed_mapping(context):
    owner, runtime, principal, change = context
    change = replace(
        change,
        values=replace(
            change.values,
            p_min_kopecks=2**80,
            min_margin_pct=Decimal("-0.12345678901234567890123456789"),
            automation_enabled=False,
            basket_norm_manual=0,
        ),
    )
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    first = service.replace(
        change, principal, auth.binding(), now=datetime(2026, 9, 9, 12, tzinfo=UTC)
    )
    second_change = replace(change, expected_version=1, command_id=str(uuid4()))
    second = service.replace(
        second_change,
        principal,
        auth.binding(),
        now=datetime(2026, 9, 9, 13, tzinfo=UTC),
    )
    assert second.revision == 2
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE marketplace_offer_id=:sku"
            ),
            {"sku": change.catalog_sku_id},
        )
    replay = service.replace(
        change, principal, auth.binding(), now=datetime(2026, 9, 10, tzinfo=UTC)
    )
    assert replay == first
    scope = domain.OverrideScope(7, 42, change.catalog_sku_id)
    assert service.get_current(scope, principal, auth.binding()) == second
    assert service.history(scope, principal, auth.binding(), limit=2) == (second, first)
    with owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_repricing_sku_override_audit WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                ),
                {"sku": change.catalog_sku_id},
            ).scalar_one()
            == 2
        )


def test_current_and_bounded_history_return_typed_immutable_revisions(context):
    _, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    scope = domain.OverrideScope(7, 42, change.catalog_sku_id)
    assert service.get_current(scope, principal, auth.binding()) is None
    assert service.history(scope, principal, auth.binding(), limit=2) == ()
    first = service.replace(
        change, principal, auth.binding(), now=datetime(2026, 9, 9, 12, tzinfo=UTC)
    )
    second = service.replace(
        replace(change, command_id=str(uuid4()), expected_version=1),
        principal,
        auth.binding(),
        now=datetime(2026, 9, 9, 13, tzinfo=UTC),
    )
    assert service.get_current(scope, principal, auth.binding()) == second
    assert service.history(scope, principal, auth.binding(), limit=1) == (second,)
    assert service.history(
        scope, principal, auth.binding(), limit=2, before_revision=2
    ) == (first,)
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        first.revision = 9


@pytest.mark.parametrize("kind", ["payload", "expected_version", "stale_new_command"])
def test_key_reuse_or_stale_command_conflicts_without_appending(context, kind):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    service.replace(change, principal, auth.binding(), now=now)
    if kind == "payload":
        altered = replace(change, values=replace(change.values, p_min_kopecks=1))
    elif kind == "expected_version":
        altered = replace(change, expected_version=1)
    else:
        altered = replace(change, command_id=str(uuid4()))
    with pytest.raises(domain.OverrideConflictError):
        service.replace(altered, principal, auth.binding(), now=now)
    with owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_repricing_sku_override_versions WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                ),
                {"sku": change.catalog_sku_id},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    "permissions",
    [
        "[]",
        '["settings:read"]',
        '["settings:write"]',
        '["settings:read","settings:write"]',
    ],
)
@pytest.mark.parametrize("operation", ["read", "replay", "replace"])
def test_live_permission_matrix_before_any_receipt_lookup(
    context, permissions, operation
):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    first = service.replace(change, principal, auth.binding(), now=now)
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET role='custom',permissions=CAST(:permissions AS jsonb) WHERE membership_id=77"
            ),
            {"permissions": permissions},
        )
    allowed = '"settings:read"' in permissions and (
        operation == "read" or '"settings:write"' in permissions
    )
    queries = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "wb_repricing_sku_override_" in statement:
            queries.append(statement)

    def invoke():
        if operation == "read":
            return service.get_current(
                domain.OverrideScope(7, 42, change.catalog_sku_id),
                principal,
                auth.binding(),
            )
        cmd = (
            change
            if operation == "replay"
            else replace(change, command_id=str(uuid4()), expected_version=1)
        )
        return service.replace(cmd, principal, auth.binding(), now=now)

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        if allowed:
            result = invoke()
            assert result == first if operation != "replace" else result.revision == 2
        else:
            with pytest.raises(PublicationGuardError):
                invoke()
            assert queries == []
    finally:
        event.remove(runtime, "before_cursor_execute", observe)


@pytest.mark.parametrize("failure_point", ["audit", "commit"])
def test_database_failure_rolls_back_whole_root_without_raw_payload(
    context, failure_point
):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)

    def fail_commit(connection):
        raise SQLAlchemyError("synthetic-sensitive-canary")

    def fail_audit(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricing_sku_override_audit"):
            raise SQLAlchemyError("synthetic-sensitive-canary")

    name, callback = (
        ("commit", fail_commit)
        if failure_point == "commit"
        else ("before_cursor_execute", fail_audit)
    )
    event.listen(runtime, name, callback)
    try:
        with pytest.raises(domain.OverridePersistenceError) as error:
            service.replace(
                change,
                principal,
                auth.binding(),
                now=datetime(2026, 9, 9, 12, tzinfo=UTC),
            )
        assert str(error.value) == "override_persistence_failed"
    finally:
        event.remove(runtime, name, callback)
    with owner.connect() as connection:
        for table in (schema.V, schema.H, schema.A):
            assert (
                connection.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                    ),
                    {"sku": change.catalog_sku_id},
                ).scalar_one()
                == 0
            )


def test_two_real_waiting_sessions_have_one_cas_winner(context):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    ready = Event()
    mutex = Lock()
    pids = set()

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "marketplace_accounts" in statement and "FOR UPDATE" in statement:
            with mutex:
                pids.add(connection.connection.driver_connection.info.backend_pid)
                if len(pids) == 2:
                    ready.set()

    def execute(cmd):
        try:
            return service.replace(
                cmd, principal, auth.binding(), now=datetime(2026, 9, 9, 12, tzinfo=UTC)
            )
        except domain.OverrideConflictError:
            return "conflict"

    event.listen(runtime, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with owner.begin() as holder:
                blocker = holder.scalar(text("SELECT pg_backend_pid()"))
                holder.execute(
                    text(
                        "SELECT marketplace_account_id FROM marketplace_accounts WHERE organization_id=7 AND marketplace_account_id=42 FOR UPDATE"
                    )
                )
                futures = [
                    pool.submit(execute, cmd)
                    for cmd in (change, replace(change, command_id=str(uuid4())))
                ]
                assert ready.wait(5)
                with owner.connect() as observer:
                    # A queued tuple waiter may block behind the other worker,
                    # not directly behind the transaction holding the row.
                    deadline = monotonic() + 5
                    while True:
                        waiting = observer.execute(
                            text(
                                "WITH RECURSIVE waits(start_pid,pid,path) AS ("
                                "SELECT p,p,ARRAY[p] FROM unnest(CAST(:pids AS integer[])) p "
                                "UNION ALL SELECT w.start_pid,b,w.path||b FROM waits w "
                                "CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) b "
                                "WHERE NOT b=ANY(w.path)) "
                                "SELECT count(DISTINCT start_pid) FROM waits WHERE pid=:holder"
                            ),
                            {"pids": list(pids), "holder": blocker},
                        ).scalar_one()
                        if waiting == 2:
                            break
                        assert monotonic() < deadline, (
                            "both owned waiter chains must reach the holder"
                        )
                        sleep(0.01)
                assert all(not future.done() for future in futures)
            results = [future.result(timeout=5) for future in futures]
        assert results.count("conflict") == 1
        winner = next(result for result in results if result != "conflict")
        assert winner.revision == 1
    finally:
        event.remove(runtime, "before_cursor_execute", observe)


def test_same_org_two_accounts_have_independent_command_namespace(context):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    first = service.replace(change, principal, auth.binding(), now=now)
    other_id = change.catalog_sku_id + 1_000_000_000
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO marketplace_products(marketplace_product_id,organization_id,marketplace_account_id,external_product_id) VALUES(:id,7,43,:external)"
            ),
            {"id": other_id, "external": f"synthetic-{other_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) VALUES(:id,7,43,:id,:external,:sku)"
            ),
            {
                "id": other_id,
                "external": f"synthetic-{other_id}",
                "sku": change.catalog_sku_id,
            },
        )
    second_change = replace(change, marketplace_account_id=43)
    binding = replace(
        auth.binding(), marketplace_account_id=43, external_account_id="synthetic-43"
    )
    second = service.replace(second_change, principal, binding, now=now)
    assert first.revision == second.revision == 1
    assert first.request_checksum != second.request_checksum
    assert service.replace(change, principal, auth.binding(), now=now) == first
    assert service.replace(second_change, principal, binding, now=now) == second


def test_changed_authenticated_actor_cannot_reuse_historical_command(context):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    service.replace(change, principal, auth.binding(), now=now)
    login = uuid4().hex
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) VALUES(:login,'repricer78',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"
            ),
            {"login": login},
        )
    other = auth.UserSessionPrincipal(7, "repricer78", 78, login)
    with pytest.raises(domain.OverrideConflictError):
        service.replace(
            replace(change, actor_membership_id=78), other, auth.binding(), now=now
        )


def test_final_live_auth_revalidation_rolls_back_pending_revision(context):
    owner, runtime, principal, change = context
    domain = import_module("app.modules.wb_repricing_override_service")
    service = domain.SkuOverrideService(runtime, max_request_bytes=8192)

    def revoke(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricing_sku_override_audit"):
            connection.execute(
                text(
                    "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                ),
                {"id": principal.session_id},
            )

    event.listen(runtime, "after_cursor_execute", revoke)
    try:
        with pytest.raises(PublicationGuardError):
            service.replace(
                change,
                principal,
                auth.binding(),
                now=datetime(2026, 9, 9, 12, tzinfo=UTC),
            )
    finally:
        event.remove(runtime, "after_cursor_execute", revoke)
    with owner.connect() as connection:
        for table in (schema.V, schema.H, schema.A):
            assert (
                connection.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE organization_id=7 AND marketplace_account_id=42 AND catalog_sku_id=:sku"
                    ),
                    {"sku": change.catalog_sku_id},
                ).scalar_one()
                == 0
            )
