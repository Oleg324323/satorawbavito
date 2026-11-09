from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from tests import test_orders_schema_candidate as candidate

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROLE = "satorna_credential_runtime_" + uuid4().hex
cluster = candidate.cluster


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _bootstrap_postgres(
    owner_engine: Engine, owner_url: str, *, create_runtime_role: bool
) -> None:
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE lk_organizations (
                    organization_id INTEGER PRIMARY KEY,
                    slug VARCHAR(64) NOT NULL UNIQUE,
                    name VARCHAR(255) NOT NULL
                );
                CREATE TABLE marketplace_accounts (
                    marketplace_account_id INTEGER PRIMARY KEY,
                    organization_id INTEGER NOT NULL REFERENCES lk_organizations(organization_id),
                    marketplace VARCHAR(16) NOT NULL,
                    external_account_id VARCHAR(128) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    credential_ref VARCHAR(255),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_marketplace_accounts_org_id UNIQUE (organization_id, marketplace_account_id),
                    CONSTRAINT uq_marketplace_accounts_org_marketplace_external
                        UNIQUE (organization_id, marketplace, external_account_id)
                );
                """
            )
        )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    with patch.dict(os.environ, {"VELLA_DATABASE_URL": owner_url}):
        command.stamp(config, "20260905_0060")
        command.upgrade(config, "20260908_0061")
        command.downgrade(config, "20260905_0060")
        command.upgrade(config, "20260908_0061")
    quoted_role = owner_engine.dialect.identifier_preparer.quote(RUNTIME_ROLE)
    with owner_engine.begin() as connection:
        if create_runtime_role:
            connection.execute(
                text(
                    f"CREATE ROLE {quoted_role} LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted_role}"))
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted_role}"
            )
        )
        connection.execute(
            text("INSERT INTO lk_organizations VALUES (1, 'one', 'One'), (2, 'two', 'Two')")
        )
        connection.execute(
            text(
                """
                INSERT INTO marketplace_accounts
                    (marketplace_account_id, organization_id, marketplace, external_account_id, status)
                VALUES (101, 1, 'wb', 'wb-one', 'connected'),
                       (202, 2, 'avito', 'avito-two', 'connected')
                """
            )
        )


def _native_postgres(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, str | None]]:
    required = ("initdb", "pg_ctl", "createdb")
    binaries = {name: shutil.which(name) for name in required}
    if any(path is None for path in binaries.values()):
        pytest.fail("disposable PostgreSQL 16 binaries are required for forced-RLS tests")
    root = tmp_path_factory.mktemp("marketplace-credential-postgres")
    data = root / "data"
    log = root / "postgres.log"
    port = _free_loopback_port()
    owner = "satorna_credential_test_owner"
    database = "satorna_credential_test"
    subprocess.run(
        [binaries["initdb"], "-D", str(data), "-A", "trust", "-U", owner, "--no-locale", "-E", "UTF8"],
        check=True,
        capture_output=True,
        text=True,
    )
    options = f"-p {port} -h 127.0.0.1 -k /tmp -c fsync=off -c synchronous_commit=off"
    startup_completed = False
    try:
        subprocess.run(
            [binaries["pg_ctl"], "-D", str(data), "-l", str(log), "-o", options, "-w", "start"],
            check=True,
            capture_output=True,
            text=True,
        )
        startup_completed = True
        subprocess.run(
            [binaries["createdb"], "-h", "127.0.0.1", "-p", str(port), "-U", owner, database],
            check=True,
            capture_output=True,
            text=True,
        )
        owner_url = f"postgresql+psycopg://{owner}@127.0.0.1:{port}/{database}"
        owner_engine = create_engine(owner_url, hide_parameters=True)
        try:
            _bootstrap_postgres(owner_engine, owner_url, create_runtime_role=True)
            runtime_url = owner_engine.url.set(username=RUNTIME_ROLE).render_as_string(
                hide_password=False
            )
            yield {
                "mode": "native",
                "owner": owner_url,
                "runtime": runtime_url,
                "database": database,
                "runtime_role": RUNTIME_ROLE,
                "host": "127.0.0.1",
                "root": str(root),
                "port": str(port),
            }
        finally:
            owner_engine.dispose()
    finally:
        try:
            subprocess.run(
                [binaries["pg_ctl"], "-D", str(data), "-m", "fast", "-w", "stop"],
                check=startup_completed,
                capture_output=True,
                text=True,
            )
        except OSError:
            if startup_completed:
                raise


def _local_postgres(cluster) -> Iterator[dict[str, str | None]]:
    with candidate.disposable_database(cluster, (RUNTIME_ROLE,)) as database:
        owner_engine = create_engine(database.url, hide_parameters=True)
        try:
            _bootstrap_postgres(
                owner_engine, database.url, create_runtime_role=False
            )
            runtime_url = owner_engine.url.set(username=RUNTIME_ROLE)
            yield {
                "mode": "local",
                "owner": database.url,
                "runtime": runtime_url.render_as_string(hide_password=False),
                "database": database.name,
                "runtime_role": RUNTIME_ROLE,
                "host": database.host,
                "port": str(database.port),
                "root": None,
            }
        finally:
            owner_engine.dispose()


@pytest.fixture(scope="module")
def disposable_postgres(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[dict[str, str | None]]:
    if os.environ.get("ORDERS_TEST_USE_LOCAL_CLUSTER") == "1":
        yield from _local_postgres(request.getfixturevalue("cluster"))
    else:
        yield from _native_postgres(tmp_path_factory)


def _credential_values(*, credential_id: str, organization_id: int, account_id: int, provider: str) -> dict[str, object]:
    return {
        "credential_id": credential_id,
        "organization_id": organization_id,
        "marketplace_account_id": account_id,
        "provider": provider,
        "credential_kind": "wb_api" if provider == "wb" else "avito_oauth_client",
        "algorithm": "AES-256-GCM",
        "key_version": 1,
        "aad_version": 1,
        "payload_schema_version": 1,
        "nonce": b"n" * 12,
        "ciphertext": b"c" * 32,
        "generation": 1,
    }


INSERT_CREDENTIAL = text(
    """
    INSERT INTO marketplace_account_credentials
        (credential_id, organization_id, marketplace_account_id, provider,
         credential_kind, algorithm, key_version, aad_version,
         payload_schema_version, nonce, ciphertext, generation)
    VALUES
        (:credential_id, :organization_id, :marketplace_account_id, :provider,
         :credential_kind, :algorithm, :key_version, :aad_version,
         :payload_schema_version, :nonce, :ciphertext, :generation)
    """
)


def test_postgres_fixture_has_actual_owned_origin(disposable_postgres, tmp_path_factory) -> None:
    runtime_url = make_url(disposable_postgres["runtime"])
    assert runtime_url.database == disposable_postgres["database"]
    assert runtime_url.username == disposable_postgres["runtime_role"]
    if disposable_postgres["mode"] == "local":
        assert disposable_postgres["root"] is None
        assert runtime_url.query["host"] in {"/tmp", "/private/tmp"}
        assert disposable_postgres["host"] == runtime_url.query["host"]
        assert re.fullmatch(r"orders_test_[0-9a-f]{32}", disposable_postgres["database"])
        assert re.fullmatch(
            r"satorna_credential_runtime_[0-9a-f]{32}",
            disposable_postgres["runtime_role"],
        )
        assert len(disposable_postgres["runtime_role"]) <= 63
        engine = create_engine(disposable_postgres["runtime"])
        try:
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT inet_server_addr() IS NULL, current_database(), current_user"
                    )
                ).one() == (
                    True,
                    disposable_postgres["database"],
                    disposable_postgres["runtime_role"],
                )
        finally:
            engine.dispose()
    else:
        assert Path(disposable_postgres["root"]).resolve().parent == tmp_path_factory.getbasetemp().resolve()
        assert runtime_url.host == "127.0.0.1"
        assert disposable_postgres["port"] != "5432"


def test_migration_cycle_leaves_forced_rls_and_non_bypass_runtime(disposable_postgres) -> None:
    engine = create_engine(disposable_postgres["owner"])
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20260908_0061"
        )
        rows = connection.execute(
            text(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname IN ('marketplace_account_credentials', 'marketplace_account_ingestion_tokens')
                ORDER BY relname
                """
            )
        ).all()
        assert rows == [
            ("marketplace_account_credentials", True, True),
            ("marketplace_account_ingestion_tokens", True, True),
        ]
        attributes = connection.execute(
            text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :role"),
            {"role": disposable_postgres["runtime_role"]},
        ).one()
        assert attributes == (False, False, False, False, False)
    engine.dispose()


def test_no_context_and_wrong_org_cannot_read_update_or_delete(disposable_postgres) -> None:
    owner = create_engine(disposable_postgres["owner"])
    runtime = create_engine(disposable_postgres["runtime"])
    values = _credential_values(
        credential_id="10000000-0000-4000-8000-000000000001",
        organization_id=1,
        account_id=101,
        provider="wb",
    )
    with owner.begin() as connection:
        connection.execute(INSERT_CREDENTIAL, values)
    with runtime.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_credentials")) == 0
        assert connection.execute(text("UPDATE marketplace_account_credentials SET generation = generation + 1")).rowcount == 0
        assert connection.execute(text("DELETE FROM marketplace_account_credentials")).rowcount == 0
    no_context = _credential_values(
        credential_id="15000000-0000-4000-8000-000000000001",
        organization_id=1,
        account_id=101,
        provider="wb",
    )
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(INSERT_CREDENTIAL, no_context)
    with runtime.begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id', '2', true)"))
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_credentials")) == 0
        assert connection.execute(text("UPDATE marketplace_account_credentials SET generation = 2")).rowcount == 0
        assert connection.execute(text("DELETE FROM marketplace_account_credentials")).rowcount == 0
    with runtime.begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id', '1', true)"))
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_credentials")) == 1
    owner.dispose()
    runtime.dispose()


def test_wrong_org_insert_and_cross_tenant_update_are_denied(disposable_postgres) -> None:
    runtime = create_engine(disposable_postgres["runtime"])
    wrong = _credential_values(
        credential_id="20000000-0000-4000-8000-000000000002",
        organization_id=1,
        account_id=101,
        provider="wb",
    )
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(text("SELECT set_config('app.organization_id', '2', true)"))
            connection.execute(INSERT_CREDENTIAL, wrong)
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(text("SELECT set_config('app.organization_id', '1', true)"))
            connection.execute(
                text("UPDATE marketplace_account_credentials SET organization_id = 2 WHERE credential_id = '10000000-0000-4000-8000-000000000001'")
            )
    runtime.dispose()


def test_provider_fk_and_one_active_constraint_are_enforced(disposable_postgres) -> None:
    owner = create_engine(disposable_postgres["owner"])
    mismatch = _credential_values(
        credential_id="30000000-0000-4000-8000-000000000003",
        organization_id=1,
        account_id=101,
        provider="avito",
    )
    with pytest.raises(IntegrityError):
        with owner.begin() as connection:
            connection.execute(INSERT_CREDENTIAL, mismatch)
    duplicate = _credential_values(
        credential_id="40000000-0000-4000-8000-000000000004",
        organization_id=1,
        account_id=101,
        provider="wb",
    )
    with pytest.raises(IntegrityError):
        with owner.begin() as connection:
            connection.execute(INSERT_CREDENTIAL, duplicate)
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_account_credentials SET revoked_at = CURRENT_TIMESTAMP, revocation_reason_code = 'credential_replaced' WHERE credential_id = '10000000-0000-4000-8000-000000000001'"
            )
        )
        connection.execute(INSERT_CREDENTIAL, duplicate)
    owner.dispose()


def test_ingestion_tokens_are_tenant_scoped(disposable_postgres) -> None:
    owner = create_engine(disposable_postgres["owner"])
    runtime = create_engine(disposable_postgres["runtime"])
    with owner.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO marketplace_account_ingestion_tokens
                    (token_id, organization_id, marketplace_account_id, provider,
                     verifier, scope, issued_at, expires_at)
                VALUES ('50000000-0000-4000-8000-000000000005', 2, 202, 'avito',
                        :verifier, 'avito.browser_snapshot.write', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP + INTERVAL '1 hour')
                """
            ),
            {"verifier": b"v" * 32},
        )
    with runtime.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_ingestion_tokens")) == 0
        assert connection.execute(text("UPDATE marketplace_account_ingestion_tokens SET last_used_at = CURRENT_TIMESTAMP")).rowcount == 0
        assert connection.execute(text("DELETE FROM marketplace_account_ingestion_tokens")).rowcount == 0
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO marketplace_account_ingestion_tokens
                        (token_id, organization_id, marketplace_account_id, provider,
                         verifier, scope, issued_at, expires_at)
                    VALUES ('55000000-0000-4000-8000-000000000005', 2, 202, 'avito',
                            :verifier, 'avito.browser_snapshot.write', CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP + INTERVAL '1 hour')
                    """
                ),
                {"verifier": b"w" * 32},
            )
    with runtime.begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id', '1', true)"))
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_ingestion_tokens")) == 0
        assert connection.execute(text("UPDATE marketplace_account_ingestion_tokens SET last_used_at = CURRENT_TIMESTAMP")).rowcount == 0
        assert connection.execute(text("DELETE FROM marketplace_account_ingestion_tokens")).rowcount == 0
    with pytest.raises(DBAPIError):
        with runtime.begin() as connection:
            connection.execute(text("SELECT set_config('app.organization_id', '1', true)"))
            connection.execute(
                text(
                    """
                    INSERT INTO marketplace_account_ingestion_tokens
                        (token_id, organization_id, marketplace_account_id, provider,
                         verifier, scope, issued_at, expires_at)
                    VALUES ('56000000-0000-4000-8000-000000000005', 2, 202, 'avito',
                            :verifier, 'avito.browser_snapshot.write', CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP + INTERVAL '1 hour')
                    """
                ),
                {"verifier": b"x" * 32},
            )
    with runtime.begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id', '2', true)"))
        assert connection.scalar(text("SELECT count(*) FROM marketplace_account_ingestion_tokens")) == 1
    owner.dispose()
    runtime.dispose()
