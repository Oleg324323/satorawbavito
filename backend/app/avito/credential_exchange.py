"""Strict, opt-in Avito OAuth evidence. Never a credential-store authority.

No import-time transport, implicit refresh, cache, database or logging. Callers
must supply trusted clock, response budget, timeout and paired account identity.
Only the explicit credential.reveal() boundary exposes the secret to its owner.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

from app.security.marketplace_credentials import MAX_SECRET_FIELD_BYTES, DecryptedCredential

_ORIGIN = "https://api.avito.ru"
_ACCOUNT_ID = re.compile(r"[1-9][0-9]*\Z", re.ASCII)
_SAFE_CODES = frozenset({
    "avito_oauth_configuration_invalid", "avito_oauth_input_invalid",
    "avito_oauth_transport_unavailable", "avito_oauth_provider_rejected",
    "avito_oauth_response_unsupported", "avito_oauth_response_too_large",
    "avito_oauth_account_mismatch", "avito_oauth_token_expired",
})


class AvitoAccountExchangeError(ValueError):
    """Allowlisted error only; never includes a request/response or raw cause."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_CODES else "avito_oauth_response_unsupported"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"AvitoAccountExchangeError(code={self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedAvitoAccountToken:
    credential: DecryptedCredential
    external_account_id: str
    exchange_started_at: datetime
    verified_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return "<VerifiedAvitoAccountToken redacted>"

    __str__ = __repr__

    def __copy__(self) -> NoReturn:
        raise AvitoAccountExchangeError("avito_oauth_input_invalid")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise AvitoAccountExchangeError("avito_oauth_input_invalid")

    def __reduce__(self) -> NoReturn:
        raise AvitoAccountExchangeError("avito_oauth_input_invalid")

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        raise AvitoAccountExchangeError("avito_oauth_input_invalid")


def _reject(code: str = "avito_oauth_response_unsupported") -> NoReturn:
    raise AvitoAccountExchangeError(code)


def _secret(value: Any, code: str) -> str:
    if type(value) is not str or not value.strip():
        _reject(code)
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        _reject(code)
    if size > MAX_SECRET_FIELD_BYTES:
        _reject(code)
    return value


def _utc_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _reject("avito_oauth_configuration_invalid")
    return value.astimezone(timezone.utc)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _no_constant(_value):
    _reject()


def _bounded_json(client, method: str, path: str, *, headers: dict,
                  timeout_seconds: float, max_response_bytes: int, data=None) -> dict:
    with client.stream(method, _ORIGIN + path, headers=headers, data=data,
                       timeout=timeout_seconds, follow_redirects=False, auth=None) as response:
        # Redirects are rejected, not followed. Error bodies are not read.
        if response.status_code != 200:
            _reject("avito_oauth_provider_rejected")
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            _reject()
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            _reject()
        declared = response.headers.get("content-length")
        if declared is not None:
            if not re.fullmatch(r"[0-9]+", declared, flags=re.ASCII):
                _reject()
            if int(declared) > max_response_bytes:
                _reject("avito_oauth_response_too_large")
        body = bytearray()
        # iter_raw, never iter_bytes/json(), so compressed input is not inflated.
        for chunk in response.iter_raw(chunk_size=min(max_response_bytes + 1, 65_536)):
            if len(body) + len(chunk) > max_response_bytes:
                _reject("avito_oauth_response_too_large")
            body.extend(chunk)
        if declared is not None and len(body) != int(declared):
            _reject()
        try:
            payload = json.loads(body.decode("utf-8", errors="strict"),
                                 object_pairs_hook=_unique_object, parse_constant=_no_constant)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            _reject()
        if type(payload) is not dict:
            _reject()
        return payload


def _exchange(client, client_id: str, client_secret: str, *, expected_external_account_id: str,
              max_response_bytes: int, timeout_seconds: float,
              clock: Callable[[], datetime]) -> VerifiedAvitoAccountToken:
    started = _utc_clock(clock)
    # Conservative: exchange latency never extends expiry, subsecond time floors.
    expiry_base = started.replace(microsecond=0)
    payload = _bounded_json(client, "POST", "/token", timeout_seconds=timeout_seconds,
                            max_response_bytes=max_response_bytes,
                            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                            data={"grant_type": "client_credentials", "client_id": client_id,
                                  "client_secret": client_secret})
    access_token = _secret(payload.get("access_token"), "avito_oauth_response_unsupported")
    if payload.get("token_type") != "Bearer":
        _reject()
    expires_in = payload.get("expires_in")
    if type(expires_in) is not int or expires_in <= 0:
        _reject()
    try:
        expires_at = expiry_base + timedelta(seconds=expires_in)
    except (OverflowError, ValueError):
        _reject()
    # A bearer value must be representable as a single HTTP header. Unsupported
    # non-ASCII/control/whitespace forms fail closed, never normalize a secret.
    if any(ord(char) < 33 or ord(char) > 126 for char in access_token):
        _reject()
    account = _bounded_json(client, "GET", "/core/v1/accounts/self",
                            timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
                            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                     "Authorization": "Bearer " + access_token})
    identity = account.get("id")
    if type(identity) is int and identity > 0:
        identity = str(identity)
    elif type(identity) is not str or _ACCOUNT_ID.fullmatch(identity) is None:
        _reject()
    if identity != expected_external_account_id:
        _reject("avito_oauth_account_mismatch")
    verified_at = _utc_clock(clock)
    if verified_at < started:
        _reject("avito_oauth_configuration_invalid")
    if expires_at <= verified_at:
        _reject("avito_oauth_token_expired")
    return VerifiedAvitoAccountToken(
        credential=DecryptedCredential({"accessToken": access_token,
                                        "expiresAt": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")}),
        external_account_id=identity, exchange_started_at=started,
        verified_at=verified_at, expires_at=expires_at,
    )


def fetch_verified_account_token(
    client_id: str, client_secret: str, *, expected_external_account_id: str,
    max_response_bytes: int, clock: Callable[[], datetime], base_url: str,
    timeout_seconds: float, client=None,
) -> VerifiedAvitoAccountToken:
    """All injected dependencies are trusted; client implements httpx.Client.stream.

    Caller-owned clients are not closed and must not carry logging hooks, cookies,
    implicit retry transports or alternative network routing. Inject a mock
    transport for tests, not a different URL. No authorization to persist implied.
    """
    error_code = "avito_oauth_transport_unavailable"
    try:
        if base_url != _ORIGIN or type(max_response_bytes) is not int or max_response_bytes <= 0:
            _reject("avito_oauth_configuration_invalid")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            _reject("avito_oauth_configuration_invalid")
        if not callable(clock):
            _reject("avito_oauth_configuration_invalid")
        if type(expected_external_account_id) is not str or _ACCOUNT_ID.fullmatch(expected_external_account_id) is None:
            _reject("avito_oauth_input_invalid")
        _secret(client_id, "avito_oauth_input_invalid")
        _secret(client_secret, "avito_oauth_input_invalid")
        kwargs = dict(expected_external_account_id=expected_external_account_id,
                      max_response_bytes=max_response_bytes, clock=clock, timeout_seconds=timeout_seconds)
        if client is not None:
            return _exchange(client, client_id, client_secret, **kwargs)
        import httpx

        with httpx.Client(follow_redirects=False, trust_env=False,
                          timeout=httpx.Timeout(timeout_seconds)) as owned:
            return _exchange(owned, client_id, client_secret, **kwargs)
    except AvitoAccountExchangeError as error:
        error_code = error.code
    except Exception:
        # No raw exception, response, header or validation detail crosses this
        # boundary. Raise OUTSIDE the except block to omit exception chaining.
        pass
    raise AvitoAccountExchangeError(error_code)
