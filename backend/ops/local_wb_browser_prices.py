"""SQLite compatibility for the single-process local preview, not production."""
from contextlib import contextmanager
from datetime import timezone
from threading import Lock, RLock

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError


def install_local_browser_price_storage():
    from app.config import get_settings
    from app.infra.db import get_session_factory
    from app.routers import wb_browser_prices as browser
    from app.wb_live.auth import _require_live_actor_records

    if get_settings().environment != "local":
        raise RuntimeError("Local browser-price adapter must not run in production")
    factory = get_session_factory()
    with factory() as session:
        if session.get_bind().dialect.name != "sqlite":
            return
    # Preview uses one API process. Serialize browser token/state operations to
    # retain check-and-write atomicity without pretending SQLite has PG row locks.
    storage_lock = RLock()
    refresh_locks = {}
    refresh_locks_guard = Lock()
    transaction_marker = object()

    @contextmanager
    def local_session():
        try:
            with storage_lock, factory() as session, session.begin():
                session.info["local_browser_price_transaction"] = transaction_marker
                try:
                    yield session
                finally:
                    session.info.pop("local_browser_price_transaction", None)
        except (browser.WbLiveError, browser.WbCredentialBindingError):
            browser._deny()
        except SQLAlchemyError:
            browser._deny("WB_BROWSER_STORAGE_UNAVAILABLE", 503)

    def local_now(session):
        # SQLite CURRENT_TIMESTAMP is UTC. Keep the clock DB-owned as in PG.
        value = session.scalar(select(func.current_timestamp()))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def local_actor(session, actor, *, permission="integrations:write", account_id=None):
        # Only the serialized preview transaction can use SQLite admission.
        # Production's PostgreSQL admission and locks remain unchanged.
        if (session.get_bind().dialect.name != "sqlite" or not session.in_transaction()
                or session.in_nested_transaction()
                or session.info.get("local_browser_price_transaction") is not transaction_marker):
            raise browser.WbLiveError("WB_ACCESS_DENIED")

        def auth_clock(s):
            # Ordinary SQLite drops tzinfo; preview's UTC type restores it.
            # Match the stored representation without changing the instant.
            from app.cabinet.orm import LkSessionRow
            login = s.get(LkSessionRow, actor.session_id) if actor.session_id else None
            now = local_now(s)
            return now.replace(tzinfo=None) if login is not None and login.expires_at.tzinfo is None else now

        return _require_live_actor_records(session, actor, permission=permission,
            account_id=account_id, database_clock=auth_clock)

    @contextmanager
    def local_refresh_lock(organization_id):
        with refresh_locks_guard:
            lock = refresh_locks.setdefault(organization_id, Lock())
        if not lock.acquire(blocking=False):
            browser._retry("WB_BROWSER_REFRESH_BUSY", 409)
        try:
            yield
        finally:
            lock.release()

    browser._session = local_session
    browser._now = local_now
    browser._refresh_lock = local_refresh_lock
    browser.require_live_actor = local_actor
