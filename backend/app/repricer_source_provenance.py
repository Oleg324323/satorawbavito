"""Source evidence shared by WB projections and strategy cache consumers."""
from datetime import datetime, timezone
from app.wb_api.source_freshness import source_is_fresh

FINANCE_BINDING_VERSION = "wb-finance-source-binding-v1"


def source_timestamp(value):
    # Match source_is_fresh: SQLite cache timestamps without an offset are UTC.
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def oldest_source_observation(payload):
    """An explicitly invalid timestamp must not fall back to a newer one."""
    values = [payload.get("fetchedAt")]
    values.extend(payload[key] for key in ("oldestFetchedAt", "oldestSourceFetchedAt") if key in payload)
    parsed = [source_timestamp(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        return None
    # An 'oldest' alias after its receipt time is contradictory evidence.
    return min(parsed) if all(value <= parsed[0] for value in parsed[1:]) else None


def source_observations_fresh(payload, *, now, date_to=None):
    values = [payload.get("fetchedAt")]
    values.extend(payload[key] for key in ("oldestFetchedAt", "oldestSourceFetchedAt") if key in payload)
    return oldest_source_observation(payload) is not None and all(source_timestamp(value) is not None and source_is_fresh(
        {"fetchedAt": value, "dateTo": date_to or payload.get("dateTo")}, now=now,
    ) for value in values)


def daily_source_evidence(payload, day):
    """Daily snapshots retain their own observation across aggregate refreshes."""
    if "dailySourceEvidence" in payload:
        evidence = payload["dailySourceEvidence"]
        selected = evidence.get(day) if isinstance(evidence, dict) else None
        return selected if isinstance(selected, dict) else {}
    # Older merged snapshots cannot prove the ownership/time of retained days.
    if any(key in payload for key in ("dailyDetailFetchedAt", "dailyDetailPreservedAt", "dailyDetailPreservedBy")):
        return {}
    return payload


def source_binding_matches(payload, *, organization_id, fingerprint, connection_key=None):
    if not fingerprint:
        return False
    if "organizationId" in payload and (type(payload["organizationId"]) is not int
                                       or payload["organizationId"] != organization_id):
        return False
    claimed = payload.get("strategyTokenFingerprint")
    binding = payload.get("strategySourceBinding")
    if binding is None:
        return claimed == fingerprint
    # Financial receipts are selected from an org-scoped, connection-keyed
    # namespace. This evidence also supports a trusted per-request connection
    # when its token differs from the organization's default token.
    return (isinstance(binding, dict)
        and binding.get("version") == FINANCE_BINDING_VERSION
        and type(binding.get("organizationId")) is int
        and binding["organizationId"] == organization_id
        and bool(connection_key) and binding.get("connectionKey") == connection_key
        and payload.get("source") == "finance_sales_reports_detailed"
        and claimed in (None, fingerprint))
