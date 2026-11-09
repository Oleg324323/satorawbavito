# T2 SKU override HTTP boundary — integration handoff

Status: isolated HTTP adapter over existing `SkuOverrideService`; not mounted,
not a legacy cutover. No new DDL, formula, provider call or config. ROOT owns
bootstrap/main; T1 owns metadata resolver/config; T4 owns frontend.

## Bootstrap contract

`create_wb_repricing_overrides_router` requires `service_factory()` returning
the existing PostgreSQL `SkuOverrideService`, and
`context_resolver(actor, account_id)` returning `(UserSessionPrincipal,
ExpectedAccountBinding)`. T1 resolver must derive active membership from the
authenticated server actor's user/org/session, and resolve the exact internal
org/account/WB metadata without loading a token. It is not an authorization
grant: the service retains its live session, permission, account and mapping
checks through commit. Never reuse the integrations-write credential resolver.

`enabled_for(org_id, account_id)` and `writer_fenced_for(org_id, account_id)`
default to denial and must return literal `True` for the exact pair. The latter
is an admission callback, NOT a transactional one-writer fence. Enable writes
only after independently proven legacy writer cutoff and calculation parity.
No permissive placeholder resolver/gate is acceptable. Reads need settings:read;
replacement needs settings:read AND settings:write (existing service policy).
Do not wire a new router merely to make imports pass.

## Wire contract

Prefix `/api/v2/wb/repricing/accounts/{account_id}/skus/{catalog_sku_id}`:

- GET `/overrides`: data `{marketplaceAccountId,catalogSkuId,version,revision}`.
  No head means version `"0"`, revision null; it does not mean effective defaults.
- GET `/overrides/history?limit=50&beforeRevision=...`: data contains `items`
  and `nextBeforeRevision`. Limit 1–100, descending exclusive revision cursor.
- PUT `/overrides`: JSON `{commandId,expectedVersion,values}`. UUID4 command ID;
  expectedVersion is a decimal string. Full replacement: ALL 14 values required,
  nullable. Omission is an error, not clear, merge or inheritance resolution.

Revision contains revision/parentRevision decimal strings, commandId,
actorMembershipId, createdAt, requestChecksum and values. Money uses nonnegative
decimal integer strings; percentages use finite fixed-decimal strings. Never
round-trip either through JavaScript Number. Booleans remain JSON booleans;
minute/basket integers follow the existing domain ranges. Explicit null remains
null. Membership/org/account fields in the body and unknown fields are rejected.
JSON budget is 16 KiB; duplicate keys are rejected. Safe errors only: conflict409,
permission403, mapping unresolved409, unavailable503. No database text returned.

## Legacy field mapping and parity gaps

| Legacy settings key | Canonical values key |
| --- | --- |
| automationEnabled | automation_enabled |
| allowNegativeMargin | allow_negative_margin |
| nightMedianEnabled | night_median_enabled |
| pMinKopecks | p_min_kopecks |
| pMaxKopecks | p_max_kopecks |
| rrpKopecks | rrp_kopecks |
| minMarginKopecks | min_margin_kopecks |
| maxMarginKopecks | max_margin_kopecks |
| minMarginPct | min_margin_pct |
| maxMarginPct | max_margin_pct |
| priceStepPct | price_step_pct |
| priceStepMinutes | price_step_minutes |
| basketNormManual | basket_norm_manual |
| basketNormMode | basket_norm_mode |

Evidence: `repricer_bff.py::put_repricer_sku_settings` merges a patch into the
effective settings, mutates global SKU settings/status and appends free-form
audit; the legacy router hydrates and flushes whole state and can reconcile
economics fields. The new boundary does none of these. It stores canonical
overrides through the existing immutable/versioned service, not effective
settings. Article text is not a CatalogSku ID; mapping must already resolve.
No conversion of legacy zero/default/sentinel values into null is authorized.
Frontend must preserve these distinctions; unsupported fields stay outside
this endpoint until their own bounded parity slice. Do not dual-write.

## Proof and rollout gates

HTTP fixtures cover default denial, exact account, unfenced write rejection,
server membership, exact large money/Decimal, required complete row, identity
injection, safe errors, pagination, body limit, duplicate JSON keys and wrong
scope response rejection. These mocks do NOT prove PostgreSQL persistence or
concurrency. Existing service owns CAS, immutable replay, audit and live guards;
ROOT must run real bootstrap + restricted DB integration before activation.

Sequence: T1 real resolver/config → ROOT bootstrap integration → T4 typed read
consumer → characterization and explicit legacy writer fence → account-scoped
write activation. Rollback disables new writes first; do not re-enable legacy
writes over diverged canonical rows without reconciliation. Current flags,
legacy flow and provider sending remain unchanged by this package.
