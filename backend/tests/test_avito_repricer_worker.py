from datetime import datetime, timezone
from types import SimpleNamespace

from app import repricer_tasks
from app.avito.listings import AvitoListingRow, AvitoListingsFetchResult
from app.avito.stats import AvitoStatsAccount
from app.cabinet.store import AvitoCredentialsSecret


def test_avito_scheduler_execute_skips_when_interval_not_due(monkeypatch):
    called = {"client": False}

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            avito_repricer_worker_enabled=True,
            avito_repricer_price_apply_enabled=False,
            avito_repricer_execute_interval_minutes=60,
            avito_repricer_period_days=30,
            avito_api_base_url="https://api.avito.ru",
            avito_api_timeout_seconds=20,
        ),
    )
    monkeypatch.setattr(
        "app.repricer_tasks.load_avito_repricer_settings",
        lambda _organization_id: {
            "enabled": True,
            "executeIntervalMinutes": 60,
            "periodDays": 30,
            "autoApplyPricesEnabled": False,
            "maxChangesPerRun": 50,
        },
    )
    monkeypatch.setattr(
        "app.repricer_tasks.get_source_cache",
        lambda _organization_id, source_key, **_kwargs: {"finishedAt": "2026-07-30T09:30:00+00:00"}
        if source_key == "avito_repricer_last_scheduler_run"
        else None,
    )
    def build_client(**_kwargs):
        called["client"] = True
        return object()

    monkeypatch.setattr("app.repricer_tasks.build_avito_listings_client", build_client)
    monkeypatch.setattr("app.repricer_tasks._utc_now_datetime", lambda: datetime(2026, 7, 30, 9, 45, tzinfo=timezone.utc))

    result = repricer_tasks.execute_avito_for_org.run(10)

    assert result["skipped"] is True
    assert result["reason"] == "avito_execute_interval_not_due"
    assert called["client"] is False


class _AvitoListingsClient:
    def fetch_listings(self, request):
        self.request = request
        return AvitoListingsFetchResult(
            status="synced",
            accounts=[AvitoStatsAccount(accountId="365024549", accountName="Bless T")],
            rows=[
                AvitoListingRow(
                    itemId="8098482225",
                    title="Худи",
                    accountId="365024549",
                    accountName="Bless T",
                    status="active",
                    priceKopecks=490000,
                    views=520,
                    contactsMessenger=17,
                    contacts=24,
                )
            ],
        )


def test_avito_scheduler_execute_creates_pending_approval_when_real_apply_disabled(monkeypatch):
    saved: list[tuple[int, str, dict]] = []
    listings_client = _AvitoListingsClient()

    monkeypatch.setattr(
        "app.repricer_tasks.get_settings",
        lambda: SimpleNamespace(
            avito_repricer_worker_enabled=True,
            avito_repricer_price_apply_enabled=False,
            avito_repricer_execute_interval_minutes=60,
            avito_repricer_period_days=30,
            avito_api_base_url="https://api.avito.ru",
            avito_api_timeout_seconds=20,
        ),
    )
    monkeypatch.setattr(
        "app.repricer_tasks.load_avito_repricer_settings",
        lambda _organization_id: {
            "enabled": True,
            "executeIntervalMinutes": 60,
            "periodDays": 30,
            "autoApplyPricesEnabled": True,
            "maxChangesPerRun": 50,
        },
    )
    monkeypatch.setattr("app.repricer_tasks.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.repricer_tasks.load_avito_repricer_strategy_assignments",
        lambda _organization_id: {"8098482225": "chat_demand_balanced"},
    )
    monkeypatch.setattr("app.repricer_tasks.save_source_cache", lambda organization_id, source_key, payload: saved.append((organization_id, source_key, payload)) or payload)
    # The worker also writes history through router helpers. Keep that boundary
    # in memory, just like pending approvals; this test must never reach Redis/DB.
    monkeypatch.setattr("app.routers.avito_repricer.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_repricer.save_source_cache", lambda organization_id, source_key, payload: saved.append((organization_id, source_key, payload)) or payload)
    monkeypatch.setattr(
        "app.repricer_tasks.get_organization_avito_credentials_secret",
        lambda _organization_id: AvitoCredentialsSecret(client_id="client", client_secret="secret", cached_access_token="token", access_token_expires_at=None),
    )
    monkeypatch.setattr("app.repricer_tasks.resolve_user_avito_access_token", lambda **_kwargs: "token")
    monkeypatch.setattr("app.repricer_tasks.build_avito_listings_client", lambda **_kwargs: listings_client)

    result = repricer_tasks.execute_avito_for_org.run(10)

    assert result["state"] == "completed"
    assert result["pendingApprovalsCreated"] == 1
    pending_payload = next(payload for _org, key, payload in saved if key == "avito_repricer_price_approvals_pending")
    assert pending_payload["items"][0]["itemId"] == "8098482225"
    assert pending_payload["items"][0]["recommendedPriceKopecks"] == 514500
    assert pending_payload["items"][0]["applyMode"] == "manual_approval"
