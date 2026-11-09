"""Self preferences are the only accountless publication contract."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.platform.integrations.publication_guard import (
    ExpectedCredential,
    ExpectedIngestionToken,
    PublicationGuardError,
    UserSessionPrincipal,
    _contracts,
)

PRINCIPAL = UserSessionPrincipal(1, "self-user", 2, "live-session")


@pytest.mark.parametrize("permission", ["preferences:read", "preferences:write"])
def test_self_preferences_accept_empty_accounts_without_authority(permission):
    assert _contracts(PRINCIPAL, frozenset({permission}), (), ()) == ((), (), ())


@pytest.mark.parametrize("permissions", [frozenset({"reviews:read"}), frozenset({"sync:run"}),
    frozenset({"preferences:read", "reviews:read"}), frozenset({"preferences:write", "preferences:read"}),
    frozenset({"integrations:write"}), frozenset()])
def test_other_permission_contracts_still_require_accounts(permissions):
    with pytest.raises(PublicationGuardError):
        _contracts(PRINCIPAL, permissions, (), ())


@pytest.mark.parametrize("authority", [ExpectedCredential(1, uuid4(), "wb_api", 1, 1, None),
    ExpectedIngestionToken(1, uuid4(), "avito.browser_snapshot.write", datetime.now(UTC) + timedelta(hours=1))])
def test_accountless_preferences_never_accept_credentials_or_ingestion_authorities(authority):
    with pytest.raises(PublicationGuardError):
        _contracts(PRINCIPAL, frozenset({"preferences:write"}), (), (authority,))


@pytest.mark.parametrize("accounts,authorities", [(set(), ()), ((), set()), ((), None), (None, ())])
def test_accountless_contract_requires_validated_sequence_types(accounts, authorities):
    with pytest.raises(PublicationGuardError):
        _contracts(PRINCIPAL, frozenset({"preferences:read"}), accounts, authorities)


def test_self_preferences_do_not_accept_an_alternate_principal():
    with pytest.raises(PublicationGuardError):
        _contracts(object(), frozenset({"preferences:read"}), (), ())
