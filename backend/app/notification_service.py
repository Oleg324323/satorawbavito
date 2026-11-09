"""Credential-independent, account-owned Review notification recipient service.

No HTTP registration, universal inbox, configured engine or transport. Trusted
bootstrap alone supplies a default-empty exact org/account/provider allowlist.
Fixed reviews:read permits viewing and recording ONLY one's own viewing state.
"""

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import (
    MarketplaceAccountContextError,
    set_marketplace_account_context,
    set_tenant_context,
)
from app.notification_repository import NotificationRepository, NotificationStorageError
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.ingestion_contract import ReviewRepositoryError
from app.reviews.storage_payloads import StoragePayloadError


class NotificationServiceError(ValueError):
    def __init__(self, code):
        allowed = {"NOTIFICATION_INVALID", "NOTIFICATION_DISABLED", "NOTIFICATION_DENIED",
                   "NOTIFICATION_NOT_FOUND", "NOTIFICATION_CONFLICT", "NOTIFICATION_UNAVAILABLE",
                   "NOTIFICATION_CONFIGURATION_INVALID", "NOTIFICATION_READBACK_REQUIRED"}
        self.code = code if code in allowed else "NOTIFICATION_UNAVAILABLE"
        super().__init__(self.code)


def _require(condition, code):
    if not condition:
        raise NotificationServiceError(code)


class ReviewNotificationService:
    def __init__(self, *, engine: Engine, allowlist=frozenset()):
        _require(isinstance(engine, Engine) and engine.dialect.name == "postgresql", "NOTIFICATION_CONFIGURATION_INVALID")
        _require(type(allowlist) is frozenset, "NOTIFICATION_CONFIGURATION_INVALID")
        for entry in allowlist:
            _require(type(entry) is tuple and len(entry) == 3, "NOTIFICATION_CONFIGURATION_INVALID")
            org, account, marketplace = entry
            _require(type(org) is int and 0 < org <= 2**31 - 1
                     and type(account) is int and 0 < account <= 2**31 - 1
                     and type(marketplace) is str and marketplace in {"wb", "avito"}, "NOTIFICATION_CONFIGURATION_INVALID")
        self._engine, self._allowlist = engine, allowlist

    def read_visible(self, *, authenticated_actor, marketplace_account_id, marketplace, event_ids):
        return self._execute(authenticated_actor, marketplace_account_id, marketplace, event_ids)

    def mark_visible(self, *, authenticated_actor, marketplace_account_id, marketplace, event_ids, action):
        _require(type(action) is str and action in {"read", "dismiss"}, "NOTIFICATION_INVALID")
        return self._execute(authenticated_actor, marketplace_account_id, marketplace, event_ids, action=action)

    def capabilities(self, *, authenticated_actor, marketplace_account_id, marketplace, event_ids):
        # This is a checked, exact visible set, not a reusable authority token.
        return self._execute(authenticated_actor, marketplace_account_id, marketplace, event_ids, capabilities=True)

    def list_visible(self, *, authenticated_actor, marketplace_account_id, marketplace, limit, cursor, codec):
        from app.notification_list import NotificationListCursorCodec

        _require(type(limit) is int and 1 <= limit <= 100 and type(codec) is NotificationListCursorCodec,
                 "NOTIFICATION_INVALID")
        _require(cursor is None or type(cursor) is str and 0 < len(cursor) <= 4096, "NOTIFICATION_INVALID")
        return self._execute(authenticated_actor, marketplace_account_id, marketplace, [],
                             discovery=(limit, cursor, codec))

    def _execute(self, actor, account_id, marketplace, event_ids, *, action=None, capabilities=False, discovery=None):
        _require(type(actor) is ActorContext and bool(actor.session_id), "NOTIFICATION_DENIED")
        _require(type(account_id) is int and 0 < account_id <= 2**31 - 1
                 and type(marketplace) is str and marketplace in {"wb", "avito"}, "NOTIFICATION_INVALID")
        _require((actor.organization_id, account_id, marketplace) in self._allowlist, "NOTIFICATION_DISABLED")
        _require(type(event_ids) is list and all(type(item) is str for item in event_ids), "NOTIFICATION_INVALID")
        ids = list(event_ids)  # Never keep a mutable caller list across auth/SQL.
        session, committing, code, result = None, False, None, None
        try:
            # Owned clean Engine-bound Session; never borrow a caller's root.
            session = Session(self._engine, autoflush=False)
            session.begin()
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id, IamMembershipRow.is_active.is_(True)))
            account = session.execute(select(MarketplaceAccountRow.external_account_id,
                MarketplaceAccountRow.credential_ref).where(
                    MarketplaceAccountRow.organization_id == actor.organization_id,
                    MarketplaceAccountRow.marketplace_account_id == account_id,
                    MarketplaceAccountRow.marketplace == marketplace)).one_or_none()
            _require(member is not None and account is not None, "NOTIFICATION_DENIED")
            binding = ExpectedAccountBinding(account_id, marketplace, account.external_account_id, account.credential_ref)
            guard = acquire_publication_guard(session,
                principal=UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id),
                required_permissions=frozenset({"reviews:read"}), accounts=(binding,), authorities=())
            set_marketplace_account_context(session, organization_id=actor.organization_id, marketplace_account_id=account_id)
            repository = NotificationRepository(session.connection(), ReviewBindingDescriptor(actor.organization_id,
                account_id, marketplace, binding.external_account_id, binding.credential_ref))
            if discovery is not None:
                from app.notification_list import read_notification_page

                limit, cursor, codec = discovery
                result = read_notification_page(repository, actor=actor, member=member,
                                                limit=limit, cursor=cursor, codec=codec)
            elif action is None:
                result = repository.read_visible(event_ids=ids, recipient_membership_id=member)
                if capabilities:
                    result = {"organizationId": actor.organization_id, "marketplaceAccountId": account_id,
                              "marketplace": marketplace, "recipientMembershipId": member, "eventIds": ids,
                              "canRead": True, "canMarkRead": True, "canDismiss": True}
            else:
                result = repository.mark_visible(event_ids=ids, recipient_membership_id=member, action=action)
            if discovery is not None:
                pass
            elif capabilities:
                result["schemaVersion"] = "review-notification-capabilities-v1"
            else:
                result = {"schemaVersion": "review-notification-receipts-v1" if action else "review-notification-visible-v1",
                          "organizationId": actor.organization_id, "marketplaceAccountId": account_id,
                          "marketplace": marketplace, "recipientMembershipId": member, "eventIds": ids,
                          "items": result, **({"action": action} if action else {})}
            guard.revalidate_before_write()
            committing = True
            session.commit()  # Existing final listener repeats live same-root checks.
        except NotificationServiceError as error:
            code = error.code
        except PublicationGuardError as error:
            code = "NOTIFICATION_UNAVAILABLE" if error.code in {
                "publication_context_invalid", "publication_persistence_failed"} else "NOTIFICATION_DENIED"
        except NotificationStorageError as error:
            code = {"NOTIFICATION_STORAGE_NOT_FOUND": "NOTIFICATION_NOT_FOUND",
                    "NOTIFICATION_STORAGE_INVALID": "NOTIFICATION_INVALID",
                    "NOTIFICATION_STORAGE_CONFLICT": "NOTIFICATION_CONFLICT"}.get(error.code, "NOTIFICATION_UNAVAILABLE")
        except ReviewRepositoryError:
            code = "NOTIFICATION_CONFLICT"
        except StoragePayloadError:
            code = "NOTIFICATION_INVALID"
        except IntegrityError:
            code = "NOTIFICATION_CONFLICT"
        except (SQLAlchemyError, MarketplaceAccountContextError):
            code = "NOTIFICATION_READBACK_REQUIRED" if action is not None and committing else "NOTIFICATION_UNAVAILABLE"
        except Exception:  # noqa: BLE001 - unknown commit must not leak SQL or imply rollback.
            code = "NOTIFICATION_READBACK_REQUIRED" if action is not None and committing else "NOTIFICATION_UNAVAILABLE"
        finally:
            if session is not None:
                failed = False
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - still attempt close after rollback failure.
                    failed = True
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - cleanup failure makes mutation outcome uncertain.
                    failed = True
                if failed:
                    code = "NOTIFICATION_READBACK_REQUIRED" if action is not None and committing else "NOTIFICATION_UNAVAILABLE"
        if code is not None:
            # Raise outside raw exception handlers: no SQL parameters/cause chain.
            raise NotificationServiceError(code)
        return result
