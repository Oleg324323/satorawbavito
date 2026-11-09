# T2 delegated marketplace account metadata discovery

Base `a4b5fe5e478c8a0887f2f6c3f1b653c3d99579f0`, branch
`codex/t2-account-discovery`, existing T2 worktree retained. Previous T2 source
branch remains at2422c48. Only new service/router/tests/report; no existing
authentication, schema, settings, connection router or shared guard modifications.

## Exact integration contract

`MarketplaceAccountDiscoveryService(*, engine, enabled=False)` accepts PostgreSQL
Engine and literal bool only. `make_marketplace_account_discovery_router(*,
service_dependency)` takes a synchronous zero-argument service factory. ROOT owns
lazy registration and `canonical_account_discovery_enabled` config supplied by
T1; no enabling default or fallback service is installed here.

GET `/api/v2/cabinet/marketplace-accounts`, optional `provider=wb|avito`; omission
returns both. Unknown, repeated or empty provider/extra query fields are422 with
fixed code. Exact response:

```json
{"data":[{"marketplaceAccountId":94102,"provider":"avito","externalAccountId":"discovery-avito","displayName":"Synthetic Avito","status":"connected"}]}
```

Internal account IDs sorted ascending; include disconnected metadata. This is not
authorization to fetch credentials, sync or send anything. Actor comes only from
existing `get_marketplace_credential_actor(request)`, which delegates to server
authentication, not integrations:write. Fixed live permission is cabinet:read.
No account IDs, actor or org from body/query; no invented synthetic account IDs.

Disabled503, invalid422, denied403, unavailable503. Original auth401 is preserved
with the same fixed safe access code; no raw exception or input echo. Every
response has Cache-Control:no-store. Service factory failures are also sanitized.

## Transaction/security properties

Own fresh Session/top-level transaction. Existing require_live_actor validates
current user, org membership, session activity/expiry, permission and selected
account ID representation under shared locks. Only after its validation do we
normalize allowed IDs; SQL filters exact org/provider/selected IDs. The metadata
SELECT contains only internal ID, provider, external ID, display_name and status;
no credential_ref or credential table access by this service.

Metadata rows locked FOR SHARE in ID order. Final before_commit callback expires
ORM state and repeats live checks plus exact metadata/scope comparison; fails
closed if it isn't the final callback, on nested root, pending ORM writes, or if
another callback is added during validation. IAM/metadata locks survive physical
commit. Result is returned only after successful commit AND Session.close. No
public empty-account publication guard exceptions are added or reused.

## Verification

- TDD red: new HTTP test collection failed with missing account_discovery module,
  exit2; implementation was absent.
- HTTP focused suite:12PASS,2existing dependency deprecation warnings,2.42s,
  exit0. Exact wire/filter/errors/no-store/auth401 covered without DB/network.
- Actual restricted-role PostgreSQL suite:10PASS,5.68s,exit0; one allocator-owned
  migrated DB. Covers WB/Avito scope, foreign org, selected IDs, disconnected,
  revoked/expired session, inactive user/member, missing permission, malformed
  allowed IDs, empty scope/default-off, actual closing expiry and commit failure.
- First PG attempt used native allocator path and failed before creating a DB:
 10 setup errors due network-denied ephemeral loopback bind. Corrected invocation
  used existing ORDERS_TEST_USE_LOCAL_CLUSTER=1 Unix-maintenance-socket fixture;
  no sandbox weakening, fixture edits or application DATABASE_URL.
- Fixture cleanup disposes both Engines and asserts exact owned database/role
  absence. Natural process exit observed; PG slot released to T1/ROOT.

Commands used `/tmp/satorna-backend311-20260909/bin/python -m pytest -q --tb=short`
with exact new test filenames under existing network-denied sandbox/env-i. PG
invocation additionally set ORDERS_TEST_USE_LOCAL_CLUSTER=1. Ruff from backend
via `/tmp/satorna-backend-verify-20260908/bin/python -m ruff`; compileall for the
four new Python files; git diff --check. No whole-suite claim.

## Remaining/rollback

ROOT actual bootstrap/config/UI integration remains a separate verification.
No public pagination contract was requested; this lists all authorized metadata.
The final-callback position guard is implemented; the10 DB run does not include
a separately injected later-listener scenario, so no claim of that extra test.
Rollback removes the unregistered factory or disables its default-off config;
no data/schema migration, account mutation or external effect to reverse.
No provider, secret store, production, price call, push or deploy was executed.
