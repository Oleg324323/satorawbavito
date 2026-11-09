# Exact-binding ingestion token and commit boundary

Binding requirements: original encryption design Task6, existing ingestion-token
store, T3 committed4e010e216d1d9b875e563d6cccce96b20215a04b browser envelope/decoder
and backend/docs/superpowers/reports/2026-09-09-orders-browser-envelope-contract.md.
This plan supplies T1 auth/lifecycle/HTTP boundaries, not a fake browser producer
or copied Orders parser/sink. No UserSessionPrincipal fabricated for a bearer.

## Global constraints

T1 worktree/source only, no production/providers/working DB/Redis/real credentials/
.env/operational flags/key or token rotation/push/deploy. Code first, final tests
after package. No intermediate tests/review or legacy migration edits. Existing
plaintext/org-wide token retirement and real producer rollout need separate explicit
policy; no auto-copy of old token hashes or inferred account mapping.

## Design decisions

Current VerifiedIngestionToken captures account ID but no external binding. Add
immutable issuance external account/ref and a dedicated account ingestion binding
version. Snapshot strings alone permit disconnect/reconnect or A→B→A revival.
An account BEFORE UPDATE trigger increments a positive BIGINT version whenever
Avito external account/ref/status changes, rejects direct counter manipulation,
and fails on overflow rather than wrapping. This is internal authorization metadata,
not a provider revision or replacement for historical Orders binding descriptors.
Legacy accounts begin at1; existing tokens remain explicitly unbound and cannot be
used by the new token-only publication boundary. No historical token binding is
backfilled. Existing other account fields/consumers stay unchanged.

Token verification and publication share one private in-session verifier, not a
second bearer scheme. Exact self-locating org/token lookup under existing forced
org RLS, then canonical account lock before token lock; fresh recheck and constant
time verifier comparison. No privileged cross-org scan. Bound tokens compare
issuance external/ref/version against actual current account; expiry/revoke/status/
generation fail closed. Uniform safe errors contain no raw input or inventory.

Token-only guard uses the existing physical-root/final-listener machinery of
publication_guard, with a real separate token principal and token validation,
never a UserSessionPrincipal placeholder. User-session acquire function retains
its strict principal contract. A shared internal installation helper may be factored
to avoid copying final-flush/nested/autocommit/poison/listener checks. User and token
guards cannot independently install competing authorization roots in one Session.
Both support the same trusted domain transaction fence after final auth validation.

The bearer authorizes only `avito.browser_snapshot.write` for its stored exact
account. It grants no settings/returns-sync/credential/history read permission.
Authenticated issue/revoke/status require fresh membership + integrations:write
and exact account scope; token expiry policy is distinct from issuing login lifetime,
and no new logout-based bearer expiry rule is invented. Explicit TTL policy required.

## Task 1: Implement complete T1 token boundary

Read the requirements/design above, actual ingestion_tokens.py, publication_guard.py,
credential_store.py, relevant account/token ORM, cabinet/access/auth API and the exact
T3 decoder/contract via git show. Do not import absent T3 modules into T1 main or
rewrite its decoder. T3 owns the token-only durable sink once guard source exists.

### Exact paths

- New additive migration under backend/alembic/versions named for actual next free
  revision and ingestion_token_binding; stop on head/collision mismatch.
- Modify backend/app/platform/integrations/orm.py (internal account/token columns).
- Modify backend/app/platform/integrations/ingestion_tokens.py (shared verifier,
  bound issuance and existing lifecycle internals, safe metadata only).
- Modify backend/app/platform/integrations/publication_guard.py (only shared
  installation/root infrastructure needed by token guard, plus validation of new
  bound-token incarnation fields when used via existing ExpectedIngestionToken;
  no user permission/profile changes or fake principal).
- Create backend/app/platform/integrations/ingestion_publication_guard.py.
- Create backend/app/platform/integrations/ingestion_api.py.
- Create backend/app/platform/integrations/ingestion_limits.py.
- Modify backend/app/config.py (optional explicit nonsecret policy configuration,
  default disabled/missing, never production TTL/rate/body defaults).
- Modify backend/ops/runtime-db-role.sql (new-column/helper rights only).
- Create backend/docs/superpowers/reports/2026-09-09-t1-ingestion-publication-handoff.md.

Do not edit current avito_orders router or main registration in this package:
that cutover requires domain sink/producer and rollout policy. Supply a real
router factory for explicit root integration, not an import-error fallback.
No tests or other paths during implementation; final obligations below retained.

### Implementation requirements

1. Add account ingestion_binding_version and immutable token binding schema1,
   external_account_id/ref and captured binding version, exact C equality, positive
   INT4 owner semantics, finite timestamps. Full NULL legacy binding is distinct
   from schema1 required external/version and nullable ref. New supported issuance
   always bound. SQL identity/binding/verifier/expiry immutable, revoke-only fields
   and last_used_at retain lifecycle semantics; no un-revoke or expiry extension.
   Account trigger cannot be bypassed by setting the next version manually. No
   private source/credential read or cross-tenant revocation trigger is introduced.
2. Factor in-session issue/revoke/verify core from existing store so authenticated
   wrappers can own a single physical guarded root, without nested public calls.
   Preserve one-time secret wrapper and existing entropy/self-locating format;
   no token mask/prefix in status/API/audit. Legacy unbound token use explicitly
   denied by new boundary, not silently rebound to the latest account metadata.
3. Implement `acquire_ingestion_publication_guard(session, *, raw_bearer)` returning
   a root-bound `IngestionPublicationGuard` with safe `owner`, `account_binding`
   and `token_id`/expiry metadata, plus `revalidate_before_write`. It verifies raw
   bearer inside that same root; takes no caller-provided org/account/permission.
   Never retain raw bearer or verifier in returned object, error or Session info.
   Set trusted account context only after verified ownership. After flush and
   domain locks, revalidate exact token, binding version, connected status, revocation
   and DB clock; poison failed/nested/foreign/ended root. Compatible trusted final
   transaction-fence registration, no network or second session while holding locks.
   Existing user-session + ExpectedIngestionToken validation must also reject a
   NEW bound token when its issuance incarnation differs from the locked account;
   it cannot bypass the new binding by using the older guard entry point. Preserve
   an explicitly documented legacy user-authenticated path only outside the new
   token-only rollout; never allow an unbound row through the token-only factory.
   Existing table-level UPDATE grants may cover additive columns; column REVOKE
   alone cannot narrow those privileges. Enforce counter/binding immutability with
   triggers and disclose actual privileges, without unrelated global ACL reform.
4. Authenticated issue/status/revoke wrappers construct live principal from real
   ActorContext + current membership and account, fixed integrations:write. Live
   guard before mutations and final commit; disconnected/rebound/wrong account/
   read-only/revoked membership fail. Show raw bearer exactly once after successful
   commit, no retry/read endpoint can recover it. Missing explicit operational
   policy denies issue, not arbitrary default TTL. Do not load provider keyring.
5. Add strict explicit policy object: approved reference/version, max token lifetime,
   max body bytes, unauthenticated IP bucket capacity/window, verified-token bucket
   capacity/window. No role/rate/duration defaults or implied approval from an enabled
   boolean. Oversized, malformed, missing or mutually incomplete config fails closed.
   Domain decoder's1,048,576 byte transport ceiling is not an operational policy;
   effective body limit cannot exceed the supplied trusted decoder maximum.
6. Limits adapter uses atomic Redis increments/expiry in a single script and fixed
   bounded namespace; no plaintext/memory fallback on Redis failure. Pre-lookup IP
   bucket uses trusted server peer identity, not arbitrary forwarded header without
   proxy trust policy. Do not allocate a key from unverified token IDs. Per-token
   key is created only after successful verification. No raw bearer/IP persisted
   in audit/logs; bounded canonical hash is cache key only, not authorization.
   Redis client injected from trusted bootstrap; no connection or script execution
   during import/implementation. No live services are called by this task.
7. Router factory receives explicit trusted decoder and durable sink; there is
   no fallback handler. Streaming body-size enforcement precedes JSON parsing,
   rejects request Content-Encoding unless a future bounded decoder is approved,
   ignores no account override (strict domain decoder rejects extra owner fields).
   Token auth is revalidated in sink's commit root. No provider/AI enrichment,
   legacy caches or returns sync side effect. Fixed safe errors, no Pydantic input
   reflection, header/body/trace payload logs. Body parse/unknown codec errors map
   to fixed codes only. Clear transient raw body/bearer references when request ends.
8. Return only bounded safe acknowledgement from the sink's committed result,
   not raw uploaded evidence. Exact request-idempotency/body conflict belongs to
   T3 durable sink; adapter must call real decoder itself with trusted owner and
   independently acquired server receipt time. Client cannot select complete.
   Publish precise factory/decoder/sink callback contracts for T3/T4/root wiring.
9. Downgrade refuses bound token history/counter-dependent use before removing new
   fields/triggers; no forced unbinding/revival or destructive cleanup. Legacy
   account/token rows and other domains preserved. New grants never allow caller
   counter/binding rewrite; parent trigger execute not public.

### Delivery and final verification

Commit schema/model/grants as `feat: bind ingestion tokens to account incarnation`;
guard/lifecycle/config/limits/API/handoff as `feat: add token-only ingestion boundary`.
Mark IMPLEMENTED / UNVERIFIED until final actual disposable PostgreSQL/isolated
Redis/API tests and T3 sink/T4 producer/root integration. No activation in this task.

Final tests: complete dual-account/org/auth matrix, bearer malformed/unknown/revoked/
expiry, actual constant-time verifier call, A→B→A and disconnect/reconnect denial,
claim/flush/commit wait expiry/revoke, no forged UserSessionPrincipal, no competing
guard/listener bypass, one-time reveal and secret canaries, body-stream/gzip limits,
prelookup forged token key growth, Redis failure denial, no returns/settings scope,
strict partial-only wire and exact body replay, real sink root atomicity and rollback,
empty/populated additive migration/downgrade refusal and old guard compatibility.
Operational TTL/abuse/proxy trust/mixed-version revoke-reissue window and a producer
with sufficient observed IDs/occurrences remain explicit owner/dependency inputs.
