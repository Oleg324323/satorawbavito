from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.cabinet.orm import LkOrganizationRow, LkUserRow, LkUserWbTokenRow
from app.infra.models import Base
from app.platform.advertising import raw_backfill as raw_backfill_module
from app.platform.advertising.orm import (
    WbAdvertisingCampaignSnapshotRow,
    WbAdvertisingFactRow,
    WbAdvertisingSpendDocumentRow,
    WbAdvertisingSyncRunRow,
)
from app.platform.advertising.raw import AdvertisingNormalizationError
from app.platform.advertising.raw_backfill import (
    RawAdvertisingBackfillError,
    backfill_raw_advertising,
)
from app.platform.advertising.service import AdvertisingService, AdvertisingSnapshot
from app.platform.catalog.orm import (
    CatalogSkuRow,
    MarketplaceOfferRow,
    MarketplaceProductRow,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period
from app.wb_api.raw_advertising import RawAdvertisingFetch, RawAdvertisingFetchError

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
INTERMEDIATE = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 1, 13, tzinfo=timezone.utc)
CLOSED = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
PERIOD = Period(date(2026, 9, 1), date(2026, 9, 1))
MANIFEST_CHECKSUM = "1ae3320b923d3ca749baa2f9440b2fba91b7a886554493dc4f2df4024c0bf13c"
TABLES = (
    WbAdvertisingSyncRunRow,
    WbAdvertisingFactRow,
    WbAdvertisingCampaignSnapshotRow,
    WbAdvertisingSpendDocumentRow,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all(
            [
                LkOrganizationRow(organization_id=1, slug="one", name="One"),
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                MarketplaceAccountRow(
                    marketplace_account_id=31,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id="shared-wb-account",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=32,
                    organization_id=2,
                    marketplace="wb",
                    external_account_id="shared-wb-account",
                    status="connected",
                ),
            ]
        )
        db.commit()
        yield db


@pytest.fixture
def raw_bundle() -> dict[str, object]:
    return json.loads(
        (
            Path(__file__).parent / "fixtures/wb_advertising_raw_sanitized.json"
        ).read_text()
    )


def _counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(table)) or 0 for table in TABLES
    )


def _map_nm(session: Session, nm_id: int = 2001) -> None:
    session.add_all(
        [
            CatalogSkuRow(
                catalog_sku_id=11,
                organization_id=1,
                code=f"SKU-{nm_id}",
            ),
            MarketplaceProductRow(
                marketplace_product_id=1011,
                organization_id=1,
                marketplace_account_id=31,
                external_product_id=str(nm_id),
            ),
        ]
    )
    session.flush()
    session.add(
        MarketplaceOfferRow(
            marketplace_offer_id=2011,
            organization_id=1,
            marketplace_account_id=31,
            marketplace_product_id=1011,
            external_offer_key=f"offer-{nm_id}",
            catalog_sku_id=11,
        )
    )
    session.commit()


def test_legacy_snapshot_constructor_keeps_raw_fields_optional() -> None:
    snapshot = AdvertisingSnapshot(
        sync_run_id="legacy",
        marketplace_account_id=31,
        source_kind="ads_fullstats",
        period=PERIOD,
        snapshot_checksum="0" * 64,
        formula_version="wb-advertising-v1",
        source_total_spend_kopecks=0,
        fact_count=0,
        evidence_status="derived_legacy",
        captured_at=NOW,
        last_observed_at=NOW,
    )

    assert snapshot.campaign_count == 0
    assert snapshot.spend_document_count == 0
    assert snapshot.document_total_spend_kopecks is None
    assert snapshot.expected_request_count is None
    assert snapshot.completed_request_count is None


def test_raw_ingest_is_atomic_idempotent_and_parent_linked(
    session: Session, raw_bundle
) -> None:
    service = AdvertisingService(session, 1, now=lambda: NOW)
    first = service.ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )
    replay_bundle = copy.deepcopy(raw_bundle)
    for index, item in enumerate(replay_bundle["manifest"]):
        item["wbRequestId"] = f"replay-{index}"
    replay_bundle["manifest"].reverse()
    replay = service.ingest_raw_payload(
        31,
        PERIOD,
        replay_bundle,
        source_reference="wb_api:ignored-on-replay",
        observed_at=LATER,
    )

    assert replay.sync_run_id == first.sync_run_id
    assert replay.last_observed_at == LATER
    assert _counts(session) == (1, 6, 2, 2)
    first_row = session.get(WbAdvertisingSyncRunRow, first.sync_run_id)
    assert first_row.source_reference == "wb_api:probe"
    assert replay.captured_at == NOW
    assert first_row.raw_manifest_checksum == MANIFEST_CHECKSUM
    assert first_row.expected_request_count == 4
    assert first_row.completed_request_count == 4
    assert first_row.raw_manifest == sorted(
        raw_bundle["manifest"],
        key=lambda item: json.dumps(item, separators=(",", ":"), sort_keys=True),
    )
    assert (
        first.fact_count,
        first.campaign_count,
        first.spend_document_count,
        first.source_total_spend_kopecks,
        first.document_total_spend_kopecks,
        first.expected_request_count,
        first.completed_request_count,
    ) == (6, 2, 2, 200, 200, 4, 4)

    changed = copy.deepcopy(raw_bundle)
    changed["fullstats"][0]["payload"][1]["sum"] = 0.6
    changed["fullstats"][0]["payload"][1]["days"][0]["sum"] = 0.6
    second = service.ingest_raw_payload(
        31,
        PERIOD,
        changed,
        source_reference="wb_api:probe",
        observed_at=LATER,
    )

    second_row = session.get(WbAdvertisingSyncRunRow, second.sync_run_id)
    assert second.sync_run_id != first.sync_run_id
    assert second_row.parent_sync_run_id == first.sync_run_id
    assert second.source_total_spend_kopecks == 210
    assert second.document_total_spend_kopecks == 200
    assert _counts(session) == (2, 12, 4, 4)


def test_raw_ingest_reconciles_only_campaign_period_facts(
    session: Session, raw_bundle
) -> None:
    snapshot = AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )

    all_levels = session.scalar(
        select(func.sum(WbAdvertisingFactRow.spend_kopecks)).where(
            WbAdvertisingFactRow.sync_run_id == snapshot.sync_run_id
        )
    )
    campaign_period = session.scalar(
        select(func.sum(WbAdvertisingFactRow.spend_kopecks)).where(
            WbAdvertisingFactRow.sync_run_id == snapshot.sync_run_id,
            WbAdvertisingFactRow.fact_scope == "campaign",
            WbAdvertisingFactRow.grain == "period",
        )
    )

    assert all_levels == 550
    assert campaign_period == snapshot.source_total_spend_kopecks == 200


def test_raw_ingest_stores_day_bounds_inside_a_multi_day_period(
    session: Session, raw_bundle
) -> None:
    period = Period(date(2026, 9, 1), date(2026, 9, 2))
    raw_bundle["period"]["dateTo"] = "2026-09-02"
    raw_bundle["fullstats"][0]["dateTo"] = "2026-09-02"
    raw_bundle["upd"][0]["dateTo"] = "2026-09-02"

    snapshot = AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31, period, raw_bundle, source_reference="wb_api:probe"
    )
    facts = session.scalars(
        select(WbAdvertisingFactRow).where(
            WbAdvertisingFactRow.sync_run_id == snapshot.sync_run_id
        )
    ).all()

    assert all(
        fact.date_from == fact.business_date == fact.date_to
        for fact in facts
        if fact.grain == "day"
    )
    assert all(
        (fact.date_from, fact.date_to) == (period.date_from, period.date_to)
        for fact in facts
        if fact.grain == "period"
    )


def test_raw_ingest_persists_signed_upd_document_total(
    session: Session, raw_bundle
) -> None:
    raw_bundle["upd"][0]["payload"][1]["updSum"] = -2

    snapshot = AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )

    assert snapshot.document_total_spend_kopecks == -50
    assert session.scalars(
        select(WbAdvertisingSpendDocumentRow.spend_kopecks).order_by(
            WbAdvertisingSpendDocumentRow.spend_kopecks
        )
    ).all() == [-200, 150]


def test_postgresql_raw_ingest_locks_account_before_run_lookup(
    session: Session, raw_bundle, monkeypatch
) -> None:
    service = AdvertisingService(session, 1, now=lambda: NOW)
    service.ingest_raw_payload(
        31,
        PERIOD,
        raw_bundle,
        source_reference="wb_api:probe",
        observed_at=LATER,
    )
    events: list[str] = []
    locks: list[tuple[str, dict[str, object]]] = []
    original_account = service._account
    original_execute = session.execute
    original_scalar = session.scalar

    def account(marketplace_account_id: int):
        result = original_account(marketplace_account_id)
        events.append("account_validated")
        return result

    def execute(statement, params=None, **kwargs):
        sql = str(statement)
        if "set_config('app.organization_id'" in sql:
            return None
        if "pg_advisory_xact_lock" in sql:
            events.append("advisory_lock")
            locks.append((sql, params))
            return None
        return original_execute(statement, params, **kwargs)

    def scalar(statement, *args, **kwargs):
        if "wb_advertising_sync_runs" in str(statement):
            events.append("run_lookup")
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(session.get_bind().dialect, "name", "postgresql")
    monkeypatch.setattr(service, "_account", account)
    monkeypatch.setattr(session, "execute", execute)
    monkeypatch.setattr(session, "scalar", scalar)

    replay = service.ingest_raw_payload(
        31,
        PERIOD,
        raw_bundle,
        source_reference="wb_api:probe",
        observed_at=NOW,
    )

    assert events[:3] == ["account_validated", "advisory_lock", "run_lookup"]
    assert locks == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": 8_189_153_638_230_522_863},
        )
    ]
    assert replay.last_observed_at == LATER


def test_raw_replay_refreshes_a_stale_identity_map_before_timestamp_update(
    session: Session, raw_bundle
) -> None:
    service = AdvertisingService(session, 1, now=lambda: NOW)
    first = service.ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )
    stale = session.get(WbAdvertisingSyncRunRow, first.sync_run_id)
    session.commit()

    with Session(session.get_bind(), expire_on_commit=False) as second_session:
        AdvertisingService(second_session, 1, now=lambda: LATER).ingest_raw_payload(
            31,
            PERIOD,
            raw_bundle,
            source_reference="wb_api:probe",
            observed_at=LATER,
        )

    assert stale.last_observed_at.replace(tzinfo=timezone.utc) == NOW

    replay = service.ingest_raw_payload(
        31,
        PERIOD,
        raw_bundle,
        source_reference="wb_api:probe",
        observed_at=INTERMEDIATE,
    )

    assert replay.last_observed_at == LATER
    with Session(session.get_bind()) as verification_session:
        persisted = verification_session.get(WbAdvertisingSyncRunRow, first.sync_run_id)
        assert persisted.last_observed_at == LATER.replace(tzinfo=None)


def test_postgresql_unchanged_replay_commits_the_lock_transaction(
    session: Session, raw_bundle, monkeypatch
) -> None:
    service = AdvertisingService(session, 1, now=lambda: NOW)
    service.ingest_raw_payload(
        31,
        PERIOD,
        raw_bundle,
        source_reference="wb_api:probe",
        observed_at=LATER,
    )
    lock_transactions = []
    committed_transactions = []
    original_execute = session.execute
    original_commit = session.commit

    def execute(statement, params=None, **kwargs):
        sql = str(statement)
        if "set_config('app.organization_id'" in sql:
            return None
        if "pg_advisory_xact_lock" in sql:
            lock_transactions.append(session.get_transaction())
            return None
        return original_execute(statement, params, **kwargs)

    def commit():
        committed_transactions.append(session.get_transaction())
        return original_commit()

    monkeypatch.setattr(session.get_bind().dialect, "name", "postgresql")
    monkeypatch.setattr(session, "execute", execute)
    monkeypatch.setattr(session, "commit", commit)

    replay = service.ingest_raw_payload(
        31,
        PERIOD,
        raw_bundle,
        source_reference="wb_api:probe",
        observed_at=NOW,
    )

    assert replay.last_observed_at == LATER
    assert committed_transactions == lock_transactions
    assert len(lock_transactions) == 1
    assert lock_transactions[0] is not None
    assert not lock_transactions[0].is_active


@pytest.mark.parametrize("manifest", [{}, None])
def test_raw_ingest_rejects_non_list_manifest_without_writes(
    session: Session, raw_bundle, manifest
) -> None:
    raw_bundle["manifest"] = manifest

    with pytest.raises(AdvertisingNormalizationError, match="manifest"):
        AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
            31, PERIOD, raw_bundle, source_reference="wb_api:probe"
        )

    assert _counts(session) == (0, 0, 0, 0)


def test_raw_ingest_rejects_partial_manifest_without_writes(
    session: Session, raw_bundle
) -> None:
    raw_bundle["manifest"][2]["ok"] = False

    with pytest.raises(AdvertisingNormalizationError, match="incomplete"):
        AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
            31, PERIOD, raw_bundle, source_reference="wb_api:probe"
        )

    assert _counts(session) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "secret_key", ["Authorization", "accessToken", "client_secret"]
)
def test_raw_ingest_rejects_nested_manifest_secrets_without_exposing_them(
    session: Session, raw_bundle, secret_key: str, caplog
) -> None:
    raw_bundle["manifest"][0]["request"] = {
        "safe": {"nested": {secret_key: "never-persist-or-log"}}
    }

    with pytest.raises(AdvertisingNormalizationError, match="manifest"):
        AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
            31, PERIOD, raw_bundle, source_reference="wb_api:probe"
        )

    assert "never-persist-or-log" not in caplog.text
    assert _counts(session) == (0, 0, 0, 0)


def test_raw_ingest_rejects_non_finite_manifest_values_without_writes(
    session: Session, raw_bundle
) -> None:
    raw_bundle["manifest"][0]["request"] = {"cursor": float("nan")}

    with pytest.raises(AdvertisingNormalizationError, match="manifest"):
        AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
            31, PERIOD, raw_bundle, source_reference="wb_api:probe"
        )

    assert _counts(session) == (0, 0, 0, 0)


def test_raw_ingest_scopes_identical_external_ids_by_tenant(
    session: Session, raw_bundle
) -> None:
    one = AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )
    two = AdvertisingService(session, 2, now=lambda: NOW).ingest_raw_payload(
        32, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )

    assert one.sync_run_id != two.sync_run_id
    assert _counts(session) == (2, 12, 4, 4)
    assert set(
        session.execute(
            select(
                WbAdvertisingCampaignSnapshotRow.organization_id,
                WbAdvertisingCampaignSnapshotRow.marketplace_account_id,
            )
        )
    ) == {(1, 31), (2, 32)}


def test_raw_ingest_rolls_back_run_and_all_children_on_insert_failure(
    session: Session, raw_bundle
) -> None:
    engine = session.get_bind()

    def fail_spend_document_insert(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO wb_advertising_spend_documents"):
            raise RuntimeError("injected spend-document failure")

    event.listen(engine, "before_cursor_execute", fail_spend_document_insert)
    try:
        with pytest.raises(RuntimeError, match="injected spend-document failure"):
            AdvertisingService(session, 1, now=lambda: NOW).ingest_raw_payload(
                31, PERIOD, raw_bundle, source_reference="wb_api:probe"
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_spend_document_insert)

    assert _counts(session) == (0, 0, 0, 0)


def test_raw_backfill_never_publishes_partial_fetch(
    session: Session, monkeypatch
) -> None:
    token = "never-emit-this-token"

    def fetch(_period: Period, *, wb_token: str) -> RawAdvertisingFetch:
        assert not session.in_transaction()
        return RawAdvertisingFetch(
            "partial",
            {"raw": token, "receivedToken": wb_token},
            4,
            3,
            ("rate_limited",),
        )

    monkeypatch.setattr(
        raw_backfill_module,
        "get_organization_wb_token_secret",
        lambda _organization_id: pytest.fail("supplied token must win"),
    )
    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fetch)
    monkeypatch.setattr(
        AdvertisingService,
        "ingest_raw_payload",
        lambda *_args, **_kwargs: pytest.fail("partial evidence must not publish"),
    )

    result = backfill_raw_advertising(
        1, PERIOD, wb_token=f"  {token}  ", session=session
    )

    assert result == {
        "state": "partial",
        "expectedRequests": 4,
        "completedRequests": 3,
        "failureCodes": ["rate_limited"],
    }
    assert token not in json.dumps(result)
    assert not session.in_transaction()
    assert (
        session.scalar(select(func.count()).select_from(WbAdvertisingSyncRunRow)) == 0
    )


def test_raw_backfill_ready_uses_resolved_token_and_owned_session(
    session: Session, raw_bundle, monkeypatch
) -> None:
    token = "resolved-secret-token"
    resolved_organizations: list[int] = []
    fetched: list[tuple[Period, str]] = []
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    def resolve(organization_id: int) -> str:
        resolved_organizations.append(organization_id)
        return f"  {token}  "

    def fetch(period: Period, *, wb_token: str) -> RawAdvertisingFetch:
        fetched.append((period, wb_token))
        return RawAdvertisingFetch("ready", raw_bundle, 4, 4, ())

    monkeypatch.setattr(
        raw_backfill_module, "get_organization_wb_token_secret", resolve
    )
    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fetch)
    monkeypatch.setattr(raw_backfill_module, "get_session_factory", lambda: factory)

    result = backfill_raw_advertising(1, PERIOD, wb_token="   ")

    assert resolved_organizations == [1]
    assert fetched == [(PERIOD, token)]
    assert set(result) == {
        "state",
        "syncRunId",
        "snapshotChecksum",
        "marketplaceAccountId",
        "factCount",
        "campaignCount",
        "spendDocumentCount",
        "sourceTotalSpendKopecks",
        "documentTotalSpendKopecks",
        "expectedRequests",
        "completedRequests",
    }
    assert result | {"syncRunId": None, "snapshotChecksum": None} == {
        "state": "ready",
        "syncRunId": None,
        "snapshotChecksum": None,
        "marketplaceAccountId": 31,
        "factCount": 6,
        "campaignCount": 2,
        "spendDocumentCount": 2,
        "sourceTotalSpendKopecks": 200,
        "documentTotalSpendKopecks": 200,
        "expectedRequests": 4,
        "completedRequests": 4,
    }
    assert token not in json.dumps(result)
    with factory() as verification_session:
        run = verification_session.scalar(select(WbAdvertisingSyncRunRow))
        assert run is not None
        assert run.sync_run_id == result["syncRunId"]
        assert run.source_reference == "wb_api:raw_backfill"


@pytest.mark.parametrize(
    ("account_count", "error_code"),
    [(0, "wb_account_missing"), (2, "wb_account_ambiguous")],
)
def test_raw_backfill_requires_exactly_one_connected_wb_account(
    session: Session, monkeypatch, account_count: int, error_code: str
) -> None:
    if account_count == 0:
        session.get(MarketplaceAccountRow, 31).status = "disconnected"
    else:
        session.add(
            MarketplaceAccountRow(
                marketplace_account_id=33,
                organization_id=1,
                marketplace="wb",
                external_account_id="second-wb-account",
                status="connected",
            )
        )
    session.commit()
    monkeypatch.setattr(
        raw_backfill_module,
        "fetch_raw_advertising",
        lambda *_args, **_kwargs: pytest.fail("invalid account scope must not fetch"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(1, PERIOD, wb_token="secret", session=session)

    assert raised.value.error_code == error_code
    assert "secret" not in str(raised.value)
    assert (
        session.scalar(select(func.count()).select_from(WbAdvertisingSyncRunRow)) == 0
    )


def test_raw_backfill_rejects_mismatched_explicit_account(
    session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(
        raw_backfill_module,
        "fetch_raw_advertising",
        lambda *_args, **_kwargs: pytest.fail("mismatched account must not fetch"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(
            1,
            PERIOD,
            marketplace_account_id=32,
            wb_token="secret",
            session=session,
        )

    assert raised.value.error_code == "wb_account_changed"


@pytest.mark.parametrize("binding_change", ["reference", "secret"])
def test_raw_backfill_revalidates_explicit_credential_after_fetch(
    session: Session, raw_bundle, monkeypatch, binding_change: str
) -> None:
    account = session.get(MarketplaceAccountRow, 31)
    assert account is not None
    account.credential_ref = "lk_user_wb_tokens:21"
    session.add_all(
        [
            LkUserRow(
                user_id="user-one",
                organization_id=1,
                email="one@example.com",
                password_hash="unused",
                full_name="One",
                permission_profile="admin",
                is_active=True,
            ),
            LkUserWbTokenRow(
                token_id=21,
                user_id="user-one",
                organization_id=1,
                wb_token="secret",
                token_masked="***",
            ),
        ]
    )
    session.commit()

    def fetch(_period: Period, *, wb_token: str) -> RawAdvertisingFetch:
        assert wb_token == "secret"
        with Session(session.get_bind()) as mutation_session:
            if binding_change == "reference":
                changed = mutation_session.get(MarketplaceAccountRow, 31)
                assert changed is not None
                changed.credential_ref = None
            else:
                changed_token = mutation_session.get(LkUserWbTokenRow, 21)
                assert changed_token is not None
                changed_token.wb_token = "rotated-secret"
            mutation_session.commit()
        return RawAdvertisingFetch("ready", raw_bundle, 4, 4, ())

    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fetch)
    monkeypatch.setattr(
        AdvertisingService,
        "ingest_raw_payload",
        lambda *_args, **_kwargs: pytest.fail("changed credential must not publish"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(
            1,
            PERIOD,
            marketplace_account_id=31,
            credential_ref="lk_user_wb_tokens:21",
            wb_token="secret",
            session=session,
        )

    assert raised.value.error_code == "wb_credential_binding_invalid"
    assert not session.in_transaction()


@pytest.mark.parametrize(
    ("account_change", "error_code"),
    [
        ("disconnect", "wb_account_missing"),
        ("add", "wb_account_ambiguous"),
        ("replace", "wb_account_changed"),
    ],
)
def test_raw_backfill_revalidates_connected_account_after_fetch(
    session: Session,
    raw_bundle,
    monkeypatch,
    account_change: str,
    error_code: str,
) -> None:
    def fetch(_period: Period, *, wb_token: str) -> RawAdvertisingFetch:
        assert wb_token == "secret"
        assert not session.in_transaction()
        with Session(session.get_bind()) as mutation_session:
            if account_change in {"disconnect", "replace"}:
                account = mutation_session.get(MarketplaceAccountRow, 31)
                assert account is not None
                account.status = "disconnected"
            if account_change in {"add", "replace"}:
                mutation_session.add(
                    MarketplaceAccountRow(
                        marketplace_account_id=33,
                        organization_id=1,
                        marketplace="wb",
                        external_account_id="replacement-wb-account",
                        status="connected",
                    )
                )
            mutation_session.commit()
        return RawAdvertisingFetch("ready", raw_bundle, 4, 4, ())

    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fetch)
    monkeypatch.setattr(
        AdvertisingService,
        "ingest_raw_payload",
        lambda *_args, **_kwargs: pytest.fail("changed account scope must not publish"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(1, PERIOD, wb_token="secret", session=session)

    assert raised.value.error_code == error_code
    assert not session.in_transaction()
    assert (
        session.scalar(select(func.count()).select_from(WbAdvertisingSyncRunRow)) == 0
    )


@pytest.mark.parametrize(
    ("organization_id", "resolved_token", "error_code"),
    [(0, "unused", "invalid_organization"), (1, "   ", "wb_token_missing")],
)
def test_raw_backfill_validates_organization_and_token_without_fetching(
    session: Session,
    monkeypatch,
    organization_id: int,
    resolved_token: str,
    error_code: str,
) -> None:
    monkeypatch.setattr(
        raw_backfill_module,
        "get_organization_wb_token_secret",
        lambda _organization_id: resolved_token,
    )
    monkeypatch.setattr(
        raw_backfill_module,
        "fetch_raw_advertising",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not fetch"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(
            organization_id, PERIOD, wb_token=None, session=session
        )

    assert raised.value.error_code == error_code


def test_raw_backfill_rejects_active_injected_session_before_resolving_token(
    session: Session, monkeypatch
) -> None:
    session.begin()
    monkeypatch.setattr(
        raw_backfill_module,
        "get_organization_wb_token_secret",
        lambda _organization_id: pytest.fail("active session must fail first"),
    )
    monkeypatch.setattr(
        raw_backfill_module,
        "fetch_raw_advertising",
        lambda *_args, **_kwargs: pytest.fail("active session must not fetch"),
    )

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(1, PERIOD, session=session)

    assert raised.value.error_code == "session_transaction_active"
    assert session.in_transaction()
    session.rollback()


def test_raw_backfill_converts_payload_fetch_error_without_secret(
    session: Session, monkeypatch
) -> None:
    token = "never-emit-this-token"

    def fail(*_args, **_kwargs):
        raise RawAdvertisingFetchError("invalid WB advertising response payload")

    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fail)

    with pytest.raises(RawAdvertisingBackfillError) as raised:
        backfill_raw_advertising(1, PERIOD, wb_token=token, session=session)

    assert raised.value.error_code == "wb_fetch_failed"
    assert token not in str(raised.value)


def test_raw_backfill_propagates_unexpected_fetch_errors(
    session: Session, monkeypatch
) -> None:
    def fail(*_args, **_kwargs):
        assert not session.in_transaction()
        raise RuntimeError("programming bug")

    monkeypatch.setattr(raw_backfill_module, "fetch_raw_advertising", fail)

    with pytest.raises(RuntimeError, match="programming bug"):
        backfill_raw_advertising(1, PERIOD, wb_token="secret", session=session)

    assert not session.in_transaction()


@pytest.mark.parametrize(
    ("result", "expected_exit"),
    [
        ({"state": "ready", "syncRunId": "run-1"}, 0),
        (
            {
                "state": "partial",
                "expectedRequests": 4,
                "completedRequests": 3,
                "failureCodes": ["rate_limited"],
            },
            1,
        ),
    ],
)
def test_raw_backfill_cli_prints_only_json_summary(
    monkeypatch, capsys, result: dict[str, object], expected_exit: int
) -> None:
    monkeypatch.setattr(
        raw_backfill_module,
        "backfill_raw_advertising",
        lambda *_args, **_kwargs: result,
    )

    exit_code = raw_backfill_module.main(
        [
            "--organization-id",
            "1",
            "--date-from",
            "2026-09-01",
            "--date-to",
            "2026-09-01",
        ]
    )
    output = capsys.readouterr()

    assert exit_code == expected_exit
    assert json.loads(output.out) == result
    assert output.err == ""


def test_raw_backfill_cli_rejects_invalid_period_as_stable_json(
    monkeypatch, capsys
) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("invalid Period must not start the backfill")

    monkeypatch.setattr(raw_backfill_module, "backfill_raw_advertising", fail)

    exit_code = raw_backfill_module.main(
        [
            "--organization-id",
            "1",
            "--date-from",
            "2026-09-02",
            "--date-to",
            "2026-09-01",
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(output.out) == {
        "state": "failed",
        "errorCode": "invalid_period",
    }
    assert output.err == ""


def test_raw_backfill_cli_converts_backfill_error_to_stable_json(
    monkeypatch, capsys
) -> None:
    def fail(*_args, **_kwargs):
        raise RawAdvertisingBackfillError("wb_token_missing")

    monkeypatch.setattr(raw_backfill_module, "backfill_raw_advertising", fail)

    exit_code = raw_backfill_module.main(
        [
            "--organization-id",
            "1",
            "--date-from",
            "2026-09-01",
            "--date-to",
            "2026-09-01",
        ]
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(output.out) == {
        "state": "failed",
        "errorCode": "wb_token_missing",
    }
    assert output.err == ""


def test_raw_reconciliation_keeps_scopes_separate(session: Session, raw_bundle) -> None:
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    snapshot = service.ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )

    result = service.get_raw_reconciliation(31, PERIOD)

    assert result.state == "ready"
    assert result.snapshot.sync_run_id == snapshot.sync_run_id
    assert result.formula_version == "wb-advertising-reconciliation-shadow-v1"
    assert (
        result.campaign.spend_kopecks,
        result.campaign.impressions,
        result.campaign.clicks,
        result.campaign.cart_adds,
        result.campaign.order_count,
        result.campaign.order_revenue_kopecks,
        result.campaign.cancel_count,
    ) == (200, 19, 3, 1, 1, 500, 1)
    assert (
        result.source_sku.spend_kopecks,
        result.source_sku.impressions,
        result.source_sku.clicks,
        result.source_sku.cart_adds,
        result.source_sku.order_count,
        result.source_sku.order_revenue_kopecks,
        result.source_sku.cancel_count,
    ) == (150, 15, 3, 1, 1, 500, 1)
    assert (
        result.campaign_only.spend_kopecks,
        result.campaign_only.impressions,
        result.campaign_only.clicks,
        result.campaign_only.cart_adds,
        result.campaign_only.order_count,
        result.campaign_only.order_revenue_kopecks,
        result.campaign_only.cancel_count,
    ) == (50, 4, 0, 0, 0, 0, 0)
    assert result.document_spend_kopecks == 200
    assert result.unknown_spend_kopecks == 0
    assert result.source_sku_by_nm[2001].spend_kopecks == 150
    assert result.mapped_catalog_sku_by_nm == {}
    assert result.unmapped_nm_ids == (2001,)
    assert result.diagnostics == ("WB_ADS_PRODUCT_MAPPING_MISSING",)


def test_raw_pnl_source_uses_effective_spend_and_ignores_finance(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    raw = service.ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )
    service.ingest_payload(
        31,
        PERIOD,
        "finance_promotion",
        {
            "rows": [
                {
                    "rrdId": 7,
                    "bonusTypeName": "WB Продвижение",
                    "deduction": "9.99",
                    "nmId": 2001,
                    "rrDate": "2026-09-01",
                }
            ],
            "dateFrom": "2026-09-01",
            "dateTo": "2026-09-01",
        },
        source_reference="finance:later",
        observed_at=CLOSED,
    )

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "ready"
    assert source.snapshot.sync_run_id == raw.sync_run_id
    assert source.source_kind == "ads_fullstats"
    assert source.evidence_status == "raw"
    assert source.total_spend_kopecks == 200
    assert source.spend_by_nm == {2001: 150}
    assert source.unattributed_spend_kopecks == 50
    assert source.blocker_ids == ("WB_PNL_ADVERTISING_UNATTRIBUTED",)


def test_raw_pnl_source_keeps_unmapped_spend_unattributed(
    session: Session, raw_bundle
) -> None:
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "ready"
    assert source.total_spend_kopecks == 200
    assert source.spend_by_nm == {}
    assert source.unattributed_spend_kopecks == 200
    assert source.blocker_ids == (
        "WB_ADS_PRODUCT_MAPPING_MISSING",
        "WB_PNL_ADVERTISING_UNATTRIBUTED",
    )


def test_raw_pnl_source_uses_source_detail_for_tolerated_kopeck(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    campaign = raw_bundle["fullstats"][0]["payload"][0]
    campaign["sum"] = campaign["days"][0]["sum"] = 1.49
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    source = service.get_pnl_source(31, PERIOD)
    reconciliation = service.get_raw_reconciliation(31, PERIOD)

    assert source.total_spend_kopecks == 200
    assert source.spend_by_nm == {2001: 150}
    assert source.unattributed_spend_kopecks == 50
    assert "WB_ADS_HIERARCHY_TOLERANCE_APPLIED" in reconciliation.diagnostics


def test_raw_pnl_source_preserves_daily_rounding_evidence(session: Session, raw_bundle):
    _map_nm(session)
    period = Period(date(2026, 9, 1), date(2026, 9, 5))
    raw_bundle["period"]["dateTo"] = "2026-09-05"
    page = raw_bundle["fullstats"][0]
    page["dateTo"] = "2026-09-05"
    campaign = page["payload"][0]
    day = campaign["days"][0]
    campaign["sum"] = 1.42
    day["sum"] = 0.36
    for app, spend in zip(day["apps"], (0.26, 0.10), strict=True):
        app["sum"] = app["nms"][0]["sum"] = spend
    campaign["days"] = [copy.deepcopy(day) for _ in range(4)]
    for index, daily in enumerate(campaign["days"]):
        daily["date"] = f"{period.date_from + timedelta(days=index)}T00:00:00+03:00"
    for metric in ("views", "clicks", "atbs", "orders", "sum_price", "canceled"):
        campaign[metric] *= 4
    service = AdvertisingService(
        session, 1, now=lambda: datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
    )

    snapshot = service.ingest_raw_payload(31, period, raw_bundle, source_reference="wb_api:probe")
    source = service.get_pnl_source(31, period)
    reconciliation = service.get_raw_reconciliation(31, period)

    assert source.state == "ready"
    assert snapshot.source_total_spend_kopecks == 192
    assert source.total_spend_kopecks == 194
    assert source.spend_by_nm == {2001: 144}
    assert source.unattributed_spend_kopecks == 50
    assert "WB_ADS_HIERARCHY_TOLERANCE_APPLIED" in reconciliation.diagnostics


def test_raw_pnl_source_accepts_complete_empty_snapshot(
    session: Session, raw_bundle
) -> None:
    raw_bundle["promotion"] = {"adverts": [], "all": 0}
    raw_bundle["adverts"] = {"adverts": []}
    raw_bundle["fullstats"] = []
    raw_bundle["upd"][0]["payload"] = []
    raw_bundle["manifest"] = [
        item
        for item in raw_bundle["manifest"]
        if item["endpoint"] != "GET /adv/v3/fullstats"
    ]
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    raw = service.ingest_raw_payload(
        31, PERIOD, raw_bundle, source_reference="wb_api:probe"
    )

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "empty"
    assert source.snapshot.sync_run_id == raw.sync_run_id
    assert source.source_kind == "ads_fullstats"
    assert source.total_spend_kopecks == 0
    assert source.spend_by_nm == {}
    assert source.unattributed_spend_kopecks == 0
    assert source.blocker_ids == ()


def test_raw_pnl_source_fails_closed_on_invalid_spend_hierarchy(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")
    campaign = session.scalar(
        select(WbAdvertisingFactRow).where(
            WbAdvertisingFactRow.campaign_id == 1001,
            WbAdvertisingFactRow.fact_scope == "campaign",
            WbAdvertisingFactRow.grain == "period",
        )
    )
    assert campaign is not None
    campaign.spend_kopecks = 148
    session.commit()

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "partial"
    assert source.snapshot is not None
    assert source.total_spend_kopecks is None
    assert source.spend_by_nm == {}
    assert source.unattributed_spend_kopecks is None
    assert source.blocker_ids == (
        "WB_ADS_HIERARCHY_RECONCILIATION_FAILED",
    )


def test_raw_pnl_source_fails_closed_when_spend_is_missing(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")
    source_row = session.scalar(
        select(WbAdvertisingFactRow).where(
            WbAdvertisingFactRow.nm_id == 2001,
            WbAdvertisingFactRow.fact_scope == "source_sku",
        )
    )
    assert source_row is not None
    source_row.spend_kopecks = None
    session.commit()

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "partial"
    assert source.total_spend_kopecks is None
    assert source.spend_by_nm == {}
    assert source.blocker_ids == ("WB_ADS_SOURCE_METRIC_INCOMPLETE",)


def test_raw_pnl_source_never_attributes_ambiguous_mapping(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    session.add_all(
        [
            CatalogSkuRow(
                catalog_sku_id=12,
                organization_id=1,
                code="SKU-2001-SECOND",
            ),
            MarketplaceOfferRow(
                marketplace_offer_id=2012,
                organization_id=1,
                marketplace_account_id=31,
                marketplace_product_id=1011,
                external_offer_key="offer-2001-second",
                catalog_sku_id=12,
            ),
        ]
    )
    session.commit()
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "ready"
    assert source.total_spend_kopecks == 200
    assert source.spend_by_nm == {}
    assert source.unattributed_spend_kopecks == 200
    assert source.blocker_ids == (
        "WB_ADS_PRODUCT_MAPPING_AMBIGUOUS",
        "WB_PNL_ADVERTISING_UNATTRIBUTED",
    )


def test_raw_pnl_source_ignores_non_spend_and_upd_diagnostics(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    del raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][1][
        "nms"
    ][0]["atbs"]
    raw_bundle["upd"][0]["payload"].append(
        {
            "updNum": 7002,
            "updTime": "2026-09-01T13:00:00+03:00",
            "updSum": 0.25,
            "advertId": 9999,
            "campName": "unobserved-campaign",
            "advertType": 8,
            "paymentType": "cpm",
            "advertStatus": 11,
            "currency": "RUB",
        }
    )
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    source = service.get_pnl_source(31, PERIOD)
    reconciliation = service.get_raw_reconciliation(31, PERIOD)

    assert source.state == "ready"
    assert source.total_spend_kopecks == 200
    assert source.blocker_ids == ("WB_PNL_ADVERTISING_UNATTRIBUTED",)
    assert "WB_ADS_SOURCE_METRIC_INCOMPLETE" in reconciliation.diagnostics
    assert "WB_ADS_DOCUMENT_SCOPE_UNKNOWN" in reconciliation.diagnostics


def test_raw_pnl_source_is_fully_attributed_only_when_residual_is_zero(
    session: Session, raw_bundle
) -> None:
    _map_nm(session)
    second = raw_bundle["fullstats"][0]["payload"][1]
    second["sum"] = second["days"][0]["sum"] = 0
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    source = service.get_pnl_source(31, PERIOD)

    assert source.state == "ready"
    assert source.total_spend_kopecks == 150
    assert source.spend_by_nm == {2001: 150}
    assert source.unattributed_spend_kopecks == 0
    assert source.blocker_ids == ()


def test_raw_reconciliation_preserves_incomplete_metric(
    session: Session, raw_bundle
) -> None:
    del raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][1]["nms"][0]["atbs"]
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    result = service.get_raw_reconciliation(31, PERIOD)

    assert result.source_sku.cart_adds is None
    assert result.source_sku_by_nm[2001].cart_adds is None
    assert result.campaign_only.cart_adds is None
    assert "WB_ADS_SOURCE_METRIC_INCOMPLETE" in result.diagnostics


def test_raw_reconciliation_reports_one_kopeck_tolerance(
    session: Session, raw_bundle
) -> None:
    campaign = raw_bundle["fullstats"][0]["payload"][0]
    campaign["sum"] = campaign["days"][0]["sum"] = 1.49
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    result = service.get_raw_reconciliation(31, PERIOD)

    assert result.campaign.spend_kopecks == 199
    assert result.source_sku.spend_kopecks == 150
    assert result.campaign_only.spend_kopecks == 50
    assert "WB_ADS_HIERARCHY_TOLERANCE_APPLIED" in result.diagnostics


def test_raw_reconciliation_keeps_unmatched_upd_spend_unknown(
    session: Session, raw_bundle
) -> None:
    raw_bundle["upd"][0]["payload"].append(
        {
            "updNum": 7002,
            "updTime": "2026-09-01T13:00:00+03:00",
            "updSum": 0.25,
            "advertId": 9999,
            "campName": "unobserved-campaign",
            "advertType": 8,
            "paymentType": "cpm",
            "advertStatus": 11,
            "currency": "RUB",
        }
    )
    service = AdvertisingService(session, 1, now=lambda: CLOSED)
    service.ingest_raw_payload(31, PERIOD, raw_bundle, source_reference="wb_api:probe")

    result = service.get_raw_reconciliation(31, PERIOD)

    assert result.document_spend_kopecks == 225
    assert result.unknown_spend_kopecks == 25
    assert "WB_ADS_DOCUMENT_SCOPE_UNKNOWN" in result.diagnostics


def test_raw_reconciliation_requires_matching_campaign_interval(
    session: Session, raw_bundle
) -> None:
    period = Period(date(2026, 9, 1), date(2026, 9, 2))
    raw_bundle["period"]["dateTo"] = "2026-09-02"
    raw_bundle["upd"][0]["dateTo"] = "2026-09-02"
    raw_bundle["upd"][0]["payload"][1]["updTime"] = "2026-09-02T12:05:00+03:00"
    service = AdvertisingService(
        session,
        1,
        now=lambda: datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
    )
    service.ingest_raw_payload(31, period, raw_bundle, source_reference="wb_api:probe")

    result = service.get_raw_reconciliation(31, period)

    assert result.unknown_spend_kopecks == 50
    assert "WB_ADS_DOCUMENT_SCOPE_UNKNOWN" in result.diagnostics


def test_raw_reconciliation_does_not_use_legacy_or_future_evidence(
    session: Session, raw_bundle
) -> None:
    service = AdvertisingService(session, 1, now=lambda: NOW)
    service.ingest_payload(
        31,
        PERIOD,
        "ads_fullstats",
        {
            "dateFrom": "2026-09-01",
            "dateTo": "2026-09-01",
            "dailyAggregates": {"2026-09-01": {}},
            "aggregates": {},
            "totals": {
                "adSpendKopecks": 0,
                "adImpressions": 0,
                "adClicks": 0,
                "adCartAdds": 0,
                "adOrders": 0,
                "adRevenueKopecks": 0,
            },
        },
        source_reference="legacy:test",
    )

    missing = service.get_raw_reconciliation(31, PERIOD)
    future = service.get_raw_reconciliation(
        31, Period(date(2026, 9, 2), date(2026, 9, 3))
    )

    assert missing.state == "partial"
    assert missing.snapshot is None
    assert future.state == "future"
    assert future.snapshot is None
