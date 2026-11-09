# Satorna Architecture Wave: Terminal 1 Stage 1 handoff

Date: 2026-09-08. Base: `c88a474695569af905b294906ba604d61f3de62f`.
Branch: `codex/arch-t1-platform`.

## Current finite foundation handoff — 2026-09-09

This section supersedes historical ownership/status statements below. Latest user
scope is the coordinator's `2026-09-09-architecture-foundation-delivery.md`.
Source tip before this documentation commit: `032e0e8c5718e426cd5e1cb20e7c5e4af63e65ec`.
T1 freezes new source work and releases shared integration ownership to root.
There are no active T1 implementers, heavy processes or resource reservations.

Source-only schema packages: 0077 `b21f2166886ade7637b1b9594b5b28c1e38d3e80`,
0078 `eb079c6da56df5d90c3c6b7cbde86ab43ee87edd`, 0079
`5ec0746bce06989337803386806721931ed45c6f`, maintenance handoff
`5b8db89e35749086aa52cc65254625b7b3fe20a4`. Source declarations end at
`20260909_0079` → `20260909_0078`; no new Alembic execution is claimed.
0079 adds only inert metadata/storage and two provision/deprovision artifacts.
The approved audit amendment `619c0c5890e193bf663b1ab0b31b49daf4fae9d5` uses a
fixed derived helper, not nonexistent audit RLS or direct runner audit rights.
See `2026-09-09-t1-credential-maintenance-handoff.md` for the complete object/ACL
matrix. Maintenance store/registrar/CLI were not started and remain P1; SQL
registration alone is not independently verified mapping. Provisioning is not authorized.

All temporary shared-path delegations below have returned and are imported unchanged:

| Origin | T1 import | Bounded package |
| --- | --- | --- |
| ef027ea87fed51b4bfb80b1e40a95413c0cfbe77 | c74c4a58ec5fd6ed59d4ffe45945a5d57466ddaf | One pure maintenance metadata contract |
| 1e4ce7a4c1578f16fde7dca35f4eb770590d42ef | 23c54753e29aa614dc5076fb658961cca978bcad | Four-path Avito failure outputs |
| 35d302ef4b18204769abe15da15783c9b1f97884 | 88bc71412d99d055af25b56e29df1545ad4cf433 | Orders client diagnostic follow-up |
| 33227b10d89e1d71ba9b05a36324e39745ec8200 | 94a062eeaf6c99ef89f25311842f4e0a2936f5d1 | Overview typed/cache diagnostics |
| 7b32749ee39a26ffd824572a9f65bb95f652325c | 032e0e8c5718e426cd5e1cb20e7c5e4af63e65ec | Repricer error/cache diagnostics |

These are output hardening, not account cutover or whole-Avito safety. Existing
management `91f2456` and persisted readback `7f76425` contracts remain in this tree.
T3 separately returned release-gate three-path source
`bf6aa9a6d40a6956a3ac012c635e0eed0d0be3d0` directly to root; T1 did not import it.
Root must retain the union with its existing installed-wheel entry points.

**P0 residual:** integrate immutable domain packages; empty bootstrap/upgrade and
restricted-role isolation; changed credential authorization/readback and domain
transaction/CAS/replay/canary gates; installed-wheel entry points; consolidated
backend/frontend/typecheck/build with natural shutdown; independent final review
and demonstrated-defect fixes. None of these new-source gates ran in this
source-first continuation. Historical 105 failures and shutdown hang remain
unresolved evidence, not an expanded or accepted green baseline.

Operational cutover, plaintext retirement, real backfill/restore, key custody,
proof/TTL/drain/observation policy, unsupported product permissions and new live
heartbeat expansion remain P1. Production assembly/printing remains P2 deferred;
KIZ/standalone matcher remain excluded. New dormant flows were not activated;
older unrelated flags were not changed or asserted universally disabled. No
provider, production, real-credential, GitHub, push or deployment action occurred
in this continuation. This is IMPLEMENTED / UNVERIFIED source, not release or
architecture completion. The rest of this document is historical slice evidence.

This slice adds the authenticated account-owned API boundary over the existing
Wave 1 crypto, persistence, forced-RLS and credential-store contracts. It does
not switch a provider consumer, implement a legacy fallback, mount a keyring,
run a backfill, enable a flag or change deployment.

## Baseline and lineage

- Git started clean at the exact requested base.
- Alembic has one head: `20260908_0061`.
- The four Wave 1 contracts are present: marketplace credentials,
  `PriceApprovalSnapshot`, `ExternalOrderIdentity` and `ExternalReviewIdentity`.
- `20260717_0019` uses `ADD COLUMN IF NOT EXISTS`, validates the resulting
  schema, and does not remove the `ai_prompt` column on its own downgrade.
- The repository's release/contract entry points remain
  `backend/ops/release_gate.py`, `backend/contracts/openapi/v1.yaml` and
  `backend/product-docs/contracts/openapi/v1.yaml`. This slice does not
  regenerate either OpenAPI artifact because no frontend consumer is switched.
- The inherited full-backend baseline is `883 passed / 105 failed`; its exact
  failure IDs were unchanged from the preceding integration run. Those known
  failures and the post-summary shutdown hang remain release-infrastructure
  debt, not an allowlist to expand.
- On this slice, the final full JUnit contains 999 tests: `894 passed / 105
  failed / 0 errors / 0 skipped` in 297.468 seconds. The 105 failure IDs match
  `ops/legacy-test-failures.txt` exactly (`added=0`, `missing=0`). Pytest again
  hung in its known background-thread shutdown after writing the summary and
  JUnit; only that exact test process was stopped with SIGINT.

## Shared-file ownership manifest

| Surface | Owner for this architecture wave |
|---|---|
| `backend/alembic/**`, shared configuration, router/task registration, runtime grants, Docker/CI/dependencies, common handoff and final integration branch | Terminal 1 |
| `backend/app/repricer_tasks.py` and repricer domain implementation | Terminal 2 |
| `backend/app/avito/orders.py` and Avito order-domain implementation | Terminal 3 |
| `backend/app/review_tasks.py`, frontend and serverless | Terminal 4 |
| `backend/app/routers/avito_orders.py` during credential/token cutover | Terminal 1 |

Terminals 2–4 submit committed schema requests to Terminal 1. Each request must
state tables, columns/types, foreign keys, unique/check/index requirements, RLS,
backfill semantics and downgrade guards. Terminal 1 serializes migration
revisions; domain owners do not edit shared migrations concurrently.

2026-09-09 scoped delegation amendment, explicitly authorized by the integration
coordinator to unblock parallel source implementation (not Stage1 verification):

- T2 temporarily owned ONLY the paired-fetch core/dedicated executor callable in
  `backend/app/platform/integrations/credential_store.py` and new
  `2026-09-09-t1-executor-credential-resolver-handoff.md`. Delivered
  `00961055428365f83d7dd05bd07f46786709c975`, imported unchanged with provenance as
  `b70f2d2afdd545005b59b65d7a40c66595b4bfcd`; ownership returned T1.
- T4 temporarily owned ONLY the additive verified OAuth method in
  `backend/app/avito/auth.py`, new `backend/app/avito/credential_exchange.py` and
  new `2026-09-09-t4-avito-account-oauth-adapter-handoff.md`. Delivered
  `d1fd534f53b879f1e741d39b13c6ce76fdad5e2f`, imported unchanged with provenance as
  `564515291612520e3ba5d585e870a37355c90e0b`; ownership returned T1. Existing legacy
  methods and consumers are unchanged.
- T2 temporarily owned ONLY safe error/diagnostic fields in
  `backend/app/avito/chats.py`, `backend/app/avito/stats.py` and its dedicated
  `2026-09-09-t2-avito-safe-diagnostics-handoff.md`. Delivered
  `1213202030039af60aa63e111e2f1de43d9b7e06`, imported unchanged as
  `dad6b661efd5844f68d6085c93e6b2662f4fbc32`; ownership returned T1. Business
  payloads/pagination remain separate from redacted diagnostics; no blanket
  claim that every non-HTTP Avito action error is now covered.
- T2 temporarily owned ONLY the ordinary put/revoke/status/resolve transaction
  core in `backend/app/platform/integrations/credential_store.py` and
  `2026-09-09-t1-credential-transaction-core-handoff.md`. Source chain
  `874782ddeed534e4f4bfd9024037853fb5197ac4`,
  `e2d51a5c9482733eb4e50a0dd47addb010280440`,
  `fb2ae0ec686aa59a2c8e69dc3fdf96dca753ee42` imported unchanged respectively as
  `944c6ed31362471fd9c7b93adc09d8dc5bd3c36b`,
  `827620abaa5b83ad169391611db5684c277a1917`,
  `742c83f690272344427de7f02d2fcfca756e8689`; ownership returned T1. The final
  follow-up preserves public post-query expiry-clock ordering. Private helpers
  are transaction participants, not authentication; paired fetch/executor/rekey
  and existing public signatures were outside this delegation.
- T4 temporarily owned ONLY the existing canonical credential-management
  composition under committed plan `2026-09-09-credential-management-commit-fence.md`,
  with exact prerequisite `b21f2166886ade7637b1b9594b5b28c1e38d3e80`: new
  `backend/app/platform/integrations/credential_management.py`, fixed management
  root support only in `publication_guard.py`, only the three canonical exact-account
  credential routes/helpers/imports in `backend/app/routers/cabinet.py`, and new
  `2026-09-09-t1-credential-management-handoff.md`. Delivered
  `33032e9ab8ae74cdfc85934801a1b7517a3d54e4`, imported unchanged as
  `91f2456d832e2c9e6285a56b7b55afa7dffe4bd8`; ownership returned T1. Store, schema, grants, config,
  legacy routes and domain consumers are excluded. T1's daily stock/evidence
  schema package touches only a new migration, runtime grants and its own handoff.
  This temporary assignment implements the explicit independent-work redistribution
  request; it does not transfer standing shared-auth ownership or acceptance.
- T2's subsequent exact two-path insert/readback extraction in `credential_store.py`
  and the existing transaction-core handoff returned as
  `59bf12b3c5e028ab1262f19f3709a0a363f190b2`, imported unchanged as
  `7f76425a59f93cd5f39546a017400f8c8ec840ce`. Existing put consumes the private
  persisted-readback core now; no maintenance entrypoint or extra crypto was
  delegated. Ownership returned T1; public/private signatures remain stable.
- T4 now temporarily owns ONLY new
  `backend/app/security/credential_maintenance_contract.py`: strict immutable
  metadata/types from the approved maintenance spec, no SQL, I/O, secrets,
  operational proof or registrar. T1 will consume the actual returned contract.
- T3 now temporarily owns ONLY failure-output hardening in
  `backend/app/avito/returns_tasks.py` (no task registration/signature changes),
  the two raw-exception response spans in `backend/app/routers/avito_orders.py`,
  `_section_error` diagnostics in `backend/app/routers/avito_overview.py`, and new
  `2026-09-09-t3-avito-failure-surface-handoff.md`. No account cutover, domain rows,
  cache keys, rollout or permission changes. T1 freezes these four paths until
  returned; maintenance/schema, T2 daily participant and T4 types are disjoint.

All these source packages are IMPLEMENTED / UNVERIFIED under the user's source-first
sequence. No new tests/review/compile/import/PG gates, operational role/flag/key or
provider actions were performed for these imports. This does not broaden domain
owners' standing rights over store writers, schema, shared guard/config/grants,
routers, other Avito consumers or deployment. T1 retains serialized shared schema
and authority; the separate coordinator owns final integration/admission.

## Account-owned credential API

The registered path is:

```text
/api/v1/cabinet/marketplace-accounts/{marketplaceAccountId}/credentials/{provider}/{credentialKind}
```

- `GET` returns only `marketplaceAccountId`, `status` and lifecycle timestamps.
- `PUT` creates or replaces an encrypted credential and returns the same
  redacted status contract.
- `DELETE` revokes the active credential; `reasonCode` is constrained by the
  store allowlist.
- Every operation requires an authenticated actor, effective
  `integrations:write`, an active organization membership whose current role
  or explicit permissions still grant `integrations:write`, and access to the
  exact account. This closes the stale access-token downgrade window.
  Organization, account and canonical provider are checked in the database
  under tenant context.
- User-managed kinds are exactly `wb/wb_api` (`wbToken`) and
  `avito/avito_oauth_client` (`clientId`, `clientSecret`). The derived
  `avito_oauth_access` kind is not writable or observable through this API.
- WB writes call the existing seller-info identity verifier. The verified
  seller UUID must equal the canonical account external ID, and that ID is
  checked again under the account-row lock before persistence.
- Avito writes require the explicit path account. No account is inferred from
  a title, client ID, organization account count, insertion time or “latest”
  credential.
- New writes are encrypted-only. Legacy plaintext rows are not read, written,
  cleared or updated by these operations.
- Keyring readiness is checked before WB seller-info verification, so a
  disabled or misconfigured encrypted store cannot send an unusable credential
  to the provider.
- Active status is authenticated by a real decrypt. Corrupt ciphertext,
  expiry, a missing key/keyring or database failure returns a typed safe error;
  it never calls a legacy reader. Revoked rows remain redacted metadata.
- Pydantic secret types, root-level rejection of unknown credential-body keys
  and the global validation-error sanitizer keep request values, unknown keys
  and validator context out of 422 responses. Credential API responses exclude
  credential UUID and generation. Audit events use the credential UUID as
  `object_id` and include safe `generation` metadata alongside credential kind,
  account ID, operation, provider and result code. Neither surface contains
  secret payloads, ciphertext, nonce, verifier, raw exception text or key inventory.

The main safe API codes are `NO_ACCESS`, `MEMBERSHIP_INACTIVE`,
`ACCOUNT_SCOPE_DENIED`, `MARKETPLACE_ACCOUNT_NOT_FOUND`,
`MARKETPLACE_ACCOUNT_PROVIDER_MISMATCH`, `CREDENTIAL_KIND_UNSUPPORTED`,
`CREDENTIAL_PAYLOAD_INVALID`, `WB_CREDENTIAL_IDENTITY_INVALID`,
`WB_SELLER_IDENTITY_MISMATCH`, `credential_missing`, `credential_expired`,
`credential_auth_failed`, `credential_configuration_invalid`,
`credential_key_unavailable`, `credential_payload_invalid`,
`credential_contract_invalid`, `credential_account_not_found`,
`credential_account_identity_mismatch`, `credential_concurrent_update`,
`credential_reason_invalid` and `credential_persistence_failed`. Callers must
branch on the code and must not surface caught exception text.

## Resolver contract for Terminals 2–4

Import only the boundary types/functions:

```python
from app.platform.integrations.credential_store import (
    CredentialStoreError,
    MarketplaceAccountCredentialOwner,
    resolve_marketplace_credential,
)
from app.security.marketplace_credentials import CredentialCryptoError
```

Construct the owner from already-authorized canonical IDs:

```python
owner = MarketplaceAccountCredentialOwner(
    organization_id=organization_id,
    marketplace_account_id=marketplace_account_id,
    provider=provider,
)
credential = resolve_marketplace_credential(owner, credential_kind)
```

Kinds and decrypted payload shapes are fixed:

| Provider/kind | Payload fields | Intended owner |
|---|---|---|
| `wb/wb_api` | `token` | exact canonical WB account |
| `avito/avito_oauth_client` | `clientId`, `clientSecret` | exact canonical Avito account |
| `avito/avito_oauth_access` | `accessToken`, `expiresAt` | exact canonical Avito account; derived worker credential only |

Worker requirements for later consumer slices:

1. Celery arguments, retries and result values carry organization/account/job
   IDs only—never a credential, decrypted payload or bearer.
2. Establish tenant context and authorize the exact canonical account before
   resolution. Resolve immediately before the provider call.
3. Keep `DecryptedCredential` wrapped. Call `reveal()` only at the adapter
   boundary and do not attach its result to logs, errors, audit, caches, task
   metadata or API responses.
4. Treat all `CredentialStoreError` and `CredentialCryptoError` codes as
   fail-closed. Corrupt, expired or revoked credentials, storage errors and
   unavailable keys must not enter a plaintext/default/memory fallback.
5. Do not instantiate ORM credential rows in domain code and do not select a
   credential by user, organization-only query, title, account count, newest
   timestamp or `credential_ref` convention.

No explicit migration-mode fallback exists in this slice. A later compatible
legacy reader, if approved, must be centralized behind this boundary, run only
when no encrypted row exists, and never run after an encrypted-row error.

## Rollout state and blockers

- All existing WB/Avito consumers and legacy cabinet endpoints retain their
  prior behavior. No consumer uses the new writer or resolver yet.
- `VELLA_MARKETPLACE_CREDENTIALS_ENABLED` remains default-off. No key directory,
  key version or key material was added to Git or deployment configuration.
- No Alembic revision or runtime grant is needed for this API-only slice; the
  existing `0061` forced-RLS schema is unchanged.
- Before Stage 2, the infrastructure owner must approve production extension
  token TTL, abuse/rate limits, extension request-body limit and mixed-version
  Celery queue drain policy. No values are invented here.
- Before any rollout, the infrastructure owner must provide the external
  read-only key mount and minimum role placement described by the approved
  encryption design. API/required workers may receive it; beat/migrate must not.
- Backfill mapping evidence, old/new key overlap, production rotation, cleanup
  and all real provider canaries remain separate, explicitly authorized stages.

GitHub, production databases/Redis, real credentials and external review
services were not used. During an early regression command, an existing Avito
orders test unexpectedly attempted one read-only `accounts/self` request with a
synthetic bearer and received HTTP 403; it sent no message or price mutation.
Subsequent regression commands use a scrubbed environment, fake provider modes
and loopback-only HTTP proxies.
