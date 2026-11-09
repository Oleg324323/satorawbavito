import copy
import json
from datetime import date
from pathlib import Path

import pytest

from app.platform.advertising.raw import (
    AdvertisingNormalizationError,
    _validate_hierarchy,
    normalize_raw_advertising,
)
from app.platform.period import Period

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 1))


@pytest.fixture
def raw_bundle() -> dict[str, object]:
    return json.loads(
        (Path(__file__).parent / "fixtures/wb_advertising_raw_sanitized.json").read_text()
    )


@pytest.mark.parametrize("app_type", [0, 1])
def test_raw_hierarchy_preserves_scope_app_and_exact_totals(raw_bundle, app_type):
    raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][0]["appType"] = app_type
    evidence = normalize_raw_advertising(PERIOD, raw_bundle)
    assert evidence.source_total_spend_kopecks == 200
    assert evidence.document_total_spend_kopecks == 200
    assert len(evidence.spend_documents) == 2
    leaves = [fact for fact in evidence.facts if fact.fact_scope == "source_sku"]
    assert [(fact.app_type, fact.spend_kopecks) for fact in leaves] == [(app_type, 110), (32, 40)]
    assert leaves[0].source_identity.endswith(f"|{app_type}|2001")
    assert leaves[1].clicks == 1
    assert leaves[1].cart_adds == 0
    assert leaves[1].cancel_count == 1


@pytest.mark.parametrize("app_type", [None, True, -1, 1.5])
def test_raw_rejects_invalid_platform_codes(raw_bundle, app_type):
    raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][0]["appType"] = app_type
    with pytest.raises(AdvertisingNormalizationError, match="appType"):
        normalize_raw_advertising(PERIOD, raw_bundle)


def test_raw_metrics_distinguish_missing_from_explicit_zero(raw_bundle):
    del raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][1]["nms"][0][
        "atbs"
    ]

    leaves = [
        fact
        for fact in normalize_raw_advertising(PERIOD, raw_bundle).facts
        if fact.fact_scope == "source_sku"
    ]

    assert leaves[0].cart_adds == 1
    assert leaves[1].order_count == 0
    assert leaves[1].cart_adds is None


def test_raw_campaign_null_nm_settings_means_no_current_membership(raw_bundle):
    raw_bundle["adverts"]["adverts"][0]["nm_settings"] = None

    campaigns = normalize_raw_advertising(PERIOD, raw_bundle).campaigns

    assert next(row for row in campaigns if row.campaign_id == 1001).member_nm_ids == ()


def test_raw_exact_duplicate_collapses_but_conflict_blocks(raw_bundle):
    duplicate = copy.deepcopy(raw_bundle["upd"][0]["payload"][0])
    raw_bundle["upd"][0]["payload"].append(duplicate)
    assert len(normalize_raw_advertising(PERIOD, raw_bundle).spend_documents) == 2
    raw_bundle["upd"][0]["payload"][-1]["updSum"] = 9
    with pytest.raises(AdvertisingNormalizationError, match="conflicting"):
        normalize_raw_advertising(PERIOD, raw_bundle)


@pytest.mark.parametrize(("field", "value"), [("sum", 9), ("views", 11)])
def test_raw_child_totals_cannot_exceed_parent(raw_bundle, field, value):
    raw_bundle["fullstats"][0]["payload"][0]["days"][0]["apps"][0]["nms"][0][
        field
    ] = value
    with pytest.raises(AdvertisingNormalizationError, match="hierarchy"):
        normalize_raw_advertising(PERIOD, raw_bundle)


def test_raw_hierarchy_compares_source_decimals_before_kopeck_rounding(raw_bundle):
    campaign = raw_bundle["fullstats"][0]["payload"][0]
    day = campaign["days"][0]
    campaign["sum"] = day["sum"] = 0.01
    for app in day["apps"]:
        app["sum"] = app["nms"][0]["sum"] = 0.005

    evidence = normalize_raw_advertising(PERIOD, raw_bundle)

    assert [
        fact.spend_kopecks
        for fact in evidence.facts
        if fact.campaign_id == 1001 and fact.fact_scope == "source_sku"
    ] == [1, 1]


@pytest.mark.parametrize(
    ("field", "parent_value", "expected_spend"),
    [("sum", 1.49, 199), ("sum_price", 4.99, 200)],
)
def test_raw_hierarchy_allows_one_kopeck_source_money_tolerance(
    raw_bundle, field, parent_value, expected_spend
):
    campaign = raw_bundle["fullstats"][0]["payload"][0]
    campaign[field] = campaign["days"][0][field] = parent_value

    evidence = normalize_raw_advertising(PERIOD, raw_bundle)

    assert evidence.source_total_spend_kopecks == expected_spend


@pytest.mark.parametrize(
    ("field", "parent_value"), [("sum", 1.4899), ("sum_price", 4.9899)]
)
def test_raw_hierarchy_rejects_money_beyond_one_kopeck(raw_bundle, field, parent_value):
    campaign = raw_bundle["fullstats"][0]["payload"][0]
    campaign[field] = campaign["days"][0][field] = parent_value

    with pytest.raises(AdvertisingNormalizationError, match="hierarchy"):
        normalize_raw_advertising(PERIOD, raw_bundle)


@pytest.mark.parametrize("field", ["sum", "sum_price"])
def test_raw_hierarchy_allows_independently_rounded_daily_money(field):
    _validate_hierarchy(
        {field: 1.42},
        [{field: value} for value in (0.36, 0.36, 0.36, 0.36, 0)],
    )


@pytest.mark.parametrize(
    ("field", "parent", "values"),
    [
        ("sum", 1.40, [0.36, 0.36, 0.36, 0.36, 0]),
        ("sum_price", 1.40, [0.36, 0.36, 0.36, 0.36, 0]),
        ("sum", 1.4299, [0.36, 0.36, 0.36, 0.36, 0]),
        ("views", 142, [36, 36, 36, 36, 0]),
        ("sum", None, [0.36] * 5),
    ],
)
def test_raw_hierarchy_keeps_rounding_bounded(field, parent, values):
    with pytest.raises(AdvertisingNormalizationError, match="hierarchy"):
        _validate_hierarchy({field: parent}, [{field: value} for value in values])


def test_raw_upd_preserves_signed_correction(raw_bundle):
    raw_bundle["upd"][0]["payload"][1]["updSum"] = -2

    evidence = normalize_raw_advertising(PERIOD, raw_bundle)

    assert [document.spend_kopecks for document in evidence.spend_documents] == [
        150,
        -200,
    ]
    assert evidence.document_total_spend_kopecks == -50


def test_raw_accepts_contained_source_subwindows(raw_bundle):
    period = Period(date(2026, 9, 1), date(2026, 9, 2))
    raw_bundle["period"] = {"dateFrom": "2026-09-01", "dateTo": "2026-09-02"}

    evidence = normalize_raw_advertising(period, raw_bundle)

    period_facts = [fact for fact in evidence.facts if fact.grain == "period"]
    assert {(fact.date_from, fact.date_to) for fact in period_facts} == {
        (date(2026, 9, 1), date(2026, 9, 1))
    }


def test_raw_excludes_spend_documents_outside_their_source_subwindow(raw_bundle):
    period = Period(date(2026, 9, 1), date(2026, 9, 2))
    raw_bundle["period"] = {"dateFrom": "2026-09-01", "dateTo": "2026-09-02"}
    raw_bundle["upd"][0]["payload"][0]["updTime"] = "2026-09-02T12:00:00+03:00"

    evidence = normalize_raw_advertising(period, raw_bundle)
    assert all(document.business_date == date(2026, 9, 1) for document in evidence.spend_documents)
    assert len(evidence.spend_documents) == len(raw_bundle["upd"][0]["payload"]) - 1


def test_empty_complete_bundle_does_not_require_unmade_fullstats_request(raw_bundle):
    raw_bundle["promotion"] = {"adverts": [], "all": 0}
    raw_bundle["adverts"] = {"adverts": []}
    raw_bundle["fullstats"] = []
    raw_bundle["upd"][0]["payload"] = []
    raw_bundle["manifest"] = [
        item
        for item in raw_bundle["manifest"]
        if item["endpoint"] != "GET /adv/v3/fullstats"
    ]

    evidence = normalize_raw_advertising(PERIOD, raw_bundle)

    assert evidence.facts == ()
    assert evidence.campaigns == ()
    assert evidence.spend_documents == ()
    assert evidence.source_total_spend_kopecks == 0
    assert evidence.document_total_spend_kopecks == 0


@pytest.mark.parametrize("campaign_source", ["promotion", "upd"])
def test_nonempty_campaign_union_still_requires_fullstats_manifest(
    raw_bundle, campaign_source
):
    raw_bundle["manifest"] = [
        item
        for item in raw_bundle["manifest"]
        if item["endpoint"] != "GET /adv/v3/fullstats"
    ]
    if campaign_source == "upd":
        raw_bundle["promotion"] = {"adverts": [], "all": 0}
        raw_bundle["adverts"] = {"adverts": []}

    with pytest.raises(AdvertisingNormalizationError, match="incomplete"):
        normalize_raw_advertising(PERIOD, raw_bundle)
