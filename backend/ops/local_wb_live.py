"""Opt-in local WB price transport and isolated repricer queue configuration."""
import json

from ops.load_local_wb_readonly import allowed_request as read_allowed

REPORT_QUEUE = "satorna.local.readonly-reports"
REPRICER_QUEUE = "satorna.local.wb-repricer"
REPORT_TASKS = (
    "reports.build_report_for_org", "reports.build_digest_for_org",
    "reports.refresh_report_sources_for_org", "reports.refresh_budget_step",
    "reports.refresh_sales_funnel",
)
REPRICER_TASKS = (
    "repricer.execute_assigned_all_orgs", "repricer.execute_assigned_for_org",
)
PRICE_ENDPOINTS = {
    ("POST", "/api/v2/upload/task"),
    ("POST", "/api/v2/upload/task/size"),
    ("GET", "/api/v2/history/tasks"),
    ("GET", "/api/v2/history/goods/task"),
    ("GET", "/api/v2/buffer/tasks"),
    ("GET", "/api/v2/buffer/goods/task"),
}


def resolve_live_scope():
    from sqlalchemy import select
    from app.infra.db import get_session_factory
    from app.platform.integrations.orm import MarketplaceAccountRow
    from app.platform.integrations.wb_credentials import resolve_bound_wb_credential

    with get_session_factory()() as session:
        accounts = session.execute(select(
            MarketplaceAccountRow.organization_id, MarketplaceAccountRow.marketplace_account_id,
        ).where(MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.status == "connected")).all()
        if len(accounts) != 1:
            raise RuntimeError("Local WB live mode requires exactly one connected WB account")
        org, account = accounts[0]
        resolve_bound_wb_credential(session, org, marketplace_account_id=account)
        return int(org), int(account)


def _assigned_upload_allowed(request, scope):
    """Recheck durable assignments at every final POST, after client waits/retries."""
    if scope is None:
        return False
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError
    from fastapi import HTTPException
    from app.infra.db import get_session_factory
    from app.platform.integrations.wb_credentials import resolve_bound_wb_credential
    from app.repricer_bff import _frontend_strategy_definition
    from app.repricer_cache.orm import WbRepricerGoodsCacheRow
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow

    try:
        body = json.loads(request.content)
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not 0 < len(rows) <= 1000 or any(
            not isinstance(row, dict) or type(row.get("nmID")) is not int or row["nmID"] <= 0 for row in rows
        ):
            return False
        targets = {row["nmID"] for row in rows}
        token = request.headers.get("Authorization", "").strip()
        if not token:
            return False
        org, account = scope
        with get_session_factory()() as session:
            resolve_bound_wb_credential(session, org, marketplace_account_id=account, wb_token=token)
            runtime = session.get(WbRepricerRuntimeStateRow, org)
            state = runtime.state_payload if runtime else {}
            articles = {}
            pages = session.scalars(select(WbRepricerGoodsCacheRow.goods_payload).where(
                WbRepricerGoodsCacheRow.organization_id == org))
            for goods in pages:
                for good in goods:
                    if good.get("nmID") in targets:
                        articles.setdefault(good["nmID"], set()).add(str(good.get("vendorCode") or ""))
            if set(articles) != targets or any(len(names) != 1 for names in articles.values()):
                return False
            for names in articles.values():
                article = next(iter(names))
                assignment = (state.get("assignments") or {}).get(article) or {}
                settings = (state.get("skuSettingsOverrides") or {}).get(article) or {}
                meta = (state.get("skuMetaOverrides") or {}).get(article) or {}
                if (not article or not assignment.get("strategyId")
                        or str(assignment.get("source") or "").strip().lower() in {"", "none", "derived"}
                        or settings.get("automationEnabled") is False
                        or meta.get("status") in {"manual", "warmup"}
                        or meta.get("assignmentSource") == "none"):
                    return False
                if _frontend_strategy_definition(assignment["strategyId"]).get("status") == "disabled":
                    return False
        return True
    except (ValueError, TypeError, KeyError, RuntimeError, SQLAlchemyError, HTTPException):
        # Fail closed without logging credentials, bodies, or database errors.
        return False


def allowed_request(request, *, scope=None):
    # Normal repricer execution retains assignment, permissions, baseline,
    # price guards, and confirmed readback checks. This only opens transport.
    price_endpoint = (
        request.url.scheme == "https"
        and request.url.port in (None, 443)
        and request.url.host == "discounts-prices-api.wildberries.ru"
        and (request.method, request.url.path) in PRICE_ENDPOINTS
    )
    return read_allowed(request) or (price_endpoint and (
        request.method == "GET" or _assigned_upload_allowed(request, scope)
    ))


def configure_local_celery(app, *, live_scope=None):
    routes = {name: {"queue": REPORT_QUEUE} for name in REPORT_TASKS}
    if live_scope is not None:
        routes.update({name: {"queue": REPRICER_QUEUE} for name in REPRICER_TASKS})
        assigned = app.conf.beat_schedule["repricer-execute-assigned"]
        # Do not start unrelated review, Avito, or full-account sync schedules.
        app.conf.beat_schedule = {
            "repricer-execute-assigned": {
                **assigned, "task": "repricer.execute_assigned_for_org",
                "kwargs": {**assigned.get("kwargs", {}), "organization_id": live_scope[0]},
                "options": {**assigned.get("options", {}), "queue": REPRICER_QUEUE},
            },
        }
    app.conf.task_routes = {**app.conf.task_routes, **routes}
