from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from app.platform.period import Period
from app.wb_api.ads_runtime import ADS_FULLSTATS_MIN_INTERVAL_S, _request_with_retry
from app.wb_api.client import (
    RateLimitedWbApiClient,
    WbApiRequest,
    WbApiResponseEnvelope,
    build_wb_ads_client,
)

_ALLOWED_STATUSES = {7, 9, 11}


class RawAdvertisingFetchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RawAdvertisingFetch:
    state: Literal["ready", "partial"]
    bundle: dict[str, Any]
    expected_requests: int
    completed_requests: int
    failure_codes: tuple[str, ...]


def fetch_raw_advertising(
    period: Period, *, wb_token: str, scenario: str = "complete"
) -> RawAdvertisingFetch:
    if not wb_token.strip():
        raise ValueError("WB token is required")
    client = RateLimitedWbApiClient(
        inner=build_wb_ads_client(scenario=scenario, token_override=wb_token)
    )
    return _execute_plan(client, period)


def _execute_plan(
    client: RateLimitedWbApiClient, period: Period
) -> RawAdvertisingFetch:
    manifest: list[dict[str, Any]] = []
    failures: list[str] = []
    expected = completed = 0

    def request(item: WbApiRequest) -> WbApiResponseEnvelope:
        nonlocal expected, completed
        response = _request_with_retry(
            client,
            item,
            min_interval_s=(
                ADS_FULLSTATS_MIN_INTERVAL_S
                if item.path == "/adv/v3/fullstats"
                else 0.0
            ),
        )
        expected += 1
        if response.ok:
            completed += 1
        else:
            code = (
                response.error.code
                if response.error is not None
                else "wb_request_failed"
            )
            if code not in failures:
                failures.append(code)
        manifest.append(
            {
                "endpoint": f"{item.method} {item.path}",
                "request": dict(item.query),
                "statusCode": response.statusCode,
                "ok": response.ok,
                "wbRequestId": response.wbRequestId,
                "responseChecksum": _checksum(response.data),
            }
        )
        return response

    promotion_response = request(
        WbApiRequest(method="GET", path="/adv/v1/promotion/count")
    )
    promotion = promotion_response.data if promotion_response.ok else {"adverts": []}
    upd: list[dict[str, Any]] = []
    campaign_ids = _campaign_ids_from_promotion(promotion)

    for start, end in _windows(period.date_from, period.date_to):
        response = request(
            WbApiRequest(
                method="GET",
                path="/adv/v1/upd",
                query={"from": start.isoformat(), "to": end.isoformat()},
            )
        )
        if response.ok:
            payload = _list_payload(response.data)
            upd.append(
                {
                    "dateFrom": start.isoformat(),
                    "dateTo": end.isoformat(),
                    "payload": payload,
                }
            )
            campaign_ids.extend(_campaign_ids_from_upd(payload))

    adverts_response = request(
        WbApiRequest(
            method="GET",
            path="/api/advert/v2/adverts",
            query={"statuses": "7,9,11"},
        )
    )
    adverts = (
        _adverts_payload(adverts_response.data)
        if adverts_response.ok
        else {"adverts": []}
    )
    campaign_ids = sorted(set(campaign_ids))
    fullstats: list[dict[str, Any]] = []

    for campaign_chunk in _chunks(campaign_ids, 50):
        for start, end in _windows(period.date_from, period.date_to):
            response = request(
                WbApiRequest(
                    method="GET",
                    path="/adv/v3/fullstats",
                    query={
                        "ids": ",".join(
                            str(campaign_id) for campaign_id in campaign_chunk
                        ),
                        "beginDate": start.isoformat(),
                        "endDate": end.isoformat(),
                    },
                )
            )
            if response.ok:
                fullstats.append(
                    {
                        "dateFrom": start.isoformat(),
                        "dateTo": end.isoformat(),
                        "campaignIds": campaign_chunk,
                        "payload": _list_payload(response.data),
                    }
                )

    return RawAdvertisingFetch(
        state="partial" if failures else "ready",
        bundle={
            "period": {
                "dateFrom": period.date_from.isoformat(),
                "dateTo": period.date_to.isoformat(),
            },
            "promotion": promotion,
            "adverts": adverts,
            "upd": upd,
            "fullstats": fullstats,
            "manifest": manifest,
        },
        expected_requests=expected,
        completed_requests=completed,
        failure_codes=tuple(failures),
    )


def _windows(start: date, end: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    while start <= end:
        window_end = min(start + timedelta(days=30), end)
        windows.append((start, window_end))
        start = window_end + timedelta(days=1)
    return windows


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _campaign_ids_from_promotion(payload: Any) -> list[int]:
    if not isinstance(payload, dict):
        return []
    campaign_ids: list[int] = []
    for group in payload.get("adverts", []):
        if (
            not isinstance(group, dict)
            or _positive_id(group.get("status")) not in _ALLOWED_STATUSES
        ):
            continue
        for advert in group.get("advert_list", []):
            if isinstance(advert, dict):
                campaign_id = _positive_id(advert.get("advertId"))
                if campaign_id is not None:
                    campaign_ids.append(campaign_id)
    return campaign_ids


def _list_payload(data: Any) -> Any:
    return (data["data"] or []) if _is_adapter_list_wrapper(data) else data


def _adverts_payload(data: Any) -> Any:
    if isinstance(data, Mapping) and not _is_adapter_list_wrapper(data):
        return data
    return {"adverts": _list_payload(data)}


def _is_adapter_list_wrapper(data: Any) -> bool:
    return (
        isinstance(data, Mapping)
        and set(data) == {"data"}
        and (data["data"] is None or isinstance(data["data"], list))
    )


def _campaign_ids_from_upd(payload: Any) -> list[int]:
    if not isinstance(payload, list):
        return []
    return [
        campaign_id
        for item in payload
        if isinstance(item, dict)
        if (campaign_id := _positive_id(item.get("advertId"))) is not None
    ]


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
        return int(value)
    return None


def _checksum(data: Any) -> str:
    try:
        encoded = _encoded(_canonical_checksum_payload(data))
    except (TypeError, ValueError):
        raise RawAdvertisingFetchError(
            "invalid WB advertising response payload"
        ) from None
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_checksum_payload(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _canonical_checksum_payload(value) for key, value in data.items()}
    if isinstance(data, list):
        items = [_canonical_checksum_payload(value) for value in data]
        return sorted(items, key=_encoded)
    return data


def _encoded(data: Any) -> str:
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
