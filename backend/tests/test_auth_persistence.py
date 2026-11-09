"""Authentication must not turn a storage outage into a disposable account."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.cabinet import store
from app.config import Settings, get_settings
from app.main import create_app
from ops.local_preview_storage import configure_preview_sqlite_storage, require_existing_preview_database


@pytest.mark.parametrize("endpoint,payload", [
    ("login", {"email": "synthetic@example.test", "password": "Synthetic-only-test!"}),
    ("register", {"email": "synthetic@example.test", "password": "Synthetic-only-test!",
                  "fullName": "Synthetic", "companyName": "Synthetic"}),
])
def test_local_storage_outage_is_503_not_false_success_or_bad_password(monkeypatch, endpoint, payload):
    monkeypatch.setattr(store, "get_settings", lambda: Settings(cabinet_require_persistent_storage=True))
    monkeypatch.setattr(store, "get_engine", lambda: (_ for _ in ()).throw(SQLAlchemyError("synthetic outage")))
    monkeypatch.setattr(store, "_MEMORY", store._MemoryState())
    response = TestClient(create_app()).post(f"/api/v1/auth/{endpoint}", json=payload)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CABINET_STORAGE_UNAVAILABLE"
    assert "База учётных записей" not in response.json()["error"]["message"]
    assert store._MEMORY.users == {} and store._MEMORY.sessions == {}


def test_missing_database_identity_does_not_fall_back_to_memory(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: Settings(cabinet_require_persistent_storage=True))
    monkeypatch.setattr(store, "_run_db", lambda _: None)
    monkeypatch.setattr(store, "_ensure_defaults", lambda *_: pytest.fail("Unexpected memory fallback"))
    assert store.get_user_auth_record_by_email("absent@example.test") is None
    assert store.resolve_active_session(session_id="absent", user_id="absent") is None
    assert store.resolve_refresh_session_by_token_hash("absent") is None


def test_failed_storage_operation_cannot_fall_through_to_memory(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: Settings(cabinet_require_persistent_storage=True))
    monkeypatch.setattr(store, "get_engine", lambda: (_ for _ in ()).throw(SQLAlchemyError("synthetic outage")))
    with pytest.raises(HTTPException) as error:
        store._run_db(lambda _: pytest.fail("Unexpected database operation"))
    assert error.value.status_code == 503


def test_preview_persistent_setting_is_loaded(monkeypatch):
    monkeypatch.setenv("VELLA_CABINET_REQUIRE_PERSISTENT_STORAGE", "true")
    assert get_settings().cabinet_require_persistent_storage is True


def test_missing_preview_database_is_not_created(tmp_path):
    database = tmp_path / "preview.sqlite"
    with pytest.raises(RuntimeError, match="missing"):
        require_existing_preview_database(database)
    assert not database.exists()
    require_existing_preview_database(database, initialize=True)
    assert not database.exists()


def test_report_read_does_not_block_account_or_checkpoint_commit(tmp_path):
    from sqlalchemy import create_engine

    database = tmp_path / "preview.sqlite"
    engine = create_engine(f"sqlite:///{database}")
    configure_preview_sqlite_storage(database, engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE synthetic_state (id INTEGER PRIMARY KEY, value TEXT)")
            connection.exec_driver_sql("INSERT INTO synthetic_state VALUES (1, 'saved account')")
        with engine.connect() as reader, engine.connect() as writer:
            assert reader.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert reader.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
            assert writer.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
            # A real SQLite read transaction, as in a streamed report query.
            reader.exec_driver_sql("BEGIN")
            assert reader.exec_driver_sql("SELECT value FROM synthetic_state WHERE id=1").scalar_one() == "saved account"
            writer.exec_driver_sql("INSERT INTO synthetic_state VALUES (2, 'report checkpoint')")
            writer.commit()  # DELETE journal mode raises 'database is locked'.
            assert reader.exec_driver_sql("SELECT count(*) FROM synthetic_state").scalar_one() == 1
            reader.rollback()
            assert reader.exec_driver_sql("SELECT count(*) FROM synthetic_state").scalar_one() == 2
            assert reader.exec_driver_sql("SELECT value FROM synthetic_state WHERE id=1").scalar_one() == "saved account"
    finally:
        engine.dispose()


def test_preview_sqlite_configuration_rejects_other_database(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'different.sqlite'}")
    try:
        with pytest.raises(RuntimeError, match="does not match"):
            configure_preview_sqlite_storage(tmp_path / "preview.sqlite", engine)
        assert not (tmp_path / "preview.sqlite").exists()
        assert not (tmp_path / "different.sqlite").exists()
    finally:
        engine.dispose()


def test_full_launcher_refuses_missing_database_before_starting_services(tmp_path, monkeypatch):
    from ops import run_local_project as startup
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "ops"))
    monkeypatch.setattr(startup, "ROOT", tmp_path)
    monkeypatch.setattr(startup, "require_free_ports", lambda _: pytest.fail("Unexpected service startup"))
    with pytest.raises(RuntimeError, match="missing"):
        startup.main()
    assert not (tmp_path / ".local-preview/preview.sqlite").exists()


def test_invalid_or_empty_preview_database_is_not_bootstrapped(tmp_path):
    database = tmp_path / "preview.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(RuntimeError, match="account schema"):
        require_existing_preview_database(database, initialize=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE lk_users (user_id TEXT)")
    with pytest.raises(RuntimeError, match="empty"):
        require_existing_preview_database(database)
    require_existing_preview_database(database, initialize=True)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO lk_users VALUES ('synthetic')")
    require_existing_preview_database(database)


def test_account_survives_new_process_and_auth_signing_key(tmp_path):
    environment = os.environ.copy()
    environment.update(VELLA_ENV="local", VELLA_WB_LIVE_SYNC_ENABLED="false",
                       VELLA_DATABASE_URL=f"sqlite+pysqlite:///{tmp_path / 'accounts.sqlite'}",
                       VELLA_CABINET_REQUIRE_PERSISTENT_STORAGE="true")
    bootstrap = """
from app.cabinet import store
from app.control_plane.auth import hash_password, login_with_email_password
from app.infra.db import get_engine, create_all_for_local_dev
# Match the existing local runtime's SQLite timezone adapter (PostgreSQL is aware).
from app.infra.models import Base
from datetime import timezone
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator
class UtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True
    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value
for table in Base.metadata.tables.values():
    for column in table.columns:
        if isinstance(column.type, DateTime) and column.type.timezone:
            column.type = UtcDateTime(timezone=True)
create_all_for_local_dev()
"""
    register = """
store.register_organization_owner(email='synthetic@example.test',
    password_hash=hash_password('Synthetic-only-test!'), full_name='Synthetic', company_name='Synthetic')
assert store.get_user_auth_record_by_email('synthetic@example.test') is not None
"""
    login = """
tokens = login_with_email_password(email='synthetic@example.test', password='Synthetic-only-test!',
    user_agent=None, ip_address=None)
assert tokens.access_token and tokens.refresh_token
assert not store._MEMORY.users
"""
    for index, action in enumerate((register, login)):
        environment["VELLA_AUTH_SECRET"] = f"synthetic-process-signing-key-{index}-not-a-real-secret"
        result = subprocess.run([sys.executable, "-c", bootstrap + action], env=environment,
                                cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, "Synthetic account persistence regression failed: " + result.stderr
