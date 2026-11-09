"""Pure contract rejection; transaction behavior is tested on real PostgreSQL."""

import traceback
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module, util
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


def api():
    name = "app.platform.integrations.publication_guard"
    assert util.find_spec(name) is not None, "missing caller-transaction publication guard API"
    return import_module(name)


def test_missing_api():
    guard = api()
    for name in ("UserSessionPrincipal", "ExpectedAccountBinding", "ExpectedCredential",
                 "ExpectedIngestionToken", "PublicationGuardError", "acquire_publication_guard"):
        assert callable(getattr(guard, name))


@pytest.mark.parametrize("field,value", [
    ("organization_id", True), ("organization_id", "1"), ("organization_id", 0),
    ("organization_id", 2**31), ("membership_id", -1), ("membership_id", 1.0),
    ("user_id", 1), ("user_id", ""), ("user_id", " " ), ("user_id", "x" * 65),
    ("session_id", None), ("session_id", "x\x00y"), ("session_id", "\ud800"),
])
def test_principal_strict_metadata(field, value):
    g = api()
    with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
        replace(g.UserSessionPrincipal(1, "user", 1, "session"), **{field: value})


@pytest.mark.parametrize("field,value", [
    ("marketplace_account_id", True), ("marketplace_account_id", "1"),
    ("provider", "unknown-secret-canary"), ("external_account_id", ""),
    ("external_account_id", "x" * 129), ("credential_ref", "x" * 256),
    ("credential_ref", 1), ("credential_ref", ""),
])
def test_account_strict_metadata(field, value):
    g = api()
    with pytest.raises(g.PublicationGuardError, match="^publication_binding_changed$"):
        replace(g.ExpectedAccountBinding(1, "wb", "seller", None), **{field: value})


@pytest.mark.parametrize("field,value", [
    ("marketplace_account_id", 0), ("credential_id", "not-uuid-canary"),
    ("credential_id", str(uuid4())), ("kind", "unknown-canary"),
    ("generation", True), ("generation", 0), ("generation", 2**63),
    ("payload_schema_version", True), ("payload_schema_version", 2),
    ("expires_at", datetime.now(UTC)),
])
def test_credential_strict_metadata(field, value):
    g = api()
    with pytest.raises(g.PublicationGuardError, match="^publication_authority_invalid$"):
        replace(g.ExpectedCredential(1, uuid4(), "wb_api", 1, 1, None), **{field: value})


def test_expiring_authority_requires_aware_exact_expiry():
    g = api()
    for expiry in (None, datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None), "2026-01-01"):
        with pytest.raises(g.PublicationGuardError):
            g.ExpectedCredential(1, uuid4(), "avito_oauth_access", 1, 1, expiry)
        with pytest.raises(g.PublicationGuardError):
            g.ExpectedIngestionToken(1, uuid4(), "avito.browser_snapshot.write", expiry)
    with pytest.raises(g.PublicationGuardError):
        g.ExpectedIngestionToken(1, uuid4(), "unknown-secret-canary", datetime.now(UTC))


@pytest.mark.parametrize("value", ["unknown-secret-canary", [], {}, None])
def test_errors_never_reflect_unknown_input(value):
    g = api()
    error = g.PublicationGuardError(value)
    assert str(error) == "publication_persistence_failed"
    assert "canary" not in repr(error)


def test_no_root_transaction_is_rejected_without_autobegin():
    g = api()
    with Session() as session:
        with pytest.raises(g.PublicationGuardError, match="^publication_context_invalid$"):
            g.acquire_publication_guard(
                session, principal=g.UserSessionPrincipal(1, "user", 1, "session"),
                required_permissions=frozenset({"cabinet:read"}),
                accounts=(g.ExpectedAccountBinding(1, "wb", "seller", None),), authorities=())
        assert not session.in_transaction()


@pytest.fixture
def physical_admission_session(monkeypatch):
    # Use a real Engine-bound Session; only connection inspection is replaced.
    # The default double models the complete ordinary physical admission state.
    engine = create_engine("postgresql+psycopg://", hide_parameters=True)
    physical = SimpleNamespace(
        get_transaction=lambda: SimpleNamespace(is_active=True),
        in_nested_transaction=lambda: False,
        connection=SimpleNamespace(dbapi_connection=SimpleNamespace(autocommit=False)),
        get_isolation_level=lambda: "READ COMMITTED",
    )

    def unexpected_sql(*args, **kwargs):
        pytest.fail("invalid physical admission issued application SQL")

    try:
        with Session(engine) as session, session.begin():
            monkeypatch.setattr(session, "connection", lambda: physical)
            monkeypatch.setattr(session, "scalar", unexpected_sql)
            monkeypatch.setattr(session, "execute", unexpected_sql)
            yield session, physical
    finally:
        engine.dispose()


def acquire_for_admission(session):
    g = api()
    return g.acquire_publication_guard(
        session, principal=g.UserSessionPrincipal(1, "user", 1, "session"),
        required_permissions=frozenset({"cabinet:read"}),
        accounts=(g.ExpectedAccountBinding(1, "wb", "seller", None),), authorities=())


@pytest.mark.parametrize("state", ["missing_root", "inactive_root", "nested", "missing_driver_flag",
                                  "unknown_driver_flag", "autocommit", "integer_zero", "integer_one"])
def test_physical_admission_fails_closed_for_unverifiable_state(physical_admission_session, state):
    session, physical = physical_admission_session
    if state == "missing_root":
        physical.get_transaction = lambda: None
    elif state == "inactive_root":
        physical.get_transaction = lambda: SimpleNamespace(is_active=False)
    elif state == "nested":
        physical.in_nested_transaction = lambda: True
    elif state == "missing_driver_flag":
        del physical.connection.dbapi_connection.autocommit
    else:
        physical.connection.dbapi_connection.autocommit = {
            "unknown_driver_flag": None, "autocommit": True, "integer_zero": 0, "integer_one": 1,
        }[state]
    with pytest.raises(api().PublicationGuardError, match="^publication_context_invalid$"):
        acquire_for_admission(session)


@pytest.mark.parametrize("inspection", ["get_bind", "connection", "get_transaction", "in_nested_transaction", "get_isolation_level"])
def test_physical_admission_sanitizes_inspection_failure(physical_admission_session, monkeypatch, inspection):
    session, physical = physical_admission_session

    def fail():
        raise SQLAlchemyError("synthetic-physical-secret-canary")

    monkeypatch.setattr(session if inspection in {"get_bind", "connection"} else physical, inspection, fail)
    with pytest.raises(api().PublicationGuardError, match="^publication_persistence_failed$") as caught:
        acquire_for_admission(session)
    assert "synthetic-physical-secret-canary" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
