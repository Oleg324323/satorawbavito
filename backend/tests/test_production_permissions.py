"""Pure contract tests for fixed Production permissions and pre-I/O validation."""

from importlib import import_module

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

EXPECTED_PERMISSION_CONSTANTS = {
    "PRODUCTION_PERMISSION_KEYS": frozenset(
        {"production:read", "production:create", "production:assign"}
    ),
    "PRODUCTION_READ_PERMISSIONS": frozenset({"production:read"}),
    "PRODUCTION_CREATE_PERMISSIONS": frozenset(
        {"production:read", "production:create"}
    ),
    "PRODUCTION_ASSIGN_PERMISSIONS": frozenset(
        {"production:read", "production:assign"}
    ),
}

EXPECTED_LEGACY_PROFILE_ALIASES = {
    "admin": "admin",
    "owner": "admin",
    "sales": "price_sender",
    "production": "settings_editor",
    "viewer": "viewer",
    "settings_editor": "settings_editor",
    "price_sender": "price_sender",
    "finance_viewer": "finance_viewer",
    "custom": "custom",
}

EXPECTED_PROFILE_PERMISSIONS = {
    "viewer": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "catalog:read",
        }
    ),
    "settings_editor": frozenset(
        {
            "settings:read",
            "settings:write",
            "settings:submit",
            "audit:read",
            "sync:read",
            "reviews:read",
            "reviews:write",
            "reviews:approve",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "preferences:write",
            "catalog:read",
            "catalog:write",
        }
    ),
    "price_sender": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "price:send",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "preferences:write",
            "catalog:read",
        }
    ),
    "finance_viewer": frozenset(
        {
            "settings:read",
            "audit:read",
            "sync:read",
            "finance:read",
            "reviews:read",
            "cabinet:read",
            "team:read",
            "sessions:read",
            "integrations:read",
            "preferences:read",
            "catalog:read",
            "costs:read",
        }
    ),
    "admin": frozenset(
        {
            "settings:read",
            "settings:write",
            "settings:submit",
            "settings:activate",
            "audit:read",
            "audit:write",
            "sync:read",
            "sync:write",
            "sync:run",
            "price:send",
            "finance:read",
            "reviews:read",
            "reviews:write",
            "reviews:approve",
            "reviews:send",
            "cabinet:read",
            "team:read",
            "team:write",
            "sessions:read",
            "sessions:write",
            "integrations:read",
            "integrations:write",
            "preferences:read",
            "preferences:write",
            "catalog:read",
            "catalog:write",
            "costs:read",
            "costs:write",
        }
    ),
    "custom": frozenset(),
}


def permissions_api():
    return import_module("app.cabinet.permissions")


def guard_api():
    return import_module("app.platform.integrations.publication_guard")


def test_exports_exact_immutable_production_permission_sets():
    permissions = permissions_api()
    actual = {
        name: getattr(permissions, name)
        for name in EXPECTED_PERMISSION_CONSTANTS
    }

    assert actual == EXPECTED_PERMISSION_CONSTANTS
    assert all(type(value) is frozenset for value in actual.values())
    assert actual["PRODUCTION_PERMISSION_KEYS"] == (
        actual["PRODUCTION_READ_PERMISSIONS"]
        | actual["PRODUCTION_CREATE_PERMISSIONS"]
        | actual["PRODUCTION_ASSIGN_PERMISSIONS"]
    )
    for value in actual.values():
        with pytest.raises(AttributeError):
            value.add("production:unknown")


def test_existing_profile_and_legacy_alias_maps_are_unchanged():
    permissions = permissions_api()

    assert permissions.LEGACY_PROFILE_ALIASES == EXPECTED_LEGACY_PROFILE_ALIASES
    assert permissions.PROFILE_PERMISSIONS == EXPECTED_PROFILE_PERMISSIONS
    assert all(
        not (grants & EXPECTED_PERMISSION_CONSTANTS["PRODUCTION_PERMISSION_KEYS"])
        for grants in permissions.PROFILE_PERMISSIONS.values()
    )


class UnexpectedDatabaseAccess(RuntimeError):
    pass


@pytest.fixture
def guarded_session(monkeypatch):
    engine = create_engine("postgresql+psycopg://", hide_parameters=True)
    calls = []

    def reject(method):
        def rejected(*args, **kwargs):
            calls.append(method)
            raise UnexpectedDatabaseAccess(method)

        return rejected

    try:
        with Session(engine) as session, session.begin():
            monkeypatch.setattr(session, "connection", reject("connection"))
            monkeypatch.setattr(session, "execute", reject("execute"))
            monkeypatch.setattr(session, "scalar", reject("scalar"))
            yield session, calls
    finally:
        engine.dispose()


def acquire(session, required_permissions):
    guard = guard_api()
    return guard.acquire_publication_guard(
        session,
        principal=guard.UserSessionPrincipal(1, "user", 1, "session"),
        required_permissions=required_permissions,
        accounts=(guard.ExpectedAccountBinding(1, "wb", "seller", None),),
        authorities=(),
    )


@pytest.mark.parametrize(
    "required_permissions",
    [
        frozenset({"production:create"}),
        frozenset({"production:assign"}),
        frozenset({"production:create", "production:assign"}),
        frozenset({"production:create", "catalog:read"}),
        frozenset({"production:assign", "reviews:read"}),
        frozenset(
            {"production:create", "production:assign", "unknown-secret-canary"}
        ),
    ],
)
def test_production_write_without_read_is_rejected_before_database_access(
    guarded_session, required_permissions
):
    session, calls = guarded_session

    with pytest.raises(
        guard_api().PublicationGuardError, match="^publication_context_invalid$"
    ) as caught:
        acquire(session, required_permissions)

    assert calls == []
    assert "unknown-secret-canary" not in repr(caught.value)


@pytest.mark.parametrize(
    "required_permissions",
    [
        frozenset({"production:read"}),
        frozenset({"production:read", "production:create"}),
        frozenset({"production:read", "production:assign"}),
        frozenset(
            {
                "production:read",
                "production:create",
                "production:assign",
                "unrelated:permission",
            }
        ),
    ],
)
def test_complete_production_requirements_continue_to_existing_database_admission(
    guarded_session, required_permissions
):
    session, calls = guarded_session

    with pytest.raises(UnexpectedDatabaseAccess, match="^connection$"):
        acquire(session, required_permissions)

    assert calls == ["connection"]
