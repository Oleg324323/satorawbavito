"""Importing canonical consumers must not mutate the legacy bootstrap schema."""

from importlib import import_module

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from app.infra.models import Base, InfraRuntimeState


def test_review_consumer_imports_preserve_legacy_sqlite_bootstrap():
    # These are the real collection/runtime import paths, not stand-in mappings.
    for module in (
        "app.reviews.canonical_repository",
        "app.reviews.local_service",
        "app.reviews.send_service",
        "app.notification_service",
        "app.review_notifications_http",
    ):
        import_module(module)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(InfraRuntimeState.__table__.insert().values(key="synthetic", value="preserved"))
            assert connection.scalar(select(InfraRuntimeState.value)) == "preserved"
        installed = set(inspect(engine).get_table_names())
        assert not installed.intersection({
            "review_sync_runs_v2", "review_facts", "review_observations", "review_sync_run_items",
        })
    finally:
        engine.dispose()


def test_review_repository_retains_postgres_tables_and_jsonb_operators():
    from app.reviews import canonical_repository as repository
    from app.reviews.canonical_orm import CanonicalReviewRunRow

    assert repository.RUN is CanonicalReviewRunRow.__table__
    assert isinstance(repository.RUN.c.coverage.type, JSONB)
    # Compile the actual PostgreSQL containment operation, never coerce JSONB
    # to SQLite JSON/string or change production storage to repair a fixture.
    query = select(repository.RUN.c.sync_run_id).where(
        repository.RUN.c.coverage.contains({"synthetic": True}),
    )
    compiled = query.compile(dialect=postgresql.dialect())
    assert " @> " in str(compiled)
    assert "::JSONB" in str(compiled)
    assert list(compiled.params.values()) == [{"synthetic": True}]
