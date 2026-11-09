"""Fixed live-session credential management, preserving disconnected setup/revoke.

Only explicit trusted dependencies. No global pool/key/provider initialization,
fetch authority, caller callback, legacy fallback or crypto outside the store.
"""

from dataclasses import dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    CredentialMetadata,
    CredentialStoreError,
    MarketplaceAccountCredentialOwner,
    _get_marketplace_credential_metadata_in_session,
    _put_marketplace_credential_in_session,
    _resolve_marketplace_credential_in_session,
    _revoke_marketplace_credential_in_session,
)
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    PublicationGuard,
    PublicationGuardError,
    UserSessionPrincipal,
    _aware,
    _install_credential_management_guard,
    _integer,
    _require_clean_publication_root,
    _text,
)
from app.platform.integrations.wb_credentials import WbCredentialBindingError
from app.security.marketplace_credentials import MAX_SECRET_FIELD_BYTES, CredentialCryptoError, CredentialKeyring


_KINDS = {"wb": "wb_api", "avito": "avito_oauth_client"}
_CODES = frozenset({
    "credential_account_not_found", "credential_account_identity_mismatch", "credential_concurrent_update",
    "credential_configuration_invalid", "credential_contract_invalid", "credential_expired", "credential_missing",
    "credential_persistence_failed", "credential_reason_invalid", "credential_auth_failed", "credential_key_unavailable",
    "credential_payload_invalid", "credential_management_access_denied", "credential_management_context_invalid",
    "credential_management_readback_required", "WB_CREDENTIAL_IDENTITY_INVALID", "WB_SELLER_IDENTITY_MISMATCH",
})
_META_FIELDS = ("credential_id", "organization_id", "marketplace_account_id", "provider", "credential_kind",
                "generation", "expires_at", "revoked_at", "revocation_reason_code", "created_at", "updated_at")


class CredentialManagementError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else "credential_persistence_failed"
        super().__init__(self.code)


def _require(condition, code="credential_management_context_invalid"):
    if not condition:
        raise CredentialManagementError(code)


@dataclass(frozen=True, slots=True, repr=False)
class _ManagementSnapshot:
    principal: UserSessionPrincipal
    # Actual org/provider/external/ref/status/incarnation, not a connected predicate.
    account: tuple


class _CredentialManagementGuard(PublicationGuard):
    """Private exact installer type; inherited live IAM/login/final-root machinery."""
    def __init__(self, session, principal, owner, kind, operation, expected_snapshot):
        _require(operation in {"snapshot", "status", "put", "revoke"})
        super().__init__(session, principal, frozenset({"integrations:write"}), (owner,), (), ())
        self._owner, self._kind, self._operation = owner, kind, operation
        self._snapshot = expected_snapshot
        self._result_ready, self._result, self._verified_active = False, None, False

    def __repr__(self):
        return "<CredentialManagementGuard>"

    def _validate_accounts(self, session, expiries):
        owner, a = self._owner, MarketplaceAccountRow
        account = session.execute(select(a.organization_id, a.marketplace, a.external_account_id,
            a.credential_ref, a.status, a.ingestion_binding_version).where(
                a.organization_id == owner.organization_id, a.marketplace_account_id == owner.marketplace_account_id,
            ).with_for_update()).one_or_none()
        _require(account is not None, "credential_account_not_found")
        _require(account.marketplace == owner.provider, "credential_account_identity_mismatch")
        current = _ManagementSnapshot(self._principal, tuple(account))
        if self._snapshot is None:
            self._snapshot = current
        _require(type(self._snapshot) is _ManagementSnapshot and self._snapshot == current,
                 "credential_account_identity_mismatch")
        if self._result_ready:
            row = self.lock_latest_metadata(session)
            expected = None if self._result is None else tuple(getattr(self._result, name) for name in _META_FIELDS)
            _require((None if row is None else tuple(row)) == expected, "credential_concurrent_update")
            if self._verified_active and self._result.expires_at is not None:
                expiries.append(self._result.expires_at)
        _require(not self._finalizing or self._operation == "snapshot" or self._result_ready)
        now = session.scalar(select(func.clock_timestamp()))
        if any(not _aware(expiry) or expiry <= now for expiry in expiries):
            raise PublicationGuardError("publication_expired")
        return now

    def lock_latest_metadata(self, session):
        # Only metadata columns outside the store; never ciphertext/key/nonce.
        _require(session is self._context())
        c, owner = MarketplaceAccountCredentialRow, self._owner
        return session.execute(select(*(getattr(c, name) for name in _META_FIELDS)).where(
            c.organization_id == owner.organization_id, c.marketplace_account_id == owner.marketplace_account_id,
            c.provider == owner.provider, c.credential_kind == self._kind,
        ).order_by(c.generation.desc(), c.created_at.desc()).limit(1).with_for_update(read=True)).one_or_none()

    def seal(self, result, *, verified_active=False):
        _require(not self._result_ready and (result is None or type(result) is CredentialMetadata))
        self._result, self._verified_active, self._result_ready = result, verified_active, True
        self.revalidate_before_write()


def _inputs(actor, account_id, provider, kind):
    _require(type(actor) is ActorContext and _integer(actor.organization_id)
             and _text(actor.user_id, 64) and _text(actor.session_id, 64), "credential_management_access_denied")
    _require(_integer(account_id) and type(provider) is str and type(kind) is str
             and _KINDS.get(provider) == kind, "credential_contract_invalid")
    return MarketplaceAccountCredentialOwner(actor.organization_id, account_id, provider)


def _plaintext(provider, value):
    """Defense in depth for service callers; HTTP retains its existing validation."""
    fields = {"token"} if provider == "wb" else {"clientId", "clientSecret"}
    _require(type(value) is dict and set(value) == fields, "credential_payload_invalid")
    copied = dict(value)
    for name, item in copied.items():
        _require(type(item) is str and len(item) >= (4 if name == "clientId" else 8)
                 and not any(char.isspace() for char in item), "credential_payload_invalid")
        invalid = False
        try:
            invalid = len(item.encode("utf-8")) > MAX_SECRET_FIELD_BYTES
        except UnicodeError:
            invalid = True
        _require(not invalid, "credential_payload_invalid")
    return copied


class CredentialManagementService:
    def __init__(self, *, session_factory, keyring_loader, wb_seller_verifier):
        _require(all(callable(value) for value in (session_factory, keyring_loader, wb_seller_verifier)),
                 "credential_configuration_invalid")
        self._factory, self._keyring_loader, self._seller_verifier = session_factory, keyring_loader, wb_seller_verifier

    def _keyring(self):
        code, keyring = None, None
        try:
            keyring = self._keyring_loader()
            _require(type(keyring) is CredentialKeyring, "credential_configuration_invalid")
            keyring.key_for(keyring.current_key_version)
        except (CredentialStoreError, CredentialCryptoError, CredentialManagementError) as error:
            code = error.code
        except Exception:  # noqa: BLE001 - configuration internals must not escape.
            code = "credential_configuration_invalid"
        if code is not None:
            raise CredentialManagementError(code)
        return keyring

    def _new_session(self):
        code, session = None, None
        try:
            session = self._factory()
        except Exception:  # noqa: BLE001 - do not expose pool/driver settings.
            code = "credential_persistence_failed"
        if code is not None:
            raise CredentialManagementError(code)
        try:
            valid = (isinstance(session, Session) and session.is_active and not session.in_transaction()
                and not session.in_nested_transaction() and not session.new and not session.dirty and not session.deleted
                and isinstance(session.get_bind(), Engine) and session.get_bind().dialect.name == "postgresql")
        except Exception:  # noqa: BLE001 - rejected foreign roots remain caller-owned.
            valid = False
        _require(valid)
        return session

    def _run(self, operation, actor, owner, kind, *, expected_snapshot=None, plaintext=None,
             keyring=None, reason_code=None):
        session = self._new_session()
        code, result, committing, guard = None, None, False, None
        writing = operation in {"put", "revoke"}
        try:
            session.begin()
            _require_clean_publication_root(session)
            set_tenant_context(session, owner.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == owner.organization_id,
                IamMembershipRow.user_id == actor.user_id, IamMembershipRow.is_active.is_(True)))
            _require(member is not None, "credential_management_access_denied")
            principal = UserSessionPrincipal(owner.organization_id, actor.user_id, member, actor.session_id)
            guard = _CredentialManagementGuard(session, principal, owner, kind, operation, expected_snapshot)
            _install_credential_management_guard(session, guard)
            if operation == "snapshot":
                result = guard._snapshot
            elif operation == "put":
                result = _put_marketplace_credential_in_session(session, owner, kind, plaintext, keyring=keyring,
                    now=guard.revalidate_before_write(), actor_user_id=principal.user_id,
                    expected_external_account_id=guard._snapshot.account[2] if owner.provider == "wb" else None)
                guard.seal(result)
            elif operation == "revoke":
                result = _revoke_marketplace_credential_in_session(session, owner, kind, reason_code,
                    now=guard.revalidate_before_write(), actor_user_id=principal.user_id)
                guard.seal(result)
            elif operation == "status":
                guard.lock_latest_metadata(session)
                result = _get_marketplace_credential_metadata_in_session(session, owner, kind)
                now = guard.revalidate_before_write()
                active = result is not None and result.revoked_at is None and (result.expires_at is None or result.expires_at > now)
                if active:
                    keys = self._keyring()
                    try:
                        # Store verifies actual ciphertext; no reveal/cache/fetch path.
                        _resolve_marketplace_credential_in_session(session, owner, kind, keyring=keys,
                                                                  now=guard.revalidate_before_write())
                    finally:
                        keys = None
                guard.seal(result, verified_active=active)
            else:
                raise CredentialManagementError("credential_contract_invalid")
            guard.revalidate_before_write()
            committing = True
            session.commit()  # Existing final listener owns flush/auth/root/poison checks.
        except (CredentialStoreError, CredentialCryptoError, CredentialManagementError) as error:
            code = error.code
        except PublicationGuardError as error:
            code = {"publication_access_denied": "credential_management_access_denied",
                    "publication_expired": "credential_management_access_denied",
                    "publication_binding_changed": "credential_account_identity_mismatch",
                    "publication_context_invalid": "credential_management_context_invalid"}.get(error.code, "credential_persistence_failed")
        except IntegrityError:
            code = "credential_concurrent_update"
        except SQLAlchemyError:
            code = ("credential_management_readback_required"
                    if writing and committing and not (guard is not None and guard._failed)
                    else "credential_persistence_failed")
        except Exception:  # noqa: BLE001 - unknown commit outcome is not permission to retry.
            code = ("credential_management_readback_required"
                    if writing and committing and not (guard is not None and guard._failed)
                    else "credential_persistence_failed")
        finally:
            # A typed exception from an after-commit listener is still uncertain
            # to the caller. Only the shared poisoned final fence proves rejection.
            if code is not None and writing and committing and guard is not None and not guard._failed:
                code = "credential_management_readback_required"
            cleanup_failed = False
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 - still attempt close after rollback failure.
                cleanup_failed = True
            try:
                session.close()
            except Exception:  # noqa: BLE001 - never publish success after failed cleanup.
                cleanup_failed = True
            if cleanup_failed:
                code = "credential_management_readback_required" if writing and committing else "credential_persistence_failed"
        if code is not None:
            raise CredentialManagementError(code)
        return result

    def status(self, *, authenticated_actor, marketplace_account_id, provider, credential_kind):
        owner = _inputs(authenticated_actor, marketplace_account_id, provider, credential_kind)
        return self._run("status", authenticated_actor, owner, credential_kind)

    def revoke(self, *, authenticated_actor, marketplace_account_id, provider, credential_kind, reason_code):
        owner = _inputs(authenticated_actor, marketplace_account_id, provider, credential_kind)
        _require(type(reason_code) is str, "credential_reason_invalid")
        return self._run("revoke", authenticated_actor, owner, credential_kind, reason_code=reason_code)

    def put(self, *, authenticated_actor, marketplace_account_id, provider, credential_kind, plaintext):
        owner = _inputs(authenticated_actor, marketplace_account_id, provider, credential_kind)
        copied = _plaintext(provider, plaintext)
        keys = None
        try:
            snapshot = self._run("snapshot", authenticated_actor, owner, credential_kind)
            keys = self._keyring()  # Readiness BEFORE seller I/O; snapshot root is closed.
            keys = None
            if provider == "wb":
                code, seller = None, None
                try:
                    seller = self._seller_verifier(copied["token"])
                except WbCredentialBindingError:
                    code = "WB_CREDENTIAL_IDENTITY_INVALID"
                except Exception:  # noqa: BLE001 - arbitrary verifier failure cannot admit a write.
                    code = "credential_persistence_failed"
                if code is not None:
                    raise CredentialManagementError(code)
                _require(type(seller) is str and seller == snapshot.account[2], "WB_SELLER_IDENTITY_MISMATCH")
            keys = self._keyring()  # Do not keep the pre-I/O keyring as readiness evidence.
            return self._run("put", authenticated_actor, owner, credential_kind,
                expected_snapshot=snapshot, plaintext=copied, keyring=keys)
        finally:
            copied.clear()
            keys = None
