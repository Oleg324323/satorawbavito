"""One read-only bounded WB history stream; no retries, persistence or defaults."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.wb_live.provider import WbReadError, _retry_after
from app.wb_live.statistics_orders import orders_request


@dataclass(frozen=True, slots=True, repr=False)
class OrdersReadStream:
    chunks: Iterator[bytes]
    retry_after_seconds: int


class ReadOnlyWbOrdersProvider:
    def __init__(self, *, transport=None):
        self._transport = transport

    @contextmanager
    def open_page(
        self, *, credential, organization_id, marketplace_account_id, date_from
    ):
        request, _ = orders_request(organization_id, marketplace_account_id, date_from)
        if type(credential) is not ResolvedCredentialForFetch or (
            credential.binding.owner.organization_id,
            credential.binding.owner.marketplace_account_id,
            credential.binding.owner.provider,
            credential.binding.credential_identity.credential_kind,
        ) != (organization_id, marketplace_account_id, "wb", "wb_api"):
            raise WbReadError("WB_SOURCE_BINDING_CHANGED")
        error = None
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                started = time.monotonic()
                with client.stream(
                    "GET",
                    "https://statistics-api.wildberries.ru" + request.path,
                    params=request.query,
                    headers={
                        "Authorization": credential.secret.reveal()["token"],
                        "Accept": "application/json",
                    },
                ) as response:
                    delay = _retry_after(response.headers, datetime.now(UTC))
                    status = response.status_code
                    if status != 200:
                        code = (
                            "WB_SOURCE_RATE_LIMITED"
                            if status == 429
                            else "WB_SOURCE_ACCESS_DENIED"
                            if status in (401, 403)
                            else "WB_SOURCE_BILLING_REQUIRED"
                            if status == 402
                            else "WB_SOURCE_REQUEST_FAILED"
                        )
                        error = WbReadError(
                            code,
                            retryable=status in (408, 429) or 500 <= status <= 599,
                            retry_after_seconds=delay,
                        )
                    else:

                        def chunks():
                            total = 0
                            for chunk in response.iter_bytes(chunk_size=65536):
                                total += len(chunk)
                                if total > 128 * 1024 * 1024:
                                    raise WbReadError("WB_SOURCE_RESPONSE_LIMIT")
                                if time.monotonic() - started > 45:
                                    raise WbReadError(
                                        "WB_SOURCE_TRANSPORT_FAILED", retryable=True
                                    )
                                yield chunk
                            if time.monotonic() - started > 45:
                                raise WbReadError(
                                    "WB_SOURCE_TRANSPORT_FAILED", retryable=True
                                )

                        yield OrdersReadStream(chunks(), delay)
        except httpx.HTTPError:
            error = WbReadError("WB_SOURCE_TRANSPORT_FAILED", retryable=True)
        if error is not None:
            raise error
