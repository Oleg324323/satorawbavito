from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from app.repricer_cache.store import get_source_cache
from app.wb_api.reports_sources_runtime import WbStockRow


@dataclass(frozen=True)
class DailyStockSnapshot:
    snapshotDate: date
    nmId: int
    warehouseName: str
    availableUnits: int
    wasOutOfStock: bool
    source: str = "captured"


def ensure_daily_stock_history(
    snapshot_date: date,
    rows: Iterable[WbStockRow | int],
    *,
    organization_id: int,
    lookback_days: int = 7,
) -> dict[int, list[DailyStockSnapshot]]:
    """Read observed days only; today's balance cannot reconstruct past stock."""
    wanted = {row if isinstance(row, int) else row.nm_id for row in rows}
    result: dict[int, list[DailyStockSnapshot]] = {}
    for offset in range(lookback_days - 1, -1, -1):
        day = snapshot_date - timedelta(days=offset)
        cached = get_source_cache(organization_id, f"stock_history_{day.isoformat()}") or {}
        for key, row in (cached.get("aggregates") or {}).items():
            nm_id = int(key)
            quantity = row.get("wbStockUnits")
            if nm_id in wanted and type(quantity) is int:
                result.setdefault(nm_id, []).append(DailyStockSnapshot(day, nm_id, "Все склады", quantity, quantity <= 0))
    return result
