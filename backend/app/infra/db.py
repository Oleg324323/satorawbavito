from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.infra.models import Base


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def set_tenant_context(session: Session, organization_id: int) -> None:
    if organization_id < 1:
        raise ValueError("organization_id must be positive")
    if session.get_bind().dialect.name == "postgresql":
        transaction = session.get_nested_transaction() or session.get_transaction()
        marker = (transaction, organization_id)
        if (
            transaction is not None
            and session.info.get("satorna_tenant_context") == marker
        ):
            return
        session.execute(
            text("SELECT set_config('app.organization_id', :organization_id, true)"),
            {"organization_id": str(organization_id)},
        )
        session.info["satorna_tenant_context"] = (
            session.get_nested_transaction() or session.get_transaction(),
            organization_id,
        )


def create_all_for_local_dev() -> None:
    """Bootstrap helper for local experiments outside Alembic."""
    Base.metadata.create_all(bind=get_engine())


_ACCOUNT_CONTEXT = "satorna_marketplace_account_context"


class MarketplaceAccountContextError(RuntimeError):
    """Expose only fixed codes, never supplied settings or database diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = (
            code if type(code) is str and code in {
                "account_context_invalid", "account_context_failed"
            } else "account_context_failed"
        )
        super().__init__(self.code)


def _clear_marketplace_account_context(session, transaction) -> None:
    # Registered on participating Sessions only; do not touch tenant/guard state
    # or mutate the event listener collection while it is being dispatched.
    if transaction.parent is None:
        session.info.pop(_ACCOUNT_CONTEXT, None)


def set_marketplace_account_context(
    session: Session, *, organization_id: int, marketplace_account_id: int
) -> None:
    """Set an exact pair locally in an Engine-bound Session's clean PG RC root.

    This supplies context, not authentication, permission or account row locks.
    Call after the trusted publication guard and before domain locks/writes.
    Every failure requires caller rollback. Arbitrary later caller SQL is not
    fenced; the publication/service layer owns final authorization checks.
    Connection-bound Sessions are unsupported: an external physical root can
    outlive Session completion and retain its transaction-local settings.
    """
    if (not isinstance(session, Session)
            or any(type(value) is not int or not 0 < value <= 2**31 - 1
                   for value in (organization_id, marketplace_account_id))):
        raise MarketplaceAccountContextError("account_context_invalid")
    root = session.get_transaction()
    if (root is None or not root.is_active or not session.is_active
            or session.in_nested_transaction()
            or session.new or session.dirty or session.deleted):
        raise MarketplaceAccountContextError("account_context_invalid")
    try:
        bind = session.get_bind()
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            raise MarketplaceAccountContextError("account_context_invalid")
        with session.no_autoflush:
            connection = session.connection()
            # Connection.begin_nested() is invisible to Session nested hooks.
            if (connection.in_nested_transaction() or not connection.in_transaction()
                    or not connection.get_transaction().is_active
                    # get_isolation_level reports READ COMMITTED even when the
                    # driver's autocommit makes SET LOCAL ineffective.
                    or getattr(connection.connection.dbapi_connection, "autocommit", None) is not False
                    or connection.get_isolation_level() != "READ COMMITTED"):
                raise MarketplaceAccountContextError("account_context_invalid")
            marker = session.info.get(_ACCOUNT_CONTEXT)
            if _ACCOUNT_CONTEXT in session.info and (
                type(marker) is not tuple or len(marker) != 3 or marker[0] is not root
                or type(marker[1]) is not int or type(marker[2]) is not int
                or marker[1:] != (organization_id, marketplace_account_id)
            ):
                raise MarketplaceAccountContextError("account_context_invalid")
            current = session.execute(text(
                "SELECT current_setting('app.organization_id', true), "
                "current_setting('app.marketplace_account_id', true)"
            )).one()
            org, account = str(organization_id), str(marketplace_account_id)
            if current[0] not in (None, "", org) or current[1] not in (None, "", account):
                raise MarketplaceAccountContextError("account_context_invalid")
            session.execute(text(
                "SELECT set_config('app.organization_id', :org, true), "
                "set_config('app.marketplace_account_id', :account, true)"
            ), {"org": org, "account": account})
            if not event.contains(session, "after_transaction_end", _clear_marketplace_account_context):
                event.listen(session, "after_transaction_end", _clear_marketplace_account_context)
            session.info[_ACCOUNT_CONTEXT] = (root, organization_id, marketplace_account_id)
    except SQLAlchemyError:
        raise MarketplaceAccountContextError("account_context_failed") from None
