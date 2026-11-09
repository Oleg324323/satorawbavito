"""Purpose-bound cursor MACs. Snapshot expiry and live authorization remain in DB."""

import base64
import binascii
import hashlib
import hmac
import json
import re

from app.modules.orders import OrderContractValidationError
from app.platform.integrations.publication_guard import UserSessionPrincipal

_PURPOSE = b"satorna.orders.cursor.v1\x00"
_FIELDS = {"version", "context", "accounts", "snapshot", "position", "query"}


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _encode(decoded) != value:
        raise ValueError
    return decoded


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError


def _context(principal, accounts, checksum):
    if (
        type(principal) is not UserSessionPrincipal
        or type(accounts) is not tuple
        or not accounts
    ):
        raise ValueError
    if any(type(value) is not int or not 0 < value < 2**31 for value in accounts):
        raise ValueError
    if (
        tuple(sorted(set(accounts))) != accounts
        or type(checksum) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
    ):
        raise ValueError
    return [
        principal.organization_id,
        principal.user_id,
        principal.membership_id,
        principal.session_id,
    ]


class OrdersCursorCodec:
    __slots__ = ("_key",)

    def __init__(self, key: bytes):
        if type(key) is not bytes or len(key) < 32:
            raise OrderContractValidationError("orders_cursor_key_invalid")
        self._key = key

    def __repr__(self):
        return "<OrdersCursorCodec redacted>"

    def __reduce_ex__(self, protocol):
        raise TypeError("orders_cursor_key_private")

    def issue(
        self, *, principal, accounts, snapshot_id, after_position, query_checksum
    ):
        try:
            context = _context(principal, accounts, query_checksum)
            _integer(snapshot_id)
            _integer(after_position)
            if snapshot_id == 0:
                raise ValueError
            payload = _encode(
                _json(
                    {
                        "version": 1,
                        "context": context,
                        "accounts": accounts,
                        "snapshot": snapshot_id,
                        "position": after_position,
                        "query": query_checksum,
                    }
                )
            )
            signature = _encode(
                hmac.digest(
                    self._key, _PURPOSE + payload.encode("ascii"), hashlib.sha256
                )
            )
            token = payload + "." + signature
            if len(token) > 16384:
                raise ValueError
            return token
        except (ValueError, TypeError):
            raise OrderContractValidationError("orders_cursor_invalid") from None

    def parse(self, token, *, principal, accounts, query_checksum) -> tuple[int, int]:
        try:
            expected_context = _context(principal, accounts, query_checksum)
            if type(token) is not str or len(token) > 16384:
                raise ValueError
            payload, signature = token.split(".")
            actual = _decode(signature)
            expected = hmac.digest(
                self._key, _PURPOSE + payload.encode("ascii"), hashlib.sha256
            )
            if not hmac.compare_digest(actual, expected):
                raise ValueError
            raw = _decode(payload)
            value = json.loads(raw)
            if type(value) is not dict or set(value) != _FIELDS or _json(value) != raw:
                raise ValueError
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError
            if (
                _json(value["context"]) != _json(expected_context)
                or _json(value["accounts"]) != _json(accounts)
                or value["query"] != query_checksum
            ):
                raise ValueError
            _integer(value["snapshot"])
            _integer(value["position"])
            if value["snapshot"] == 0:
                raise ValueError
            return value["snapshot"], value["position"]
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
            raise OrderContractValidationError("orders_cursor_invalid") from None
