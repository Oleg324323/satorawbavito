"""Real PostgreSQL regression tests; never consume an inherited database URL."""

import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from ops.release_gate import run_migration_roundtrip, safe_environment
from tests import test_orders_schema_candidate as candidate

ROOT = Path(__file__).resolve().parents[1]


cluster = candidate.cluster


@pytest.fixture
def database(cluster, monkeypatch):
    with candidate.disposable_database(cluster) as allocated:
        monkeypatch.setenv("VELLA_DATABASE_URL", allocated.url)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", allocated.url)
        engine = create_engine(allocated.url)
        try:
            yield config, engine
        finally:
            engine.dispose()


def prompt_column(engine):
    return next(column for column in inspect(engine).get_columns("rv_review_sync_settings")
                if column["name"] == "ai_prompt")


def insert_settings(engine, prompt="Operator prompt — preserve me"):
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO rv_review_sync_settings
                (organization_id, enabled, interval_minutes, token_type,
                 unanswered_only, take, lookback_days, ai_prompt)
            VALUES (1, false, 60, 'personal', true, 10, 7, :prompt)
        """), {"prompt": prompt})


def read_prompt(engine):
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT ai_prompt FROM rv_review_sync_settings WHERE organization_id = 1"
        )).scalar_one()


def test_empty_database_upgrades_to_head_without_stamp(database):
    config, engine = database
    completed = []
    environment = safe_environment()
    # Preserve the allocator URL: SQLAlchemy's rendering percent-encodes the Unix
    # socket path, which ConfigParser would interpret rather than pass to psycopg.
    environment["VELLA_DATABASE_URL"] = config.get_main_option("sqlalchemy.url")
    run_migration_roundtrip(sys.executable, environment, completed)
    assert completed == ["migration_upgrade_head", "migration_downgrade_one", "migration_reupgrade_head"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            ScriptDirectory.from_config(config).get_current_head()
        )
    column = prompt_column(engine)
    assert column["nullable"] is False
    assert column["default"] == "''::text"


def test_0019_roundtrip_preserves_column_owned_by_0017_and_prompt(database):
    config, engine = database
    command.upgrade(config, "20260717_0018")
    insert_settings(engine)
    before = prompt_column(engine)
    command.upgrade(config, "20260717_0019")
    command.downgrade(config, "20260717_0018")
    assert read_prompt(engine) == "Operator prompt — preserve me"
    assert str(prompt_column(engine)["type"]) == str(before["type"])
    assert prompt_column(engine)["nullable"] == before["nullable"]
    assert prompt_column(engine)["default"] == before["default"]
    command.upgrade(config, "20260717_0019")
    assert read_prompt(engine) == "Operator prompt — preserve me"


def test_0019_repairs_legacy_schema_without_prompt(database):
    config, engine = database
    command.upgrade(config, "20260717_0018")
    insert_settings(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE rv_review_sync_settings DROP COLUMN ai_prompt"))
    command.upgrade(config, "20260717_0019")
    assert read_prompt(engine) == ""
    assert prompt_column(engine)["nullable"] is False
    command.downgrade(config, "20260717_0018")
    assert read_prompt(engine) == ""


def test_0019_downgrade_of_already_stamped_database_preserves_prompt(database):
    config, engine = database
    command.upgrade(config, "20260717_0018")
    insert_settings(engine)
    # Represents an existing deployment; only this compatibility test stamps.
    command.stamp(config, "20260717_0019")
    command.downgrade(config, "20260717_0018")
    assert read_prompt(engine) == "Operator prompt — preserve me"


def test_0019_rejects_incompatible_existing_column_without_changing_data(database):
    config, engine = database
    command.upgrade(config, "20260717_0018")
    insert_settings(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE rv_review_sync_settings ALTER COLUMN ai_prompt DROP NOT NULL"))
    with pytest.raises(DBAPIError, match="incompatible rv_review_sync_settings.ai_prompt schema"):
        command.upgrade(config, "20260717_0019")
    assert read_prompt(engine) == "Operator prompt — preserve me"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260717_0018"
