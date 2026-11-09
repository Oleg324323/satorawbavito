"""One bounded raw ratings GET; no authority, retry, answer write or cache."""

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.avito.reviews import LiveAvitoReviewsClient
from app.platform.integrations.credential_store import ResolvedCredentialForFetch


class AvitoReviewFetchError(ValueError):
    def __init__(self):
        super().__init__("AVITO_REVIEW_FETCH_UNAVAILABLE")


def validate_offset(offset):
    if type(offset) is not int or not 0 <= offset < 2**63:
        raise AvitoReviewFetchError()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AvitoReviewFetchError()
        result[key] = value
    return result


def _invalid_constant(_):
    raise AvitoReviewFetchError()


class _RawResponse:
    """The existing helper consumes already checked status and parsed JSON."""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class _OneGet:
    def __init__(self, client, offset):
        self.client, self.offset = client, offset
        self.calls = 0
        self.started = time.monotonic()

    def get(self, url, *, headers, params=None):
        remaining = 30 - (time.monotonic() - self.started)
        if (
            self.calls != 0
            or remaining <= 0
            or url != "https://api.avito.ru/ratings/v1/reviews"
            or params != {"limit": 50, "offset": self.offset}
        ):
            raise AvitoReviewFetchError()
        self.calls += 1
        maximum = 4 * 1024 * 1024
        # httpx timeouts bound individual blocking operations, NOT total wall
        # time. Elapsed checks bracket stream/chunks; no hard 30s SLA is claimed.
        with self.client.stream(
            "GET",
            url,
            params=params,
            headers={**headers, "Accept-Encoding": "identity"},
            timeout=min(20, remaining),
        ) as response:
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").strip().lower()
                != "identity"
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise AvitoReviewFetchError()
            declared = response.headers.get("content-length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]{1,10}", declared) or int(declared) > maximum
            ):
                raise AvitoReviewFetchError()
            body = bytearray()
            # Do not accumulate 64KiB before observing elapsed time: a trickle
            # stream could hide many blocking reads inside that buffer.
            for chunk in response.iter_raw():
                if (
                    len(body) + len(chunk) > maximum
                    or time.monotonic() - self.started > 30
                ):
                    raise AvitoReviewFetchError()
                body.extend(chunk)
            if time.monotonic() - self.started > 30 or (
                declared is not None and len(body) != int(declared)
            ):
                raise AvitoReviewFetchError()
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
        if type(value) is not dict or any(
            key in value for key in ("accountId", "userId", "sellerId")
        ):
            raise AvitoReviewFetchError()
        rows = value.get("reviews")
        if (
            type(rows) is not list
            or len(rows) > 50
            or any(type(row) is not dict for row in rows)
        ):
            raise AvitoReviewFetchError()
        return _RawResponse(value)


class BoundedAvitoReviewSourceClient:
    def __init__(self, resolved, *, transport=None):
        self._resolved, self._transport = resolved, transport

    def fetch_page(self, *, offset):
        try:
            validate_offset(offset)
            value = self._resolved
            if type(value) is not ResolvedCredentialForFetch:
                raise AvitoReviewFetchError()
            binding, identity = value.binding, value.binding.credential_identity
            if (
                binding.owner.provider != "avito"
                or identity.provider != "avito"
                or identity.credential_kind != "avito_oauth_access"
                or identity.organization_id != binding.owner.organization_id
                or identity.marketplace_account_id
                != binding.owner.marketplace_account_id
                or identity.payload_schema_version != 1
                or type(binding.external_account_id) is not str
                or not re.fullmatch(r"[1-9][0-9]{0,127}", binding.external_account_id)
                or type(identity.expires_at) is not datetime
                or identity.expires_at.utcoffset() is None
                or identity.expires_at <= datetime.now(UTC)
            ):
                raise AvitoReviewFetchError()
            secret = value.secret.reveal()
            if (
                type(secret) is not dict
                or set(secret) != {"accessToken", "expiresAt"}
                or type(secret["accessToken"]) is not str
                or not secret["accessToken"].strip()
                or secret["expiresAt"]
                != identity.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            ):
                raise AvitoReviewFetchError()
            with httpx.Client(
                transport=self._transport,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                edge = _OneGet(client, offset)
                # Reuse exact existing GET/header helper, NOT two-GET fetch or
                # its lossy row normalizer / raw-exception error DTO wrapper.
                result = LiveAvitoReviewsClient(secret["accessToken"])._get_json(
                    edge, "/ratings/v1/reviews", {"limit": 50, "offset": offset}
                )
                if edge.calls != 1:
                    raise AvitoReviewFetchError()
                return tuple(result["reviews"])
        except Exception:  # noqa: BLE001, S110 -- close raw request/token/error context.
            pass
        raise AvitoReviewFetchError()
