"""Explicit approved policy and injected atomic Redis limits. No import-time I/O."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, fields

from app.platform.integrations.ingestion_tokens import VerifiedIngestionToken, _validate_owner

_INT4_MAX = 2**31 - 1
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z", re.ASCII)
_CONFIG_CEILING = 4096  # Parser allocation ceiling, not an operational policy.
_NAMESPACE = "satorna:ingestion:v1:"
_COUNTER = """
local value = redis.call('GET', KEYS[1])
local cap = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
if value then
  local count = tonumber(value)
  if not count or count < 1 or count ~= math.floor(count) then return -1 end
  local ttl = redis.call('TTL', KEYS[1])
  if ttl < 0 or ttl > window then return -1 end
  if count >= cap then return 0 end
end
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], window) end
return 1
"""


class IngestionLimitError(ValueError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in {
            "ingestion_policy_unavailable", "ingestion_rate_limited", "ingestion_limits_unavailable",
            "ingestion_peer_unavailable",
        } else "ingestion_limits_unavailable"
        super().__init__(self.code)


def _positive(value):
    return type(value) is int and 0 < value <= _INT4_MAX


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise IngestionLimitError("ingestion_policy_unavailable")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class IngestionPolicy:
    """All values are owner-approved input; absence never means default approval.

    Peer policy identifies the bootstrap's actual direct-peer/proxy trust decision.
    The resolver paired with it must return a server-verified numeric IP address.
    """

    approval_reference: str
    approval_version: int
    max_token_lifetime_seconds: int
    max_body_bytes: int
    ip_bucket_capacity: int
    ip_bucket_window_seconds: int
    token_bucket_capacity: int
    token_bucket_window_seconds: int
    peer_policy_reference: str
    peer_policy_version: int

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in {"approval_reference", "peer_policy_reference"}:
                valid = type(value) is str and _REFERENCE.fullmatch(value) is not None
            else:
                valid = _positive(value)
            if not valid:
                raise IngestionLimitError("ingestion_policy_unavailable")

    def require_decoder_limit(self, decoder_max_body_bytes: int) -> None:
        self.__post_init__()
        if not _positive(decoder_max_body_bytes) or self.max_body_bytes > decoder_max_body_bytes:
            raise IngestionLimitError("ingestion_policy_unavailable")


def load_ingestion_policy(raw: str | None, *, decoder_max_body_bytes: int) -> IngestionPolicy:
    """Called only by explicit ingestion bootstrap, never global Settings startup."""
    try:
        if type(raw) is not str or not 0 < len(raw) <= _CONFIG_CEILING:
            raise IngestionLimitError("ingestion_policy_unavailable")
        values = json.loads(raw, object_pairs_hook=_pairs)
        if type(values) is not dict or set(values) != {field.name for field in fields(IngestionPolicy)}:
            raise IngestionLimitError("ingestion_policy_unavailable")
        policy = IngestionPolicy(**values)
        policy.require_decoder_limit(decoder_max_body_bytes)
        return policy
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise IngestionLimitError("ingestion_policy_unavailable") from None


class IngestionLimits:
    """A trusted bootstrap injects an async Redis client; errors always deny.

    Fixed window counters use one atomic script. Hashes are cache keys only, not
    credentials or audit identifiers. No unverified token locator allocates a key.
    """

    def __init__(self, *, redis_client, policy: IngestionPolicy):
        if type(policy) is not IngestionPolicy:
            raise IngestionLimitError("ingestion_policy_unavailable")
        policy.__post_init__()
        self._redis = redis_client
        self.policy = policy

    async def _take(self, key: str, capacity: int, window: int):
        try:
            result = await self._redis.eval(_COUNTER, 1, key, capacity, window)
        except Exception:
            raise IngestionLimitError("ingestion_limits_unavailable") from None
        if type(result) is not int or result not in (0, 1):
            raise IngestionLimitError("ingestion_limits_unavailable")
        if result == 0:
            raise IngestionLimitError("ingestion_rate_limited")

    async def check_peer(self, trusted_peer_ip: str):
        """Input comes from the approved server peer resolver, never a header."""
        try:
            if type(trusted_peer_ip) is not str or len(trusted_peer_ip) > 45 or "%" in trusted_peer_ip:
                raise ValueError()
            peer = ipaddress.ip_address(trusted_peer_ip)
            # IPv4-mapped IPv6 and IPv4 share the same bucket.
            peer = getattr(peer, "ipv4_mapped", None) or peer
            digest = hashlib.sha256(peer.packed).hexdigest()
        except (ValueError, TypeError):
            raise IngestionLimitError("ingestion_peer_unavailable") from None
        finally:
            trusted_peer_ip = None
        await self._take(_NAMESPACE + "ip:" + digest,
                         self.policy.ip_bucket_capacity, self.policy.ip_bucket_window_seconds)

    async def check_verified_token(self, verified: VerifiedIngestionToken):
        # Trusted internal call only, after the verifier returned successfully.
        # This DTO and its hash are not authorization for the durable sink.
        if type(verified) is not VerifiedIngestionToken:
            raise IngestionLimitError("ingestion_limits_unavailable")
        _validate_owner(verified.owner)
        identity = f"{verified.owner.organization_id}:{verified.owner.marketplace_account_id}:{verified.token_id}"
        digest = hashlib.sha256(identity.encode("ascii")).hexdigest()
        await self._take(_NAMESPACE + "token:" + digest,
                         self.policy.token_bucket_capacity, self.policy.token_bucket_window_seconds)
