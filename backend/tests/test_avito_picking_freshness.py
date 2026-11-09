from datetime import datetime, timezone
import pytest
from app.avito.orders import AvitoOrdersBrowserSnapshot
from app.routers.avito_orders import _picking_freshness

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

@pytest.mark.parametrize("time,checkpoint,account,expected", [
    ("2026-10-08T11:59:00Z", False, "a", True),
    ("2026-10-08T11:49:00Z", False, "a", False),
    ("2026-10-08T12:01:00Z", False, "a", False),
    ("2026-10-08T11:59:00Z", True, "a", False),
    ("2026-10-08T11:59:00Z", False, "b", False),
    ("2026-10-08T11:59:00", False, "a", False),
])
def test_observation_coverage_and_account(time, checkpoint, account, expected):
    snapshot = AvitoOrdersBrowserSnapshot.model_validate({"capturedAt": time,
        "collector": {"status": "completed", "checkpoint": checkpoint},
        "orders": [{"orderId": "70000000532427280", "accountId": "a", "status": "ready_to_ship", "items": []}]})
    assert _picking_freshness({}, snapshot, account, NOW)["fresh"] is expected

def test_missing_or_old_observation_is_not_rejuvenated_by_save_time():
    assert not _picking_freshness({"savedAt": NOW.isoformat(), "complete": True}, None, None, NOW)["fresh"]

def test_account_a_export_from_fresh_combined_snapshot():
    snapshot = AvitoOrdersBrowserSnapshot.model_validate({"capturedAt": "2026-10-08T11:59:00Z",
        "collector": {"status": "completed", "checkpoint": False, "coveredAccountIds": ["a", "b"]},
        "orders": [{"orderId": account, "accountId": account, "status": "ready_to_ship", "items": []} for account in ["a", "b"]]})
    assert _picking_freshness({}, snapshot, "a", NOW)["fresh"]

def test_account_a_collection_does_not_certify_stale_account_b_export():
    snapshot = AvitoOrdersBrowserSnapshot.model_validate({"capturedAt": "2026-10-08T11:59:00Z",
        "collector": {"status": "completed", "checkpoint": False, "coveredAccountIds": ["a"]},
        "orders": [{"orderId": "a", "accountId": "a", "status": "ready_to_ship", "items": []}]})
    cached = {"rows": [{"orderId": "b", "accountId": "b", "status": "ready_to_ship", "items": []}]}
    assert not _picking_freshness(cached, snapshot, None, NOW)["fresh"]
    assert _picking_freshness(cached, snapshot, "a", NOW)["fresh"]

def test_recent_scan_does_not_certify_unseen_old_order_of_same_account():
    snapshot = AvitoOrdersBrowserSnapshot.model_validate({"capturedAt": "2026-10-08T11:59:00Z",
        "collector": {"status": "completed", "checkpoint": False, "coveredAccountIds": ["a"]},
        "orders": [{"orderId": "new", "accountId": "a", "status": "ready_to_ship", "items": []}]})
    cached = {"rows": [{"orderId": "old", "accountId": "a", "status": "ready_to_ship", "items": []}]}
    assert not _picking_freshness(cached, snapshot, "a", NOW)["fresh"]

def test_exact_browser_identity_resolves_missing_api_account_without_mutating_cache():
    snapshot = AvitoOrdersBrowserSnapshot.model_validate({"capturedAt": "2026-10-08T11:59:00Z",
        "collector": {"status": "completed", "checkpoint": False, "coveredAccountIds": ["a"]},
        "orders": [{"orderId": "70000000532427280", "accountId": "a", "status": "ready_to_ship", "items": []}]})
    cached = {"rows": [{"orderId": "api-id", "marketplaceId": "70000000532427280", "accountId": None, "status": "ready_to_ship", "items": []}]}
    assert _picking_freshness(cached, snapshot, None, NOW)["fresh"]
    assert cached['rows'][0]['accountId'] is None
    snapshot.orders.append(snapshot.orders[0].model_copy(update={'accountId': 'b'}))
    snapshot.collector['coveredAccountIds'] = ['a', 'b']
    assert not _picking_freshness(cached, snapshot, None, NOW)["fresh"]
