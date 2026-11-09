from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import get_db_session
from app.modules.wb_reports.abc_pnl import AbcPnlPage, WbAbcPnlService
from app.modules.wb_reports.schemas import (
    AbcPnlMetaView,
    AbcPnlPageView,
    AbcPnlRowView,
    AbcPnlSummaryView,
)
from app.modules.wb_reports.table_export import (
    ReportTableExportRequest,
    render_report_table_export,
)
from app.platform.clock import utc_now
from app.platform.finance.access import (
    get_finance_actor,
    require_finance_read,
    require_wb_account_scope,
)
from app.platform.finance.schemas import FinancePeriodView, FinanceSnapshotView
from app.platform.finance.service import FinanceAccountNotFound
from app.platform.economics.wb_quarterly_tax import QuarterlyTaxConflict
from app.platform.period import Period, PeriodValidationError
from app.wb_reports_sprint_d import abc_spp_by_nm

router = APIRouter(tags=["wb-reports-v2"])
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_TABLE_EXPORT_BODY_BYTES = 5 * 1024 * 1024


async def _table_export_request(request: Request) -> ReportTableExportRequest:
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > MAX_TABLE_EXPORT_BODY_BYTES
    ):
        raise HTTPException(413, detail={"code": "REPORT_EXPORT_BODY_TOO_LARGE"})
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_TABLE_EXPORT_BODY_BYTES:
            raise HTTPException(413, detail={"code": "REPORT_EXPORT_BODY_TOO_LARGE"})
        body.extend(chunk)
    try:
        return ReportTableExportRequest.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(
            422,
            detail={"code": "VALIDATION_ERROR", "message": "Request validation failed"},
        ) from None


def _view(
    page: AbcPnlPage,
    marketplace_account_id: int,
    resolved_now: datetime,
    organization_id: int,
    include_spp: bool,
) -> AbcPnlPageView:
    period = page.period
    snapshot = page.snapshot
    spp_by_nm = abc_spp_by_nm(organization_id, period.date_from, period.date_to) if include_spp else {}
    return AbcPnlPageView(
        items=[
            AbcPnlRowView(
                nmId=row.nm_id,
                sellerArticle=row.seller_article,
                **spp_by_nm.get(str(row.nm_id), {}),
                catalogSkuId=row.catalog_sku_id,
                operationCount=row.operation_count,
                revenueKopecks=row.revenue_kopecks,
                salesRevenueKopecks=row.sales_revenue_kopecks,
                returnsRevenueKopecks=row.returns_revenue_kopecks,
                mainRevenueKopecks=row.main_revenue_kopecks,
                redemptionsRevenueKopecks=row.redemptions_revenue_kopecks,
                lateCorrectionRevenueKopecks=row.late_correction_revenue_kopecks,
                unknownRevenueKopecks=row.unknown_revenue_kopecks,
                salesUnits=row.sales_units,
                returnsUnits=row.returns_units,
                netUnits=row.net_units,
                commissionKopecks=row.commission_kopecks,
                logisticsKopecks=row.logistics_kopecks,
                storageKopecks=row.storage_kopecks,
                acceptanceKopecks=row.acceptance_kopecks,
                penaltyKopecks=row.penalty_kopecks,
                deductionKopecks=row.deduction_kopecks,
                additionalPaymentKopecks=row.additional_payment_kopecks,
                financeOtherExpensesKopecks=row.finance_other_expenses_kopecks,
                compensationKopecks=row.compensation_kopecks,
                acquiringKopecks=row.acquiring_kopecks,
                financeExpensesKopecks=row.finance_expenses_kopecks,
                costValueState=row.cost_value_state,
                costEvidenceStatus=row.cost_evidence_status,
                cogsKopecks=row.cogs_kopecks,
                settlementProfitKopecks=row.settlement_profit_kopecks,
                economicsValueState=row.economics_value_state,
                economicsEvidenceStatus=row.economics_evidence_status,
                taxKopecks=row.tax_kopecks,
                otherExpensesKopecks=row.other_expenses_kopecks,
                profitBeforeAdsAndLoyaltyKopecks=(
                    row.profit_before_ads_and_loyalty_kopecks
                ),
                advertisingSpendKopecks=row.advertising_spend_kopecks,
                profitBeforeLoyaltyKopecks=row.profit_before_loyalty_kopecks,
                cashbackAmountKopecks=row.cashback_amount_kopecks,
                cashbackDiscountKopecks=row.cashback_discount_kopecks,
                cashbackCommissionChangeKopecks=(
                    row.cashback_commission_change_kopecks
                ),
                loyaltyNetCostKopecks=row.loyalty_net_cost_kopecks,
                profitAfterLoyaltyKopecks=row.profit_after_loyalty_kopecks,
                salesClass=row.sales_class,
                profitClass=row.profit_class,
                abcCode=row.abc_code,
                netProfitKopecks=row.net_profit_kopecks,
                profitBeforeInternalExpensesKopecks=(
                    row.profit_before_internal_expenses_kopecks
                ),
                internalExpensesKopecks=row.internal_expenses_kopecks,
                blockerIds=list(row.blocker_ids),
            )
            for row in page.items
        ],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        summary=AbcPnlSummaryView(
            operationCount=page.summary.operation_count,
            skuCount=page.summary.sku_count,
            revenueKopecks=page.summary.revenue_kopecks,
            salesRevenueKopecks=page.summary.sales_revenue_kopecks,
            returnsRevenueKopecks=page.summary.returns_revenue_kopecks,
            salesUnits=page.summary.sales_units,
            returnsUnits=page.summary.returns_units,
            netUnits=page.summary.net_units,
            commissionKopecks=page.summary.commission_kopecks,
            logisticsKopecks=page.summary.logistics_kopecks,
            storageKopecks=page.summary.storage_kopecks,
            acceptanceKopecks=page.summary.acceptance_kopecks,
            penaltyKopecks=page.summary.penalty_kopecks,
            deductionKopecks=page.summary.deduction_kopecks,
            financeOtherExpensesKopecks=page.summary.finance_other_expenses_kopecks,
            compensationKopecks=page.summary.compensation_kopecks,
            acquiringKopecks=page.summary.acquiring_kopecks,
            financeExpensesKopecks=page.summary.finance_expenses_kopecks,
            cogsKopecks=page.summary.cogs_kopecks,
            settlementProfitKopecks=page.summary.settlement_profit_kopecks,
            taxKopecks=page.summary.tax_kopecks,
            otherExpensesKopecks=page.summary.other_expenses_kopecks,
            profitBeforeAdsAndLoyaltyKopecks=(
                page.summary.profit_before_ads_and_loyalty_kopecks
            ),
            advertisingSpendKopecks=page.summary.advertising_spend_kopecks,
            unattributedAdvertisingSpendKopecks=(
                page.summary.unattributed_advertising_spend_kopecks
            ),
            profitBeforeLoyaltyKopecks=(page.summary.profit_before_loyalty_kopecks),
            cashbackAmountKopecks=page.summary.cashback_amount_kopecks,
            cashbackDiscountKopecks=page.summary.cashback_discount_kopecks,
            cashbackCommissionChangeKopecks=(
                page.summary.cashback_commission_change_kopecks
            ),
            loyaltyNetCostKopecks=page.summary.loyalty_net_cost_kopecks,
            profitAfterLoyaltyKopecks=page.summary.profit_after_loyalty_kopecks,
            netProfitKopecks=page.summary.net_profit_kopecks,
            profitBeforeInternalExpensesKopecks=(
                page.summary.profit_before_internal_expenses_kopecks
            ),
            internalExpensesKopecks=page.summary.internal_expenses_kopecks,
        ),
        meta=AbcPnlMetaView(
            state=page.state,
            marketplaceAccountId=marketplace_account_id,
            period=FinancePeriodView(
                dateFrom=period.date_from,
                dateTo=period.date_to,
                startAt=period.start_at,
                endExclusiveAt=period.end_exclusive_at,
                days=period.days,
                temporalState=period.temporal_state(resolved_now),
            ),
            snapshot=(
                FinanceSnapshotView(
                    syncRunId=snapshot.sync_run_id,
                    snapshotChecksum=snapshot.snapshot_checksum,
                    formulaVersion=snapshot.formula_version,
                    operationCount=snapshot.operation_count,
                    capturedAt=snapshot.captured_at,
                    lastObservedAt=snapshot.last_observed_at,
                )
                if snapshot
                else None
            ),
            formulaVersion=page.formula_version,
            costLedgerRevision=page.cost_ledger_revision,
            economicsRevision=page.economics_revision,
            advertisingSource=(
                page.advertising_snapshot.source_kind
                if page.advertising_snapshot
                else "finance_promotion" if snapshot else None
            ),
            advertisingSnapshotChecksum=(
                page.advertising_snapshot.snapshot_checksum
                if page.advertising_snapshot
                else snapshot.snapshot_checksum if snapshot else None
            ),
            advertisingEvidenceStatus=(
                page.advertising_snapshot.evidence_status
                if page.advertising_snapshot
                else "raw" if snapshot else None
            ),
            blockerIds=list(page.blocker_ids),
        ),
        timestamp=resolved_now,
    )


@router.get("/api/v2/wb/reports/abc-pnl", response_model=AbcPnlPageView)
def get_wb_abc_pnl(
    actor: ActorContext = Depends(get_finance_actor),
    session: Session = Depends(get_db_session),
    marketplaceAccountId: int = Query(gt=0),
    periodDays: int = Query(default=7, ge=1, le=90),
    dateFrom: date | None = Query(default=None),
    dateTo: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    includeSpp: bool = Query(default=False),
    includePnlExpenses: bool = Query(default=False),
) -> AbcPnlPageView:
    require_finance_read(actor)
    require_wb_account_scope(session, actor, marketplaceAccountId)
    resolved_now = utc_now()
    try:
        period = Period.resolve(
            period_days=periodDays,
            date_from=dateFrom,
            date_to=dateTo,
            now=resolved_now,
        )
    except PeriodValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PERIOD", "message": str(exc)},
        ) from exc
    try:
        page = WbAbcPnlService(
            session,
            actor.organization_id,
            now=lambda: resolved_now,
        ).get_page(
            marketplaceAccountId,
            period,
            limit=limit,
            offset=offset,
        )
    except QuarterlyTaxConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "WB_TAX_SETTINGS_CHANGED", "message": str(exc)},
        ) from exc
    except FinanceAccountNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "WB_ACCOUNT_NOT_FOUND", "message": str(exc)},
        ) from exc
    view = _view(page, marketplaceAccountId, resolved_now, actor.organization_id, includeSpp)
    if includePnlExpenses:
        from app.platform.advertising.raw_backfill import _connected_wb_account_id, RawAdvertisingBackfillError
        from app.modules.wb_reports.schemas import PnlProductExpensesView, PnlExpenseOnlyView
        from app.wb_pnl_expenses import load_pnl_finance, product_expenses
        # The legacy raw cache is connection-scoped, not account-ID scoped.
        # Never attach it to a different/ambiguous canonical WB account.
        try:
            _connected_wb_account_id(session, actor.organization_id, marketplaceAccountId)
        except RawAdvertisingBackfillError:
            finance = {}
        else:
            finance = load_pnl_finance(actor.organization_id, period.date_from, period.date_to)
        for item in view.items:
            item.pnlExpenses = PnlProductExpensesView(**product_expenses(finance, item.nmId))
        view.meta.pnlExpenseRevision = finance.get("pnlExpenseRevision")
        # Carry fee-only products and unallocated charges independently of
        # canonical pagination. The client merges these AFTER all pages, never
        # inventing revenue/profit for a row not yet in the canonical snapshot.
        fee_ids = set(finance.get("pnlExpenseAmounts", {})) | set(finance.get("pnlExpenseUnknown", {}))
        view.meta.pnlExpenseRows = [PnlExpenseOnlyView(nmId=int(nm) or None,
            **product_expenses(finance, int(nm))) for nm in sorted(fee_ids)
            if any(finance.get("pnlExpenseAmounts", {}).get(nm, {}).values())
            or finance.get("pnlExpenseUnknown", {}).get(nm)]
    return view


@router.post("/api/v2/wb/reports/table.xlsx")
def export_wb_report_table(
    payload: Annotated[ReportTableExportRequest, Depends(_table_export_request)],
    actor: Annotated[ActorContext, Depends(get_finance_actor)],
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    require_finance_read(actor)
    require_wb_account_scope(session, actor, payload.marketplaceAccountId)
    try:
        Period(payload.dateFrom, payload.dateTo)
    except PeriodValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PERIOD", "message": str(exc)},
        ) from exc
    content = render_report_table_export(
        payload,
        organization_id=actor.organization_id,
    )
    filename = f"wb-{payload.reportKind}-{payload.dateFrom}-{payload.dateTo}.xlsx"
    return Response(
        content,
        media_type=XLSX_MIME,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
