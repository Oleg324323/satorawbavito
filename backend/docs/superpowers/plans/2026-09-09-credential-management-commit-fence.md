# Existing credential API: live authorization through commit

Bounded hardening of approved credential Stage1, not a new provider consumer or
permission policy. Actual cabinet canonical status/put/delete currently check
membership in one Session and invoke a public store opening another transaction.
Current put/revoke/status store accepts disconnected accounts; MarketplaceAccount
defaults disconnected. Generic fetch PublicationGuard requires connected account.
Blindly reusing it would break credential setup and disconnected revocation.

Actual transaction primitives are in944c6ed31362471fd9c7b93adc09d8dc5bd3c36b.
Normal resolve in-session core is actual827620abaa5b83ad169391611db5684c277a1917
plus742c83f690272344427de7f02d2fcfca756e8689 (T2e2d51a5+fb2ae0e unchanged imports).
`_resolve_marketplace_credential_in_session(session, owner, kind, *, keyring, now)`
returns only existing DecryptedCredential. Public old resolver retains its
post-query clock sampling through shared private lookup/decrypt core. These are
implemented source, not verified runtime. No imagined helper or ORM decryption.
Source-first: no tests/test authoring/import/compile/lint/review/PG/Redis now;
central final acceptance deferred, not waived. No production/providers/network,
real credentials/.env/flags/roles/deploy/push or legacy writer cutover.

## Task 1: Fence the existing exact-account credential management operations

Read actual cabinet canonical three routes/helpers, access.py, ActorContext and
live publication guard/IAM/session source, exact store primitives and crypto
contract. Read latest shared guard after ingestion changes, not an old copy.

Exact permitted paths:
- New backend/app/platform/integrations/credential_management.py.
- backend/app/platform/integrations/publication_guard.py only narrow fixed
  credential-management root installation/reuse if genuinely necessary; all
  existing user/fetch/Orders/Repricer/Review/ingestion contracts remain strict.
- backend/app/routers/cabinet.py only canonical exact-account credential routes,
  associated imports/helpers; legacy cabinet/WB/Avito plaintext routes untouched.
- New backend/docs/superpowers/reports/2026-09-09-t1-credential-management-handoff.md.

No schema/grants/config/dependencies/profiles/permissions/store/domain/test edits.
If actual store helper is insufficient, report exact missing operation rather
than decrypt ORM or duplicate crypto outside the unique store boundary.

New module owns fixed credential management operations, not a generic callback
or permission registry. Explicit trusted session factory/keyring loader and WB
seller verifier injection, no import-time connection/key/provider activity.
Only user-managed wb/wb_api and avito/avito_oauth_client kinds, preserving existing
route schemas, payload validation and safe response status shape. No user-supplied
Avito access token, OAuth exchange, inferred account mapping or generation refresh.

For every root derive real membership from authenticated ActorContext/current DB,
then lock user -> membership -> actual login -> exact org/account/provider ->
credential rows. Fixed integrations:write and current allowed-account scope,
active user/member, real unrevoked/unexpired current login, no auth from actor ID.
Use a clean Engine-bound PostgreSQL READ COMMITTED physical root, no joined
Connection/savepoint/autocommit/pending ORM or callback-owned commit. Reuse strict
final listener/flush/poison/root invariants; do not copy a weakened guard loop.

Management binding captures exact account external ID/ref/status as actual data,
not a required connected predicate. Any unchanged existing account lifecycle
state remains eligible for management as before; no connect/reset/reactivate or
new status policy. Final exact binding and current auth rechecked after flush.
This exception exists ONLY in fixed management guard; public fetch/provider and
ingestion guards still require connected. No current credential needed to revoke
or set up credentials, no keyring needed merely to revoke/missing/revoked status.

Put sequence: strict existing payload validation; short fresh authorization and
account snapshot root, physically finish before provider I/O. Keyring readiness
must fail before WB seller verification. Preserve real existing WB seller ID
verification and exact expected external ID; no validator exception swallowed or
name/count mapping. All provider I/O outside locks. Then own new fresh guarded
root rechecks SAME caller/account snapshot after any wait and calls actual store
put primitive with newly available trusted keyring and DB clock. No nested public
store call/transaction or alternative writer. Credential row, replacement metadata
and safe audit commit together; no success before physical commit.

Revoke uses same live management root + actual revoke primitive + DB clock,
preserving existing allowlisted reasons/missing error. Disconnected account must
remain revocable; revocation never falls back to legacy or needs decryption.

Status uses one root for account auth, latest safe metadata and, when current
status is active, actual normal in-session resolver to verify decryptability.
No connected-only paired fetch resolver, ORM ciphertext outside store, process
cache, guessed status or fallback on corrupt/expired/revoked/unavailable storage.
Preserve current metadata/status view fields; no key inventory/nonce/verifier/raw
secret exposed. Live auth and physical root checked before result leaves service.

Map all SQL/crypto/auth/persistence errors to bounded existing safe codes or a
small new fixed management code allowlist. Never expose str(exc), DB DETAIL,
parameters/key paths/body or chained raw errors. Unknown commit/cleanup outcome
must return explicit safe uncertain outcome/readback requirement, not retry a
write or claim rollback. Status is the existing safe reconciliation read. No
operation promises exactly-once replacement without an existing request ID.
Foreign/rejected sessions are not closed or rolled back; always clean own roots.

Route wiring replaces only separate-check/public-store composition for these
three existing canonical routes, retains response contract and readiness order.
Do not remove existing legacy routes or switch other consumers. Existing disabled
keyring behavior remains fail-closed; no new enabled flag/default permission.

One bounded source commit: fix: fence credential management through physical commit.
Handoff exact imports/signatures/response/error mapping/root lifecycle and pending
tests. IMPLEMENTED / UNVERIFIED only, no READY or operational security proof.

Final centralized cases: revoked/inactive user/member/login and permission/scope
changes while waiting; same-org wrong account/provider; disconnected setup/revoke;
account binding change around WB verification; invalid WB seller/keyring before
provider; active-corrupt status denial with no legacy/fetch path; same-root row+
audit rollback; replacement generation races; final flush/poison/ended/foreign
root sabotage; unknown commit/cleanup readback; synthetic canaries/errors; old API
response/validation/disabled-keyring and legacy-reader compatibility.
