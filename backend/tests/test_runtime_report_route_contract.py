from __future__ import annotations

import warnings
from copy import deepcopy
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from vella_wb_19_05.router import router as reference_router
from vella_wb_19_05.stubs import build_pnl_report_stub

_REFERENCE_ROUTES_BEFORE_RUNTIME_IMPORT = tuple(reference_router.routes)
_REFERENCE_ROUTE_INVENTORY_BEFORE_RUNTIME_IMPORT = tuple(
    (route.path, frozenset(route.methods or ()), route.endpoint)
    for route in _REFERENCE_ROUTES_BEFORE_RUNTIME_IMPORT
)

from app import main as runtime_main
from app.control_plane.auth import ActorContext
from app.routers import wb_19_05 as runtime_adapter
from app.routers import wb_reports_sprint_d as real_reports

SUPERSEDED_REPORT_PATHS = frozenset(
    {
        "/api/v1/wb-reports/pnl",
        "/api/v1/wb-reports/ads/performance",
        "/api/v1/wb-reports/rnp",
        "/api/v1/wb-reports/abc",
    }
)

REAL_REPORT_ENDPOINTS = {
    "/api/v1/wb-reports/pnl": real_reports.get_pnl_report,
    "/api/v1/wb-reports/ads/performance": real_reports.get_ads_performance,
    "/api/v1/wb-reports/rnp": real_reports.get_rnp_report,
    "/api/v1/wb-reports/abc": real_reports.get_abc_report,
}

ORIGINAL_DUPLICATE_OPERATION_IDS = frozenset(
    {
        "get_pnl_report_api_v1_wb_reports_pnl_get",
        "get_ads_performance_api_v1_wb_reports_ads_performance_get",
        "get_rnp_report_api_v1_wb_reports_rnp_get",
        "get_abc_report_api_v1_wb_reports_abc_get",
    }
)

LOCAL_ADAPTER_ENDPOINTS = {
    "/api/v1/source-registry": runtime_adapter.list_blockers_for_demo,
    "/api/v1/source-registry/blockers/paginated": runtime_adapter.list_blockers_paginated,
}


def _get_routes(routes: list[Any], path: str) -> list[Any]:
    return [
        route
        for route in iter_route_contexts(routes)
        if getattr(route, "path", None) == path
        and "GET" in (getattr(route, "methods", None) or ())
    ]


def _app_with(router: Any) -> FastAPI:
    app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
    app.include_router(router)
    return app


def _without_tags(operation: dict[str, Any]) -> dict[str, Any]:
    comparable = deepcopy(operation)
    comparable.pop("tags", None)
    return comparable


def test_runtime_registers_each_superseded_report_get_once_with_real_endpoint() -> None:
    app = runtime_main.create_app()

    for path, endpoint in REAL_REPORT_ENDPOINTS.items():
        matches = _get_routes(app.routes, path)
        assert len(matches) == 1, path
        assert matches[0].endpoint is endpoint
        assert matches[0].endpoint.__module__ == "app.routers.wb_reports_sprint_d"


def test_runtime_openapi_uses_real_report_operations_without_known_duplicate_warnings() -> None:
    app = runtime_main.create_app()
    real_only_schema = _app_with(real_reports.router).openapi()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runtime_schema = app.openapi()

    duplicate_warnings = {
        operation_id
        for warning in caught
        for operation_id in ORIGINAL_DUPLICATE_OPERATION_IDS
        if operation_id in str(warning.message)
    }
    assert duplicate_warnings == set()

    for path in SUPERSEDED_REPORT_PATHS:
        assert runtime_schema["paths"][path]["get"] == real_only_schema["paths"][path]["get"]

    pnl_parameters = {
        parameter["name"]: parameter
        for parameter in runtime_schema["paths"]["/api/v1/wb-reports/pnl"]["get"]["parameters"]
    }
    assert pnl_parameters["source"] == {
        "name": "source",
        "in": "query",
        "required": False,
        "schema": {
            "enum": ["operative", "preliminary", "final"],
            "type": "string",
            "default": "preliminary",
            "title": "Source",
        },
    }


def test_reference_router_and_retained_runtime_operations_are_unchanged() -> None:
    current_inventory = tuple(
        (route.path, frozenset(route.methods or ()), route.endpoint)
        for route in reference_router.routes
    )
    assert tuple(reference_router.routes) == _REFERENCE_ROUTES_BEFORE_RUNTIME_IMPORT
    assert current_inventory == _REFERENCE_ROUTE_INVENTORY_BEFORE_RUNTIME_IMPORT

    standalone_app = _app_with(reference_router)
    standalone_routes = tuple(iter_route_contexts(standalone_app.routes))
    assert tuple(
        (route.path, frozenset(route.methods or ()), route.endpoint)
        for route in standalone_routes
    ) == _REFERENCE_ROUTE_INVENTORY_BEFORE_RUNTIME_IMPORT

    for path in SUPERSEDED_REPORT_PATHS:
        [standalone_route] = _get_routes(standalone_app.routes, path)
        assert standalone_route.endpoint.__module__ == "vella_wb_19_05.router"

    app = runtime_main.create_app()
    runtime_schema = app.openapi()
    standalone_schema = standalone_app.openapi()
    retained_reference_routes = [
        route
        for route in _REFERENCE_ROUTES_BEFORE_RUNTIME_IMPORT
        if not (
            route.path in SUPERSEDED_REPORT_PATHS
            and frozenset(route.methods or ()) == frozenset({"GET"})
        )
    ]
    for reference_route in retained_reference_routes:
        [runtime_route] = [
            route
            for route in _get_routes(app.routes, reference_route.path)
            if route.endpoint is reference_route.endpoint
        ]
        assert runtime_route.endpoint is reference_route.endpoint
        assert _without_tags(runtime_schema["paths"][reference_route.path]["get"]) == (
            _without_tags(standalone_schema["paths"][reference_route.path]["get"])
        )
        assert runtime_schema["paths"][reference_route.path]["get"]["tags"] == [
            "wb-19-05-runtime-boundary"
        ]

    for path, endpoint in LOCAL_ADAPTER_ENDPOINTS.items():
        [adapter_route] = _get_routes(runtime_adapter.router.routes, path)
        [runtime_route] = _get_routes(app.routes, path)
        assert adapter_route.endpoint is endpoint
        assert runtime_route.endpoint is endpoint


def test_unauthorized_pnl_request_is_denied_before_report_data_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = ActorContext(
        actor_id="actor-denied",
        user_id="user-denied",
        organization_id=701,
        permission_profile="viewer",
        permissions=frozenset(),
    )
    permission_calls: list[str] = []

    monkeypatch.setattr(real_reports, "actor_from_request", lambda _request: actor)

    def deny_permission(*, permission: str, **_kwargs: Any) -> None:
        permission_calls.append(permission)
        raise HTTPException(status_code=403, detail=f"NO_ACCESS:{permission}")

    monkeypatch.setattr(real_reports, "assert_permission_or_audit", deny_permission)
    monkeypatch.setattr(
        real_reports,
        "has_permission",
        lambda *_args, **_kwargs: pytest.fail("finance permission checked after denial"),
    )
    monkeypatch.setattr(
        real_reports,
        "build_pnl_report",
        lambda **_kwargs: pytest.fail("P&L data accessed before permission denial"),
    )
    monkeypatch.setattr(
        real_reports,
        "record_audit_event",
        lambda **_kwargs: pytest.fail("post-report audit reached after permission denial"),
    )

    client = TestClient(runtime_main.create_app())
    try:
        response = client.get("/api/v1/wb-reports/pnl")
    finally:
        client.close()

    assert response.status_code == 403
    assert permission_calls == ["settings:read"]


def test_authorized_pnl_request_reaches_real_handler_with_source_and_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = ActorContext(
        actor_id="actor-authorized",
        user_id="user-authorized",
        organization_id=702,
        permission_profile="finance",
        permissions=frozenset({"settings:read", "finance:read"}),
    )
    permission_calls: list[dict[str, Any]] = []
    build_calls: list[dict[str, Any]] = []
    audit_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(real_reports, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(
        real_reports,
        "assert_permission_or_audit",
        lambda **kwargs: permission_calls.append(kwargs),
    )
    monkeypatch.setattr(
        real_reports,
        "has_permission",
        lambda actual_actor, permission: actual_actor is actor and permission == "finance:read",
    )

    def fake_build_pnl_report(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return build_pnl_report_stub(
            date_from=kwargs["date_from"],
            date_to=kwargs["date_to"],
            group_by=kwargs["group_by"],
        )

    monkeypatch.setattr(real_reports, "build_pnl_report", fake_build_pnl_report)
    monkeypatch.setattr(
        real_reports,
        "record_audit_event",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    client = TestClient(runtime_main.create_app())
    try:
        response = client.get(
            "/api/v1/wb-reports/pnl",
            params={
                "dateFrom": "2026-04-01",
                "dateTo": "2026-04-30",
                "groupBy": "brand",
                "source": "operative",
            },
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert permission_calls == [
        {
            "actor": actor,
            "permission": "settings:read",
            "action": "reports.pnl.get",
            "object_type": "wb_report",
            "object_id": "pnl:brand",
            "reason": "actor cannot read P&L report",
        }
    ]
    assert build_calls == [
        {
            "date_from": date(2026, 4, 1),
            "date_to": date(2026, 4, 30),
            "group_by": "brand",
            "requested_state": "operative",
            "finance_allowed": True,
            "organization_id": 702,
            "wb_token": None,
        }
    ]
    assert len(audit_calls) == 1
    assert audit_calls[0]["actor"] is actor
    assert audit_calls[0]["action"] == "reports.pnl.get"
