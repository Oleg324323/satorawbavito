"""Dormant trusted registrar over 0079; opaque proof IDs are not verification.

No operational verifier, key loader, provisioning, retry or default identity.
The required verifier independently attests exact source fields before DB locks.
Plaintext is compared only in memory, never supplied to SQL registration.
"""

from dataclasses import fields
from uuid import UUID

from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from app.platform.integrations.publication_guard import (
    _physical_connection,
    _require_clean_publication_root,
)
from app.security.credential_maintenance_contract import (
    MaintenanceAuthorization,
    MaintenanceContractError,
)
from app.security.marketplace_credentials import DecryptedCredential

_CODES = frozenset({"maintenance_contract_invalid", "maintenance_authorization_denied",
    "maintenance_binding_changed", "maintenance_registration_conflict", "maintenance_unavailable",
    "maintenance_commit_unknown"})
# Exact physical 0079 composite order; values are exclusively nonsecret metadata.
_COLUMNS = (
    ("authorization_id", "uuid"), ("target_credential_id", "uuid"),
    ("organization_id", "integer"), ("marketplace_account_id", "integer"),
    ("provider", "text"), ("credential_kind", "text"), ("source_id", "integer"),
    ("source_user_id", "varchar(64)"), ("expected_external_account_id", "varchar(128)"),
    ("expected_credential_ref", "varchar(255)"), ("recipient_role_oid", "oid"),
    ("recipient_role_name", "name"), ("review_id", "uuid"), ("review_authority_id", "uuid"),
    ("proof_reference_id", "uuid"), ("reviewed_at", "timestamptz"),
    ("allow_backfill", "boolean"), ("allow_verify", "boolean"),
    ("not_before", "timestamptz"), ("expires_at", "timestamptz"),
    ("revoked_at", "timestamptz"), ("revocation_reason_code", "text"),
)
_ROW = "ROW(" + ",".join(f"CAST(:{name} AS {kind})" for name, kind in _COLUMNS) + ")::credential_maintenance.authorizations"
_LOCK = text("SELECT token,client_id,client_secret FROM credential_maintenance.lock_registration(" + _ROW + ")")
_REGISTER = text("SELECT credential_maintenance.register_authorization(" + _ROW + ",CAST(:previous_authorization_id AS uuid))")
_SET_SCOPE = text("SELECT set_config('app.organization_id',:organization_id,true), "
                  "set_config('app.marketplace_account_id',:marketplace_account_id,true)")
_CHECK_SCOPE = text("SELECT current_setting('app.organization_id',true)=:organization_id AND "
                   "current_setting('app.marketplace_account_id',true)=:marketplace_account_id")
_ROLE = text("""
 SELECT current_user=session_user AND r.oid=CAST(:registrar_oid AS oid)
  AND r.rolname=:registrar_name AND r.rolname=session_user
  AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
    OR r.rolinherit OR r.rolreplication OR r.rolbypassrls)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=r.oid OR roleid=r.oid)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relowner=r.oid)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspowner=r.oid)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_proc WHERE proowner=r.oid)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_database WHERE datdba=r.oid)
 FROM pg_catalog.pg_roles r WHERE r.oid=CAST(:registrar_oid AS oid)
""")


class MaintenanceRegistrarError(ValueError):
    def __init__(self, code="maintenance_unavailable"):
        self.code = code if type(code) is str and code in _CODES else "maintenance_unavailable"
        super().__init__(self.code)

    def __repr__(self):
        return f"MaintenanceRegistrarError('{self.code}')"


def _require(condition, code="maintenance_authorization_denied"):
    if not condition:
        raise MaintenanceRegistrarError(code)


def _verified_fields(provider, verified):
    _require(type(verified) is DecryptedCredential, "maintenance_binding_changed")
    payload = verified.reveal()
    expected = {"token"} if provider == "wb" else {"clientId", "clientSecret"}
    _require(set(payload) == expected and all(type(value) is str for value in payload.values()),
             "maintenance_binding_changed")
    return {key: value.encode("utf-8", errors="strict") for key, value in payload.items()}


def _compare(provider, row, verified):
    _require(set(row) == {"token", "client_id", "client_secret"}, "maintenance_binding_changed")
    if provider == "wb":
        _require(row["client_id"] is None and row["client_secret"] is None, "maintenance_binding_changed")
        current = {"token": row["token"]}
    else:
        _require(row["token"] is None, "maintenance_binding_changed")
        current = {"clientId": row["client_id"], "clientSecret": row["client_secret"]}
    _require(all(type(value) is str for value in current.values()), "maintenance_binding_changed")
    _require({key: value.encode("utf-8", errors="strict") for key, value in current.items()} == verified,
             "maintenance_binding_changed")


class CredentialMaintenanceRegistrar:
    __slots__ = ("_factory", "_role", "_verifier")

    def __init__(self, *, session_factory, verifier, registrar_role_oid: int, registrar_role_name: str):
        invalid = False
        try:
            invalid = (not callable(session_factory) or not callable(verifier)
                or type(registrar_role_oid) is not int or not 0 < registrar_role_oid <= 2**32 - 1
                or type(registrar_role_name) is not str or not registrar_role_name.strip()
                or "\x00" in registrar_role_name or not 0 < len(registrar_role_name.encode("utf-8")) <= 63)
        except Exception:  # noqa: BLE001 - never expose injected object/encoding errors.
            invalid = True
        if invalid:
            raise MaintenanceRegistrarError("maintenance_contract_invalid")
        self._factory, self._verifier = session_factory, verifier
        self._role = {"registrar_oid": registrar_role_oid, "registrar_name": registrar_role_name}

    def __repr__(self):
        return "<CredentialMaintenanceRegistrar redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise MaintenanceRegistrarError("maintenance_contract_invalid")

    def register(self, authorization: MaintenanceAuthorization, *, previous_authorization_id: UUID | None) -> UUID:
        session, owned, committing, code, result = None, False, False, None, None
        try:
            _require(type(authorization) is MaintenanceAuthorization, "maintenance_contract_invalid")
            authorization = MaintenanceAuthorization(**{field.name: getattr(authorization, field.name)
                                                       for field in fields(MaintenanceAuthorization)})
            _require(previous_authorization_id is None or (
                type(previous_authorization_id) is UUID and previous_authorization_id.int != 0),
                "maintenance_contract_invalid")
            _require(authorization.revoked_at is None and authorization.revocation_reason_code is None,
                     "maintenance_contract_invalid")
            params = {name: getattr(authorization, name) for name, _ in _COLUMNS}
            # Verifier performs no work under account/source locks; identity IDs
            # alone can never manufacture verified source fields here.
            verified = _verified_fields(authorization.provider, self._verifier(authorization))
            _require(params == {name: getattr(authorization, name) for name, _ in _COLUMNS},
                     "maintenance_contract_invalid")
            session = self._factory()
            _require(isinstance(session, Session) and not session.in_transaction()
                and not session.in_nested_transaction() and session.is_active
                and not session.new and not session.dirty and not session.deleted)
            bind = session.get_bind()
            _require(isinstance(bind, Engine) and bind.dialect.name == "postgresql")
            owned = True
            session.begin()
            _require_clean_publication_root(session)
            root = session.get_transaction()
            physical = _physical_connection(session).get_transaction()
            _require(session.scalar(_ROLE, self._role) is True)
            scope = {"organization_id": str(authorization.organization_id),
                     "marketplace_account_id": str(authorization.marketplace_account_id)}
            # Helper ownership does not bypass the existing forced tenant RLS.
            # Owner values come from the verified metadata, not caller GUCs.
            session.execute(_SET_SCOPE, scope)

            def clean():
                _require_clean_publication_root(session)
                _require(session.get_transaction() is root and root.is_active
                         and _physical_connection(session).get_transaction() is physical)

            def register_locked():
                clean()
                _require(session.scalar(_ROLE, self._role) is True)
                _require(session.scalar(_CHECK_SCOPE, scope) is True)
                # Existing helper enforces user→account→source→authorization locks.
                row = session.execute(_LOCK, params).mappings().one()
                _compare(authorization.provider, row, verified)
                clean()
                saved = session.execute(_REGISTER, {**params,
                    "previous_authorization_id": previous_authorization_id}).scalar_one()
                _require(type(saved) is UUID and saved == authorization.authorization_id,
                         "maintenance_registration_conflict")

            def closing(current):
                _require(current is session and tuple(current.dispatch.before_commit)[-1:] == (closing,))
                # Same exact registration is an immutable replay. Its helper
                # rechecks current recipient and DB clock after any earlier wait.
                register_locked()
                clean()
                # Final checks do not issue SQL or permit queued ORM work after
                # the last driver/root validation above.
                _require(current.is_active and current.get_transaction() is root and root.is_active
                    and not current.in_nested_transaction() and not current.new
                    and not current.dirty and not current.deleted and physical.is_active
                    and tuple(current.dispatch.before_commit)[-1:] == (closing,))

            event.listen(session, "before_commit", closing)
            register_locked()
            committing = True
            session.commit()
            result = authorization.authorization_id
        except MaintenanceRegistrarError as error:
            code = error.code
        except MaintenanceContractError:
            code = "maintenance_contract_invalid"
        except Exception as error:  # noqa: BLE001 - source/verifier/driver diagnostics are private.
            # Only fixed SQL helper codes are translated; never retain a raw
            # exception or its parameters as a cause/context on public errors.
            diagnostic = getattr(getattr(error, "orig", None), "diag", None)
            message = getattr(diagnostic, "message_primary", None)
            code = (message if type(message) is str and message in _CODES else
                    "maintenance_commit_unknown" if committing else "maintenance_unavailable")
        finally:
            if owned:
                for cleanup in (session.rollback, session.close):
                    try:
                        cleanup()
                    except Exception:  # noqa: BLE001 - cleanup cannot establish a known commit outcome.
                        code = "maintenance_commit_unknown" if committing else "maintenance_unavailable"
        if code is not None:
            raise MaintenanceRegistrarError(code)
        return result
