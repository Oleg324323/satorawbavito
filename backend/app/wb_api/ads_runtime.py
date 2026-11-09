from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import time
from typing import Any, Callable, Literal

from vella_wb_19_05.models import Confidence, SourceEvidence, SourceStatus, utc_now

from app.config import get_settings
from app.wb_api.client import FakeWbApiClient, RateLimitedWbApiClient, WbApiRequest, WbApiResponseEnvelope, build_wb_ads_client


AttributionLevel = Literal["exact_sku", "campaign_sku", "campaign_only", "unknown"]
ADS_DATE_WINDOW_MAX_DAYS = 31
ADS_RETRY_MAX_ATTEMPTS = 3
ADS_FULLSTATS_MIN_INTERVAL_S = 20.0
_ads_fullstats_last_request_at = 0.0


@dataclass(frozen=True)
class AdsAttributionRow:
    campaign_id: str | None
    sku_id: str | None
    attribution_level: AttributionLevel
    confidence: Confidence
    ad_spend_kopecks: int | None
    impressions: int | None
    clicks: int | None
    cart_adds: int | None
    orders_count: int | None
    orders_kopecks: int | None
    campaign_name: str | None = None
    campaign_type: int | str | None = None
    campaign_status: int | str | None = None
    payment_type: str | None = None
    change_time: str | None = None
    budget_cash_kopecks: int | None = None
    budget_netting_kopecks: int | None = None
    budget_total_kopecks: int | None = None
    spend_source: str = "fullstats"


@dataclass(frozen=True)
class AdsAttributionSnapshot:
    source_status: SourceStatus
    confidence: Confidence
    blocker_ids: list[str]
    source_evidence: list[SourceEvidence]
    totals: dict[str, int | None]
    rows: list[AdsAttributionRow]
    cabinet_balance: dict[str, Any] = field(default_factory=dict)
    spend_documents: list[dict[str, Any]] = field(default_factory=list)
    daily_rows: list[dict[str, Any]] = field(default_factory=list)
    search_clusters: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    performance_totals: dict[str, int | None] = field(default_factory=dict)
    daily_performance_totals: dict[str, dict[str, int | None]] = field(default_factory=dict)
    campaign_membership: dict[str, list[int]] = field(default_factory=dict)


def _rate_limit_payload(response: WbApiResponseEnvelope) -> dict[str, int | None] | None:
    if response.rateLimit is None:
        return None
    return response.rateLimit.model_dump(mode="json")


def _merge_known_metrics(target: dict, observation: dict) -> None:
    """One unavailable contribution makes that metric unknown, not a false zero."""
    for key, value in observation.items():
        if value is None or (key in target and target[key] is None):
            target[key] = None
        else:
            target[key] = target.get(key, 0) + value


def _response_diagnostics(
    *,
    source_id: str,
    endpoint: str,
    response: WbApiResponseEnvelope,
    rows: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    status = "fresh" if response.ok else "blocked"
    payload: dict[str, Any] = {
        "sourceId": source_id,
        "endpoint": endpoint,
        "status": status,
        "statusCode": response.statusCode,
        "requestId": response.wbRequestId,
    }
    if rows is not None:
        payload["rows"] = rows
    if response.error is not None:
        payload.update(
            {
                "errorCode": response.error.code,
                "message": response.error.message,
                "retryable": response.error.retryable,
                "operationalAlert": response.error.operationalAlert,
            }
        )
    rate_limit = _rate_limit_payload(response)
    if rate_limit is not None:
        payload["rateLimit"] = rate_limit
    if note:
        payload["note"] = note
    return payload


def _ads_diagnostics(sources: list[dict[str, Any]], *, blocked_at: str | None = None) -> dict[str, Any]:
    return {
        "summary": {
            "blockedAt": blocked_at,
            "blockedSources": [source["sourceId"] for source in sources if source.get("status") == "blocked"],
        },
        "sources": sources,
    }


def _retry_after_seconds(response: WbApiResponseEnvelope, fallback: float) -> float:
    if response.rateLimit and response.rateLimit.retryAfterSeconds is not None:
        return float(response.rateLimit.retryAfterSeconds)
    return fallback


def _request_with_retry(
    client: RateLimitedWbApiClient,
    request: WbApiRequest,
    *,
    min_interval_s: float = 0.0,
    max_attempts: int = ADS_RETRY_MAX_ATTEMPTS,
) -> WbApiResponseEnvelope:
    from app.wb_api.report_reads import active_report_read
    context = active_report_read.get()
    if context is not None:
        # Checkpoints skip successful batches on resume. Retry only temporary
        # failures, cooperatively, within a bounded budget and provider delay.
        for attempt in range(max_attempts):
            response = client.request(request)
            if response.ok or response.statusCode not in {429, 500, 502, 503, 504} or attempt == max_attempts - 1:
                return response
            delay = max(min_interval_s, _retry_after_seconds(response, fallback=60 if response.statusCode == 429 else 2 ** attempt))
            if delay > 300:
                return response
            remaining = delay
            while remaining > 0:
                context.check()
                step = min(remaining, 1)
                time.sleep(step)
                remaining -= step
    global _ads_fullstats_last_request_at
    last_response: WbApiResponseEnvelope | None = None
    if isinstance(client.inner, FakeWbApiClient):
        min_interval_s = 0.0
    for attempt in range(max_attempts):
        if min_interval_s > 0:
            now = time.monotonic()
            wait_s = min_interval_s - (now - _ads_fullstats_last_request_at)
            if wait_s > 0:
                time.sleep(wait_s)
            _ads_fullstats_last_request_at = time.monotonic()

        response = client.request(request)
        last_response = response
        if response.ok or response.statusCode != 429 or attempt >= max_attempts - 1:
            return response
        time.sleep(_retry_after_seconds(response, fallback=min_interval_s or 1.0))
    return last_response if last_response is not None else client.request(request)


def _date_windows(date_from: date, date_to: date, *, max_days: int = ADS_DATE_WINDOW_MAX_DAYS) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    start = date_from
    while start <= date_to:
        end_ordinal = min(date_to.toordinal(), start.toordinal() + max_days - 1)
        end = date.fromordinal(end_ordinal)
        windows.append((start, end))
        start = date.fromordinal(end.toordinal() + 1)
    return windows


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return int(float(normalized))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _rub_to_kopecks(value: Any) -> int | None:
    rub = _as_float(value)
    if rub is None:
        return None
    return int(round(rub * 100))


def _payload(envelope_data: Any) -> Any:
    if not isinstance(envelope_data, dict):
        return envelope_data
    nested = envelope_data.get("data")
    return nested if nested is not None else envelope_data


def _campaign_ids_from_promotion(payload: Any) -> list[int]:
    ids: list[int] = []
    if not isinstance(payload, dict):
        return ids
    adverts = payload.get("adverts")
    if not isinstance(adverts, list):
        return ids
    for group in adverts:
        if not isinstance(group, dict):
            continue
        advert_list = group.get("advert_list")
        if isinstance(advert_list, list):
            for campaign in advert_list:
                advert_id = _as_int(campaign)
                if isinstance(campaign, dict):
                    advert_id = _as_int(campaign.get("advertId") or campaign.get("advertID") or campaign.get("id"))
                if advert_id is not None:
                    ids.append(advert_id)
            continue
        advert_id = _as_int(group.get("advertId"))
        if advert_id is not None:
            ids.append(advert_id)
    return sorted(set(ids))


def _campaign_ids_from_spend_documents(rows: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for row in rows:
        advert_id = _as_int(row.get("campaignId"))
        if advert_id is not None:
            ids.append(advert_id)
    return sorted(set(ids))


def _looks_like_fullstats_campaign(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if _as_int(item.get("advertId") or item.get("advertID") or item.get("id")) is not None:
        return True
    return any(key in item for key in ("views", "clicks", "sum", "spend", "atbs", "orders", "sum_price", "days", "apps", "boosterStats"))


def _campaigns_from_fullstats(payload: Any) -> list[dict[str, Any]]:
    data = _payload(payload)
    candidates: list[Any]
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        nested = data.get("adverts") or data.get("data")
        if isinstance(nested, list):
            candidates = nested
        else:
            candidates = [data]
    else:
        candidates = []
    return [row for row in candidates if _looks_like_fullstats_campaign(row)]


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _request_fullstats_campaigns(
    client: RateLimitedWbApiClient,
    *,
    campaign_ids: list[int],
    date_from: date,
    date_to: date,
    progress_callback: Callable[[str, str, int], None] | None = None,
    progress_stage_prefix: str = "ads-fullstats",
    progress_start: int = 50,
    progress_end: int = 85,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    campaigns: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    all_ok = True
    chunks = _chunks(campaign_ids, 50)
    windows = _date_windows(date_from, date_to)
    total_steps = max(1, len(chunks) * len(windows))
    completed_steps = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        ids_query = ",".join(str(advert_id) for advert_id in chunk)
        for window_index, (window_from, window_to) in enumerate(windows, start=1):
            if progress_callback is not None:
                pct = progress_start + int((progress_end - progress_start) * completed_steps / total_steps)
                progress_callback(
                    f"{progress_stage_prefix}-{chunk_index}-{window_index}",
                    f"Загружаем рекламу WB fullstats {completed_steps + 1}/{total_steps}",
                    pct,
                )
            response = _request_with_retry(
                client,
                WbApiRequest(
                    method="GET",
                    path="/adv/v3/fullstats",
                    query={
                        "ids": ids_query,
                        "beginDate": window_from.isoformat(),
                        "endDate": window_to.isoformat(),
                    },
                ),
                min_interval_s=ADS_FULLSTATS_MIN_INTERVAL_S,
            )
            window_note = f"campaign ids: {ids_query}; dates: {window_from.isoformat()}..{window_to.isoformat()}"
            if not response.ok:
                all_ok = False
                diagnostics.append(
                    _response_diagnostics(
                        source_id="wb-ads-fullstats",
                        endpoint="GET /adv/v3/fullstats",
                        response=response,
                        rows=0,
                        note=window_note,
                    )
                )
                continue
            page_campaigns = _campaigns_from_fullstats(response.data)
            campaigns.extend(page_campaigns)
            diagnostics.append(
                _response_diagnostics(
                    source_id="wb-ads-fullstats",
                    endpoint="GET /adv/v3/fullstats",
                    response=response,
                    rows=len(page_campaigns),
                    note=window_note,
                )
            )
            completed_steps += 1
            if progress_callback is not None:
                pct = progress_start + int((progress_end - progress_start) * completed_steps / total_steps)
                progress_callback(
                    f"{progress_stage_prefix}-{chunk_index}-{window_index}",
                    f"Реклама WB fullstats {completed_steps}/{total_steps}",
                    pct,
                )
    return campaigns, all_ok, diagnostics


def _request_adverts_payload(
    client: RateLimitedWbApiClient,
    *,
    campaign_ids: list[int],
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    any_ok = False
    for chunk in _chunks(campaign_ids, 50):
        ids_query = ",".join(str(advert_id) for advert_id in chunk)
        response = _request_with_retry(
            client,
            WbApiRequest(
                method="GET",
                path="/api/advert/v2/adverts",
                query={"ids": ids_query},
            )
        )
        if not response.ok:
            diagnostics.append(
                _response_diagnostics(
                    source_id="wb-ads-adverts",
                    endpoint="GET /api/advert/v2/adverts",
                    response=response,
                    rows=0,
                    note=f"campaign ids: {ids_query}",
                )
            )
            continue
        any_ok = True
        before = len(items)
        payload = _payload(response.data)
        if isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            adverts = payload.get("adverts")
            if isinstance(adverts, list):
                items.extend(item for item in adverts if isinstance(item, dict))
            else:
                items.append(payload)
        diagnostics.append(
            _response_diagnostics(
                source_id="wb-ads-adverts",
                endpoint="GET /api/advert/v2/adverts",
                response=response,
                rows=len(items) - before,
                note=f"campaign ids: {ids_query}",
            )
        )
    return items, any_ok, diagnostics


def _campaign_meta_from_promotion(payload: Any) -> dict[int, dict[str, Any]]:
    meta: dict[int, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return meta
    adverts = payload.get("adverts")
    if not isinstance(adverts, list):
        return meta
    for group in adverts:
        if not isinstance(group, dict):
            continue
        group_type = group.get("type")
        group_status = group.get("status")
        advert_list = group.get("advert_list")
        if not isinstance(advert_list, list):
            continue
        for campaign in advert_list:
            if not isinstance(campaign, dict):
                continue
            advert_id = _as_int(campaign.get("advertId"))
            if advert_id is None:
                continue
            meta.setdefault(advert_id, {}).update(
                {
                    "type": group_type,
                    "status": group_status,
                    "changeTime": campaign.get("changeTime"),
                }
            )
    return meta


def _campaign_meta_from_adverts(payload: Any) -> dict[int, dict[str, Any]]:
    meta: dict[int, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        data = payload.get("adverts")
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        else:
            items = [payload]

    for item in items:
        advert_id = _as_int(item.get("advertId") or item.get("advertID") or item.get("id"))
        if advert_id is None:
            continue
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        timestamps = item.get("timestamps") if isinstance(item.get("timestamps"), dict) else {}
        meta.setdefault(advert_id, {}).update(
            {
                "name": settings.get("name") or item.get("name") or item.get("campName"),
                "status": item.get("status"),
                "type": item.get("type") or item.get("advertType"),
                "paymentType": settings.get("payment_type") or item.get("payment_type") or item.get("paymentType"),
                "bidType": item.get("bid_type"),
                "createdAt": timestamps.get("created"),
                "startedAt": timestamps.get("started"),
                "updatedAt": timestamps.get("updated"),
                "deletedAt": timestamps.get("deleted"),
            }
        )
    return meta


def _sku_membership_from_adverts(payload: Any) -> dict[int, set[int]]:
    membership: dict[int, set[int]] = {}
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        data = payload.get("adverts")
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        else:
            items = [payload]

    for item in items:
        advert_id = _as_int(item.get("advertId") or item.get("advertID") or item.get("id"))
        if advert_id is None:
            continue
        bucket = membership.setdefault(advert_id, set())

        nm_cpm = item.get("nmCPM")
        if isinstance(nm_cpm, list):
            for raw in nm_cpm:
                if isinstance(raw, dict):
                    nm_id = _as_int(raw.get("nm") or raw.get("nmId"))
                    if nm_id is not None:
                        bucket.add(nm_id)

        nms = item.get("nms")
        if isinstance(nms, list):
            for raw in nms:
                nm_id = _as_int(raw)
                if nm_id is not None:
                    bucket.add(nm_id)

        nm_settings = item.get("nm_settings")
        if isinstance(nm_settings, list):
            for raw in nm_settings:
                if isinstance(raw, dict):
                    nm_id = _as_int(raw.get("nm_id") or raw.get("nmId") or raw.get("nm"))
                else:
                    nm_id = _as_int(raw)
                if nm_id is not None:
                    bucket.add(nm_id)
    return membership


def _extract_metrics(node: dict[str, Any]) -> dict[str, int | None]:
    impressions = _as_int(node.get("views") if node.get("views") is not None else node.get("impressions"))
    clicks = _as_int(node.get("clicks"))
    cart_adds = _as_int(node.get("atbs") if node.get("atbs") is not None else node.get("cartAdds"))
    orders = _as_int(node.get("orders"))
    spend = _rub_to_kopecks(node.get("sum") if node.get("sum") is not None else node.get("spend"))
    revenue = _rub_to_kopecks(node.get("sum_price") if node.get("sum_price") is not None else node.get("revenue"))
    return {
        "impressions": impressions,
        "clicks": clicks,
        "cart_adds": cart_adds,
        "orders_count": orders,
        "ad_spend_kopecks": spend,
        "orders_kopecks": revenue,
    }


def _merge_metrics(target: dict[str, int | None], source: dict[str, int | None]) -> None:
    for key, value in source.items():
        if value is None:
            continue
        current = target.get(key)
        target[key] = (current or 0) + value


def _collect_nm_rows(node: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if _as_int(node.get("nmId")) is not None:
            rows.append(node)
        for value in node.values():
            rows.extend(_collect_nm_rows(value))
    elif isinstance(node, list):
        for value in node:
            rows.extend(_collect_nm_rows(value))
    return rows


def _collect_day_rows(
    campaign: dict[str, Any],
    *,
    campaign_id: str | None,
    default_attribution_level: AttributionLevel,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    days = campaign.get("days")
    if not isinstance(days, list):
        return rows
    for day in days:
        if not isinstance(day, dict):
            continue
        day_date = day.get("date")
        day_metrics = _extract_metrics(day)
        apps = day.get("apps")
        app_rows = apps if isinstance(apps, list) else []
        had_nm_rows = False
        for app in app_rows:
            if not isinstance(app, dict):
                continue
            app_type = app.get("appType") or app.get("app_type")
            for nm_row in _collect_nm_rows(app):
                nm_id = _as_int(nm_row.get("nmId"))
                if nm_id is None:
                    continue
                had_nm_rows = True
                metrics = _extract_metrics(nm_row)
                rows.append(
                    {
                        "date": day_date,
                        "campaignId": campaign_id,
                        "appType": app_type,
                        "skuId": str(nm_id),
                        "productName": str(nm_row.get("name") or "").strip() or None,
                        "attributionLevel": "exact_sku" if default_attribution_level == "exact_sku" else "campaign_sku",
                        "adSpendKopecks": metrics.get("ad_spend_kopecks"),
                        "impressions": metrics.get("impressions"),
                        "clicks": metrics.get("clicks"),
                        "cartAdds": metrics.get("cart_adds"),
                        "ordersCount": metrics.get("orders_count"),
                        "ordersKopecks": metrics.get("orders_kopecks"),
                    }
                )
        if not had_nm_rows:
            rows.append(
                {
                    "date": day_date,
                    "campaignId": campaign_id,
                    "appType": None,
                    "skuId": None,
                    "attributionLevel": "campaign_only",
                    "adSpendKopecks": day_metrics.get("ad_spend_kopecks"),
                    "impressions": day_metrics.get("impressions"),
                    "clicks": day_metrics.get("clicks"),
                    "cartAdds": day_metrics.get("cart_adds"),
                    "ordersCount": day_metrics.get("orders_count"),
                    "ordersKopecks": day_metrics.get("orders_kopecks"),
                }
            )
    return rows


def _normalize_balance(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "balanceKopecks": _rub_to_kopecks(payload.get("balance")),
        "netKopecks": _rub_to_kopecks(payload.get("net")),
        "bonusKopecks": _rub_to_kopecks(payload.get("bonus")),
        "cashbacks": payload.get("cashbacks") if isinstance(payload.get("cashbacks"), list) else [],
    }


def _normalize_budget(payload: Any) -> dict[str, int | None]:
    if not isinstance(payload, dict):
        return {"cashKopecks": None, "nettingKopecks": None, "totalKopecks": None}
    return {
        "cashKopecks": _rub_to_kopecks(payload.get("cash")),
        "nettingKopecks": _rub_to_kopecks(payload.get("netting")),
        "totalKopecks": _rub_to_kopecks(payload.get("total")),
    }


def _normalize_upd_rows(payload: Any) -> list[dict[str, Any]]:
    items = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "updNum": item.get("updNum"),
                "updTime": item.get("updTime"),
                "adSpendKopecks": _rub_to_kopecks(item.get("updSum")),
                "campaignId": str(item.get("advertId")) if item.get("advertId") is not None else None,
                "campaignName": item.get("campName"),
                "campaignType": item.get("advertType"),
                "paymentType": item.get("paymentType"),
                "campaignStatus": item.get("advertStatus"),
            }
        )
    return rows


def _normalize_search_clusters(payload: Any) -> list[dict[str, Any]]:
    data = _payload(payload)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        advert_id = item.get("advertId")
        nm_id = item.get("nmId")
        daily_stats = item.get("dailyStats")
        if not isinstance(daily_stats, list):
            continue
        for daily in daily_stats:
            if not isinstance(daily, dict):
                continue
            stats = daily.get("stats") or daily.get("stat")
            stat_rows = stats if isinstance(stats, list) else [stats] if isinstance(stats, dict) else []
            for stat in stat_rows:
                if not isinstance(stat, dict):
                    continue
                rows.append(
                    {
                        "campaignId": str(advert_id) if advert_id is not None else None,
                        "skuId": str(nm_id) if nm_id is not None else None,
                        "date": daily.get("date"),
                        "normQuery": stat.get("normQuery"),
                        "impressions": _as_int(stat.get("views")),
                        "clicks": _as_int(stat.get("clicks")),
                        "adSpendKopecks": _rub_to_kopecks(stat.get("spend")),
                        "ordersCount": _as_int(stat.get("orders")),
                        "cartAdds": _as_int(stat.get("atbs")),
                        "avgPosition": _as_float(stat.get("avgPos")),
                    }
                )
    return rows


def _build_evidence(fields: list[str]) -> list[SourceEvidence]:
    return [
        SourceEvidence(
            sourceId="wb-ads-promotion-count",
            sourceType="wb_api",
            sourceName="WB Ads API /adv/v1/promotion/count",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["adverts[].advert_list[].advertId"],
        ),
        SourceEvidence(
            sourceId="wb-ads-adverts",
            sourceType="wb_api",
            sourceName="WB Ads API /api/advert/v2/adverts",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["advertId", "nmCPM[].nm"],
        ),
        SourceEvidence(
            sourceId="wb-ads-fullstats",
            sourceType="wb_api",
            sourceName="WB Ads API /adv/v3/fullstats",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=fields,
        ),
        SourceEvidence(
            sourceId="wb-ads-budget",
            sourceType="wb_api",
            sourceName="WB Ads API /adv/v1/budget",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["cash", "netting", "total"],
        ),
        SourceEvidence(
            sourceId="wb-ads-balance",
            sourceType="wb_api",
            sourceName="WB Ads API /adv/v1/balance",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["balance", "net", "bonus", "cashbacks"],
        ),
        SourceEvidence(
            sourceId="wb-ads-upd",
            sourceType="wb_api",
            sourceName="WB Ads API /adv/v1/upd",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["updSum", "advertId", "campName", "advertType", "paymentType", "advertStatus"],
        ),
    ]


def build_ads_attribution_snapshot(
    *,
    date_from: date,
    date_to: date,
    group_by: str,
    scenario: str = "complete",
    wb_token: str | None = None,
    progress_callback: Callable[[str, str, int], None] | None = None,
    progress_stage_prefix: str = "ads",
    progress_start: int = 20,
    progress_end: int = 95,
    include_budgets: bool = True,
    budget_cache: dict[int, dict[str, int | None]] | None = None,
    budget_collected_callback: Callable[[int, dict[str, int | None]], None] | None = None,
    performance_only: bool = False,
) -> AdsAttributionSnapshot:
    progress_start = max(0, min(100, int(progress_start)))
    progress_end = max(progress_start, min(100, int(progress_end)))

    def progress_at(fraction: float) -> int:
        bounded_fraction = max(0.0, min(1.0, fraction))
        return progress_start + round((progress_end - progress_start) * bounded_fraction)

    if get_settings().wb_api_mode == "real" and not wb_token:
        raise RuntimeError("WB_TOKEN_REQUIRED")
    client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario=scenario, token_override=wb_token))
    if progress_callback is not None:
        progress_callback(f"{progress_stage_prefix}-promotion", "Загружаем список рекламных кампаний WB", progress_at(0.0))
    promotion = client.request(WbApiRequest(method="GET", path="/adv/v1/promotion/count"))
    diagnostics_sources = [
        _response_diagnostics(
            source_id="wb-ads-promotion-count",
            endpoint="GET /adv/v1/promotion/count",
            response=promotion,
        )
    ]
    if not promotion.ok:
        return AdsAttributionSnapshot(
            source_status="blocked",
            confidence="blocked",
            blocker_ids=["WB-02"],
            source_evidence=[],
            totals={},
            rows=[],
            diagnostics=_ads_diagnostics(diagnostics_sources, blocked_at="wb-ads-promotion-count"),
        )

    promotion_payload = _payload(promotion.data)
    campaign_ids = _campaign_ids_from_promotion(promotion_payload)

    if performance_only and promotion_payload.get("adverts") == [] and promotion_payload.get("all") == 0:
        zero = dict(ad_spend_kopecks=0, impressions=0, clicks=0, cart_adds=0, orders_count=0, orders_kopecks=0)
        diagnostics = _ads_diagnostics(diagnostics_sources)
        diagnostics.setdefault("summary", {})["sourceComplete"] = True
        return AdsAttributionSnapshot(source_status="fresh", confidence="high", blocker_ids=[],
            source_evidence=[], totals=zero, rows=[], performance_totals=zero,
            daily_performance_totals={(date_from + timedelta(days=i)).isoformat(): dict(zero)
                for i in range((date_to-date_from).days+1)}, diagnostics=diagnostics)

    if progress_callback is not None and not performance_only:
        progress_callback(f"{progress_stage_prefix}-balance", "Загружаем баланс рекламы WB", progress_at(0.07))
    cabinet_balance = {}
    if not performance_only:
        balance = client.request(WbApiRequest(method="GET", path="/adv/v1/balance"))
        diagnostics_sources.append(_response_diagnostics(
            source_id="wb-ads-balance", endpoint="GET /adv/v1/balance", response=balance))
        cabinet_balance = _normalize_balance(_payload(balance.data)) if balance.ok else {}

    spend_documents: list[dict[str, Any]] = []
    windows = [] if performance_only else _date_windows(date_from, date_to)
    for index, (window_from, window_to) in enumerate(windows, start=1):
        if progress_callback is not None:
            pct = progress_at(0.11 + 0.10 * (index - 1) / max(1, len(windows)))
            progress_callback(f"{progress_stage_prefix}-upd-{index}", f"Загружаем акты рекламы WB {index}/{len(windows)}", pct)
        upd = _request_with_retry(
            client,
            WbApiRequest(
                method="GET",
                path="/adv/v1/upd",
                query={"from": window_from.isoformat(), "to": window_to.isoformat()},
            ),
        )
        window_documents = _normalize_upd_rows(_payload(upd.data)) if upd.ok else []
        spend_documents.extend(window_documents)
        diagnostics_sources.append(
            _response_diagnostics(
                source_id="wb-ads-upd",
                endpoint="GET /adv/v1/upd",
                response=upd,
                rows=len(window_documents),
                note=f"Spend documents; campaign-only spend is not allocated to SKU RNP. dates: {window_from.isoformat()}..{window_to.isoformat()}",
            )
        )
    spend_campaign_ids = _campaign_ids_from_spend_documents(spend_documents)
    combined_campaign_ids = list(dict.fromkeys([*campaign_ids, *spend_campaign_ids]))
    if not combined_campaign_ids:
        return AdsAttributionSnapshot(
            source_status="blocked",
            confidence="blocked",
            blocker_ids=["WB-02"],
            source_evidence=[],
            totals={},
            rows=[],
            cabinet_balance=cabinet_balance,
            spend_documents=spend_documents,
            diagnostics=_ads_diagnostics(diagnostics_sources, blocked_at="wb-ads-campaign-ids"),
        )

    if progress_callback is not None:
        progress_callback(f"{progress_stage_prefix}-adverts", "Загружаем карточки рекламных кампаний WB", progress_at(0.24))
    adverts_payload, adverts_ok, adverts_diagnostics = _request_adverts_payload(client, campaign_ids=combined_campaign_ids)
    diagnostics_sources.extend(adverts_diagnostics)
    if not adverts_ok and not spend_documents:
        return AdsAttributionSnapshot(
            source_status="blocked",
            confidence="blocked",
            blocker_ids=["WB-02"],
            source_evidence=[],
            totals={},
            rows=[],
            diagnostics=_ads_diagnostics(diagnostics_sources, blocked_at="wb-ads-adverts"),
        )

    campaigns, fullstats_ok, fullstats_diagnostics = _request_fullstats_campaigns(
        client,
        campaign_ids=combined_campaign_ids,
        date_from=date_from,
        date_to=date_to,
        progress_callback=progress_callback,
        progress_stage_prefix=f"{progress_stage_prefix}-fullstats",
        progress_start=progress_at(0.32),
        progress_end=progress_at(0.90 if include_budgets else 1.0),
    )
    diagnostics_sources.extend(fullstats_diagnostics)
    if not fullstats_ok and not spend_documents:
        return AdsAttributionSnapshot(
            source_status="blocked",
            confidence="blocked",
            blocker_ids=["WB-02"],
            source_evidence=[],
            totals={},
            rows=[],
            diagnostics=_ads_diagnostics(diagnostics_sources, blocked_at="wb-ads-fullstats"),
        )

    membership = _sku_membership_from_adverts(adverts_payload)
    campaign_meta = _campaign_meta_from_promotion(promotion_payload)
    for advert_id, item in _campaign_meta_from_adverts(adverts_payload).items():
        campaign_meta.setdefault(advert_id, {}).update(item)
    for document in spend_documents:
        advert_id = _as_int(document.get("campaignId"))
        if advert_id is None:
            continue
        meta = campaign_meta.setdefault(advert_id, {})
        if document.get("campaignName") is not None:
            meta.setdefault("name", document.get("campaignName"))
        if document.get("campaignType") is not None:
            meta.setdefault("type", document.get("campaignType"))
        if document.get("campaignStatus") is not None:
            meta.setdefault("status", document.get("campaignStatus"))
        if document.get("paymentType") is not None:
            meta.setdefault("paymentType", document.get("paymentType"))

    campaign_budgets = {key: value for key, value in (budget_cache or {}).items() if key in combined_campaign_ids}
    if include_budgets:
        for index, advert_id in enumerate(combined_campaign_ids, start=1):
            if advert_id in campaign_budgets:
                continue
            if progress_callback is not None and (index == 1 or index % 10 == 0 or index == len(combined_campaign_ids)):
                pct = progress_at(0.90 + 0.10 * index / max(1, len(combined_campaign_ids)))
                progress_callback(f"{progress_stage_prefix}-budget-{index}", f"Загружаем бюджеты РК WB {index}/{len(combined_campaign_ids)}", pct)
            budget = client.request(WbApiRequest(method="GET", path="/adv/v1/budget", query={"id": advert_id}))
            if budget.ok:
                campaign_budgets[advert_id] = _normalize_budget(_payload(budget.data))
                if budget_collected_callback is not None:
                    budget_collected_callback(advert_id, campaign_budgets[advert_id])
            else:
                diagnostics_sources.append(_response_diagnostics(source_id="wb-ads-budget", endpoint="GET /adv/v1/budget", response=budget))
                if budget.statusCode in {401, 403, 429}:
                    break  # Keep the collected part; don't request every remaining campaign.

    rows: list[AdsAttributionRow] = []
    daily_rows: list[dict[str, Any]] = []
    totals = {
        "ad_spend_kopecks": 0,
        "impressions": 0,
        "clicks": 0,
        "cart_adds": 0,
        "orders_count": 0,
        "orders_kopecks": 0,
    }
    has_exact = False
    has_campaign_sku = False
    daily_performance_totals: dict[str, dict[str, int]] = {}

    for campaign in campaigns:
        advert_id = _as_int(campaign.get("advertId") or campaign.get("advertID") or campaign.get("id"))
        campaign_id = str(advert_id) if advert_id is not None else None
        meta = campaign_meta.get(advert_id or -1, {})
        budget = campaign_budgets.get(advert_id or -1, {})
        campaign_metrics = _extract_metrics(campaign)
        _merge_known_metrics(totals, campaign_metrics)
        for observation in campaign.get("days", []) or []:
            if not isinstance(observation, dict):
                continue
            day = str(observation.get("date") or "")[:10]
            if not day:
                continue
            day_totals = daily_performance_totals.setdefault(day, {})
            _merge_known_metrics(day_totals, _extract_metrics(observation))

        nm_rows = _collect_nm_rows(campaign)
        per_sku: dict[int, dict[str, int | None]] = {}
        for nm_row in nm_rows:
            nm_id = _as_int(nm_row.get("nmId"))
            if nm_id is None:
                continue
            bucket = per_sku.setdefault(
                nm_id,
                {
                    "ad_spend_kopecks": 0,
                    "impressions": 0,
                    "clicks": 0,
                    "cart_adds": 0,
                    "orders_count": 0,
                    "orders_kopecks": 0,
                },
            )
            _merge_metrics(bucket, _extract_metrics(nm_row))

        if per_sku:
            complete = all(
                metrics.get("ad_spend_kopecks") is not None
                and metrics.get("clicks") is not None
                and metrics.get("orders_count") is not None
                and metrics.get("orders_kopecks") is not None
                for metrics in per_sku.values()
            )
            level: AttributionLevel = "exact_sku" if complete else "campaign_sku"
            conf: Confidence = "high" if level == "exact_sku" else "medium"
            if level == "exact_sku":
                has_exact = True
            else:
                has_campaign_sku = True
            daily_rows.extend(_collect_day_rows(campaign, campaign_id=campaign_id, default_attribution_level=level))

            if group_by == "campaign":
                rows.append(
                    AdsAttributionRow(
                        campaign_id=campaign_id,
                        sku_id=None,
                        attribution_level=level,
                        confidence=conf,
                        ad_spend_kopecks=campaign_metrics.get("ad_spend_kopecks"),
                        impressions=campaign_metrics.get("impressions"),
                        clicks=campaign_metrics.get("clicks"),
                        cart_adds=campaign_metrics.get("cart_adds"),
                        orders_count=campaign_metrics.get("orders_count"),
                        orders_kopecks=campaign_metrics.get("orders_kopecks"),
                        campaign_name=meta.get("name"),
                        campaign_type=meta.get("type"),
                        campaign_status=meta.get("status"),
                        payment_type=meta.get("paymentType"),
                        change_time=meta.get("changeTime") or meta.get("updatedAt"),
                        budget_cash_kopecks=budget.get("cashKopecks"),
                        budget_netting_kopecks=budget.get("nettingKopecks"),
                        budget_total_kopecks=budget.get("totalKopecks"),
                    )
                )
            else:
                for nm_id, metrics in per_sku.items():
                    rows.append(
                        AdsAttributionRow(
                            campaign_id=campaign_id,
                            sku_id=str(nm_id),
                            attribution_level=level,
                            confidence=conf,
                            ad_spend_kopecks=metrics.get("ad_spend_kopecks"),
                            impressions=metrics.get("impressions"),
                            clicks=metrics.get("clicks"),
                            cart_adds=metrics.get("cart_adds"),
                            orders_count=metrics.get("orders_count"),
                            orders_kopecks=metrics.get("orders_kopecks"),
                            campaign_name=meta.get("name"),
                            campaign_type=meta.get("type"),
                            campaign_status=meta.get("status"),
                            payment_type=meta.get("paymentType"),
                            change_time=meta.get("changeTime") or meta.get("updatedAt"),
                            budget_cash_kopecks=budget.get("cashKopecks"),
                            budget_netting_kopecks=budget.get("nettingKopecks"),
                            budget_total_kopecks=budget.get("totalKopecks"),
                        )
                    )
            continue

        member_skus = sorted(membership.get(advert_id or -1, set()))
        daily_rows.extend(_collect_day_rows(campaign, campaign_id=campaign_id, default_attribution_level="campaign_only"))
        if group_by == "campaign":
            rows.append(
                AdsAttributionRow(
                    campaign_id=campaign_id,
                    sku_id=None,
                    attribution_level="campaign_only",
                    confidence="low",
                    ad_spend_kopecks=campaign_metrics.get("ad_spend_kopecks"),
                    impressions=campaign_metrics.get("impressions"),
                    clicks=campaign_metrics.get("clicks"),
                    cart_adds=campaign_metrics.get("cart_adds"),
                    orders_count=campaign_metrics.get("orders_count"),
                    orders_kopecks=campaign_metrics.get("orders_kopecks"),
                    campaign_name=meta.get("name"),
                    campaign_type=meta.get("type"),
                    campaign_status=meta.get("status"),
                    payment_type=meta.get("paymentType"),
                    change_time=meta.get("changeTime") or meta.get("updatedAt"),
                    budget_cash_kopecks=budget.get("cashKopecks"),
                    budget_netting_kopecks=budget.get("nettingKopecks"),
                    budget_total_kopecks=budget.get("totalKopecks"),
                )
            )
        elif member_skus:
            has_campaign_sku = True
            for nm_id in member_skus:
                rows.append(
                    AdsAttributionRow(
                        campaign_id=campaign_id,
                        sku_id=str(nm_id),
                        attribution_level="campaign_sku",
                        confidence="medium",
                        ad_spend_kopecks=None,
                        impressions=None,
                        clicks=None,
                        cart_adds=None,
                        orders_count=None,
                        orders_kopecks=None,
                        campaign_name=meta.get("name"),
                        campaign_type=meta.get("type"),
                        campaign_status=meta.get("status"),
                        payment_type=meta.get("paymentType"),
                        change_time=meta.get("changeTime") or meta.get("updatedAt"),
                        budget_cash_kopecks=budget.get("cashKopecks"),
                        budget_netting_kopecks=budget.get("nettingKopecks"),
                        budget_total_kopecks=budget.get("totalKopecks"),
                    )
                )

    upd_by_campaign: dict[int, dict[str, Any]] = {}
    for document in spend_documents:
        advert_id = _as_int(document.get("campaignId"))
        if advert_id is None:
            continue
        bucket = upd_by_campaign.setdefault(
            advert_id,
            {
                "ad_spend_kopecks": 0,
                "campaignName": None,
                "campaignType": None,
                "paymentType": None,
                "campaignStatus": None,
            },
        )
        bucket["ad_spend_kopecks"] += int(document.get("adSpendKopecks") or 0)
        for source_key, target_key in (
            ("campaignName", "campaignName"),
            ("campaignType", "campaignType"),
            ("paymentType", "paymentType"),
            ("campaignStatus", "campaignStatus"),
        ):
            if bucket.get(target_key) is None and document.get(source_key) is not None:
                bucket[target_key] = document.get(source_key)

    performance_totals = dict(totals)
    added_upd_spend = 0
    if group_by == "campaign" and upd_by_campaign:
        for advert_id, document_metrics in upd_by_campaign.items():
            campaign_id = str(advert_id)
            spend_kopecks = int(document_metrics.get("ad_spend_kopecks") or 0)
            if spend_kopecks <= 0:
                continue
            meta = campaign_meta.get(advert_id, {})
            budget = campaign_budgets.get(advert_id, {})
            matched = False
            for index, row in enumerate(rows):
                if row.campaign_id != campaign_id:
                    continue
                matched = True
                if row.ad_spend_kopecks:
                    break
                rows[index] = replace(
                    row,
                    ad_spend_kopecks=spend_kopecks,
                    spend_source="spend_document",
                    campaign_name=row.campaign_name or meta.get("name") or document_metrics.get("campaignName"),
                    campaign_type=row.campaign_type or meta.get("type") or document_metrics.get("campaignType"),
                    campaign_status=row.campaign_status or meta.get("status") or document_metrics.get("campaignStatus"),
                    payment_type=row.payment_type or meta.get("paymentType") or document_metrics.get("paymentType"),
                    budget_cash_kopecks=row.budget_cash_kopecks if row.budget_cash_kopecks is not None else budget.get("cashKopecks"),
                    budget_netting_kopecks=row.budget_netting_kopecks if row.budget_netting_kopecks is not None else budget.get("nettingKopecks"),
                    budget_total_kopecks=row.budget_total_kopecks if row.budget_total_kopecks is not None else budget.get("totalKopecks"),
                )
                added_upd_spend += spend_kopecks
                break
            if matched:
                continue
            rows.append(
                AdsAttributionRow(
                    campaign_id=campaign_id,
                    sku_id=None,
                    attribution_level="campaign_only",
                    confidence="low",
                    ad_spend_kopecks=spend_kopecks,
                    spend_source="spend_document",
                    impressions=None,
                    clicks=None,
                    cart_adds=None,
                    orders_count=None,
                    orders_kopecks=None,
                    campaign_name=meta.get("name") or document_metrics.get("campaignName"),
                    campaign_type=meta.get("type") or document_metrics.get("campaignType"),
                    campaign_status=meta.get("status") or document_metrics.get("campaignStatus"),
                    payment_type=meta.get("paymentType") or document_metrics.get("paymentType"),
                    change_time=meta.get("changeTime") or meta.get("updatedAt"),
                    budget_cash_kopecks=budget.get("cashKopecks"),
                    budget_netting_kopecks=budget.get("nettingKopecks"),
                    budget_total_kopecks=budget.get("totalKopecks"),
                )
            )
            added_upd_spend += spend_kopecks
        if totals["ad_spend_kopecks"] is not None:
            totals["ad_spend_kopecks"] += added_upd_spend
    elif not rows and totals["ad_spend_kopecks"] == 0 and upd_by_campaign:
        totals["ad_spend_kopecks"] = sum(int(item.get("ad_spend_kopecks") or 0) for item in upd_by_campaign.values())

    if has_exact:
        confidence: Confidence = "high"
    elif has_campaign_sku:
        confidence = "medium"
    else:
        confidence = "low"

    if group_by == "sku" and not rows:
        source_status: SourceStatus = "partial"
    elif not fullstats_ok or not campaigns:
        source_status = "partial"
    else:
        source_status = "fresh"
    if include_budgets and len(campaign_budgets) < len(combined_campaign_ids):
        source_status = "partial"

    return AdsAttributionSnapshot(
        source_status=source_status,
        performance_totals=performance_totals,
        daily_performance_totals=daily_performance_totals,
        confidence=confidence,
        blocker_ids=[],
        source_evidence=_build_evidence(
            [
                "advertId",
                "days[].date",
                "days[].apps[].appType",
                "apps[].nm[].nmId",
                "apps[].nm[].views",
                "apps[].nm[].clicks",
                "apps[].nm[].sum",
                "apps[].nm[].orders",
                "apps[].nm[].sum_price",
            ]
        ),
        totals=totals,
        rows=rows,
        cabinet_balance=cabinet_balance,
        spend_documents=spend_documents,
        daily_rows=daily_rows,
        diagnostics=_ads_diagnostics(diagnostics_sources),
        campaign_membership={str(key): sorted(value) for key, value in membership.items()},
    )


def fetch_ads_search_clusters(
    *,
    items: list[dict[str, int]],
    date_from: date,
    date_to: date,
    scenario: str = "complete",
    wb_token: str | None = None,
) -> list[dict[str, Any]]:
    if not items:
        return []
    if get_settings().wb_api_mode == "real" and not wb_token:
        raise RuntimeError("WB_TOKEN_REQUIRED")
    client = RateLimitedWbApiClient(inner=build_wb_ads_client(scenario=scenario, token_override=wb_token))
    response = client.request(
        WbApiRequest(
            method="POST",
            path="/adv/v1/normquery/stats",
            jsonBody={
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "items": items[:100],
            },
        )
    )
    if not response.ok:
        return []
    return _normalize_search_clusters(response.data)
