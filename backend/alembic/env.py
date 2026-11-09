from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

# Allow `alembic upgrade` without an editable install when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.account_health import orm as _account_health_models  # noqa: F401
from app.avito import returns_orm as _avito_returns_models  # noqa: F401
from app.avito import labels_orm as _avito_labels_models  # noqa: F401
from app.avito import images_orm as _avito_images_models  # noqa: F401
from app.cabinet import orm as _cabinet_models  # noqa: F401
from app.cash_flow import orm as _cash_flow_models  # noqa: F401
from app.config import get_settings
from app.control_plane import orm as _control_plane_models  # noqa: F401
from app.infra.models import Base
from app.platform.advertising import orm as _advertising_models  # noqa: F401
from app.platform.catalog import orm as _catalog_models  # noqa: F401
from app.platform.economics import orm as _economics_models  # noqa: F401
from app.platform.finance import orm as _finance_models  # noqa: F401
from app.platform.funnel import orm as _funnel_models  # noqa: F401
from app.platform.identity import orm as _identity_models  # noqa: F401
from app.platform.integrations import orm as _integration_models  # noqa: F401
from app.repricer_cache import orm as _repricer_cache_models  # noqa: F401
from app.repricer_persistence import orm as _repricer_persistence_models  # noqa: F401
from app.report_rules import orm as _report_rules_models  # noqa: F401
from app.source_registry import orm as _source_registry_models  # noqa: F401
from app.wb_ads_cache import orm as _wb_ads_cache_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# ConfigParser consumes doubled percent signs; SQLAlchemy must receive the
# original URI escapes (including percent-encoded Unix socket paths).
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
