"""Actual guard/repository compatibility; only the owned disposable database."""
# Keep exception assertion outside root context to expose complete rollback.
# ruff: noqa: SIM117

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    ExpectedCredential,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.ingestion_contract import ReviewRepositoryError
from tests import test_review_facts_repository as existing

cluster = existing.cluster
db = existing.db


@pytest.fixture(scope="module")
def authority(db):
    credential_id = uuid4()
    # Metadata-only synthetic authority: never decrypted or passed to a provider.
    with db[0].begin() as c:
        c.execute(
            text("""INSERT INTO marketplace_account_credentials
            (credential_id,organization_id,marketplace_account_id,provider,
             credential_kind,payload_schema_version,algorithm,key_version,
             aad_version,nonce,ciphertext,generation)
            VALUES (:id,91001,91101,'avito','avito_oauth_client',1,'AES-256-GCM',1,
                    1,:nonce,:ciphertext,1)"""),
            {"id": credential_id, "nonce": b"n" * 12, "ciphertext": b"c" * 32},
        )
    return ExpectedCredential(91101, credential_id, "avito_oauth_client", 1, 1, None)


@pytest.fixture
def principal(db):
    user_id, login_id = uuid4().hex, uuid4().hex
    now = datetime.now(UTC)
    with db[0].begin() as c:
        c.execute(
            text("""INSERT INTO lk_users
            (user_id,organization_id,email,password_hash,full_name,permission_profile,is_active)
            VALUES (:id,91001,:email,'synthetic','Synthetic','custom',true)"""),
            {"id": user_id, "email": user_id + "@example.invalid"},
        )
        member = c.execute(
            text("""INSERT INTO iam_memberships
            (organization_id,user_id,role,permissions,scope_mode,allowed_account_ids,is_active)
            VALUES (91001,:id,'custom','["sync:run","cabinet:read"]','selected','[91101]',true)
            RETURNING membership_id"""),
            {"id": user_id},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO lk_sessions
            (session_id,user_id,issued_at,last_seen_at,expires_at)
            VALUES (:sid,:uid,:now,:now,:expiry)"""),
            {
                "sid": login_id,
                "uid": user_id,
                "now": now,
                "expiry": now + timedelta(hours=1),
            },
        )
    return UserSessionPrincipal(91001, user_id, member, login_id)


def guarded(session, principal, authority, *, read=False):
    guard = acquire_publication_guard(
        session,
        principal=principal,
        required_permissions=frozenset({"cabinet:read" if read else "sync:run"}),
        accounts=(ExpectedAccountBinding(91101, "avito", "synthetic-a", None),),
        authorities=() if read else (authority,),
    )
    repo = ReviewFactsRepository(
        session.connection(),
        ReviewOwner(91001, 91101, "avito", "synthetic-a"),
        command_savepoints=False,
    )
    return guard, repo


@pytest.mark.parametrize("suffix", ["", "\0🚀"])
def test_guarded_publication_and_new_session_read_use_no_savepoints(
    db, principal, authority, suffix
):
    source, key = uuid4().hex + suffix, uuid4().hex + suffix
    savepoints = []
    with Session(db[1]) as session, session.begin():
        c = session.connection()
        event.listen(c, "savepoint", lambda connection, name: savepoints.append(name))
        guard, repo = guarded(session, principal, authority)
        guard.revalidate_before_write()
        run = repo.reserve_run(
            source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
        )
        existing.ingest(repo, run, [existing.fact(key, source)], {key: 0})
        assert repo.get_fact(key).text == "A"
    with Session(db[1]) as session, session.begin():
        _, repo = guarded(session, principal, authority, read=True)
        assert repo.get_fact(key).version == 1
    assert savepoints == []


@pytest.mark.parametrize("suffix", ["", "\0🚀"])
def test_root_failure_rolls_back_prior_run_and_facts(db, principal, authority, suffix):
    source, key = uuid4().hex + suffix, uuid4().hex + suffix
    with pytest.raises(ReviewRepositoryError, match="REVIEW_REPLAY_CONFLICT"):
        with Session(db[1]) as session, session.begin():
            guard, repo = guarded(session, principal, authority)
            guard.revalidate_before_write()
            run = repo.reserve_run(
                source_run_id=source, request_checksum="a" * 64, started_at=existing.NOW
            )
            existing.ingest(repo, run, [existing.fact(key, source)], {key: 0})
            repo.reserve_run(
                source_run_id=source, request_checksum="b" * 64, started_at=existing.NOW
            )
    with db[0].connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM review_sync_runs_v2 WHERE "
                    "COALESCE(source_run_id_utf8,convert_to(source_run_id,'UTF8'))=:id"
                ),
                {"id": source.encode("utf-8")},
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM review_facts WHERE "
                    "COALESCE(external_review_id_utf8,convert_to(external_review_id,'UTF8'))=:id"
                ),
                {"id": key.encode("utf-8")},
            ).scalar_one()
            == 0
        )


def test_revoked_membership_denies_actual_repository_publication(
    db, principal, authority
):
    with db[0].begin() as c:
        c.execute(
            text("UPDATE iam_memberships SET is_active=false WHERE membership_id=:id"),
            {"id": principal.membership_id},
        )
    with pytest.raises(PublicationGuardError, match="publication_access_denied"):
        with Session(db[1]) as session, session.begin():
            guarded(session, principal, authority)


@pytest.mark.parametrize("mode", [None, 0, 1, "false", []])
def test_transaction_mode_never_coerces_untrusted_values(mode):
    with pytest.raises(ReviewRepositoryError, match="REVIEW_STORAGE_INVALID"):
        ReviewFactsRepository(
            object(),
            ReviewOwner(91001, 91101, "avito", "synthetic-a"),
            command_savepoints=mode,
        )


def test_root_mode_rejects_an_existing_connection_savepoint(db, principal, authority):
    with Session(db[1]) as session, session.begin():
        _, repo = guarded(session, principal, authority)
        with session.connection().begin_nested():
            with pytest.raises(
                ReviewRepositoryError, match="REVIEW_TRANSACTION_REQUIRED"
            ):
                repo.get_fact("synthetic-missing")
        # This is an intentionally unsupported caller. Do not attempt publication.
        session.rollback()
