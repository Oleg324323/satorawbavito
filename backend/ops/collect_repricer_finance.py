"""Explicit local, read-only WB collection; no price or account mutations.

Resumes durable cursor checkpoints and saves full operations for the repricer.
Does not change existing legacy report projections, COGS or tax policies.
"""
import argparse
from datetime import date, datetime
from pathlib import Path
import os
import socket
import sys
import json
from hashlib import sha256


def restore_saved(org, token):
    """Reuse only complete, current-credential cursor chains; never fragments."""
    from sqlalchemy import select
    from app.infra.db import get_session_factory
    from app.repricer_cache.orm import WbRepricerSourceCacheRow
    from app.repricer_page_finance import save_operations
    from app.wb_api.client import build_wb_finance_client
    from app.wb_api.client import WbApiRequest
    client = build_wb_finance_client(token_override=token)
    groups = {}
    with get_session_factory()() as session:
        saved = session.execute(select(WbRepricerSourceCacheRow.source_key, WbRepricerSourceCacheRow.payload).where(
            WbRepricerSourceCacheRow.organization_id == org,
            WbRepricerSourceCacheRow.source_key.like("wb_report_http_%"))).all()
    for key, payload in saved:
        envelope = payload.get("response") or {}
        request = envelope.get("request") or {}
        body = request.get("jsonBody") or {}
        if request.get("path") != "/api/finance/v1/sales-reports/detailed" or not envelope.get("ok"):
            continue
        if not {"sellerOperName", "forPay", "retailPriceWithDisc", "deliveryService", "paidStorage", "paidAcceptance", "penalty", "deduction"} <= set(body.get("fields") or []):
            continue
        normalized = WbApiRequest.model_validate(request).model_dump(mode="json")
        identity = json.dumps([client.base_url, sha256(token.encode()).hexdigest(), normalized], sort_keys=True, separators=(",", ":"))
        if key != "wb_report_http_" + sha256(identity.encode()).hexdigest():
            continue  # Never import a different account/credential's response.
        grouping = json.dumps({k:v for k,v in body.items() if k != "rrdId"}, sort_keys=True)
        groups.setdefault(grouping, {})[int(body.get("rrdId") or 0)] = envelope
    restored = 0
    for grouping, pages in groups.items():
        cursor, rows, visited = 0, {}, set()
        while cursor in pages and cursor not in visited:
            visited.add(cursor)
            envelope = pages[cursor]
            data = envelope.get("data")
            page = data.get("data") if isinstance(data, dict) else data
            if envelope.get("statusCode") == 204 or page == []:
                body = json.loads(grouping)
                save_operations(org, token, date.fromisoformat(body["dateFrom"]), date.fromisoformat(body["dateTo"]), list(rows.values()),
                                requested_fields=body.get("fields"), report_period=body.get("period", "daily"))
                restored += 1
                break
            if not isinstance(page, list) or not page or any(not isinstance(row, dict) for row in page):
                break
            next_cursor = int(page[-1].get("rrdId") or 0)
            if next_cursor <= cursor:
                break
            for row in page:
                identity = int(row.get("rrdId") or 0)
                if identity <= 0 or identity in rows and rows[identity] != row:
                    raise ValueError("WB_FINANCE_CONFLICTING_CHECKPOINT")
                rows[identity] = row
            cursor = next_cursor
    print({"restoredCompleteWindows": restored}, flush=True)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "ops"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--restore-saved", action="store_true")
    parser.add_argument("--report-id", type=int)
    parser.add_argument("--period", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--organization", type=int, required=True)
    args = parser.parse_args()
    if args.report_id is not None and (args.report_id < 1 or args.restore_saved):
        parser.error("A positive report ID cannot be combined with --restore-saved")
    if not args.restore_saved and (not args.start or not args.end or args.start > args.end or (args.end-args.start).days > 90):
        parser.error("Expected an inclusive period of at most 91 days")
    os.environ.update(VELLA_DATABASE_URL=f"sqlite+pysqlite:///{ROOT / '.local-preview' / 'preview.sqlite'}",
                      VELLA_REDIS_URL="redis://127.0.0.1:59999/0", VELLA_WB_API_MODE="real",
                      VELLA_WB_FINANCE_API_TIMEOUT_SECONDS="90")
    from load_local_wb_readonly import install_readonly_network_guard
    install_readonly_network_guard(socket.socket.connect, socket.socket.connect_ex, local_broker_port=59999)
    from app.cabinet.store import get_organization_wb_token_secret
    from app.repricer_bff import fetch_finance_report_aggregates
    token = get_organization_wb_token_secret(args.organization)
    if not token:
        raise SystemExit("No WB connection in the selected local organization")
    if args.restore_saved:
        restore_saved(args.organization, token)
        return
    result = fetch_finance_report_aggregates(
        "complete", wb_token=token, date_from=datetime.combine(args.start, datetime.min.time()),
        date_to=datetime.combine(args.end, datetime.min.time()), repricer_operations_org=args.organization,
        report_id=args.report_id, report_period=args.period)
    print({"sourceComplete": result["sourceComplete"], "pagesLoaded": result["pagesLoaded"],
           "rowsCount": result["rowsCount"], "dateFrom": str(args.start), "dateTo": str(args.end)}, flush=True)


if __name__ == "__main__":
    main()
