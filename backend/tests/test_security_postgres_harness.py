from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, text

from tests import test_avito_ingestion_token_store as ingestion
from tests import test_marketplace_credential_rls as credential
from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster


@pytest.mark.parametrize("module", [credential, ingestion], ids=["credential", "ingestion"])
@pytest.mark.parametrize(
    ("flag", "expected_route"),
    [(None, "native"), ("0", "native"), ("true", "native"), ("1", "local")],
)
@pytest.mark.parametrize("completion", ["close", "error"])
def test_disposable_postgres_routes_exactly_once_and_finalizes_selected_generator(
    module, flag, expected_route, completion, monkeypatch
) -> None:
    events: list[object] = []
    cluster = object()
    tmp_path_factory = object()

    def sentinel(route: str, argument: object) -> Iterator[dict[str, str]]:
        events.append(("start", route, argument))
        try:
            yield {"mode": route}
            if completion == "error":
                raise RuntimeError(f"{route} sentinel failure")
        finally:
            events.append(("finally", route))

    monkeypatch.setattr(
        module,
        "_native_postgres",
        lambda argument: sentinel("native", argument),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_local_postgres",
        lambda argument: sentinel("local", argument),
        raising=False,
    )
    if flag is None:
        monkeypatch.delenv("ORDERS_TEST_USE_LOCAL_CLUSTER", raising=False)
    else:
        monkeypatch.setenv("ORDERS_TEST_USE_LOCAL_CLUSTER", flag)

    requested: list[str] = []

    def getfixturevalue(name: str) -> object:
        requested.append(name)
        assert name == "cluster"
        return cluster

    request = SimpleNamespace(getfixturevalue=getfixturevalue)
    fixture_body = module.disposable_postgres.__wrapped__(tmp_path_factory, request)
    assert next(fixture_body) == {"mode": expected_route}
    assert events == [
        (
            "start",
            expected_route,
            cluster if expected_route == "local" else tmp_path_factory,
        )
    ]
    assert requested == (["cluster"] if expected_route == "local" else [])

    if completion == "close":
        fixture_body.close()
    else:
        with pytest.raises(RuntimeError, match=f"{expected_route} sentinel failure"):
            next(fixture_body)

    assert events == [
        (
            "start",
            expected_route,
            cluster if expected_route == "local" else tmp_path_factory,
        ),
        ("finally", expected_route),
    ]


def test_fresh_unstamped_database_upgrades_to_0061(cluster) -> None:
    with candidate.disposable_database(cluster) as database:
        engine = create_engine(database.url, hide_parameters=True)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("SELECT to_regclass('public.alembic_version')")
                ) is None
            result = candidate.migrate(
                database.url, "upgrade", "20260908_0061"
            )
            assert result.returncode == 0, result.stderr
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20260908_0061"
                )
                assert connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('marketplace_account_credentials', "
                        "'marketplace_account_ingestion_tokens') ORDER BY relname"
                    )
                ).all() == [
                    ("marketplace_account_credentials", True, True),
                    ("marketplace_account_ingestion_tokens", True, True),
                ]
        finally:
            engine.dispose()


@pytest.mark.parametrize("module", [credential, ingestion], ids=["credential", "ingestion"])
def test_local_bootstrap_failure_disposes_engine_and_removes_owned_resources(
    module, cluster, monkeypatch, capsys
) -> None:
    allocation: dict[str, object] = {}
    real_disposable_database = candidate.disposable_database
    real_create_engine = module.create_engine
    dispose_calls: list[str] = []

    @contextmanager
    def tracked_disposable_database(cluster_value, roles=()):
        with real_disposable_database(cluster_value, roles) as database:
            allocation["database"] = database
            allocation["roles"] = roles
            yield database

    def tracked_create_engine(url, **kwargs):
        engine = real_create_engine(url, **kwargs)
        original_dispose = engine.dispose

        def dispose(*, close: bool = True) -> None:
            dispose_calls.append(str(engine.url.database))
            original_dispose(close=close)

        monkeypatch.setattr(engine, "dispose", dispose)
        return engine

    def fail_bootstrap(*_args, **_kwargs) -> None:
        raise RuntimeError("synthetic bootstrap failure")

    monkeypatch.setattr(candidate, "disposable_database", tracked_disposable_database)
    monkeypatch.setattr(module, "create_engine", tracked_create_engine)
    monkeypatch.setattr(module, "_bootstrap_postgres", fail_bootstrap)

    fixture_body = module._local_postgres(cluster)
    with pytest.raises(RuntimeError, match="synthetic bootstrap failure"):
        next(fixture_body)

    database = allocation["database"]
    assert allocation["roles"] == (module.RUNTIME_ROLE,)
    assert re.fullmatch(r"orders_test_[0-9a-f]{32}", database.name)
    assert len(module.RUNTIME_ROLE) <= 63
    assert dispose_calls == [database.name]
    cleanup_output = capsys.readouterr().out
    assert (
        f"Orders cleanup verified: database={database.name}; roles={module.RUNTIME_ROLE}"
        in cleanup_output
    )
    print(cleanup_output, end="")

    _, root, port, owner = cluster
    host = "/tmp" if root is None else str(root)
    with psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        user=owner,
        autocommit=True,
        passfile="/dev/null",
    ) as admin:
        assert not admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database.name,)
        ).fetchone()
        assert not admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (module.RUNTIME_ROLE,)
        ).fetchone()
