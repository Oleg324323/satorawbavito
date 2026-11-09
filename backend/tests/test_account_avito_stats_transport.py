"""Synthetic HTTP only. The existing totals parser remains exercised."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.avito.account_stats import (
    AccountStatsError,
    BoundedAvitoTotalsClient,
    project_stats_result,
)
from app.avito.stats import AvitoStatsFetchRequest
from app.platform.integrations.credential_store import (
    CredentialFetchBinding,
    MarketplaceAccountCredentialOwner,
    ResolvedCredentialForFetch,
)
from app.security.marketplace_credentials import CredentialIdentity, DecryptedCredential


def resolved(*, expired=False, external="123"):
    expiry = (datetime.now(UTC) + timedelta(hours=-1 if expired else 1)).replace(
        microsecond=0
    )
    return ResolvedCredentialForFetch(
        DecryptedCredential(
            {
                "accessToken": "synthetic-only-canary",
                "expiresAt": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ),
        CredentialFetchBinding(
            MarketplaceAccountCredentialOwner(1, 2, "avito"),
            external,
            None,
            CredentialIdentity(
                1, 2, "avito", "avito_oauth_access", 1, uuid4(), 1, expiry
            ),
        ),
    )


def request():
    return AvitoStatsFetchRequest(
        dateFrom=date(2026, 9, 1),
        dateTo=date(2026, 9, 1),
        accountIds=["123"],
        grouping="totals",
    )


def payload(value=9007199254740993):
    return {
        "result": {
            "groupings": [
                {
                    "type": "totals",
                    "metrics": [
                        {"slug": "allSpending", "value": value},
                        {"slug": "views", "value": 0},
                        {"slug": "orderedItems", "value": None},
                    ],
                }
            ]
        }
    }


def response(data):
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(data).encode()),
    )


@pytest.mark.parametrize("missing", ["null", "metric", "day"])
def test_incomplete_daily_coverage_cannot_become_complete_total(missing):
    query = request()
    query.dateTo = date(2026, 9, 2)
    groups = [
        {
            "type": "date",
            "date": "2026-09-01",
            "metrics": [{"slug": "views", "value": 5}],
        },
        {
            "type": "date",
            "date": "2026-09-02",
            "metrics": [{"slug": "views", "value": 0}],
        },
    ]
    if missing == "null":
        groups[1]["metrics"][0]["value"] = None
    elif missing == "metric":
        groups[1]["metrics"] = [{"slug": "orderedItems", "value": 2}]
    else:
        groups.pop()
    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(),
            transport=httpx.MockTransport(
                lambda req: response({"result": {"groupings": groups}})
            ),
        ).fetch_stats(query)


def test_complete_daily_zero_is_observation_not_missing():
    query = request()
    query.dateTo = date(2026, 9, 2)
    groups = [
        {
            "type": "date",
            "date": "2026-09-01",
            "metrics": [{"slug": "views", "value": 5}],
        },
        {
            "type": "date",
            "date": "2026-09-02",
            "metrics": [{"slug": "views", "value": 0}],
        },
    ]
    result = BoundedAvitoTotalsClient(
        resolved(),
        transport=httpx.MockTransport(
            lambda req: response({"result": {"groupings": groups}})
        ),
    ).fetch_stats(query)
    value = project_stats_result(
        result, external_id="123", date_from=query.dateFrom, date_to=query.dateTo
    )
    assert value["rows"][0]["metrics"]["views"] == "5"
    assert [row["metrics"]["views"] for row in value["daily"]] == ["5", "0"]


def test_one_exact_totals_request_and_lossless_existing_parser():
    calls = []

    def respond(req):
        calls.append(req)
        return response(payload())

    value = BoundedAvitoTotalsClient(
        resolved(), transport=httpx.MockTransport(respond)
    ).fetch_stats(request())
    projected = project_stats_result(
        value, external_id="123", date_from=date(2026, 9, 1), date_to=date(2026, 9, 1)
    )
    assert projected["rows"][0]["metrics"]["spendKopecks"] == "9007199254740993"
    assert projected["rows"][0]["metrics"]["views"] == "0"
    assert projected["rows"][0]["metrics"]["orders"] is None
    assert len(calls) == 1 and calls[0].method == "POST"
    assert str(calls[0].url) == "https://api.avito.ru/stats/v2/accounts/123/items"
    body = json.loads(calls[0].content)
    assert body["grouping"] == "totals" and body["limit"] == 1 and body["offset"] == 0


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_rejected_provider_never_retry_redirect_or_expose_body(status):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(
            status,
            headers={"location": "https://example.invalid/secret"},
            text="synthetic-sensitive",
        )

    with pytest.raises(AccountStatsError) as error:
        BoundedAvitoTotalsClient(
            resolved(), transport=httpx.MockTransport(respond)
        ).fetch_stats(request())
    assert len(calls) == 1 and error.value.__context__ is None
    assert str(error.value) == "AVITO_ACCOUNT_STATS_UNAVAILABLE"


@pytest.mark.parametrize("value", [True, -1, 1.5, "9007199254740993"])
def test_noncanonical_metrics_not_coerced(value):
    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(),
            transport=httpx.MockTransport(lambda req: response(payload(value))),
        ).fetch_stats(request())


@pytest.mark.parametrize("case", ["expired", "foreign", "item", "external_path"])
def test_invalid_authority_or_request_never_calls_provider(case):
    calls = []
    value = request()
    if case == "foreign":
        value.accountIds = ["456"]
    if case == "item":
        value.grouping = "item"
    credential = resolved(
        expired=case == "expired",
        external="../123" if case == "external_path" else "123",
    )
    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            credential, transport=httpx.MockTransport(lambda req: calls.append(req))
        ).fetch_stats(value)
    assert calls == []


def test_declared_oversize_body_is_not_consumed():
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("oversize body consumed")

    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    headers={
                        "content-type": "application/json",
                        "content-length": str(8 * 1024 * 1024),
                    },
                    stream=Body(),
                )
            ),
        ).fetch_stats(request())


@pytest.mark.parametrize(
    "case", ["mixed_metrics", "incomplete_page", "duplicate_metric", "duplicate_days"]
)
def test_ambiguous_or_incomplete_payload_not_published(case):
    data = payload()
    if case == "mixed_metrics":
        data["result"]["metrics"] = [{"slug": "views", "value": 1.5}]
    elif case == "incomplete_page":
        data["result"]["dataTotalCount"] = 2
    elif case == "duplicate_metric":
        data["result"]["groupings"][0]["metrics"].append({"slug": "views", "value": 3})
    else:
        group = {"type": "date", "date": "2026-09-01", "metrics": []}
        data["result"]["groupings"] = [group, group]
    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(), transport=httpx.MockTransport(lambda req: response(data))
        ).fetch_stats(request())


def test_undeclared_stream_budget_closes_response_without_reading_tail():
    events = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(65):
                yield b" " * 65536
            pytest.fail("read beyond bounded body")

        def close(self):
            events.append("closed")

    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=Body()
                )
            ),
        ).fetch_stats(request())
    assert events == ["closed"]


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-encoding": "gzip"}, b"synthetic-not-gzip"),
        ({}, b'{"result":{},"result":{}}'),
        ({}, b'{"result":{"metrics":[{"slug":"views","value":NaN}]}}'),
    ],
)
def test_compressed_or_duplicate_json_or_nan_rejected(headers, body):
    with pytest.raises(AccountStatsError):
        BoundedAvitoTotalsClient(
            resolved(),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    headers={"content-type": "application/json", **headers},
                    stream=httpx.ByteStream(body),
                )
            ),
        ).fetch_stats(request())
