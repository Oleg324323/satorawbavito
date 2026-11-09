"""Confirm user-approved saved economics in the local WB database only.

Dry-run by default. Requires an explicit effective date and --apply. No report
downloads, shared schedules, price writes, synthetic accounts or guessed costs.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", required=True, type=int)
    parser.add_argument("--date-from", required=True, type=date.fromisoformat)
    parser.add_argument("--date-to", required=True, type=date.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    # Reuse the local preview's DB path and marketplace-write network guard.
    sys.argv.append("--wb-readonly")
    import current_cost_local_preview as preview
    from sqlalchemy import select
    from app.cabinet.orm import LkUserWbTokenRow
    from app.infra.db import get_session_factory, set_tenant_context
    from app.platform.integrations.orm import MarketplaceAccountRow
    from app.platform.integrations.wb_credentials import fetch_wb_seller_id
    from app.platform.economics.backfill import load_legacy_cost_snapshot, confirm_saved_costs
    from app.platform.economics.wb_quarterly_tax import settings_in_session, year_view, save_year
    from app.platform.period import Period
    from app.wb_pnl_expenses import load_pnl_finance

    logging.disable(logging.CRITICAL)  # Never include provider payloads in errors.
    period = Period(args.date_from, args.date_to)
    if period.date_from.year != period.date_to.year:
        raise ValueError("Confirm one calendar year at a time")
    org = args.organization_id
    finance = load_pnl_finance(org, period.date_from, period.date_to) or {}
    if finance.get("sourceComplete") is not True:
        raise ValueError("Complete saved financial coverage is required")
    ids = set((finance.get("aggregates") or {}).keys())
    snapshot = load_legacy_cost_snapshot(org)
    def included(row):
        return str(row.get("nmID") or row.get("nmId")) in ids
    snapshot = replace(snapshot, goods=[r for r in snapshot.goods if included(r)],
        content_cards=[r for r in snapshot.content_cards if included(r)])
    with get_session_factory()() as db:
        set_tenant_context(db, org)
        tokens = db.scalars(select(LkUserWbTokenRow.wb_token).where(
            LkUserWbTokenRow.organization_id == org)).all()
        if len(set(tokens)) != 1 or not tokens[0]:
            raise ValueError("Exactly one saved WB credential is required")
        # One read-only identity request; never manufacture an account binding.
        seller_id = fetch_wb_seller_id(tokens[0])
        accounts = db.scalars(select(MarketplaceAccountRow).where(
            MarketplaceAccountRow.organization_id == org,
            MarketplaceAccountRow.marketplace == "wb")).all()
        if len(accounts) > 1 or any(a.external_account_id != seller_id or a.status != "connected" for a in accounts):
            raise ValueError("Existing WB account binding needs review")
        settings = settings_in_session(db, org)
        view = year_view(settings, period.date_from.year)
        quarters = tuple(range((period.date_from.month - 1) // 3, (period.date_to.month - 1) // 3 + 1))
        print(json.dumps({"mode": "apply" if args.apply else "preview",
            "effectiveFrom": period.start_at.isoformat(), "productsInFinance": len(ids),
            "savedGarmentCostsRub": settings.get("cogsByGarmentRub"),
            "taxRates": view["rates"], "confirmQuarters": [q + 1 for q in quarters]}), flush=True)
        if not args.apply:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = preview.STATE / "backups" / f"before-wb-economics-confirmation-{stamp}.sqlite"
        backup.parent.mkdir(mode=0o700, exist_ok=True)
        # Exclusive creation prevents replacing the original recovery point.
        with backup.open("xb"):
            pass
        with sqlite3.connect(f"file:{preview.STATE / 'preview.sqlite'}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
        account = accounts[0] if accounts else MarketplaceAccountRow(
            organization_id=org, marketplace="wb", external_account_id=seller_id, status="connected")
        db.add(account)
        db.commit()
        result = confirm_saved_costs(db, organization_id=org,
            marketplace_account_id=account.marketplace_account_id, snapshot=snapshot,
            effective_from=period.start_at)
        # WB-only quarterly confirmation; do not alter shared/Avito policies or
        # tax amounts. Optimistic revision protects concurrent profile edits.
        save_year(db, org, year=period.date_from.year, rates=view["rates"],
            expected_revision=view["revision"], confirmed_quarters=quarters)
        print(json.dumps({**result, "backup": str(Path(backup).relative_to(preview.ROOT))}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"errorType": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
