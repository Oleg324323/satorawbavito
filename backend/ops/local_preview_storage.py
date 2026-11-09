"""Refuse to silently replace the local account database with an empty one."""
from pathlib import Path
from contextlib import closing
import sqlite3


def require_existing_preview_database(path: Path, *, initialize: bool = False) -> None:
    if not path.exists():
        if initialize:
            return
        raise RuntimeError(
            "Local account database is missing. Restore .local-preview/preview.sqlite; "
            "no empty replacement was created. For a genuinely new installation only, "
            "run ops/current_cost_local_preview.py --init-local-database from backend."
        )
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            account = db.execute("SELECT user_id FROM lk_users LIMIT 1").fetchone()
    except sqlite3.Error:
        raise RuntimeError(
            "Local account database is unreadable or has no account schema. "
            "Restore the existing database; it was not replaced."
        ) from None
    if account is None and not initialize:
        raise RuntimeError("Local account database is empty. Restore the existing database; no demo accounts were created.")


def configure_preview_sqlite_storage(path: Path, engine) -> None:
    """Allow report readers and API writers to coexist in the local SQLite DB.

    PostgreSQL deployment configuration is deliberately unchanged.
    """
    from sqlalchemy import event

    if engine.dialect.name != "sqlite" or Path(engine.url.database).resolve() != path.resolve():
        raise RuntimeError("Local SQLite configuration does not match the preview database")
    with closing(sqlite3.connect(path, timeout=30)) as db:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError("Local SQLite could not enable concurrent report reads")

    @event.listens_for(engine, "connect")
    def connection_timeout(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()
