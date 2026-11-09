# Frontend ABC/P&L Canonical Cutover Plan

## Scope

Move the ABC and financial P&L frontend consumers to `GET /api/v2/wb/reports/abc-pnl` for explicitly allowlisted organizations only. Keep the legacy routes as the default and a rollback path requiring a frontend rebuild/redeploy. Do not change backend code or the final P&L formula.

## Contract boundaries

- Resolve rollout from a default-empty `organizationId:marketplaceAccountId` environment mapping.
- Send the selected UI period as explicit `dateFrom` and `dateTo` query parameters for every page.
- Validate every response page with the backend schema and reject non-JSON or contract drift.
- Fetch all pages with a maximum page size of 500 and reject pagination/meta drift.
- Preserve canonical `null` values, source state/evidence, and blocker IDs.
- Present `profitAfterLoyaltyKopecks` as preliminary profit after loyalty; never label it final net profit and never synthesize a second ABC class.
- Preserve loading, empty/future/missing, partial, no-access, error, abort, and stale-request behavior.

## Implementation

1. Add failing unit tests for rollout parsing, exact/custom periods, pagination, contract validation, state preservation, null semantics, adapter output, non-JSON API responses, and Vercel routing.
2. Add a typed canonical ABC/P&L client and compatibility adapters.
3. Route the existing ABC live bridge and financial P&L island through the canonical client only when the organization mapping is enabled.
4. Add canonical-aware labels and state messages while leaving the operational P&L and all legacy paths intact.
5. Run focused tests, typecheck, build, lint on changed files where available, and the project suite to record baseline failures.

## Rollback

Remove the organization from `VITE_CANONICAL_WB_ABC_PNL_ROLLOUT` (or leave the variable empty) and redeploy the frontend build. The new frontend build uses the existing legacy ABC/P&L consumers; no backend deploy is required.
