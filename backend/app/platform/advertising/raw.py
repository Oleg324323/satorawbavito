from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from app.platform.period import MOSCOW, Period

FactGrain = Literal["day", "period"]
FactScope = Literal["campaign", "source_sku"]

_METRICS = (
    ("spend_kopecks", "sum", True),
    ("impressions", "views", False),
    ("clicks", "clicks", False),
    ("cart_adds", "atbs", False),
    ("order_count", "orders", False),
    ("order_revenue_kopecks", "sum_price", True),
    ("cancel_count", "canceled", False),
)
_MANIFEST_ENDPOINTS = {
    "GET /adv/v1/promotion/count",
    "GET /api/advert/v2/adverts",
    "GET /adv/v1/upd",
}
_FULLSTATS_ENDPOINT = "GET /adv/v3/fullstats"
_MONEY_HIERARCHY_TOLERANCE = Decimal("0.01")
_SECRET_KEY_PARTS = ("token", "authorization", "secret")


class AdvertisingNormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RawAdvertisingFact:
    source_identity: str
    payload_checksum: str
    grain: FactGrain
    date_from: date
    date_to: date
    business_date: date | None
    campaign_id: int
    fact_scope: FactScope
    app_type: int | None
    nm_id: int | None
    currency: str | None
    spend_kopecks: int | None
    impressions: int | None
    clicks: int | None
    cart_adds: int | None
    order_count: int | None
    order_revenue_kopecks: int | None
    cancel_count: int | None


@dataclass(frozen=True, slots=True)
class RawAdvertisingCampaign:
    source_identity: str
    payload_checksum: str
    campaign_id: int
    name: str | None
    campaign_type: int | None
    status: int | None
    payment_type: str | None
    bid_type: str | None
    member_nm_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RawAdvertisingSpendDocument:
    source_identity: str
    payload_checksum: str
    upd_num: str
    upd_time: datetime
    business_date: date
    campaign_id: int
    campaign_name: str | None
    campaign_type: int | None
    payment_type: str | None
    campaign_status: int | None
    currency: str | None
    spend_kopecks: int


@dataclass(frozen=True, slots=True)
class RawAdvertisingEvidence:
    facts: tuple[RawAdvertisingFact, ...]
    campaigns: tuple[RawAdvertisingCampaign, ...]
    spend_documents: tuple[RawAdvertisingSpendDocument, ...]
    source_total_spend_kopecks: int
    document_total_spend_kopecks: int
    raw_manifest: list[dict[str, Any]]
    raw_manifest_checksum: str
    snapshot_checksum: str


def normalize_raw_advertising(period: Period, bundle: dict[str, Any]) -> RawAdvertisingEvidence:
    _validate_bundle_period(period, bundle)
    raw_manifest = _canonical_manifest(bundle.get("manifest"))
    _require_complete_manifest(
        raw_manifest, require_fullstats=_has_campaign_ids(bundle)
    )
    campaigns = _normalize_campaigns(bundle.get("promotion"), bundle.get("adverts"))
    facts = _normalize_fullstats(period, bundle.get("fullstats"))
    documents = _normalize_upd(period, bundle.get("upd"))
    return _evidence(facts, campaigns, documents, raw_manifest, _hash(raw_manifest))


def _validate_bundle_period(period: Period, bundle: dict[str, Any]) -> None:
    if not isinstance(bundle, dict):
        raise AdvertisingNormalizationError("invalid raw advertising bundle")
    _validate_period(period, bundle.get("period"), "bundle")


def _validate_period(period: Period, payload: Any, source: str) -> None:
    if not isinstance(payload, dict):
        raise AdvertisingNormalizationError(f"invalid {source} period")
    for key, expected in (("dateFrom", period.date_from), ("dateTo", period.date_to)):
        try:
            actual = date.fromisoformat(str(payload[key]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdvertisingNormalizationError(f"invalid {source} period") from exc
        if actual != expected:
            raise AdvertisingNormalizationError("advertising period mismatch")


def _page_period(period: Period, payload: Any, source: str) -> Period:
    if not isinstance(payload, dict):
        raise AdvertisingNormalizationError(f"invalid {source} period")
    try:
        page_period = Period(
            date.fromisoformat(str(payload["dateFrom"])),
            date.fromisoformat(str(payload["dateTo"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AdvertisingNormalizationError(f"invalid {source} period") from exc
    if page_period.date_from < period.date_from or page_period.date_to > period.date_to:
        raise AdvertisingNormalizationError("advertising period mismatch")
    return page_period


def _require_complete_manifest(
    manifest: list[dict[str, Any]], *, require_fullstats: bool
) -> None:
    endpoints: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict) or item.get("ok") is not True:
            raise AdvertisingNormalizationError("advertising manifest is incomplete")
        if item.get("statusCode") != 200 or not isinstance(item.get("endpoint"), str):
            raise AdvertisingNormalizationError("advertising manifest is incomplete")
        endpoints.add(item["endpoint"])
    required = _MANIFEST_ENDPOINTS | (
        {_FULLSTATS_ENDPOINT} if require_fullstats else set()
    )
    if not required.issubset(endpoints):
        raise AdvertisingNormalizationError("advertising manifest is incomplete")


def _has_campaign_ids(bundle: dict[str, Any]) -> bool:
    promotion = bundle.get("promotion")
    groups = promotion.get("adverts", []) if isinstance(promotion, dict) else []
    for group in groups if isinstance(groups, list) else []:
        adverts = group.get("advert_list", []) if isinstance(group, dict) else []
        if any(
            _is_positive_integer(item.get("advertId"))
            for item in adverts
            if isinstance(item, dict)
        ):
            return True

    pages = bundle.get("upd")
    for page in pages if isinstance(pages, list) else []:
        payload = page.get("payload", []) if isinstance(page, dict) else []
        if any(
            _is_positive_integer(item.get("advertId"))
            for item in payload
            if isinstance(item, dict)
        ):
            return True
    return False


def _is_positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and (
        (isinstance(value, int) and value > 0)
        or (isinstance(value, str) and value.strip().isdigit() and int(value) > 0)
    )


def _normalize_campaigns(promotion: Any, adverts: Any) -> tuple[RawAdvertisingCampaign, ...]:
    if not isinstance(promotion, dict) or not isinstance(adverts, dict):
        raise AdvertisingNormalizationError("invalid advertising campaigns")
    promotion_rows = promotion.get("adverts")
    advert_rows = adverts.get("adverts")
    if not isinstance(promotion_rows, list) or not isinstance(advert_rows, list):
        raise AdvertisingNormalizationError("invalid advertising campaigns")

    promotion_meta: dict[int, tuple[int | None, int | None]] = {}
    for group in promotion_rows:
        if not isinstance(group, dict) or not isinstance(group.get("advert_list"), list):
            raise AdvertisingNormalizationError("invalid promotion campaign")
        campaign_type = _nullable_integer(group.get("type"), "type")
        status = _nullable_integer(group.get("status"), "status")
        for item in group["advert_list"]:
            if not isinstance(item, dict):
                raise AdvertisingNormalizationError("invalid promotion campaign")
            campaign_id = _positive_integer(item.get("advertId"), "advertId")
            meta = (campaign_type, status)
            if campaign_id in promotion_meta and promotion_meta[campaign_id] != meta:
                raise AdvertisingNormalizationError("conflicting promotion campaign")
            promotion_meta[campaign_id] = meta

    normalized: list[RawAdvertisingCampaign] = []
    seen_ids: set[int] = set()
    for row in advert_rows:
        if not isinstance(row, dict):
            raise AdvertisingNormalizationError("invalid advert campaign")
        campaign_id = _positive_integer(row.get("id"), "advertId")
        seen_ids.add(campaign_id)
        campaign_type, promotion_status = promotion_meta.get(campaign_id, (None, None))
        settings = row.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise AdvertisingNormalizationError("invalid advert campaign")
        nm_settings = row.get("nm_settings")
        if nm_settings is None:
            nm_settings = []
        if not isinstance(nm_settings, list):
            raise AdvertisingNormalizationError("invalid advert campaign")
        if any(not isinstance(item, dict) for item in nm_settings):
            raise AdvertisingNormalizationError("invalid advert campaign")
        member_nm_ids = tuple(
            sorted({_positive_integer(item.get("nm_id"), "nmId") for item in nm_settings})
        )
        status = _nullable_integer(row.get("status"), "status")
        normalized.append(
            _campaign(
                campaign_id=campaign_id,
                name=_nullable_string((settings or {}).get("name")),
                campaign_type=campaign_type,
                status=status if status is not None else promotion_status,
                payment_type=_nullable_string((settings or {}).get("payment_type")),
                bid_type=_nullable_string(row.get("bid_type")),
                member_nm_ids=member_nm_ids,
            )
        )
    for campaign_id, (campaign_type, status) in promotion_meta.items():
        if campaign_id not in seen_ids:
            normalized.append(
                _campaign(
                    campaign_id=campaign_id,
                    name=None,
                    campaign_type=campaign_type,
                    status=status,
                    payment_type=None,
                    bid_type=None,
                    member_nm_ids=(),
                )
            )
    return _dedupe(normalized)


def _campaign(**values: Any) -> RawAdvertisingCampaign:
    source_identity = f"campaign-v1|{values['campaign_id']}"
    payload = {"source_identity": source_identity, **values}
    return RawAdvertisingCampaign(
        source_identity=source_identity, payload_checksum=_hash(payload), **values
    )


def _normalize_fullstats(period: Period, fullstats: Any) -> tuple[RawAdvertisingFact, ...]:
    if not isinstance(fullstats, list):
        raise AdvertisingNormalizationError("invalid fullstats")
    facts: list[RawAdvertisingFact] = []
    for response in fullstats:
        page_period = _page_period(period, response, "fullstats")
        payload = response.get("payload")
        if not isinstance(payload, list):
            raise AdvertisingNormalizationError("invalid fullstats payload")
        for row in payload:
            facts.extend(_normalize_fullstats_campaign(page_period, row))
    return _dedupe(facts)


def _normalize_fullstats_campaign(period: Period, row: Any) -> list[RawAdvertisingFact]:
    if not isinstance(row, dict):
        raise AdvertisingNormalizationError("invalid fullstats row")
    campaign_id = _positive_integer(row.get("advertId"), "advertId")
    currency = _nullable_string(row.get("currency"))
    header_metrics = _metrics(row)
    _require_metrics(header_metrics)
    facts = [
        _fact(
            period=period,
            grain="period",
            business_date=None,
            campaign_id=campaign_id,
            fact_scope="campaign",
            app_type=None,
            nm_id=None,
            currency=currency,
            metrics=header_metrics,
        )
    ]
    if "days" not in row:
        if "apps" in row:
            raise AdvertisingNormalizationError("unsupported period-only fullstats")
        return facts
    days = row["days"]
    if not isinstance(days, list):
        raise AdvertisingNormalizationError("invalid fullstats days")
    for day_row in days:
        if not isinstance(day_row, dict):
            raise AdvertisingNormalizationError("invalid fullstats day")
        business_date = _business_date(day_row.get("date"), "fullstats date")
        _require_date_in_period(period, business_date)
        day_metric_values = _metrics(day_row)
        _require_metrics(day_metric_values)
        facts.append(
            _fact(
                period=period,
                grain="day",
                business_date=business_date,
                campaign_id=campaign_id,
                fact_scope="campaign",
                app_type=None,
                nm_id=None,
                currency=currency,
                metrics=day_metric_values,
            )
        )
        apps = day_row.get("apps", [])
        if not isinstance(apps, list):
            raise AdvertisingNormalizationError("invalid fullstats apps")
        for app in apps:
            if not isinstance(app, dict):
                raise AdvertisingNormalizationError("invalid fullstats app")
            app_type = _integer(app.get("appType"), "appType")
            if app_type is None:
                raise AdvertisingNormalizationError("invalid appType")
            app_metric_values = _metrics(app)
            _require_metrics(app_metric_values)
            nms = app.get("nms", [])
            if not isinstance(nms, list):
                raise AdvertisingNormalizationError("invalid fullstats nms")
            for nm in nms:
                if not isinstance(nm, dict):
                    raise AdvertisingNormalizationError("invalid fullstats nm")
                leaf_metrics = _metrics(nm)
                _require_metrics(leaf_metrics)
                facts.append(
                    _fact(
                        period=period,
                        grain="day",
                        business_date=business_date,
                        campaign_id=campaign_id,
                        fact_scope="source_sku",
                        app_type=app_type,
                        nm_id=_positive_integer(nm.get("nmId"), "nmId"),
                        currency=currency,
                        metrics=leaf_metrics,
                    )
                )
            _validate_hierarchy(app, nms)
        _validate_hierarchy(day_row, apps)
    _validate_hierarchy(row, days)
    return facts


def _fact(
    *,
    period: Period,
    grain: FactGrain,
    business_date: date | None,
    campaign_id: int,
    fact_scope: FactScope,
    app_type: int | None,
    nm_id: int | None,
    currency: str | None,
    metrics: dict[str, int | None],
) -> RawAdvertisingFact:
    identity_date = business_date.isoformat() if business_date else period.cache_key
    date_from = business_date if grain == "day" else period.date_from
    date_to = business_date if grain == "day" else period.date_to
    source_identity = (
        f"fullstats-v1|{grain}|{fact_scope}|{campaign_id}|{identity_date}|"
        f"{app_type if app_type is not None else ''}|{nm_id or ''}"
    )
    payload = {
        "source_identity": source_identity,
        "grain": grain,
        "date_from": date_from,
        "date_to": date_to,
        "business_date": business_date,
        "campaign_id": campaign_id,
        "fact_scope": fact_scope,
        "app_type": app_type,
        "nm_id": nm_id,
        "currency": currency,
        **metrics,
    }
    return RawAdvertisingFact(
        source_identity=source_identity,
        payload_checksum=_hash(payload),
        grain=grain,
        date_from=date_from,
        date_to=date_to,
        business_date=business_date,
        campaign_id=campaign_id,
        fact_scope=fact_scope,
        app_type=app_type,
        nm_id=nm_id,
        currency=currency,
        **metrics,
    )


def _normalize_upd(period: Period, upd: Any) -> tuple[RawAdvertisingSpendDocument, ...]:
    if not isinstance(upd, list):
        raise AdvertisingNormalizationError("invalid upd")
    documents: list[RawAdvertisingSpendDocument] = []
    for response in upd:
        page_period = _page_period(period, response, "upd")
        payload = response.get("payload")
        if not isinstance(payload, list):
            raise AdvertisingNormalizationError("invalid upd payload")
        for row in payload:
            if not isinstance(row, dict):
                raise AdvertisingNormalizationError("invalid upd row")
            upd_num = _identifier(row.get("updNum"), "updNum")
            campaign_id = _positive_integer(row.get("advertId"), "advertId")
            upd_time = _timestamp(row.get("updTime"), "updTime")
            business_date = upd_time.astimezone(MOSCOW).date()
            if not page_period.date_from <= business_date <= page_period.date_to:
                # WB can include a posting just after midnight on the following day.
                # Keep its raw response checksum, but never charge another business day.
                continue
            spend_kopecks = _money(row.get("updSum"), "updSum", nonnegative=False)
            if spend_kopecks is None:
                raise AdvertisingNormalizationError("invalid updSum")
            source_identity = (
                f"upd-v1|{upd_num}|{campaign_id}|{upd_time.astimezone(timezone.utc).isoformat()}"
            )
            values = {
                "upd_num": upd_num,
                "upd_time": upd_time.astimezone(timezone.utc),
                "business_date": business_date,
                "campaign_id": campaign_id,
                "campaign_name": _nullable_string(row.get("campName")),
                "campaign_type": _nullable_integer(row.get("advertType"), "advertType"),
                "payment_type": _nullable_string(row.get("paymentType")),
                "campaign_status": _nullable_integer(row.get("advertStatus"), "advertStatus"),
                "currency": _nullable_string(row.get("currency")),
                "spend_kopecks": spend_kopecks,
            }
            documents.append(
                RawAdvertisingSpendDocument(
                    source_identity=source_identity,
                    payload_checksum=_hash({"source_identity": source_identity, **values}),
                    **values,
                )
            )
    return _dedupe(documents)


def _metrics(row: dict[str, Any]) -> dict[str, int | None]:
    return {
        target: _money(row.get(source), source)
        if money
        else _integer(row.get(source), source)
        for target, source, money in _METRICS
    }


def _require_metrics(metrics: dict[str, int | None]) -> None:
    if not any(value is not None for value in metrics.values()):
        raise AdvertisingNormalizationError("fullstats source metrics are missing")


def _validate_hierarchy(
    parent: dict[str, Any], children: list[dict[str, Any]]
) -> None:
    for _, field, money in _METRICS:
        parent_value = _source_number(parent.get(field))
        child_values = [
            value
            for child in children
            if (value := _source_number(child.get(field))) is not None
        ]
        tolerance = _MONEY_HIERARCHY_TOLERANCE if money else 0
        if money and parent_value is not None and all(
            value == value.quantize(_MONEY_HIERARCHY_TOLERANCE)
            for value in [parent_value, *child_values]
        ):
            # Match reconciliation's bound for independently rounded kopecks.
            tolerance *= max(1, (len(child_values) + 1) // 2)
        if child_values and (
            parent_value is None or sum(child_values) > parent_value + tolerance
        ):
            raise AdvertisingNormalizationError("fullstats hierarchy totals are invalid")


def _source_number(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    return Decimal(str(raw).replace(",", "."))


def _positive_integer(raw: Any, field: str) -> int:
    value = _integer(raw, field)
    if value is None or value < 1:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _nullable_integer(raw: Any, field: str) -> int | None:
    return _integer(raw, field)


def _integer(raw: Any, field: str) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise AdvertisingNormalizationError(f"invalid {field}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        value = int(raw)
    else:
        raise AdvertisingNormalizationError(f"invalid {field}")
    if value < 0:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _money(raw: Any, field: str, *, nonnegative: bool = True) -> int | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise AdvertisingNormalizationError(f"invalid {field}")
    try:
        value = Decimal(str(raw).replace(",", ".")) * 100
    except InvalidOperation as exc:
        raise AdvertisingNormalizationError(f"invalid {field}") from exc
    if not value.is_finite() or (nonnegative and value < 0):
        raise AdvertisingNormalizationError(f"invalid {field}")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _timestamp(raw: Any, field: str) -> datetime:
    if raw in (None, ""):
        raise AdvertisingNormalizationError(f"invalid {field}")
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdvertisingNormalizationError(f"invalid {field}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _business_date(raw: Any, field: str) -> date:
    return _timestamp(raw, field).astimezone(MOSCOW).date()


def _require_date_in_period(period: Period, value: date) -> None:
    if not period.date_from <= value <= period.date_to:
        raise AdvertisingNormalizationError("advertising date is outside period")


def _nullable_string(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise AdvertisingNormalizationError("invalid advertising string")
    return raw.strip() or None


def _identifier(raw: Any, field: str) -> str:
    if raw in (None, "") or isinstance(raw, bool):
        raise AdvertisingNormalizationError(f"invalid {field}")
    value = str(raw).strip()
    if not value:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _dedupe(items: list[Any]) -> tuple[Any, ...]:
    unique: dict[str, Any] = {}
    for item in items:
        previous = unique.get(item.source_identity)
        if previous is not None and previous.payload_checksum != item.payload_checksum:
            raise AdvertisingNormalizationError(
                f"source identity {item.source_identity} has conflicting payloads"
            )
        unique[item.source_identity] = item
    return tuple(sorted(unique.values(), key=lambda item: item.source_identity))


def _canonical_manifest(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, list):
        raise AdvertisingNormalizationError("advertising manifest is missing")
    if _contains_secret_key(manifest):
        raise AdvertisingNormalizationError(
            "advertising manifest contains forbidden key"
        )
    try:
        copied = json.loads(_json(manifest))
    except (TypeError, ValueError) as exc:
        raise AdvertisingNormalizationError("advertising manifest is invalid") from exc
    return sorted(copied, key=_json)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            any(part in str(key).casefold() for part in _SECRET_KEY_PARTS)
            or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _evidence(
    facts: tuple[RawAdvertisingFact, ...],
    campaigns: tuple[RawAdvertisingCampaign, ...],
    documents: tuple[RawAdvertisingSpendDocument, ...],
    raw_manifest: list[dict[str, Any]],
    manifest_checksum: str,
) -> RawAdvertisingEvidence:
    logical_manifest = [
        {key: value for key, value in item.items() if key != "wbRequestId"}
        for item in raw_manifest
    ]
    source_total = sum(
        fact.spend_kopecks or 0
        for fact in facts
        if fact.grain == "period" and fact.fact_scope == "campaign"
    )
    document_total = sum(document.spend_kopecks for document in documents)
    snapshot_checksum = _hash(
        {
            "facts": facts,
            "campaigns": campaigns,
            "spend_documents": documents,
            "source_total_spend_kopecks": source_total,
            "document_total_spend_kopecks": document_total,
            "raw_manifest_checksum": _hash(logical_manifest),
        }
    )
    return RawAdvertisingEvidence(
        facts=facts,
        campaigns=campaigns,
        spend_documents=documents,
        source_total_spend_kopecks=source_total,
        document_total_spend_kopecks=document_total,
        raw_manifest=raw_manifest,
        raw_manifest_checksum=manifest_checksum,
        snapshot_checksum=snapshot_checksum,
    )


def _hash(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _json(payload: Any) -> str:
    return json.dumps(
        _jsonable(payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
