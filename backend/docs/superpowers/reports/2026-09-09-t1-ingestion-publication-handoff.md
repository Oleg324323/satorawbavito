# T1 token-only ingestion boundary — IMPLEMENTED / UNVERIFIED

Source-first implementation, no activation. Schema commit
`d1eca023fbb0c921cc9979cf936c42e2536ccd6e`, revision `20260909_0076`, down
`20260909_0075`. Dispatch source was `cc8203530e0c363a80abacb2f3042600ab20e5c7`;
read-only revision/down_revision source graph and Git inventory showed sole0075
and no0076 collision before authoring. This is source observation, not Alembic or
PostgreSQL execution. Other controller commits on this branch are preserved.

## Physical schema and privileges

`marketplace_accounts.ingestion_binding_version`: BIGINT NOT NULL DEFAULT1,
positive check. Existing rows start1. BEFORE INSERT/UPDATE trigger
`ingestion_account_incarnation_guard()` rejects supplied non1 initial counters
and any changed caller counter, including OLD+1. Changes of Avito external ID,
credential ref, status, or transition to/from Avito advance OLD+1 automatically.
Text comparisons use C collation, nullable refs use IS DISTINCT FROM. BIGINT
exhaustion refuses the write. Other account fields do not advance the counter.

`marketplace_account_ingestion_tokens` adds:

| Column | Physical type | Meaning |
| --- | --- | --- |
| binding_schema_version | nullable SMALLINT | NULL legacy or1 |
| binding_external_account_id | nullable VARCHAR(128), C | exact issuance external ID |
| binding_credential_ref | nullable VARCHAR(255), C | exact nullable issuance ref |
| binding_version | nullable BIGINT | captured positive incarnation |

`ck_ingestion_binding_shape` admits exactly four NULLs for legacy, or schema1
with nonblank external ID, positive version and nullable/nonblank ref.
`ck_ingestion_binding_owner` limits both owner IDs to positive INT4 semantics
even though token storage columns are BIGINT. `ck_ingestion_binding_times`
requires finite issuance/expiry within Python datetime years1..9999; nullable
revoke/use timestamps must be finite, >=issuance and before year10000. Existing
expiry, verifier32-byte, provider, scope, revocation and owner/provider FK checks
remain. No old token binding is backfilled.

`ingestion_token_binding_guard()` freezes token ID, org/account/provider/scope,
verifier, issued_at, expires_at and all binding fields. Revoked rows cannot be
unrevoked or have their revocation fields rewritten. last_used_at cannot be
cleared or moved backwards. Bound insertion locks the RLS-visible exact Avito
account, requires connected/exact external/ref/version and future DB-clock
expiry; new bound rows start without revoke/use timestamps. Deleting bound token
history is refused. Legacy all-NULL insertion remains physically possible for
mixed-version compatibility; new supported issuance always binds it. Neither
the token-only factory nor standalone verifier accepts an unbound row.

Both new functions are SECURITY INVOKER, fixed search_path, metadata-only and
PUBLIC EXECUTE is revoked. Runtime SQL additionally revokes direct EXECUTE from
the configured runtime role. Triggers execute automatically; no credential,
private-source or cross-tenant maintenance helper was introduced. Existing
forced tenant RLS, table CRUD and default privileges are preserved. In
particular existing table UPDATE already covers additive columns: column REVOKE
would not narrow it. Actual counter/binding immutability comes from triggers,
not an inaccurate claim of column-only privileges. ORM bootstrap is not an
equivalent of physical migration/RLS/trigger enforcement.

Downgrade takes exclusive table locks, uses row_security=off so hidden history
cannot silently be missed, and refuses any bound token or account counter !=1.
Only a privileged migration owner that can see all history may downgrade. No
forced unbinding, deletion, reissue, cleanup or revival is performed.

## Exact token and root interfaces

`ingestion_tokens.IngestionAccountBinding` is a frozen, repr-hidden dataclass:
`marketplace_account_id:int, provider:str, external_account_id:str,
credential_ref:str|None, binding_version:int, binding_schema_version:int=1`.
`VerifiedIngestionToken` now also carries `account_binding:IngestionAccountBinding`
besides `token_id:UUID, owner:MarketplaceAccountCredentialOwner, scope:str,
expires_at:datetime`. It is metadata, never independent publication authority.

One private verifier:
`_verify_ingestion_token_in_session(session, *, raw_bearer)->VerifiedIngestionToken`.
It uses self-locating sat1 org/token solely as an untrusted tenant-RLS locator;
no cross-org scan. It locks the account before the token, uses
`hmac.compare_digest` on the SHA256 verifier, then checks exact owner/binding,
connected status, revocation and fresh `clock_timestamp()` after lock waits.
No verifier-bearing ORM token row enters its identity map. Transient bearer,
secret and verifier locals are cleared in its finally block. No bearer/verifier
is placed in guard, Session.info, errors, audit or logs. Python/framework memory
is not claimed to be cryptographically zeroized.

New factory, no caller owner/permission:
`acquire_ingestion_publication_guard(session, *, raw_bearer)->IngestionPublicationGuard`.
Caller must have begun a fresh clean Engine-bound PostgreSQL READ COMMITTED
Session root, no joined Connection, autocommit or savepoint. Verification occurs
inside that root. Verified ownership precedes exact marketplace-account GUC and
Session marker setup. Successful admission updates last_used_at in that root.

Guard safe public interface:

- `owner -> MarketplaceAccountCredentialOwner` (positive org/account, avito).
- `account_binding -> ExpectedAccountBinding` (actual existing T1 class).
- `binding_version -> int`, `token_id -> UUID`, `expires_at -> datetime`.
- `revalidate_before_write() -> datetime` (fresh DB timestamp).
- `require_participation(session) -> self` (same Session/logical/physical root;
  revalidates and poisons on mismatch/failure).

No UserSessionPrincipal is constructed for bearer ingestion. The shared guard
state slot and existing final before_commit listener are reused. Final listener
must be last; it flushes, rejects requeued writes, revalidates token/account/time,
then preserves existing exact Orders/Repricer/Review fences. Nested, foreign,
ended, competing or failed roots fail closed. Physical transaction identity is
captured as well as logical Session transaction identity. Existing user guard
now additionally checks bound-token incarnation against the locked account.
Its all-NULL legacy ExpectedIngestionToken path remains only for an independently
authenticated pre-rollout user publication; it is not a token-only bypass.

T3 ruling: no absent-module imports or speculative sys.modules registry. Current
T3 has no additional domain-fence class to register. Token guard independently
owns all final token/account/root validation. Actual domain invariants are owned
by T3's trusted participant and physical constraints. If T3 later supplies an
additional final invariant/class, any registration is a separate exact extension.

Private lifecycle participants are `_issue_ingestion_token_in_session(session,
owner, *, expires_at, actor_user_id=None)`, `_revoke_ingestion_tokens_in_session(
session,owner,reason_code,*,actor_user_id=None)` and
`_get_ingestion_token_status_in_session(session,owner)`. They do not commit or
authenticate. Existing public store functions retain signatures and compose
these cores; they are trusted storage entry points, not new public routes.
New issue always captures binding. SHA256 verifier/sat1/32 random bytes and
one-time `IssuedIngestionToken.reveal()` are preserved. No status or audit contains
a bearer mask/prefix. Failed/uncertain issuance never reveals its wrapper.

## Explicit policy and limit interface

Optional nonsecret config `Settings.ingestion_policy_config` reads
`VELLA_INGESTION_POLICY`; default None. Global Settings does not parse it.
`load_ingestion_policy(raw, *, decoder_max_body_bytes)->IngestionPolicy` is an
explicit ingestion bootstrap operation. All ten fields are required, with no
unknown/duplicate fields:

`approval_reference, approval_version, max_token_lifetime_seconds, max_body_bytes,
ip_bucket_capacity, ip_bucket_window_seconds, token_bucket_capacity,
token_bucket_window_seconds, peer_policy_reference, peer_policy_version`.

References are nonempty bounded ASCII identifiers (128 chars); every numeric
field is a strict positive INT4, not bool/float/string. `require_decoder_limit`
rejects body policy above the supplied trusted decoder ceiling. The4096-char
config parser ceiling and4096-byte issuance JSON parser ceiling are technical
limits, not operational defaults. T3's1048576-byte decoder ceiling is also not
an approved quota. No TTL, rate, proxy trust or enablement approval is inferred.

Malformed/missing policy is denied by the affected issue/ingest path. Bootstrap
may catch `ingestion_policy_unavailable` and retain policy=None to mount a disabled
router without affecting unrelated startup. Status/revoke remain authenticated
and usable without issuance/ingest policy. No provider keyring is loaded.

`IngestionLimits(*,redis_client,policy)` requires an injected async Redis client.
`check_peer(trusted_peer_ip)` canonicalizes a numeric IP, folds IPv4-mapped IPv6,
then SHA256 hashes packed bytes. `check_verified_token(VerifiedIngestionToken)`
is called only after verification. Fixed namespace `satorna:ingestion:v1:` plus
`ip:`/`token:` and64hex digest; raw IP/bearer are never cache keys/audit/log data.
Single Lua script atomically reads bounded count/TTL, increments and sets expiry
for the first hit. Invalid counter/TTL, Redis failure or missing adapter denies;
no process-memory/plaintext fallback or unverified-token-key allocation.

`TrustedIngestionPeerResolver(policy_reference,policy_version,resolve)` must be
injected by server bootstrap, matching the policy reference/version. Its resolver
returns the numeric peer from the explicitly approved direct-peer/proxy path.
T1 reads no forwarded header and supplies no permissive proxy default. A callback
alone is not evidence that proxy trust was operationally approved.

## Authenticated service and HTTP integration

`AuthenticatedIngestionTokens(*,session_factory,policy,decoder_max_body_bytes)`:

- `issue(*,authenticated_actor,marketplace_account_id,lifetime_seconds)` returns
  IssuedIngestionToken only after successful physical commit.
- `status(*,authenticated_actor,marketplace_account_id)` returns safe metadata|None.
- `revoke(*,authenticated_actor,marketplace_account_id)` revokes current rows with
  fixed operator_revoked and returns safe metadata tuple.
- `verify_for_admission(*,raw_bearer)` returns safe verified metadata from a short
  committed root, released before Redis/decoder. This is not sink authorization.

All lifecycle methods require actual ActorContext, current real user/login/member,
connected exact Avito account and fixed integrations:write under the live existing
user guard. Issuance lifetime is explicit and <=approved maximum; token lifetime
is distinct from login lifetime. A newly issued token is also registered in that
same user guard's ExpectedIngestionToken set, so final expiry/incarnation checks
apply before issue commit. No logout-based token revocation policy is invented.

Factory:
`create_ingestion_router(*,token_service,limits,trusted_peer,decoder,
decoder_max_body_bytes,sink)->APIRouter`. It supplies four dormant relative routes:

- POST `/ingestion/avito/browser-snapshots`.
- POST `/accounts/{marketplace_account_id}/ingestion-token`, exact JSON object
  `{"lifetime_seconds": <positive integer>}`; one-time `{token,metadata}` response.
- GET same token path -> `{metadata: object|null}`.
- DELETE same token path -> `{revoked: metadata[]}`.

Existing avito_orders router/main registration are untouched. No fallback sink or
decoder. Authorization header is a single bounded `Bearer <sat1...>` value. Peer
limiting precedes token DB lookup. Streaming cumulative byte enforcement precedes
JSON decoding, checks Content-Length consistency and refuses any Content-Encoding.
Only explicit application/json (optionally charset=utf-8) is accepted. Manual
closed input parsing avoids Pydantic input reflection; unknown codec failures map
to fixed body-invalid. Application code emits no body/header/trace payload logs.
Transient request bearer/body/decoded references are dropped in finally. All
responses are no-store. Existing framework/proxy logging must retain that policy.

## Exact T3/T4 callback contracts

Actual source read: T3 `4e010e216d1d9b875e563d6cccce96b20215a04b`
`app.orders.browser_envelope.decode_avito_browser_envelope` and envelope handoff;
actual Orders publication source read by git show, not imported here. Root reports
new T3 `87359150a9045acd0e6d4b3c83a354a8ccaa57c2` transaction-local
`_persist_orders_manifest` extraction as the domain sink composition basis.

Decoder contract matches the actual T3 function:
`decoder(payload:bytes, *, organization_id:int,marketplace_account_id:int,
observed_at:datetime)->DecodedBrowserEnvelope`. Router calls it itself using
verified owner and separately acquired server UTC receipt time. Client fields
cannot select owner/receipt/complete; actual strict decoder produces only partial,
nonterminal manifests and rejects unknown/duplicate fields. T1 copies no codec.

Sync durable sink contract:
`sink(*,raw_bearer:str,admission:VerifiedIngestionToken,envelope:object,
observed_at:datetime)->CommittedIngestionAcknowledgement`.
Sink must open its own fresh physical root, call the real token factory there,
compare admission owner/token/expiry/full binding(version included) and decoded
owner against the newly verified guard, then require_participation(session), run
its actual domain participant and commit. It must reject substituted owner or
binding and own exact body replay/idempotency conflict. No network, second session
or provider/AI enrichment while root locks are held. It returns only after commit.

`CommittedIngestionAcknowledgement(run_id:int,state:str,replayed:bool)` accepts
positive BIGINT run_id, exactly `partial` and strict bool. It is only a bounded
response DTO, not a commit certificate. The trusted actual sink owns the physical
commit proof; router refuses unknown result types and never returns uploaded
evidence. T3 may raise `IngestionAPIError('ingestion_conflict')` for a proven
idempotency/body conflict; other exceptions get fixed safe mapping.

The external HTTP acknowledgement is exactly
`{"run_id":"<canonical positive base10 decimal>","state":"partial","replayed":false}`
(replayed is a boolean and may be true). Wire run_id is a STRING without leading
zeros, preserving the entire positive BIGINT range through browser JSON.parse.
The internal callback DTO and database run_id remain integers with their existing
bounds. State/replayed fields and no-store/HTTP behavior are unchanged.

Public HTTP codes: access_denied403, token_invalid401, body_invalid400,
body_too_large413, encoding_unsupported415, conflict409, rate_limited429;
policy_unavailable/peer_unavailable/limits_unavailable/unavailable503, and
issue_outcome_unknown503. Each carries the `ingestion_` prefix. Unknown commit
does not reveal/retry issue: authenticated status/revoke and a separately chosen
reissue workflow are required. Existing fixed publication/token-store codes remain
internal; no raw driver/codec details are returned.

## Pending centralized gates and owner inputs

NO tests authored/run, RED, import, compilation, lint, review/critic, Alembic,
PostgreSQL or Redis execution during this package, per binding source-first order.
No final acceptance claim for0074/0075/0076 or shared authority source.

Central gates retained: dual-org/account/auth matrix; missing/wrong/live/revoked
login/member and readonly scope; ACK run_id values2**53-1,2**53+1,2**63-1 must
round-trip through normal browser JSON.parse with exact decimal string text;
malformed/unknown/revoked/expired/unbound bearer;
actual constant-time compare and secret canaries; A→B→A and disconnect/reconnect;
lock/flush/commit wait expiry/revoke and competing/nested/foreign/listener bypass;
one-time reveal/uncertain commit; streaming/gzip/content-length/JSON limits;
forged token key-growth and actual isolated Redis failure; strict partial envelope
and exact body replay; real T3 sink root atomicity/rollback; empty/populated additive
migration, SQL invariants/ACLs and downgrade refusal; old user guard compatibility.

Still required: approved TTL/abuse/body/proxy policy, mixed-version retirement and
revoke/reissue window, T3 durable sink and root integration, T4 new producer with
actual observed IDs/occurrences. No legacy hash copy/account inference, operational
token/key rotation, roles, flags, production/provider/network services, push/deploy,
returns-sync/settings/history side effects or registration were performed.
