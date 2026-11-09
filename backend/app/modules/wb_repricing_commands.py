"""Dormant committed user-session approval commands, not a price sender.

Principals and exact account bindings come from a trusted authentication/service
boundary, never directly from request bodies. The live publication guard checks
them again with the service-fixed existing price:send permission. This surface
is credential-independent: it cannot reserve, dispatch, record worker outcomes
or authorize provider I/O. Worker authority and existing flow wiring are separate
gates. Every public method owns its physical root and returns only after commit.
"""

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.modules.wb_repricing import ApprovalValidationError
from app.modules.wb_repricing_dispatch import CanonicalApplyRequest
from app.modules.wb_repricing_postgres import (
    ApprovalPersistenceError,
    ApprovalTransaction,
)
from app.modules.wb_repricing_repository import (
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)


class UserApprovalCommands:
    """Fixed-permission commands; no user-supplied actor or permission override."""

    def __init__(self, engine: Engine):
        if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
            raise ApprovalValidationError("PostgreSQL engine required")
        self._engine = engine

    def _execute(self, scope, principal, binding, operation):
        if (
            type(scope) is not ApprovalRepositoryScope
            or type(principal) is not UserSessionPrincipal
            or type(binding) is not ExpectedAccountBinding
            or principal.organization_id != scope.organization_id
            or binding.marketplace_account_id != scope.marketplace_account_id
            or binding.provider != "wb"
        ):
            raise PublicationGuardError("publication_context_invalid")
        actor = AuthenticatedApprovalActor(
            principal.organization_id, principal.membership_id
        )
        try:
            with Session(self._engine) as session, session.begin():
                guard = acquire_publication_guard(
                    session,
                    principal=principal,
                    required_permissions=frozenset({"price:send"}),
                    accounts=(binding,),
                    authorities=(),
                )
                transaction = ApprovalTransaction(session, scope)
                guard.revalidate_before_write()
                result = operation(transaction, actor)
                # Guard's finalizer runs through the physical commit boundary.
            return result
        except SQLAlchemyError:
            # Includes deferred constraints and physical COMMIT failures. No
            # fallback, return of an uncommitted value or raw SQL/payload leak.
            raise ApprovalPersistenceError() from None

    def create_intent(self, request: CanonicalApplyRequest, principal, binding):
        if type(request) is not CanonicalApplyRequest:
            raise ApprovalValidationError("canonical request required")
        return self._execute(
            request.scope,
            principal,
            binding,
            lambda transaction, actor: transaction.create_intent(request, actor),
        )

    def claim(self, scope, expected_version, principal, binding):
        return self._execute(
            scope,
            principal,
            binding,
            lambda transaction, actor: transaction.claim(expected_version, actor),
        )

    def reject(self, scope, expected_version, principal, binding, reason_code):
        return self._execute(
            scope,
            principal,
            binding,
            lambda transaction, actor: transaction.reject(
                expected_version, actor, reason_code
            ),
        )

    def block(self, scope, expected_version, principal, binding, safe_blocker_code):
        return self._execute(
            scope,
            principal,
            binding,
            lambda transaction, actor: transaction.block(
                expected_version, actor, safe_blocker_code
            ),
        )
