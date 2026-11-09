"""Owned synthetic source publication; run only in the allocated PostgreSQL slot."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.infra.db import set_tenant_context
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
    _resolve_fetch_in_session,
)
from app.reviews import shadow_service as service
from app.reviews.local_service import read_local_review_context
from tests import test_account_avito_stats_postgres as stats

cluster, stats_db = stats.cluster, stats.stats_db


@pytest.fixture
def context(stats_db):
    # Existing context fixture seeds only synthetic encrypted access credentials.
    context = stats.context.__wrapped__(stats_db)
    # T1-approved fixture-only PURE4: migration grants to then-existing writers,
    # while this reusable fixture grants its runtime table rights after migrate.
    # No trigger/all-functions/operational-role permission expansion.
    with context.owner.begin() as connection:
        role = connection.dialect.identifier_preparer.quote(
            context.runtime.url.username
        )
        connection.exec_driver_sql(
            "GRANT EXECUTE ON FUNCTION public.review_strict_utf8(bytea), "
            "public.review_coverage_json_object_utf8(bytea), "
            "public.review_binding_ascii_string(text), "
            "public.review_run_binding_bytes(integer,integer,text,text,text) TO " + role
        )
    stats.change(
        context,
        'UPDATE iam_memberships SET permissions=\'["reviews:write","reviews:read"]\'::json WHERE user_id=:user',
    )
    with Session(context.runtime) as session, session.begin():
        set_tenant_context(session, context.actor.organization_id)
        resolved = _resolve_fetch_in_session(
            session,
            MarketplaceAccountCredentialOwner(91001, context.account, "avito"),
            "avito_oauth_access",
            stats._keyring(),
        )
    context.binding = resolved.binding
    context.settings = Settings(
        review_shadow_enabled=True,
        review_shadow_account_pairs=((91001, context.account),),
    )

    def safe_database_failure(error_context):
        code = getattr(error_context.original_exception, "sqlstate", None)
        if not (
            type(code) is str and len(code) == 5 and code.isascii() and code.isalnum()
        ):
            code = "unknown"
        print("received-review-source SQLSTATE=" + code)

    event.listen(context.runtime, "handle_error", safe_database_failure)
    try:
        yield context
    finally:
        event.remove(context.runtime, "handle_error", safe_database_failure)


def begin(context, **changes):
    return service.begin_avito_review_shadow(
        context.runtime,
        actor=changes.pop("actor", context.actor),
        settings=changes.pop("settings", context.settings),
        binding=changes.pop("binding", context.binding),
        source_run_id=changes.pop("source_run_id", str(uuid4())),
        request_checksum=changes.pop("request_checksum", "a" * 64),
    )


def raw(body=" [TEST] exact\0source "):
    return {"id": "001", "createdAt": 1785230400, "text": body, "answer": None}


def publish(context, ticket, rows=None):
    return service.publish_received_avito_review_rows(
        context.runtime, ticket=ticket, rows=(raw(),) if rows is None else rows
    )


def run_status(context, ticket):
    with context.owner.connect() as connection:
        return connection.execute(
            text("SELECT status FROM review_sync_runs_v2 WHERE sync_run_id=:id"),
            {"id": ticket.run.sync_run_id},
        ).scalar_one()


def roundtrip_binding(connection, context):
    # 0076 advances incarnation itself; never write the protected version.
    for external in (context.external + "1", context.external):
        connection.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id=:external WHERE marketplace_account_id=:id"
            ),
            {"external": external, "id": context.account},
        )
    return connection.execute(
        text(
            "SELECT ingestion_binding_version FROM marketplace_accounts WHERE marketplace_account_id=:id"
        ),
        {"id": context.account},
    ).scalar_one()


def assert_no_published_facts(context):
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_facts WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_observations WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 0
        )


def test_committed_reservation_exact_text_replay_and_existing_avito_read_context(
    context,
):
    with context.runtime.connect() as connection:
        for signature in (
            "public.review_strict_utf8(bytea)",
            "public.review_coverage_json_object_utf8(bytea)",
            "public.review_binding_ascii_string(text)",
            "public.review_run_binding_bytes(integer,integer,text,text,text)",
        ):
            allowed = connection.execute(
                text(
                    "SELECT has_function_privilege(current_user,:signature,'EXECUTE')"
                ),
                {"signature": signature},
            ).scalar_one()
            print(signature + " EXECUTE=" + str(allowed))
        for table, privilege in (
            ("public.review_sync_runs_v2", "SELECT"),
            ("public.review_sync_runs_v2", "INSERT"),
            ("public.marketplace_accounts", "SELECT"),
            ("public.marketplace_accounts", "UPDATE"),
        ):
            allowed = connection.execute(
                text("SELECT has_table_privilege(current_user,:table,:privilege)"),
                {"table": table, "privilege": privilege},
            ).scalar_one()
            print(table + " " + privilege + "=" + str(allowed))
    ticket = begin(context)
    assert run_status(context, ticket) == "running"
    # No account lock/root left open across the boundary where caller will fetch.
    with context.owner.begin() as connection:
        connection.execute(
            text(
                "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:id FOR UPDATE NOWAIT"
            ),
            {"id": context.account},
        )
    first = publish(context, ticket)
    replay = publish(context, ticket)
    assert first == replay and first.observed_count == 1
    assert run_status(context, ticket) == "partial"
    with context.owner.connect() as connection:
        rid = connection.execute(
            text("SELECT review_id FROM review_facts WHERE marketplace_account_id=:id"),
            {"id": context.account},
        ).scalar_one()
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_observations WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 1
        )
    value = read_local_review_context(
        context.runtime,
        actor=context.actor,
        settings=context.settings,
        marketplace_account_id=context.account,
        marketplace="avito",
        review_id=str(rid),
        external_review_id="001",
    )
    assert value["review"]["text"] == " [TEST] exact\0source "
    assert value["review"]["sourceOrderState"] == "current"
    assert value["draft"] is None and value["decision"] is None


def test_disabled_source_has_no_reservation(context):
    with pytest.raises(service.ReviewShadowError):
        begin(context, settings=Settings())
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_sync_runs_v2 WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 0
        )


def test_cabinet_read_cannot_authorize_publication(context):
    stats.change(
        context,
        "UPDATE iam_memberships SET permissions='[\"cabinet:read\"]'::json WHERE user_id=:user",
    )
    with pytest.raises(service.ReviewShadowError):
        begin(context)


@pytest.mark.parametrize(
    "case", ["permission", "session", "binding", "incarnation", "replacement"]
)
def test_changed_original_authority_keeps_reservation_unpublished(context, case):
    ticket = begin(context)
    statements = {
        "permission": "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
        "session": "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
        "binding": "UPDATE marketplace_accounts SET external_account_id=external_account_id||'1' WHERE marketplace_account_id=:account",
    }
    if case == "replacement":
        stats.put_access(context)
    elif case == "incarnation":
        with context.owner.begin() as connection:
            assert (
                roundtrip_binding(connection, context) == ticket.account_incarnation + 2
            )
    else:
        stats.change(context, statements[case])
    with pytest.raises(service.ReviewShadowError) as error:
        publish(context, ticket)
    assert error.value.__context__ is None
    if case == "incarnation":
        assert error.value.code == "REVIEW_SHADOW_AUTHORITY_CHANGED"
        assert_no_published_facts(context)
    assert run_status(context, ticket) == "running"


def test_closed_run_changed_payload_conflicts_without_new_observation(context):
    ticket = begin(context)
    publish(context, ticket)
    with pytest.raises(service.ReviewShadowError, match="CONFLICT"):
        publish(context, ticket, (raw("changed"),))
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_observations WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 1
        )


def test_duplicate_page_rolls_back_without_publishing_any_fact(context):
    ticket = begin(context)
    with pytest.raises(service.ReviewShadowError):
        publish(context, ticket, (raw(), raw()))
    assert run_status(context, ticket) == "running"


def test_empty_partial_page_does_not_delete_existing_fact(context):
    publish(context, begin(context))
    ticket = begin(context)
    assert publish(context, ticket, ()).observed_count == 0
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM review_facts WHERE marketplace_account_id=:id"
                ),
                {"id": context.account},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("operation", ["begin", "publish"])
def test_physical_commit_failure_does_not_return_success(context, operation):
    ticket = begin(context) if operation == "publish" else None
    calls = []

    def fail(connection):
        calls.append(1)
        raise RuntimeError("synthetic-private")

    event.listen(context.runtime, "commit", fail)
    try:
        with pytest.raises(service.ReviewShadowError) as error:
            begin(context) if operation == "begin" else publish(context, ticket)
        assert error.value.__context__ is None
        assert calls == [1]
    finally:
        event.remove(context.runtime, "commit", fail)
    if ticket:
        assert run_status(context, ticket) == "running"


def test_cross_org_actor_cannot_claim_the_paired_binding(context):
    with pytest.raises(service.ReviewShadowError):
        begin(context, actor=replace(context.actor, organization_id=91002))


def test_late_registered_class_listener_cannot_commit_incarnation_tamper(context):
    ticket = begin(context)
    calls = []

    def tamper(session):
        if session.get_bind() is context.runtime:
            assert roundtrip_binding(session, context) == ticket.account_incarnation + 2
            calls.append(1)

    # Class listeners run before instance listeners even when registered later.
    event.listen(Session, "before_commit", tamper)
    try:
        with pytest.raises(service.ReviewShadowError, match="AUTHORITY_CHANGED"):
            publish(context, ticket)
    finally:
        event.remove(Session, "before_commit", tamper)
    assert calls == [1] and run_status(context, ticket) == "running"
    assert_no_published_facts(context)


def test_intervening_listener_between_incarnation_and_shared_guard_is_rejected(
    context, monkeypatch
):
    ticket = begin(context)
    original = service.acquire_publication_guard
    calls = []

    def tamper(session):
        calls.append(1)
        roundtrip_binding(session, context)

    def acquire(session, **kwargs):
        # Own listener installed already; shared final installed by original.
        event.listen(session, "before_commit", tamper)
        return original(session, **kwargs)

    monkeypatch.setattr(service, "acquire_publication_guard", acquire)
    with pytest.raises(service.ReviewShadowError):
        publish(context, ticket)
    assert calls == [] and run_status(context, ticket) == "running"


def test_pending_orm_from_class_listener_cannot_flush_after_incarnation_check(context):
    from app.platform.integrations.orm import MarketplaceAccountRow

    ticket = begin(context)
    external = "synthetic-queued-" + uuid4().hex
    calls = []

    def queue(session):
        if session.get_bind() is context.runtime:
            calls.append(1)
            session.add(
                MarketplaceAccountRow(
                    organization_id=91001,
                    marketplace="avito",
                    external_account_id=external,
                    status="connected",
                )
            )

    event.listen(Session, "before_commit", queue)
    try:
        with pytest.raises(service.ReviewShadowError):
            publish(context, ticket)
    finally:
        event.remove(Session, "before_commit", queue)
    assert calls == [1] and run_status(context, ticket) == "running"
    with context.owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM marketplace_accounts WHERE external_account_id=:external"
                ),
                {"external": external},
            ).scalar_one()
            == 0
        )
