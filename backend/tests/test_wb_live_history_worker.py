"""Synthetic HTTP/staging orchestration, not a database atomicity proof."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
from app.wb_live.history_provider import ReadOnlyWbOrdersProvider
from app.wb_live.worker import run_one_batch

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ID = "11111111-1111-4111-8111-111111111111"
LOCATOR = JobLocator(1, 2, ID)
DATE_FROM = "2026-09-01T00:00:00.12345"


def credential(account=2):
    return ResolvedCredentialForFetch(
        DecryptedCredential({"token": "synthetic-only"}),
        CredentialFetchBinding(
            MarketplaceAccountCredentialOwner(1, account, "wb"),
            "synthetic",
            None,
            CredentialIdentity(1, account, "wb", "wb_api", 1, UUID(ID), 1),
        ),
    )


def payload(count):
    return json.dumps(
        [
            {
                "srid": f"unit-{number}",
                "nmId": 10,
                "isCancel": False,
                "lastChangeDate": "2026-09-02T00:00:00.12345",
            }
            for number in range(count)
        ]
    ).encode()


class Body(httpx.SyncByteStream):
    def __init__(self, raw, trace):
        self.raw, self.trace = raw, trace

    def __iter__(self):
        for offset in range(0, len(self.raw), 4096):
            yield self.raw[offset : offset + 4096]

    def close(self):
        self.trace.append("network_closed")


class Repository:
    def __init__(self):
        self.lease = BatchLease(
            LOCATOR,
            "wb-statistics-supplier-orders",
            ID,
            UUID(ID),
            1,
            1,
            {"dateFrom": DATE_FROM},
            1,
            ID,
            NOW + timedelta(seconds=120),
        )
        self.trace, self.rows, self.receipt = [], [], None
        self.stage_accept = self.commit_accept = True

    def claim_batch(self, locator):
        assert locator == LOCATOR
        lease, self.lease = self.lease, None
        return lease

    def resolve_for_fetch(self, lease):
        self.trace.append("resolve")
        return credential()

    def begin_history_page(self, lease, *, request_checksum):
        self.trace.append("begin")
        self.request_checksum = request_checksum
        return ID

    def stage_history_rows(self, lease, *, page_id, first_ordinal, rows):
        assert page_id == ID and first_ordinal == len(self.rows)
        assert type(rows) is tuple and 0 < len(rows) <= 1000
        self.trace.append(("stage", len(rows)))
        if self.stage_accept:
            self.rows.extend(rows)
        return self.stage_accept

    def commit_history_page(self, lease, *, page_id, end, next_due_at):
        assert self.trace.index("network_closed") < len(self.trace)
        assert end.row_count == len(self.rows)
        assert end.request_checksum == self.request_checksum
        self.trace.append("EOF")
        self.next_due = next_due_at
        if self.commit_accept:
            self.receipt = end
        return self.commit_accept

    def fail_batch(self, lease, *, error_code):
        self.trace.append(("fail", error_code))
        return True

    def defer_batch(self, lease, *, error_code, next_due_at):
        self.trace.append(("defer", error_code))
        self.next_due = next_due_at
        return True


def provider(repository, raw=None, *, status=200, headers=None):
    def handle(request):
        repository.trace.append("HTTP")
        assert "begin" in repository.trace
        assert request.method == "GET"
        assert request.url.host == "statistics-api.wildberries.ru"
        assert request.url.path == "/api/v1/supplier/orders"
        assert dict(request.url.params) == {"dateFrom": DATE_FROM, "flag": "0"}
        assert request.headers["Authorization"] == "synthetic-only"
        return httpx.Response(
            status,
            stream=Body(raw if raw is not None else payload(1), repository.trace),
            headers=headers,
        )

    return ReadOnlyWbOrdersProvider(transport=httpx.MockTransport(handle))


def run(repository, source):
    return run_one_batch(
        repository, object(), LOCATOR, clock=lambda: NOW, history_provider=source
    )


def test_bounded_staging_then_closed_transport_before_EOF():
    repository = Repository()
    result = run(repository, provider(repository, payload(2001)))
    assert result == {"status": "partial"}
    assert [event for event in repository.trace if event != "network_closed"] == [
        "resolve",
        "begin",
        "HTTP",
        ("stage", 1000),
        ("stage", 1000),
        ("stage", 1),
        "EOF",
    ]
    # httpx may close upstream before yielding its final buffered chunk. The
    # required boundary is closed before EOF publication, not after every stage.
    assert repository.trace.index("network_closed") < repository.trace.index("EOF")
    assert not repository.receipt.terminal and repository.receipt.row_count == 2001
    assert repository.next_due == NOW + timedelta(seconds=10800)


def test_duplicate_delivery_does_not_fetch_again():
    repository = Repository()
    source = provider(repository)
    assert run(repository, source) == {"status": "partial"}
    assert run(repository, source) == {"status": "skipped"}
    assert repository.trace.count("HTTP") == 1


def test_empty_EOF_completes_source_without_synthetic_rows():
    repository = Repository()
    assert run(repository, provider(repository, b"[]")) == {"status": "complete"}
    assert repository.rows == [] and repository.receipt.terminal
    assert repository.receipt.next_date_from == DATE_FROM


def test_truncated_stream_keeps_staged_rows_provisional():
    repository = Repository()
    result = run(repository, provider(repository, payload(1001)[:-1]))
    assert result == {"status": "failed", "error_code": "WB_RESPONSE_INVALID"}
    assert len(repository.rows) == 1000 and repository.receipt is None
    assert "EOF" not in repository.trace and "network_closed" in repository.trace


@pytest.mark.parametrize("phase", ["stage", "commit"])
def test_stale_lease_never_claims_publication_success(phase):
    repository = Repository()
    setattr(repository, f"{phase}_accept", False)
    assert run(repository, provider(repository, payload(1001))) == {
        "status": "lease_lost"
    }
    assert repository.receipt is None and "network_closed" in repository.trace


@pytest.mark.parametrize("attempt,expected", [(1, "deferred"), (8, "failed")])
def test_rate_limit_persists_retry_without_inprocess_second_request(attempt, expected):
    repository = Repository()
    repository.lease = replace(repository.lease, attempt=attempt)
    result = run(
        repository, provider(repository, status=429, headers={"Retry-After": "12000"})
    )
    assert result["status"] == expected and repository.trace.count("HTTP") == 1
    assert repository.receipt is None
    if expected == "deferred":
        assert repository.next_due == NOW + timedelta(seconds=12000)
    else:
        assert result["error_code"] == "WB_RETRY_EXHAUSTED"


def test_revoked_binding_never_reaches_begin_or_network():
    repository = Repository()

    def denied(lease):
        raise WbLiveError("WB_BINDING_CHANGED")

    repository.resolve_for_fetch = denied
    assert run(repository, provider(repository)) == {
        "status": "failed",
        "error_code": "WB_BINDING_CHANGED",
    }
    assert "begin" not in repository.trace and "HTTP" not in repository.trace


def test_database_error_propagates_without_memory_publication():
    repository = Repository()

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic database failure")

    repository.stage_history_rows = broken
    with pytest.raises(RuntimeError):
        run(repository, provider(repository))
    assert repository.receipt is None and "network_closed" in repository.trace


def test_no_implicit_history_start_date():
    repository = Repository()
    repository.lease = replace(repository.lease, checkpoint={})
    with pytest.raises(WbLiveError, match="WB_RESPONSE_INVALID"):
        run(repository, provider(repository))
    assert not repository.trace


def test_wrong_account_secret_is_not_used():
    repository = Repository()
    repository.resolve_for_fetch = lambda lease: credential(3)
    assert run(repository, provider(repository)) == {
        "status": "failed",
        "error_code": "WB_BINDING_CHANGED",
    }
    assert "HTTP" not in repository.trace


def test_redirect_never_followed_and_body_never_reflected():
    repository = Repository()
    result = run(
        repository,
        provider(
            repository,
            b"synthetic-sensitive-body",
            status=302,
            headers={"Location": "https://example.invalid"},
        ),
    )
    assert result == {"status": "failed", "error_code": "WB_PROVIDER_UNAVAILABLE"}
    assert repository.trace.count("HTTP") == 1 and repository.receipt is None
