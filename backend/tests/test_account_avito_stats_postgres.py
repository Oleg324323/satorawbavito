"""Fresh canonical authority across synthetic provider I/O; owned PG only."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.avito.account_stats import AccountAvitoStatsService, AccountStatsError
from app.avito.stats import AvitoStatsAccount, AvitoStatsFetchResult, AvitoStatsItem
from app.cabinet.orm import LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
    _put_marketplace_credential_in_session,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from tests import test_orders_schema_candidate as candidate
from tests.test_marketplace_credential_crypto import _keyring
from tests.test_orders_exact_text_migration import migrated_database

cluster = candidate.cluster


@pytest.fixture(scope="module")
def stats_db(cluster):
    with migrated_database(cluster, "head") as engines:
        yield engines


def put_access(context):
    expiry = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    with Session(context.owner) as session, session.begin():
        _put_marketplace_credential_in_session(
            session,
            MarketplaceAccountCredentialOwner(91001, context.account, "avito"),
            "avito_oauth_access",
            {
                "accessToken": "synthetic-stats-only",
                "expiresAt": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            keyring=_keyring(),
            now=datetime.now(UTC),
        )


@pytest.fixture
def context(stats_db):
    owner, runtime = stats_db
    user, login = uuid4().hex, uuid4().hex
    external = str(uuid4().int)
    with Session(owner) as session, session.begin():
        account = MarketplaceAccountRow(
            organization_id=91001,
            marketplace="avito",
            external_account_id=external,
            status="connected",
        )
        session.add(account)
        session.add(
            LkUserRow(
                user_id=user,
                organization_id=91001,
                email=user + "@example.invalid",
                password_hash="synthetic-only",
                full_name="Synthetic",
                permission_profile="custom",
            )
        )
        session.flush()
        internal = account.marketplace_account_id
        session.add(
            IamMembershipRow(
                organization_id=91001,
                user_id=user,
                role="custom",
                permissions=["cabinet:read"],
                scope_mode="selected",
                allowed_account_ids=[internal],
            )
        )
        now = datetime.now(UTC)
        session.add(
            LkSessionRow(
                session_id=login,
                user_id=user,
                issued_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    value = SimpleNamespace(
        owner=owner,
        runtime=runtime,
        account=internal,
        external=external,
        actor=ActorContext(
            "ignored", user, 91001, "custom", frozenset(), session_id=login
        ),
    )
    put_access(value)
    return value


def change(context, statement):
    with context.owner.begin() as connection:
        connection.execute(
            text(statement),
            {
                "account": context.account,
                "user": context.actor.user_id,
                "login": context.actor.session_id,
            },
        )


def service(context, *, during=None, enabled=True, keyring_loader=_keyring):
    calls = []

    class Client:
        def fetch_stats(self, request):
            with context.owner.begin() as connection:
                connection.execute(
                    text(
                        "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:account FOR UPDATE NOWAIT"
                    ),
                    {"account": context.account},
                )
            assert (
                request.accountIds == [context.external]
                and request.grouping == "totals"
            )
            calls.append(request)
            if during:
                during()
            return AvitoStatsFetchResult(
                status="synced",
                accounts=[
                    AvitoStatsAccount(
                        accountId=context.external, accountName=context.external
                    )
                ],
                items=[
                    AvitoStatsItem(
                        itemId=f"account:{context.external}:totals",
                        accountId=context.external,
                        accountName=context.external,
                        title="Synthetic",
                        views=0,
                    )
                ],
            )

    def factory(resolved):
        assert resolved.binding.owner.marketplace_account_id == context.account
        assert resolved.binding.external_account_id == context.external
        return Client()

    return AccountAvitoStatsService(
        engine=context.runtime,
        keyring_loader=keyring_loader,
        client_factory=factory,
        enabled_for=lambda org, account: enabled,
    ), calls


def fetch(service, context, **changes):
    return service.fetch(
        changes.pop("actor", context.actor),
        marketplace_account_id=changes.pop("account", context.account),
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 1),
        **changes,
    )


def test_two_closed_roots_exact_account_and_safe_projection(context):
    instance, calls = service(context)
    value = fetch(instance, context)
    assert len(calls) == 1
    assert (
        value["marketplaceAccountId"] == context.account
        and value["provider"] == "avito"
    )
    assert value["rows"][0]["metrics"]["views"] == "0"


def test_default_off_no_provider(context):
    instance = AccountAvitoStatsService(
        engine=context.runtime,
        keyring_loader=lambda: pytest.fail("key loaded"),
        client_factory=lambda credential: pytest.fail("client created"),
    )
    with pytest.raises(AccountStatsError, match="DISABLED"):
        fetch(instance, context)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE lk_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=:login",
        "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
        "DELETE FROM marketplace_account_credentials WHERE marketplace_account_id=:account",
        "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
        "UPDATE iam_memberships SET allowed_account_ids='[true]'::json WHERE user_id=:user",
        "UPDATE marketplace_account_credentials SET ciphertext=set_byte(ciphertext,0,get_byte(ciphertext,0)#1) WHERE marketplace_account_id=:account",
    ],
)
def test_initial_authority_failure_never_calls_provider(context, statement):
    change(context, statement)
    instance, calls = service(context)
    with pytest.raises(AccountStatsError) as error:
        fetch(instance, context)
    assert calls == [] and error.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    ["expiry", "binding", "incarnation", "replacement", "scope", "permission"],
)
def test_during_fetch_authority_change_discards_result(context, mutation):
    statements = {
        "expiry": "UPDATE marketplace_account_credentials SET expires_at=clock_timestamp()-interval '1 second' WHERE marketplace_account_id=:account",
        "binding": "UPDATE marketplace_accounts SET external_account_id=external_account_id||'1' WHERE marketplace_account_id=:account",
        "incarnation": "UPDATE marketplace_accounts SET ingestion_binding_version=ingestion_binding_version+1 WHERE marketplace_account_id=:account",
        "scope": "UPDATE iam_memberships SET allowed_account_ids='[]'::json WHERE user_id=:user",
        "permission": "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
    }

    def mutate():
        if mutation == "replacement":
            put_access(context)
        else:
            change(context, statements[mutation])

    instance, calls = service(context, during=mutate)
    with pytest.raises(AccountStatsError) as error:
        fetch(instance, context)
    assert len(calls) == 1 and error.value.__context__ is None


@pytest.mark.parametrize("closing_root", [1, 2])
def test_physical_commit_failure_never_returns_data(context, closing_root):
    count = []

    def fail_commit(connection):
        count.append(1)
        if len(count) == closing_root:
            raise RuntimeError("synthetic-private-db-detail")

    event.listen(context.runtime, "commit", fail_commit)
    try:
        instance, calls = service(context)
        with pytest.raises(AccountStatsError) as error:
            fetch(instance, context)
        assert len(calls) == closing_root - 1 and error.value.__context__ is None
    finally:
        event.remove(context.runtime, "commit", fail_commit)


@pytest.mark.parametrize("case", ["org", "account"])
def test_foreign_scope_never_calls_provider(context, case):
    instance, calls = service(context)
    with pytest.raises(AccountStatsError):
        fetch(
            instance,
            context,
            **(
                {"actor": replace(context.actor, organization_id=91002)}
                if case == "org"
                else {"account": 91101}
            ),
        )
    assert calls == []


def test_key_loader_failure_no_plaintext_fallback(context):
    def unavailable():
        raise RuntimeError("synthetic-key-loader-private")

    instance, calls = service(context, keyring_loader=unavailable)
    with pytest.raises(AccountStatsError) as error:
        fetch(instance, context)
    assert calls == [] and error.value.__context__ is None
