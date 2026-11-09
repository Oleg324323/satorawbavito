"""Synthetic root/SQL doubles prove registrar orchestration, not real-role ACLs."""

from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.security.credential_maintenance_contract import MaintenanceAuthorization
from app.security.marketplace_credentials import DecryptedCredential


def metadata(provider="wb"):
    now = datetime(2026, 9, 10, tzinfo=UTC)
    return MaintenanceAuthorization(uuid4(), uuid4(), 1, 2, provider,
        "wb_api" if provider == "wb" else "avito_oauth_client", 3, "synthetic-user", "4",
        "lk_user_wb_tokens:3" if provider == "wb" else None, 6002, "synthetic_runner",
        uuid4(), uuid4(), uuid4(), now, True, True, now, now + timedelta(hours=1), None, None)


def module():
    assert util.find_spec("app.security.credential_maintenance_registrar"), "registrar missing"
    return import_module("app.security.credential_maintenance_registrar")


class RootDouble(Session):
    def __init__(self, auth):
        super().__init__(bind=create_engine("postgresql+psycopg://"), autoflush=False)
        self.auth, self.root, self.calls, self.closed = auth, None, [], False
        self.role_valid, self.commit_failure, self.compare_failure = True, False, False
        self.role_changes, self.register_calls, self.cleanup_failure = False, 0, False
        self.scope, self.require_scope = None, False
        self.source = {"token": "synthetic é", "client_id": None, "client_secret": None}
        self.physical = SimpleNamespace(is_active=True)
        self.link = SimpleNamespace(get_transaction=lambda: self.physical,
            in_nested_transaction=lambda: False, get_isolation_level=lambda: "READ COMMITTED",
            connection=SimpleNamespace(dbapi_connection=SimpleNamespace(autocommit=False)))

    def in_transaction(self):
        return self.root is not None

    def get_transaction(self):
        return self.root

    def begin(self):
        self.calls.append(("begin", {}))
        self.root = SimpleNamespace(is_active=True)

    def connection(self):
        return self.link

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "set_config" in sql:
            self.scope = (params["organization_id"], params["marketplace_account_id"])
        if "lock_registration" in sql:
            if self.require_scope and self.scope != (str(self.auth.organization_id), str(self.auth.marketplace_account_id)):
                raise RuntimeError("synthetic RLS scope absent")
            row = dict(self.source)
            if self.compare_failure and sum("lock_registration" in call[0] for call in self.calls) > 1:
                row["token"] = "changed"
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))
        if "register_authorization" in sql:
            self.register_calls += 1
            if self.role_changes:
                self.role_valid = False
            return SimpleNamespace(scalar_one=lambda: self.auth.authorization_id)
        return SimpleNamespace(scalar_one=lambda: self.role_valid)

    def scalar(self, statement, params=None):
        return self.execute(statement, params).scalar_one()

    def commit(self):
        self.calls.append(("commit-start", {}))
        for callback in self.dispatch.before_commit:
            callback(self)
        if self.commit_failure:
            raise RuntimeError("SYNTHETIC_PRIVATE_COMMIT")
        self.calls.append(("committed", {}))
        self.root.is_active = False
        self.physical.is_active = False

    def rollback(self):
        self.calls.append(("rollback", {}))

    def close(self):
        self.closed = True
        self.calls.append(("close", {}))
        if self.cleanup_failure:
            raise RuntimeError("SYNTHETIC_PRIVATE_CLEANUP")


def registrar(auth, session, verifier=None):
    api = module()
    return api.CredentialMaintenanceRegistrar(session_factory=lambda: session,
        verifier=verifier or (lambda _: DecryptedCredential({"token": "synthetic é"})),
        registrar_role_oid=6001, registrar_role_name="synthetic_registrar")


def test_registration_compares_locked_source_and_returns_after_commit_without_plaintext_sql():
    auth = metadata()
    session = RootDouble(auth)
    verified = []
    def verify(received):
        assert not session.in_transaction()
        verified.append(received)
        return DecryptedCredential({"token": "synthetic é"})
    service = registrar(auth, session, verify)
    assert service.register(auth, previous_authorization_id=None) == auth.authorization_id
    names = [item[0] for item in session.calls]
    assert len(verified) == 1 and session.closed
    assert names.index("commit-start") < names.index("committed") < names.index("close")
    assert sum("lock_registration" in name for name in names) == 2
    assert sum("register_authorization" in name for name in names) == 2
    assert all("synthetic é" not in str(params) and "token" not in params for _, params in session.calls)
    assert "synthetic" not in repr(service)


def test_avito_exact_two_fields_and_explicit_previous_uuid():
    auth = metadata("avito")
    session = RootDouble(auth)
    session.source = {"token": None, "client_id": "é", "client_secret": "synthetic-only"}
    old = uuid4()
    service = registrar(auth, session, lambda _: DecryptedCredential({"clientId": "é", "clientSecret": "synthetic-only"}))
    assert service.register(auth, previous_authorization_id=old) == auth.authorization_id
    writes = [params for sql, params in session.calls if "register_authorization" in sql]
    assert all(params["previous_authorization_id"] == old for params in writes)
    assert all("synthetic-only" not in str(params) for _, params in session.calls)


@pytest.mark.parametrize("payload", [{"token": "different"}, {"token": "synthetic é"},
    {"token": b"synthetic"}, {"token": "synthetic é", "extra": "private"}, {"token": "bad\ud800"}])
def test_unverified_or_changed_source_never_registers(payload):
    auth = metadata()
    session = RootDouble(auth)
    service = registrar(auth, session, lambda _: DecryptedCredential(payload))
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        service.register(auth, previous_authorization_id=None)
    assert caught.value.__context__ is None
    assert not any("register_authorization" in sql for sql, _ in session.calls)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("failure", ["role", "dirty-root", "autocommit", "isolation", "final-source", "commit"])
def test_root_identity_and_commit_fail_closed(failure):
    auth = metadata()
    session = RootDouble(auth)
    if failure == "role":
        session.role_valid = False
    elif failure == "dirty-root":
        session.begin()
    elif failure == "autocommit":
        session.link.connection.dbapi_connection.autocommit = True
    elif failure == "isolation":
        session.link.get_isolation_level = lambda: "REPEATABLE READ"
    elif failure == "final-source":
        session.compare_failure = True
    else:
        session.commit_failure = True
    service = registrar(auth, session)
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        service.register(auth, previous_authorization_id=None)
    assert caught.value.__context__ is None
    assert "PRIVATE" not in repr(caught.value)
    if failure == "commit":
        assert caught.value.code == "maintenance_commit_unknown"
    if failure == "dirty-root":
        assert not session.closed and not any(sql == "rollback" for sql, _ in session.calls)
    else:
        assert session.closed


def test_missing_verifier_never_opens_session():
    api = module()
    with pytest.raises(api.MaintenanceRegistrarError):
        api.CredentialMaintenanceRegistrar(session_factory=lambda: pytest.fail("opened"), verifier=None,
            registrar_role_oid=6001, registrar_role_name="synthetic_registrar")


def test_verifier_error_does_not_retain_sensitive_context_or_open_root():
    auth = metadata()
    session = RootDouble(auth)
    def failed(_):
        raise RuntimeError("SYNTHETIC_PRIVATE_VERIFIER")
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        registrar(auth, session, failed).register(auth, previous_authorization_id=None)
    assert caught.value.__context__ is None and session.calls == []


def test_scoped_helper_receives_verified_owner_gucs_before_source_lock():
    auth = metadata()
    session = RootDouble(auth)
    session.require_scope = True
    assert registrar(auth, session).register(auth, previous_authorization_id=None) == auth.authorization_id


def test_corrupted_metadata_is_invalid_before_verifier_or_session():
    auth = metadata()
    object.__setattr__(auth, "organization_id", 0)
    session = RootDouble(auth)
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        registrar(auth, session, lambda _: pytest.fail("verifier called")).register(auth, previous_authorization_id=None)
    assert caught.value.code == "maintenance_contract_invalid"
    assert caught.value.__context__ is None and session.calls == []


def test_identity_change_is_rejected_by_final_check():
    auth = metadata()
    session = RootDouble(auth)
    session.role_changes = True
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        registrar(auth, session).register(auth, previous_authorization_id=None)
    assert caught.value.code == "maintenance_authorization_denied"
    assert session.register_calls == 1
    assert not any(sql == "committed" for sql, _ in session.calls)


def test_cleanup_failure_after_commit_reports_unknown_never_success():
    auth = metadata()
    session = RootDouble(auth)
    session.cleanup_failure = True
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        registrar(auth, session).register(auth, previous_authorization_id=None)
    assert caught.value.code == "maintenance_commit_unknown"
    assert caught.value.__context__ is None
    assert any(sql == "committed" for sql, _ in session.calls)


@pytest.mark.parametrize("previous", ["bad", 1, True])
def test_previous_authorization_requires_explicit_typed_uuid(previous):
    auth = metadata()
    session = RootDouble(auth)
    with pytest.raises(module().MaintenanceRegistrarError) as caught:
        registrar(auth, session).register(auth, previous_authorization_id=previous)
    assert caught.value.code == "maintenance_contract_invalid"
    assert session.calls == []
