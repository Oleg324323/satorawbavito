"""Explicit one-shot WB read into the existing cache; API remains network-disabled.

No scheduler, price writes, synthetic financial policies or demo catalog copied.
Run from backend: ../.venv/bin/python ops/load_local_wb_readonly.py --org ID
"""
import argparse
import fcntl
import logging
import os
import socket
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


ALLOWED = {
    ("GET", "dp-calendar-api.wildberries.ru", "/api/v1/calendar/promotions"),
    ("GET", "dp-calendar-api.wildberries.ru", "/api/v1/calendar/promotions/details"),
    ("GET", "dp-calendar-api.wildberries.ru", "/api/v1/calendar/promotions/nomenclatures"),
    ("GET", "discounts-prices-api.wildberries.ru", "/api/v2/list/goods/filter"),
    ("POST", "discounts-prices-api.wildberries.ru", "/api/v2/list/goods/filter"),
    ("POST", "content-api.wildberries.ru", "/content/v2/get/cards/list"),
    ("GET", "common-api.wildberries.ru", "/api/v1/tariffs/commission"),
    ("GET", "common-api.wildberries.ru", "/api/v1/seller-info"),
    ("POST", "seller-analytics-api.wildberries.ru", "/api/analytics/v3/sales-funnel/products"),
    ("POST", "seller-analytics-api.wildberries.ru", "/api/analytics/v1/stocks-report/wb-warehouses"),
    ("POST", "seller-analytics-api.wildberries.ru", "/api/v2/stocks-report/products/products"),
    ("GET", "statistics-api.wildberries.ru", "/api/v1/supplier/orders"),
    ("GET", "statistics-api.wildberries.ru", "/api/v1/supplier/sales"),
    ("POST", "finance-api.wildberries.ru", "/api/finance/v1/sales-reports/detailed"),
    ("POST", "finance-api.wildberries.ru", "/api/finance/v1/sales-reports/list"),
    ("POST", "seller-analytics-api.wildberries.ru", "/api/analytics/v3/sales-funnel/products/history"),
    ("GET", "advert-api.wildberries.ru", "/adv/v1/promotion/count"),
    ("GET", "advert-api.wildberries.ru", "/api/advert/v2/adverts"),
    ("GET", "advert-api.wildberries.ru", "/adv/v3/fullstats"),
    ("GET", "advert-api.wildberries.ru", "/adv/v1/budget"),
    ("GET", "advert-api.wildberries.ru", "/adv/v1/balance"),
    ("GET", "advert-api.wildberries.ru", "/adv/v1/upd"),
}


def allowed_request(request):
    prefix = "/api/finance/v1/sales-reports/detailed/"
    exact_finance = (request.method == "POST" and request.url.host == "finance-api.wildberries.ru"
                     and request.url.path.startswith(prefix)
                     and request.url.path[len(prefix):].isascii() and request.url.path[len(prefix):].isdecimal()
                     and not request.url.path[len(prefix):].startswith("0"))
    return (request.url.scheme == "https" and request.url.port in (None, 443)
            and ((request.method, request.url.host, request.url.path) in ALLOWED or exact_finance))


def install_readonly_network_guard(original_connect, original_connect_ex, *, request_allowed=allowed_request, local_broker_port=None):
    """Only allow sockets inside an explicitly allowlisted synchronous HTTP call."""
    import httpx
    original_send = httpx.Client.send
    scope = threading.local()
    def guarded_send(client, request, **kwargs):
        if not request_allowed(request):
            raise RuntimeError("LOCAL_READ_ENDPOINT_NOT_ALLOWED")
        kwargs["follow_redirects"] = False
        previous = getattr(scope, "allowed", False)
        scope.allowed = True
        try:
            return original_send(client, request, **kwargs)
        finally:
            scope.allowed = previous
    def connect(sock, address):
        local_broker = isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] == local_broker_port
        if not getattr(scope, "allowed", False) and not local_broker:
            raise OSError("LOCAL_EXTERNAL_SOCKET_NOT_ALLOWED")
        return original_connect(sock, address)
    def connect_ex(sock, address):
        local_broker = isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] == local_broker_port
        if not getattr(scope, "allowed", False) and not local_broker:
            raise OSError("LOCAL_EXTERNAL_SOCKET_NOT_ALLOWED")
        return original_connect_ex(sock, address)
    httpx.Client.send = guarded_send
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", type=int, required=True)
    parser.add_argument("--sources", default="goods,content,stocks,period-stats,finance")
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--bind-catalog", action="store_true")
    args = parser.parse_args()
    sources = args.sources.split(",")
    if bool(args.date_from) != bool(args.date_to) or (args.date_from and args.date_from > args.date_to):
        raise SystemExit("Specify a valid inclusive date range")
    if not set(sources) <= {"goods", "content", "stocks", "period-stats", "finance", "baskets"}:
        raise SystemExit("Unsupported source")
    os.umask(0o077)
    sys.argv.append("--wb-readonly-data")
    import current_cost_local_preview as preview
    lock = (preview.STATE / "wb-readonly-load.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    logging.disable(logging.CRITICAL)  # Never print upstream payloads or secrets.

    source = preview.STATE / "preview.sqlite"
    target = preview.STATE / "wb-readonly.sqlite"
    with sqlite3.connect(f"file:{target}?mode=rw", uri=True) as db:
        db.execute("ATTACH DATABASE ? AS original", (f"file:{source}?mode=ro",))
        present = db.execute("SELECT count(*) FROM lk_organizations").fetchone()[0]
        if not present:
            # Copy authentication only. Never copy demo costs, policies, goods,
            # financial facts, mappings, assignments or connected demo account.
            for table in ("lk_organizations", "lk_users", "iam_memberships", "lk_user_wb_tokens"):
                db.execute(f"INSERT INTO main.{table} SELECT * FROM original.{table} WHERE organization_id=?", (args.org,))
            for table in ("lk_user_permissions", "lk_sessions"):
                db.execute(f"INSERT INTO main.{table} SELECT * FROM original.{table} WHERE user_id IN (SELECT user_id FROM main.lk_users)")
        tokens = db.execute("SELECT wb_token FROM lk_user_wb_tokens WHERE organization_id=?", (args.org,)).fetchall()
        if len(tokens) != 1 or not tokens[0][0]:
            raise RuntimeError("EXPECTED_ONE_SAVED_WB_TOKEN")
    from app.repricer_sync import refresh_wb_data_sources
    if args.bind_catalog:
        from sqlalchemy import select
        from app.infra.db import get_session_factory
        from app.platform.integrations.wb_credentials import fetch_wb_seller_id
        from app.platform.integrations.orm import MarketplaceAccountRow
        from app.platform.economics.backfill import LegacyCostSnapshot, apply_cost_backfill
        from app.repricer_cache.store import list_cached_goods, get_source_cache
        seller_id = fetch_wb_seller_id(tokens[0][0])
        with get_session_factory()() as session:
            accounts = session.scalars(select(MarketplaceAccountRow).where(
                MarketplaceAccountRow.organization_id == args.org,
                MarketplaceAccountRow.marketplace == "wb")).all()
            if any(a.external_account_id != seller_id for a in accounts):
                raise RuntimeError("ACCOUNT_BINDING_REQUIRES_REVIEW")
            account = accounts[0] if accounts else MarketplaceAccountRow(
                organization_id=args.org, marketplace="wb", external_account_id=seller_id, status="connected")
            session.add(account)
            session.commit()
            snapshot = LegacyCostSnapshot(
                captured_at=datetime.now(timezone.utc), goods=list_cached_goods(args.org),
                content_cards=(get_source_cache(args.org, "content_cards", slim=False) or {}).get("cards", []),
                runtime_state={}, algorithm_settings={}, costs_excel={})
            mapped = apply_cost_backfill(session, organization_id=args.org,
                marketplace_account_id=account.marketplace_account_id, snapshot=snapshot, include_costs=False)
            print({"mapped": mapped.applied_count, "needs_review": mapped.preview.needs_review_count,
                   "rejected": mapped.preview.rejected_count}, flush=True)
    yesterday = datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    result = refresh_wb_data_sources(
        organization_id=args.org, wb_token=tokens[0][0], period_days=7,
        date_from=args.date_from or yesterday-timedelta(days=6), date_to=args.date_to or yesterday,
        sources=sources, force=True, _parallelize=False,
    )
    print({"state": result.get("state"), "steps": [
        {key: step.get(key) for key in ("source", "status", "count")}
        for step in result.get("steps", [])]}, flush=True)
    lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print({"error_type": type(exc).__name__}, flush=True)
        raise SystemExit(1) from None
