"""Synthetic transport/orchestration tests, not a PostgreSQL durability proof."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.platform.integrations.credential_store import (
    CredentialFetchBinding,
    MarketplaceAccountCredentialOwner,
    ResolvedCredentialForFetch,
)
from app.security.marketplace_credentials import CredentialIdentity, DecryptedCredential
from app.wb_live.contracts import BatchLease, JobLocator, WbLiveError
from app.wb_live.provider import (
    ReadOnlyWbProvider,
    WbReadError,
    parse_page,
    request_for,
)
from app.wb_live.worker import dispatch_due, run_one_batch

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ID = "11111111-1111-4111-8111-111111111111"
LOCATOR = JobLocator(1, 2, ID)


def credential(account=2):
    return ResolvedCredentialForFetch(
        DecryptedCredential({"token": "synthetic-only"}),
        CredentialFetchBinding(
            MarketplaceAccountCredentialOwner(1, account, "wb"),
            "synthetic-seller",
            None,
            CredentialIdentity(1, account, "wb", "wb_api", 1, UUID(ID), 1),
        ),
    )


def goods(sizes=None):
    return {
        "data": {
            "listGoods": [
                {
                    "nmID": 10,
                    "vendorCode": "test",
                    "discount": 0,
                    "sizes": sizes
                    if sizes is not None
                    else [{"sizeID": 20, "price": 1.01}],
                }
            ]
        }
    }


def card(nm=10):
    return {
        "nmID": nm,
        "vendorCode": "test",
        "title": "Fixture",
        "updatedAt": NOW.isoformat(),
        "sizes": [{"chrtID": 20, "techSize": "M", "skus": ["123"]}],
    }


def content(cards=None):
    cards = [card()] if cards is None else cards
    return {
        "cards": cards,
        "cursor": {
            "updatedAt": NOW.isoformat(),
            "nmID": cards[-1]["nmID"] if cards else 10,
            "total": len(cards),
        },
    }


def parse(payload, source="prices", checkpoint=None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return parse_page(
        source,
        checkpoint or {},
        raw,
        organization_id=1,
        marketplace_account_id=2,
        received_at=NOW,
    )


def test_prices_get_exact_money_presence_and_empty_only_completion():
    page = parse(
        goods(
            [
                {
                    "sizeID": 20,
                    "price": 1.01,
                    "discountedPrice": None,
                    "clubDiscountedPrice": 0,
                },
                {"sizeID": 21},
            ]
        )
    )
    assert page.rows[0]["sizes"] == [
        {
            "chrtId": 20,
            "priceKopecks": 101,
            "discountedPriceKopecks": None,
            "clubPriceKopecks": 0,
        },
        {"chrtId": 21},
    ]
    assert not page.complete  # Short nonempty page is NOT terminal for GET.
    assert page.checkpoint == {"offset": 1000}
    manifest = json.loads(page.request_bytes)
    assert manifest["method"] == "GET" and manifest["body"] is None
    assert manifest["query"] == {"limit": 1000, "offset": 0}
    assert parse({"data": {"listGoods": []}}, checkpoint=page.checkpoint).complete


def test_content_native_cursor_incremental_and_full_page():
    page = parse(content([card(nm) for nm in range(1, 101)]), "content")
    assert not page.complete
    _, request = request_for("content", page.checkpoint)
    assert request.jsonBody["settings"]["cursor"] == {"limit": 100, **page.checkpoint}
    assert request.jsonBody["settings"]["sort"] == {"ascending": True}
    empty = parse(content([]), "content", page.checkpoint)
    assert empty.complete and empty.checkpoint == page.checkpoint


def test_completed_content_refresh_uses_native_checkpoint_through_transport():
    # WB explicitly documents carrying the last response cursor into the first
    # request of the next run, not synthesizing a lower timestamp or nmID=0.
    checkpoint = {"updatedAt": "2026-09-09T23:59:59.12345Z", "nmID": 10}
    repository = Repository()
    repository.lease = replace(
        repository.lease, source="content", checkpoint=checkpoint
    )
    requests = []

    def handle(request):
        requests.append(request)
        settings = json.loads(request.content)["settings"]
        assert settings["cursor"] == {"limit": 100, **checkpoint}
        assert settings["sort"] == {"ascending": True}
        return httpx.Response(200, json=content())

    result = run_one_batch(
        repository,
        ReadOnlyWbProvider(transport=httpx.MockTransport(handle)),
        LOCATOR,
        clock=lambda: NOW,
    )
    assert result == {"status": "complete"} and len(requests) == 1
    assert repository.events[-1][0] == "commit"
    assert repository.events[-1][1]["next_checkpoint"] == {
        "updatedAt": NOW.isoformat(),
        "nmID": 10,
    }
    assert checkpoint == {"updatedAt": "2026-09-09T23:59:59.12345Z", "nmID": 10}
    with pytest.raises(WbReadError):
        request_for("content", {**checkpoint, "nmID": 0})
    with pytest.raises(WbReadError):
        parse(content([card(0)]), "content", checkpoint)


@pytest.mark.parametrize(
    "payload,source,checkpoint",
    [
        (b'{"data":1,"data":2}', "prices", {}),
        (b'{"data":{"listGoods":[]},"error":true}', "prices", {}),
        (goods([{"price": 1}]), "prices", {}),
        (goods([{"sizeID": True}]), "prices", {}),
        (goods([{"sizeID": 20, "price": -1}]), "prices", {}),
        (goods([{"sizeID": 20}, {"sizeID": 20}]), "prices", {}),
        (content([card(), card()]), "content", {}),
        (content(), "content", {"updatedAt": NOW.isoformat(), "nmID": 10}),
    ],
)
def test_invalid_page_is_not_published(payload, source, checkpoint):
    with pytest.raises(WbReadError):
        parse(payload, source, checkpoint)


def test_transport_fixed_endpoint_binding_and_no_redirect():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "synthetic-only"
        return httpx.Response(302, headers={"Location": "https://example.invalid"})

    provider = ReadOnlyWbProvider(transport=httpx.MockTransport(handle))
    with pytest.raises(WbReadError):
        provider.fetch(
            "prices",
            {},
            credential=credential(3),
            organization_id=1,
            marketplace_account_id=2,
        )
    assert not requests
    with pytest.raises(WbReadError):
        provider.fetch(
            "prices",
            {},
            credential=credential(),
            organization_id=1,
            marketplace_account_id=2,
        )
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "discounts-prices-api.wildberries.ru"


@pytest.mark.parametrize(
    "status,expected,retryable",
    [
        (429, "WB_SOURCE_RATE_LIMITED", True),
        (401, "WB_SOURCE_ACCESS_DENIED", False),
        (503, "WB_SOURCE_REQUEST_FAILED", True),
    ],
)
def test_errors_never_reflect_provider_body(status, expected, retryable):
    provider = ReadOnlyWbProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                text="sensitive synthetic provider body",
                headers={"Retry-After": "1800"},
            )
        )
    )
    with pytest.raises(WbReadError) as result:
        provider.fetch(
            "prices",
            {},
            credential=credential(),
            organization_id=1,
            marketplace_account_id=2,
        )
    assert str(result.value) == expected
    assert (
        result.value.retryable is retryable and result.value.retry_after_seconds == 1800
    )
    assert result.value.__context__ is None


def test_transport_response_budget():
    provider = ReadOnlyWbProvider(
        max_response_bytes=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 11)
        ),
    )
    with pytest.raises(WbReadError, match="WB_SOURCE_RESPONSE_LIMIT"):
        provider.fetch(
            "prices",
            {},
            credential=credential(),
            organization_id=1,
            marketplace_account_id=2,
        )


class Repository:
    def __init__(self, attempt=1, accept=True):
        self.lease = BatchLease(
            LOCATOR,
            "prices",
            ID,
            UUID(ID),
            1,
            1,
            {},
            attempt,
            ID,
            NOW + timedelta(seconds=120),
        )
        self.accept, self.events = accept, []

    def claim_batch(self, locator):
        assert locator == LOCATOR
        lease, self.lease = self.lease, None
        return lease

    def resolve_for_fetch(self, lease):
        self.events.append("resolve")
        return credential()

    def commit_batch(self, lease, **kwargs):
        self.events.append(("commit", kwargs))
        return self.accept

    def defer_batch(self, lease, **kwargs):
        self.events.append(("defer", kwargs))
        return self.accept

    def fail_batch(self, lease, **kwargs):
        self.events.append(("fail", kwargs))
        return self.accept


def test_duplicate_delivery_no_second_fetch_and_price_floor():
    repository, calls = Repository(), []

    def fetch(*args, **kwargs):
        calls.append(args)
        return parse(goods())

    provider = SimpleNamespace(fetch=fetch)
    assert run_one_batch(repository, provider, LOCATOR, clock=lambda: NOW) == {
        "status": "partial"
    }
    assert run_one_batch(repository, provider, LOCATOR, clock=lambda: NOW) == {
        "status": "skipped"
    }
    assert len(calls) == 1
    publication = repository.events[-1][1]
    assert publication["next_due_at"] == NOW + timedelta(seconds=900)
    assert len(publication["page_digest"]) == 64


@pytest.mark.parametrize(
    "attempt,kind,code",
    [(1, "defer", "WB_RATE_LIMITED"), (8, "fail", "WB_RETRY_EXHAUSTED")],
)
def test_bounded_retry_persists_schedule(attempt, kind, code):
    repository = Repository(attempt)

    def fetch(*args, **kwargs):
        raise WbReadError(
            "WB_SOURCE_RATE_LIMITED", retryable=True, retry_after_seconds=1800
        )

    run_one_batch(repository, SimpleNamespace(fetch=fetch), LOCATOR, clock=lambda: NOW)
    assert repository.events[-1][0] == kind
    assert repository.events[-1][1]["error_code"] == code
    if kind == "defer":
        assert repository.events[-1][1]["next_due_at"] == NOW + timedelta(seconds=1800)


def test_lost_lease_not_success():
    repository = Repository(accept=False)
    assert run_one_batch(
        repository,
        SimpleNamespace(fetch=lambda *a, **k: parse(goods())),
        LOCATOR,
        clock=lambda: NOW,
    ) == {"status": "lease_lost"}


def test_database_failure_no_fallback():
    repository = Repository()

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic database unavailable")

    repository.commit_batch = broken
    with pytest.raises(RuntimeError):
        run_one_batch(
            repository,
            SimpleNamespace(fetch=lambda *a, **k: parse(goods())),
            LOCATOR,
            clock=lambda: NOW,
        )


def test_dispatch_metadata_only_and_visible_broker_failure():
    repository = SimpleNamespace(due_jobs=lambda limit: [LOCATOR])
    calls = []
    assert dispatch_due(repository, lambda *args: calls.append(args)) == {"enqueued": 1}
    assert calls == [(1, 2, ID)]

    def fail(*args):
        raise RuntimeError("synthetic broker secret")

    with pytest.raises(WbLiveError, match="WB_BROKER_UNAVAILABLE") as error:
        dispatch_due(repository, fail)
    assert error.value.__context__ is None


def test_task_entrypoint_locator_validated_before_factory(monkeypatch):
    from app.wb_live import tasks

    monkeypatch.setattr(
        tasks, "_repository", lambda: pytest.fail("invalid locator reached repository")
    )
    with pytest.raises(WbLiveError):
        tasks.run_batch.run(True, 2, ID)
    assert tasks.run_batch.name == "wb_live.run_batch"
    assert tasks.run_batch.acks_late and tasks.run_batch.reject_on_worker_lost


def test_successful_http_fetch_and_checksum():
    provider = ReadOnlyWbProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=goods(), headers={"X-Ratelimit-Retry": "901"}
            )
        )
    )
    page = provider.fetch(
        "prices",
        {},
        credential=credential(),
        organization_id=1,
        marketplace_account_id=2,
    )
    assert page.rows[0]["sizes"][0]["priceKopecks"] == 101
    assert page.retry_after_seconds == 901 and len(page.raw_checksum) == 64


def test_binding_changed_never_fetches():
    repository = Repository()

    def changed(lease):
        raise WbLiveError("WB_BINDING_CHANGED")

    repository.resolve_for_fetch = changed
    result = run_one_batch(
        repository,
        SimpleNamespace(fetch=lambda *a, **k: pytest.fail("fetch")),
        LOCATOR,
        clock=lambda: NOW,
    )
    assert result == {"status": "failed", "error_code": "WB_BINDING_CHANGED"}


@pytest.mark.parametrize("entrypoint", ["run_batch", "dispatch_pending"])
def test_task_redacts_unexpected_database_errors(monkeypatch, entrypoint):
    from app.wb_live import tasks

    def broken():
        raise RuntimeError("synthetic private database DSN")

    monkeypatch.setattr(tasks, "_repository", broken)
    args = (1, 2, ID) if entrypoint == "run_batch" else ()
    with pytest.raises(WbLiveError) as error:
        getattr(tasks, entrypoint).run(*args)
    assert str(error.value) == "WB_LIVE_UNAVAILABLE"
    assert error.value.__context__ is None
