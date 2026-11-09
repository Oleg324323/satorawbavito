"""HTTP boundary fixtures; existing service owns PostgreSQL/CAS/permission proof."""

from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.control_plane.auth import ActorContext
from app.modules.wb_repricing_override_service import (
    OverrideConflictError,
    OverrideRevision,
)
from app.modules.wb_repricing_overrides import OverrideChange, OverrideValues
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
)
from app.routers.wb_repricing_overrides import create_wb_repricing_overrides_router

NOW = datetime(2026, 9, 10, tzinfo=UTC)
COMMAND = "11111111-1111-4111-8111-111111111111"
PATH = "/api/v2/wb/repricing/accounts/2/skus/3/overrides"


def body():
    return {
        "commandId": COMMAND,
        "expectedVersion": "0",
        "values": {field.name: None for field in fields(OverrideValues)},
    }


def revision(change):
    return OverrideRevision(
        change,
        change.expected_version + 1,
        change.expected_version or None,
        NOW,
        change.canonical_bytes(max_bytes=16384),
        change.checksum(max_bytes=16384),
    )


class Service:
    def __init__(self):
        self.calls, self.current, self.pages = [], None, ()
        self.error = None

    def get_current(self, scope, principal, binding):
        self.calls.append(("get", scope, principal, binding))
        if self.error:
            raise self.error
        return self.current

    def replace(self, change, principal, binding, *, now):
        self.calls.append(("replace", change, principal, binding, now))
        if self.error:
            raise self.error
        return revision(change)

    def history(self, scope, principal, binding, *, limit, before_revision):
        self.calls.append(("history", scope, limit, before_revision))
        return self.pages


def client(service, *, enabled=True, fenced=True, resolver=None):
    actor = ActorContext(
        "ignored-actor-name",
        "user-1",
        1,
        "custom",
        frozenset({"settings:read", "settings:write"}),
        session_id="session-1",
    )

    def context(actor, account):
        assert account == 2
        return UserSessionPrincipal(
            1, "user-1", 42, "session-1"
        ), ExpectedAccountBinding(2, "wb", "seller", None)

    app = FastAPI()
    app.include_router(
        create_wb_repricing_overrides_router(
            service_factory=lambda: service,
            context_resolver=resolver or context,
            actor_resolver=lambda request: actor,
            enabled_for=(lambda org, account: enabled and (org, account) == (1, 2))
            if enabled is not None
            else None,
            writer_fenced_for=(lambda org, account: fenced and (org, account) == (1, 2))
            if fenced is not None
            else None,
            clock=lambda: NOW,
        )
    )
    return TestClient(app)


def test_default_denial_and_exact_account_scope_before_service():
    service = Service()
    assert client(service, enabled=None).get(PATH).status_code == 404
    assert (
        client(service).get(PATH.replace("accounts/2", "accounts/9")).status_code == 404
    )
    assert service.calls == []


def test_unfenced_write_denied_but_read_available():
    service = Service()
    response = client(service, fenced=None).put(PATH, json=body())
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "WB_OVERRIDES_WRITER_NOT_FENCED"
    )
    assert not service.calls
    assert client(service, fenced=None).get(PATH).json()["data"] == {
        "marketplaceAccountId": 2,
        "catalogSkuId": 3,
        "version": "0",
        "revision": None,
    }


def test_full_replace_preserves_exact_money_decimal_and_server_membership():
    service = Service()
    request = body()
    request["values"].update(
        p_min_kopecks="9007199254740993",
        min_margin_pct="12.34567890123456789",
        automation_enabled=False,
        basket_norm_manual=0,
    )
    response = client(service).put(PATH, json=request)
    assert response.status_code == 200
    change = service.calls[0][1]
    assert (
        change.organization_id,
        change.marketplace_account_id,
        change.catalog_sku_id,
        change.actor_membership_id,
    ) == (1, 2, 3, 42)
    assert change.expected_version == 0 and change.command_id == COMMAND
    assert change.values.p_min_kopecks == 9007199254740993
    assert change.values.min_margin_pct == Decimal("12.34567890123456789")
    data = response.json()["data"]
    assert data["version"] == "1" and data["revision"]["actorMembershipId"] == 42
    assert data["revision"]["values"]["p_min_kopecks"] == "9007199254740993"
    assert data["revision"]["values"]["min_margin_pct"] == "12.34567890123456789"
    assert "credential" not in response.text.lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("p_min_kopecks", 1.0),
        ("p_min_kopecks", 0),
        ("p_min_kopecks", "-1"),
        ("p_min_kopecks", "01"),
        ("min_margin_pct", "NaN"),
        ("price_step_minutes", True),
        ("automation_enabled", "false"),
        ("basket_norm_mode", "invented"),
    ],
)
def test_invalid_values_never_reach_service(field, value):
    service = Service()
    request = body()
    request["values"][field] = value
    response = client(service).put(PATH, json=request)
    assert response.status_code == 422 and service.calls == []
    assert response.json()["detail"] == {"code": "WB_OVERRIDES_INVALID_REQUEST"}


@pytest.mark.parametrize(
    "injection",
    ["actorMembershipId", "organizationId", "marketplaceAccountId", "scenario"],
)
def test_no_client_identity_or_legacy_mode_override(injection):
    service = Service()
    request = {**body(), injection: 999}
    assert client(service).put(PATH, json=request).status_code == 422
    assert service.calls == []


def test_omitted_fields_are_not_implicitly_cleared():
    service = Service()
    request = body()
    del request["values"]["rrp_kopecks"]
    assert client(service).put(PATH, json=request).status_code == 422
    assert not service.calls


def test_conflict_and_permission_error_are_explicit():
    service = Service()
    service.error = OverrideConflictError()
    assert (
        client(service).put(PATH, json=body()).json()["detail"]["code"]
        == "WB_OVERRIDES_CONFLICT"
    )
    service.error = PublicationGuardError("publication_access_denied")
    assert client(service).get(PATH).status_code == 403


def test_wrong_principal_from_resolver_rejected_before_service():
    service = Service()

    def wrong(actor, account):
        return UserSessionPrincipal(
            9, "user-1", 42, "session-1"
        ), ExpectedAccountBinding(2, "wb", "seller", None)

    assert client(service, resolver=wrong).get(PATH).status_code == 403
    assert not service.calls


def test_safe_database_error_body():
    service = Service()
    service.error = RuntimeError("synthetic secret database connection")
    response = client(service).get(PATH)
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "WB_OVERRIDES_UNAVAILABLE"}


def test_bounded_keyset_history_and_decimal_revision():
    service = Service()
    values = OverrideValues(**{field.name: None for field in fields(OverrideValues)})
    service.pages = tuple(
        revision(OverrideChange(1, 2, 3, 42, COMMAND, number, values))
        for number in (2, 1, 0)
    )
    response = client(service).get(PATH + "/history?limit=2&beforeRevision=4")
    assert response.status_code == 200
    assert service.calls[0][2:] == (3, 4)
    assert [row["revision"] for row in response.json()["data"]["items"]] == ["3", "2"]
    assert response.json()["data"]["nextBeforeRevision"] == "2"
    assert client(service).get(PATH + "/history?limit=101").status_code == 422


def test_body_limit_and_duplicate_keys():
    service = Service()
    response = client(service).put(
        PATH, content=b" " * 16385, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    response = client(service).put(
        PATH,
        content=b'{"commandId":"a","commandId":"b"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and not service.calls


def test_service_cannot_return_another_account_revision():
    service = Service()
    values = OverrideValues(**{field.name: None for field in fields(OverrideValues)})
    service.current = revision(OverrideChange(1, 9, 3, 42, COMMAND, 0, values))
    response = client(service).get(PATH)
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "WB_OVERRIDES_UNAVAILABLE"}


@pytest.mark.parametrize(
    "path",
    [
        PATH.replace("accounts/2", "accounts/0"),
        PATH.replace("skus/3", "skus/-1"),
        PATH.replace("skus/3", "skus/2147483648"),
    ],
)
def test_invalid_internal_path_ids_never_call_service(path):
    service = Service()
    assert client(service).get(path).status_code == 422
    assert not service.calls
