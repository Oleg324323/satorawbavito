from __future__ import annotations

from fastapi import APIRouter, Query
from vella_wb_19_05.registry import BLOCKER_REGISTRY, BlockerRegistryEntry
from vella_wb_19_05.router import router as reference_router

from app.contracts.envelopes import PaginatedEnvelope

router = APIRouter(tags=["wb-19-05-runtime-boundary"])

_SUPERSEDED_REPORT_PATHS = frozenset(
    {
        "/api/v1/wb-reports/pnl",
        "/api/v1/wb-reports/ads/performance",
        "/api/v1/wb-reports/rnp",
        "/api/v1/wb-reports/abc",
    }
)


@router.get("/api/v1/source-registry", response_model=list[BlockerRegistryEntry])
def list_blockers_for_demo(
    module: str | None = None,
    status: str | None = None,
) -> list[BlockerRegistryEntry]:
    rows = list(BLOCKER_REGISTRY.values())
    if module is not None and module != "wb":
        rows = [row for row in rows if row.module == module]
    if status is not None:
        rows = [row for row in rows if row.status == status]
    return rows


@router.get(
    "/api/v1/source-registry/blockers/paginated",
    response_model=PaginatedEnvelope[BlockerRegistryEntry],
)
def list_blockers_paginated(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    module: str | None = None,
    status: str | None = None,
) -> PaginatedEnvelope[BlockerRegistryEntry]:
    rows = list(BLOCKER_REGISTRY.values())
    if module is not None:
        rows = [row for row in rows if row.module == module]
    if status is not None:
        rows = [row for row in rows if row.status == status]
    return PaginatedEnvelope[BlockerRegistryEntry](
        items=rows[offset : offset + limit],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


_runtime_reference_router = APIRouter(
    routes=[
        route
        for route in reference_router.routes
        if not (
            getattr(route, "path", None) in _SUPERSEDED_REPORT_PATHS
            and getattr(route, "methods", None) == {"GET"}
        )
    ]
)
router.include_router(_runtime_reference_router)
