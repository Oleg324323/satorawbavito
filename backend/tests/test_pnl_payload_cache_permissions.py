"""Report payload caches must preserve the builder's finance permission scope."""

from copy import deepcopy
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import repricer_tasks
from app.main import create_app
from app.routers import wb_reports_bff as reports
from tests.auth_helpers import auth_headers

START, END = date(2026, 8, 17), date(2026, 8, 23)
PARAMS = {"preset": "custom", "from": START.isoformat(), "to": END.isoformat()}
PRIVATE_CASH_FLOW = {
    "status": "ready",
    "data": {
        "rows": [
            {
                "article": "synthetic-private-rent",
                "type": "expense",
                "operationalExpense": True,
                "amountKopecks": 164_000,
            }
        ],
        "totals": {"operationalExpenseKopecks": 164_000},
        "balances": {"closingBalanceKopecks": 130_000},
    },
}


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(reports, '_report_start_guard', lambda *a: nullcontext())
    monkeypatch.setattr(repricer_tasks.build_report_for_org, 'apply_async',
        lambda args, **kw: repricer_tasks.build_report_for_org.delay(*args))
    monkeypatch.setattr(repricer_tasks.build_report_for_org, 'delay', lambda *a: SimpleNamespace(id='synthetic-task'))
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (True, []))
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    monkeypatch.setenv('VELLA_1C_ENABLED', 'true')
    entries = {}
    reports._saved_previews.clear()
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    monkeypatch.setattr(
        reports,
        "get_source_cache",
        lambda org, key, **kw: deepcopy(entries.get((org, key))),
    )
    monkeypatch.setattr(
        reports,
        "save_source_cache",
        lambda org, key, value: entries.__setitem__((org, key), deepcopy(value)),
    )
    monkeypatch.setattr(
        reports,
        "list_source_cache_by_prefix",
        lambda org, prefix, **kw: [
            {**deepcopy(value), "sourceKey": key}
            for (owner, key), value in reversed(list(entries.items()))
            if owner == org and key.startswith(prefix)
        ],
    )
    monkeypatch.setattr(
        repricer_tasks, "_report_snapshot_sources_ready", lambda *args, **kw: (True, [])
    )
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period", lambda **kw: deepcopy(PRIVATE_CASH_FLOW)
    )

    def build_report(**kwargs):
        row = {
            "label": "SKU-1",
            "revenueKopecks": 100_000,
            "overheadKopecks": 12_345 if kwargs["finance_allowed"] else None,
        }
        return SimpleNamespace(
            blockerIds=[],
            sourceStatus="fresh",
            groupBy="sku",
            totals=SimpleNamespace(
                revenueKopecks=100_000, netProfitKopecks=20_000, marginPct=20
            ),
            rows=[
                SimpleNamespace(
                    label="SKU-1", marginPct=20, model_dump=lambda **kw: deepcopy(row)
                )
            ],
        )

    monkeypatch.setattr(reports, "build_pnl_report", build_report)
    return entries


@pytest.mark.parametrize(
    "endpoint,params",
    [
        ("/api/wb/reports/pnl", PARAMS),
        ("/api/wb/reports/pnl/latest-cache", {}),
        ("/api/wb/reports/pnl/latest-cache", PARAMS),
    ],
)
@pytest.mark.parametrize("build_order", [(True, False), (False, True)])
def test_worker_and_readers_keep_both_permission_variants(
    cache, endpoint, params, build_order
):
    for finance_allowed in build_order:
        result = repricer_tasks.build_report_for_org.run(
            1,
            "synthetic-user",
            "pnl",
            START.isoformat(),
            END.isoformat(),
            "sku",
            "operational",
            finance_allowed,
            None,
        )
        assert result["state"] == "completed"
    api = TestClient(create_app())
    for profile, expected in (("viewer", None), ("finance_viewer", 12_345)):
        response = api.get(endpoint, params=params, headers=auth_headers(api, profile))
        assert response.status_code == 200
        assert response.json()["rows"][0]["overheadKopecks"] == expected
        assert response.json()["cashFlow"] == (
            PRIVATE_CASH_FLOW if expected is not None else None
        )
    assert (
        len([key for org, key in cache if key.startswith("reports_payload_pnl_")]) == 2
    )


@pytest.mark.parametrize(
    "endpoint,params,status",
    [
        ("/api/wb/reports/pnl", PARAMS, 200),
        ("/api/wb/reports/pnl/latest-cache", {}, 404),
        ("/api/wb/reports/pnl/latest-cache", PARAMS, 404),
    ],
)
@pytest.mark.parametrize(
    "legacy_prefix,legacy_suffix,version",
    [("", "", "v1"), ("", "_nofinance", "v1"), ("v2_", "_nofinance", "v2")],
)
def test_reader_does_not_reuse_old_unscoped_financial_payload(
    cache, endpoint, params, status, legacy_prefix, legacy_suffix, version
):
    # Older endpoints accepted arbitrary source strings; a suffix alone is not provenance.
    key = f"reports_payload_pnl_{legacy_prefix}{START}_{END}_sku_operational{legacy_suffix}"
    cache[1, key] = {
        "completedAt": reports._utc_now_iso(),
        "dateFrom": str(START),
        "dateTo": str(END),
        "report": {
            "cacheVersion": version,
            "rows": [
                {
                    "label": "PRIVATE",
                    "overheadKopecks": None if version == "v2" else 12_345,
                }
            ],
            "cashFlow": deepcopy(PRIVATE_CASH_FLOW),
        },
    }
    api = TestClient(create_app())
    response = api.get(endpoint, params=params, headers=auth_headers(api, "viewer"))
    # Missing compatible projections are built by the worker, not an HTTP
    # permission fallback that can block the gateway on a large raw ledger.
    assert response.status_code == status
    assert not response.json().get("rows")
    assert 'PRIVATE' not in response.text


def test_exact_cache_writer_uses_finance_scope(cache):
    for finance_allowed in (True, False):
        reports._save_exact_report_payload_cache(
            organization_id=1,
            report_id="pnl",
            date_from=START,
            date_to=END,
            group_by="sku",
            source="financial",
            finance_allowed=finance_allowed,
            report={
                "cacheVersion": reports.PNL_REPORT_PAYLOAD_VERSION,
                "rows": [{"overheadKopecks": 12_345 if finance_allowed else None}],
            },
        )
    assert len(cache) == 2
    for finance_allowed, expected in ((True, 12_345), (False, None)):
        latest = reports._latest_report_payload_cache(
            organization_id=1,
            report_id="pnl",
            group_by="sku",
            source="financial",
            finance_allowed=finance_allowed,
        )
        assert latest is not None
        assert latest[0]["report"]["rows"][0]["overheadKopecks"] == expected


@pytest.mark.parametrize(
    "profile,finance_allowed", [("viewer", False), ("finance_viewer", True)]
)
def test_job_endpoints_select_the_callers_permission_cache(
    cache, monkeypatch, profile, finance_allowed
):
    repricer_tasks.build_report_for_org.run(
        1,
        "synthetic-user",
        "pnl",
        str(START),
        str(END),
        "sku",
        "operational",
        not finance_allowed,
        None,
    )
    monkeypatch.setattr(
        reports, "_report_daily_sources_ready", lambda *args, **kw: (True, [])
    )
    enqueued = []

    def enqueue(*args):
        enqueued.append(args)
        return SimpleNamespace(id="synthetic-task")

    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", enqueue)
    api = TestClient(create_app())
    headers = auth_headers(api, profile)
    status = api.get("/api/wb/reports/pnl/jobs", params=PARAMS, headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "idle"
    started = api.post("/api/wb/reports/pnl/jobs", params=PARAMS, headers=headers)
    assert started.status_code == 200
    assert started.json()["state"] == "queued"
    assert started.json()["cashFlow"] == (
        PRIVATE_CASH_FLOW if finance_allowed else None
    )
    assert len(enqueued) == 1
    assert enqueued[0][-2:] == (finance_allowed, None)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/reports/cash-flow"),
        ("GET", "/api/wb/reports/cash-flow"),
        ("GET", "/api/wb/reports/expenses"),
        ("GET", "/api/wb/reports/expenses/latest-cache"),
        ("GET", "/api/wb/reports/expenses/jobs"),
        ("POST", "/api/wb/reports/expenses/jobs"),
    ],
)
@pytest.mark.parametrize("profile", ["viewer", "finance_viewer"])
def test_cash_flow_and_expenses_require_finance_access(
    cache, monkeypatch, method, path, profile
):
    reports._save_exact_report_payload_cache(
        organization_id=1,
        report_id="expenses",
        date_from=START,
        date_to=END,
        group_by="sku",
        source="operational",
        report={
            "cacheVersion": reports.EXPENSES_REPORT_PAYLOAD_VERSION,
            "rows": [{"amountKopecks": 164_000}],
            "cashFlow": deepcopy(PRIVATE_CASH_FLOW),
        },
    )
    reads = []

    def read_cash_flow(**kwargs):
        reads.append(kwargs)
        return deepcopy(PRIVATE_CASH_FLOW)

    monkeypatch.setattr(reports, "get_cash_flow_for_period", read_cash_flow)
    api = TestClient(create_app())
    response = api.request(
        method, path, params=PARAMS, headers=auth_headers(api, profile)
    )
    assert response.status_code == (403 if profile == "viewer" else 200)
    if profile == "viewer":
        assert "finance:read" in response.text
        assert (
            "164000" not in response.text
            and "synthetic-private-rent" not in response.text
        )
        assert reads == []
    elif not path.endswith("/jobs"):
        actual = response.json() if "cash-flow" in path else response.json()["cashFlow"]
        assert actual == PRIVATE_CASH_FLOW


@pytest.fixture
def abc_cache(cache, monkeypatch):
    monkeypatch.setattr(reports, "_abc_economics_version", lambda org: "cafef00d")

    def build_abc(**kwargs):
        assert (kwargs["organization_id"], kwargs["date_from"], kwargs["date_to"]) == (1, START, END)
        allowed = kwargs["finance_allowed"]
        return SimpleNamespace(
            groupBy="sku", sourceStatus="fresh" if allowed else "partial", confidence="high",
            blockerIds=[] if allowed else ["WB_ABC_FINANCE_FORBIDDEN"], sourceEvidence=[],
            rows=[{
                "sku": "SYNTHETIC-ABC", "nmId": 101,
                "ordersComposite": {"units": 1, "kopecks": 90_000},
                "commissionKopecks": 12_345 if allowed else None,
                "netTotalKopecks": 45_678 if allowed else None,
            }],
            filteredSummary=SimpleNamespace(model_dump=lambda **kw: {
                "skuCount": 1, "ordersCount": 1, "ordersKopecks": 90_000,
                "profitKopecks": 45_678 if allowed else None,
            }),
        )

    monkeypatch.setattr(reports, "build_abc_report", build_abc)
    return cache


@pytest.mark.parametrize("report_id", ["abc", "pnl"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_background_report_publishes_retryable_tax_cache_metadata(abc_cache, monkeypatch, report_id, unavailable):
    build = getattr(reports, f"build_{report_id}_report")
    def with_tax_status(**kwargs):
        result = build(**kwargs)
        if unavailable:
            result.blockerIds = ["tax_policy_unavailable"]
        return result
    monkeypatch.setattr(reports, f"build_{report_id}_report", with_tax_status)
    result = repricer_tasks.build_report_for_org.run(
        1, "synthetic-user", report_id, str(START), str(END), "sku", "operational", True, None,
    )
    assert result["state"] == "completed"
    key = reports._report_cache_key(report_id, START, END, "sku", "operational", organization_id=1, finance_allowed=True)
    cached = abc_cache[1, key]
    assert cached["taxRevision"] == "synthetic-confirmation"
    before = deepcopy(abc_cache)
    api = TestClient(create_app())
    response = api.get(f"/api/wb/reports/{report_id}/latest-cache", params=PARAMS, headers=auth_headers(api, "finance_viewer"))
    assert response.status_code == 200
    if unavailable:
        assert response.json()['cache']['fresh'] is False
        assert 'tax_policy_unavailable' in response.json()['blockerIds']
    assert abc_cache == before


@pytest.mark.parametrize("report_id", ["abc", "pnl"])
@pytest.mark.parametrize("build_order", [(True, False), (False, True)])
def test_overlapping_report_jobs_keep_permission_scopes_isolated(
    abc_cache, monkeypatch, report_id, build_order
):
    monkeypatch.setattr(
        reports, "_report_daily_sources_ready", lambda *args, **kw: (True, [])
    )
    enqueued = {}

    def enqueue(*args):
        finance_allowed = args[-2]
        task = SimpleNamespace(
            id=f"{'finance' if finance_allowed else 'nofinance'}-task"
        )
        enqueued[finance_allowed] = args
        return task

    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", enqueue)
    api = TestClient(create_app())

    task_ids = {}
    for finance_allowed in build_order:
        headers = auth_headers(
            api, "finance_viewer" if finance_allowed else "viewer"
        )
        started = api.post(
            f"/api/wb/reports/{report_id}/jobs", params=PARAMS, headers=headers
        )
        assert started.status_code == 200
        assert started.json()["reused"] is False
        assert started.json()["taskId"]
        task_ids[finance_allowed] = started.json()["taskId"]

        repeated = api.post(
            f"/api/wb/reports/{report_id}/jobs", params=PARAMS, headers=headers
        )
        assert repeated.status_code == 200
        assert repeated.json()["reused"] is True
        assert repeated.json()["taskId"] == started.json()["taskId"]

    source_suffix = "_operational" if report_id == "pnl" else ""
    expected_job_keys = {
        f"reports_job_{report_id}_{START}_{END}_sku{source_suffix}_finance",
        f"reports_job_{report_id}_{START}_{END}_sku{source_suffix}_nofinance",
    }
    assert {
        key for org, key in abc_cache if key.startswith(f"reports_job_{report_id}_")
    } == expected_job_keys
    assert set(enqueued) == {False, True}

    first_allowed, second_allowed = build_order
    first_result = repricer_tasks.build_report_for_org.run(*enqueued[first_allowed])
    assert first_result["state"] == "completed"

    first_headers = auth_headers(
        api, "finance_viewer" if first_allowed else "viewer"
    )
    second_headers = auth_headers(
        api, "finance_viewer" if second_allowed else "viewer"
    )
    first_status = api.get(
        f"/api/wb/reports/{report_id}/jobs", params=PARAMS, headers=first_headers
    )
    second_status = api.get(
        f"/api/wb/reports/{report_id}/jobs", params=PARAMS, headers=second_headers
    )
    assert first_status.json()["state"] == "completed"
    assert second_status.json()["state"] == "queued"
    assert second_status.json()["taskId"] == task_ids[second_allowed]

    second_result = repricer_tasks.build_report_for_org.run(*enqueued[second_allowed])
    assert second_result["state"] == "completed"
    for finance_allowed, headers in (
        (first_allowed, first_headers),
        (second_allowed, second_headers),
    ):
        response = api.get(
            f"/api/wb/reports/{report_id}", params=PARAMS, headers=headers
        )
        latest = api.get(
            f"/api/wb/reports/{report_id}/latest-cache",
            params=PARAMS,
            headers=headers,
        )
        assert response.status_code == latest.status_code == 200
        expected = 12_345 if finance_allowed else None
        field = "commissionKopecks" if report_id == "abc" else "overheadKopecks"
        assert response.json()["rows"][0].get(field) == expected
        assert latest.json()["rows"][0].get(field) == expected
        if report_id == "pnl":
            expected_cash_flow = PRIVATE_CASH_FLOW if finance_allowed else None
            assert response.json()["cashFlow"] == expected_cash_flow
            assert latest.json()["cashFlow"] == expected_cash_flow
        assert response.json()["reportJob"]["state"] == "completed"
        assert latest.json()["reportJob"]["state"] == "completed"


@pytest.mark.parametrize("build_order", [(True, False), (False, True)])
@pytest.mark.parametrize("suffix,params", [("", PARAMS), ("/latest-cache", PARAMS), ("/latest-cache", {})])
def test_abc_worker_and_readers_keep_both_permission_variants(abc_cache, build_order, suffix, params):
    for allowed in build_order:
        result = repricer_tasks.build_report_for_org.run(
            1, "synthetic-user", "abc", str(START), str(END), "sku", "operational", allowed, None
        )
        assert result["state"] == "completed"
    api = TestClient(create_app())
    for profile, expected in (("viewer", None), ("finance_viewer", 12_345)):
        response = api.get(f"/api/wb/reports/abc{suffix}", params=params, headers=auth_headers(api, profile))
        assert response.status_code == 200
        assert response.json()["rows"][0]["sku"] == "SYNTHETIC-ABC"
        assert response.json()["rows"][0].get("commissionKopecks") == expected
        assert ("WB_ABC_FINANCE_FORBIDDEN" in response.json()["blockerIds"]) == (expected is None)
    assert len([key for org, key in abc_cache if key.startswith("reports_payload_abc_")]) == 2


@pytest.mark.parametrize("build_order", [(True, False), (False, True)])
def test_abc_http_writers_keep_both_permission_variants(abc_cache, monkeypatch, build_order):
    monkeypatch.setattr(reports, "_report_daily_sources_ready", lambda *args, **kw: (True, []))
    api = TestClient(create_app())
    for allowed in build_order:
        response = api.get(
            "/api/wb/reports/abc", params=PARAMS,
            headers=auth_headers(api, "finance_viewer" if allowed else "viewer"),
        )
        assert response.status_code == 200
        assert response.json()["rows"][0].get("commissionKopecks") == (12_345 if allowed else None)
    assert len([key for org, key in abc_cache if key.startswith("reports_payload_abc_")]) == 2


def test_abc_exact_writer_and_latest_lookup_keep_finance_scope(abc_cache):
    for allowed in (True, False):
        reports._save_exact_report_payload_cache(
            organization_id=1, report_id="abc", date_from=START, date_to=END,
            group_by="sku", source="operational", finance_allowed=allowed,
            report={"cacheVersion": reports.ABC_REPORT_PAYLOAD_VERSION,
                    "rows": [{"commissionKopecks": 12_345 if allowed else None}]},
        )
    assert len(abc_cache) == 2
    for allowed in (True, False):
        latest = reports._latest_report_payload_cache(
            organization_id=1, report_id="abc", group_by="sku", source="operational", finance_allowed=allowed
        )
        assert latest is not None
        assert latest[0]["report"]["rows"][0]["commissionKopecks"] == (12_345 if allowed else None)


@pytest.mark.parametrize("legacy_suffix", ["", "_nofinance"])
@pytest.mark.parametrize("suffix,params,status", [("", PARAMS, 200), ("/latest-cache", PARAMS, 404), ("/latest-cache", {}, 404)])
def test_abc_readers_reject_old_unscoped_finance_cache(abc_cache, monkeypatch, legacy_suffix, suffix, params, status):
    # An arbitrary old source ending in _nofinance is not permission provenance.
    key = f"reports_payload_abc_v16_cafef00d_org1_{START}_{END}_sku_operational{legacy_suffix}"
    abc_cache[1, key] = {
        "completedAt": reports._utc_now_iso(), "dateFrom": str(START), "dateTo": str(END),
        "report": {"cacheVersion": "v16", "economicsVersion": "cafef00d",
                   "rows": [{"sku": "PRIVATE-OLD", "commissionKopecks": 12_345}]},
    }
    monkeypatch.setattr(reports, "_report_daily_sources_ready", lambda *args, **kw: (False, ["finance"]))
    api = TestClient(create_app())
    response = api.get(f"/api/wb/reports/abc{suffix}", params=params, headers=auth_headers(api, "viewer"))
    assert response.status_code == status
    assert not response.json().get("rows")
    assert "PRIVATE-OLD" not in response.text


@pytest.mark.parametrize("allowed", [True, False])
def test_abc_job_endpoints_select_the_callers_permission_cache(abc_cache, monkeypatch, allowed):
    repricer_tasks.build_report_for_org.run(
        1, "synthetic-user", "abc", str(START), str(END), "sku", "operational", not allowed, None
    )
    monkeypatch.setattr(reports, "_report_daily_sources_ready", lambda *args, **kw: (True, []))
    enqueued = []

    def enqueue(*args):
        enqueued.append(args)
        return SimpleNamespace(id="synthetic-task")

    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", enqueue)
    api = TestClient(create_app())
    headers = auth_headers(api, "finance_viewer" if allowed else "viewer")
    status = api.get("/api/wb/reports/abc/jobs", params=PARAMS, headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "idle"
    started = api.post("/api/wb/reports/abc/jobs", params=PARAMS, headers=headers)
    assert started.status_code == 200
    assert started.json()["state"] == "queued"
    assert len(enqueued) == 1 and enqueued[0][-2:] == (allowed, None)


def test_scheduled_abc_snapshot_is_saved_in_finance_scope(abc_cache, monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._build_repricer_sku_snapshot", lambda *args, **kw: {})
    monkeypatch.setattr(
        repricer_tasks, "_report_snapshot_sources_ready",
        lambda org, sources, *args: (sources == ("period-stats", "finance", "ads", "baskets"), []),
    )
    for name in ("build_cached_wb_reports_sources_snapshot", "_build_digest_ads_snapshot", "_build_digest_funnel_snapshot", "build_plan_fact_report"):
        monkeypatch.setattr(reports, name, lambda *args, **kw: None)
    monkeypatch.setattr(reports, "_build_digest_payload", lambda *args: {})
    monkeypatch.setattr(repricer_tasks, "_digest_report_summary", lambda *args: {})
    save_exact = reports._save_exact_report_payload_cache

    def save(**kwargs):
        if kwargs["report_id"] == "abc":
            assert kwargs.get("finance_allowed") is True
        return save_exact(**kwargs)

    monkeypatch.setattr(reports, "_save_exact_report_payload_cache", save)
    profile = SimpleNamespace(period_days=7, date_from=START, date_to=END, sources=("period-stats",), window_kind="periodic")
    result = repricer_tasks._materialize_report_snapshots_for_profile(1, profile, persist_progress=False)
    assert result["state"] == "completed", result
    assert "abc" in result["reports"]
    key = reports._report_cache_key("abc", START, END, "sku", "operational", organization_id=1, finance_allowed=True)
    assert abc_cache[1, key]["report"]["rows"][0]["commissionKopecks"] == 12_345
    assert (1, reports._report_cache_key("abc", START, END, "sku", "operational", organization_id=1)) not in abc_cache


@pytest.mark.parametrize("report_id", ["abc", "pnl"])
@pytest.mark.parametrize("build_order", [(True, False), (False, True), (True,)])
def test_rules_preview_uses_only_the_callers_finance_cache(abc_cache, report_id, build_order):
    version = reports.ABC_REPORT_PAYLOAD_VERSION if report_id == "abc" else reports.PNL_REPORT_PAYLOAD_VERSION
    for allowed in build_order:
        reports._save_exact_report_payload_cache(
            organization_id=1, report_id=report_id, date_from=START, date_to=END,
            group_by="sku", source="operational", finance_allowed=allowed,
            report={"meta": {"id": report_id}, "cacheVersion": version,
                    "rows": [{"sku": "SYNTHETIC-PREVIEW", "ctrPct": 9,
                              "marginPct": 12.345 if allowed else None}]},
        )
    reports._save_exact_report_payload_cache(
        organization_id=1, report_id="stock", date_from=START, date_to=END,
        group_by="sku", source="operational",
        report={"meta": {"id": "stock"}, "rows": [{"sku": "PUBLIC-STOCK", "ctrPct": 9}]},
    )
    api = TestClient(create_app())
    for profile, allowed in (("settings_editor", False), ("admin", True)):
        headers = auth_headers(api, profile)
        rules = api.get("/api/wb/reports/rules", headers=headers).json()["profile"]
        config = deepcopy(rules["config"])
        config["qualityBands"]["ctrPct"] = {"goodMin": 7, "averageMin": 6}
        config["qualityBands"]["marginPct"] = {"goodMin": 30, "thinMin": 20, "lossBelow": 15}
        response = api.post(
            "/api/wb/reports/rules/preview", headers=headers,
            json={"expectedVersion": rules["version"], "name": "Synthetic preview",
                  "preset": "custom", "config": config},
        )
        assert response.status_code == 200
        assert ("12.345" in response.text) is allowed
        payload = response.json()
        expected_count = 2 if allowed in build_order else 1
        assert payload["affectedSkuCount"] == expected_count
        assert len(payload["sampleRows"]) == expected_count
        assert any(row["sku"] == "PUBLIC-STOCK" for row in payload["sampleRows"])
        assert payload["availableReports"] == ([report_id, "stock"] if allowed in build_order else ["stock"])
        assert payload["previewToken"]


@pytest.mark.parametrize("report_id,version", [("abc", "v16_cafef00d_org1"), ("pnl", "v2")])
def test_rules_preview_rejects_legacy_unscoped_finance_cache(abc_cache, report_id, version):
    key = f"reports_payload_{report_id}_{version}_{START}_{END}_sku_operational_nofinance"
    abc_cache[1, key] = {
        "completedAt": reports._utc_now_iso(),
        "report": {"meta": {"id": report_id}, "cacheVersion": version.split("_")[0],
                   "economicsVersion": "cafef00d",
                   "rows": [{"sku": "PRIVATE-LEGACY", "marginPct": 12.345}]},
    }
    api = TestClient(create_app())
    headers = auth_headers(api, "settings_editor")
    rules = api.get("/api/wb/reports/rules", headers=headers).json()["profile"]
    config = deepcopy(rules["config"])
    config["qualityBands"]["marginPct"] = {"goodMin": 30, "thinMin": 20, "lossBelow": 15}
    response = api.post(
        "/api/wb/reports/rules/preview", headers=headers,
        json={"expectedVersion": rules["version"], "name": "Synthetic preview",
              "preset": "custom", "config": config},
    )
    assert response.status_code == 200
    assert response.json()["sampleRows"] == []
    assert response.json()["affectedSkuCount"] == 0
    assert response.json()["availableReports"] == []
    assert "12.345" not in response.text and "PRIVATE-LEGACY" not in response.text
