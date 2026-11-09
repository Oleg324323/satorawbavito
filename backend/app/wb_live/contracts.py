"""Worker handoff contains metadata only; provider secrets never enter jobs."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

SOURCES = ("content", "prices")
ACTIVE_STATES = ("queued", "running", "partial")
ERROR_CODES = frozenset({"WB_LIVE_UNAVAILABLE", "WB_ACCESS_DENIED", "WB_BINDING_CHANGED",
    "WB_RATE_LIMITED", "WB_PROVIDER_UNAVAILABLE", "WB_RESPONSE_INVALID", "WB_RETRY_EXHAUSTED",
    "WB_LEASE_LOST", "WB_BROKER_UNAVAILABLE", "WB_SYNC_CONFLICT"})

class WbLiveError(ValueError):
    def __init__(self, code="WB_LIVE_UNAVAILABLE"):
        self.code = code if code in ERROR_CODES else "WB_LIVE_UNAVAILABLE"
        super().__init__(self.code)

@dataclass(frozen=True, slots=True)
class JobLocator:
    org_id: int
    account_id: int
    job_id: str

    def __post_init__(self):
        if (type(self.org_id) is not int or self.org_id <= 0 or type(self.account_id) is not int
                or self.account_id <= 0 or type(self.job_id) is not str):
            raise WbLiveError("WB_SYNC_CONFLICT")
        try:
            UUID(self.job_id)
        except (ValueError, TypeError):
            raise WbLiveError("WB_SYNC_CONFLICT") from None

@dataclass(frozen=True, slots=True)
class BatchLease:
    locator: JobLocator
    source: str
    lease_token: str
    credential_id: UUID
    generation: int
    account_incarnation: int
    checkpoint: dict[str, Any]
    attempt: int
    run_id: str
    expires_at: datetime
