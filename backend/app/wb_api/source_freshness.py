"""Revalidate recent WB days hourly and historical days daily for corrections."""
from datetime import datetime, timezone, timedelta


def source_is_fresh(payload, *, now=None):
    observed = payload.get("fetchedAt")
    if not observed:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        fetched = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(str(payload.get("dateTo"))[:10]).date()
    except (ValueError, TypeError):
        return False
    recent = end >= now.astimezone(timezone(timedelta(hours=3))).date() - timedelta(days=7)
    ttl = timedelta(hours=1 if recent else 24)
    return timedelta(0) <= now - fetched <= ttl
