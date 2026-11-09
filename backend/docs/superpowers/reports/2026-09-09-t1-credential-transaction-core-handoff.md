# Credential put/revoke/status transaction core — T1 source handoff

**IMPLEMENTED / UNVERIFIED**. Scoped delegation changes only `credential_store.py`
put/revoke/metadata operations and this report. No tests/import/compile/review/PG/
Redis/network gates or provider calls run. No access/router/config/guard/schema,
paired fetch/executor resolver, reencrypt or maintenance edits.

## Exact private interfaces in the existing store

### Persisted INSERT verification follow-up

Additional store-only private primitive, consumed immediately by the existing put
participant (not a new maintenance implementation):

```python
_insert_verified_credential_in_session(
    session: Session, identity: CredentialIdentity, plaintext: Mapping[str, Any],
    *, keyring: CredentialKeyring, now: datetime,
) -> MarketplaceAccountCredentialRow  # store-internal only
```

Existing put still selects/locks account and active row, revokes replacement,
chooses its own UUID/next generation and creates the full identity. It delegates
only encryption/INSERT/readback, then audits exactly once after verification. The
primitive receives explicit exact identity/keyring and aware time, validates typed
input, reuses the existing encrypt/decrypt equality check BEFORE INSERT, flushes,
expires the ORM row and runs an exact frozen-owner/ID SELECT with populate_existing
in the same Session/physical root. It does not trust the identity map or values
read from the pre-expiry object to form the lookup.

Persisted identity/envelope are reconstructed with existing `_identity/_encrypted`;
raw integer types are checked before normalization, exact full identity and exact
encrypted envelope must equal requested/generated values, revoked metadata must
remain null and creation/update timestamps must match. Actual persisted envelope
is then decrypted and its full typed plaintext compared in memory before audit.
Missing/reassigned/corrupted row or mismatched identity/envelope/payload fails closed
with fixed credential_auth_failed (or existing typed crypto/contract/SQL mapping).
Root/Connection identity remains checked across flush/readback/decrypt. PG requires
the existing physical non-autocommit READ COMMITTED checks; SQLite public-path
compatibility retains an active same-root check without PG-only validation.

No new crypto, session/key/clock/account selection, commit/rollback, audit, retry,
maintenance SQL/view/CLI or exported ciphertext/ORM response exists in this helper.
Returning the verified row is allowed only inside store; existing put still emits
safe metadata. Caller retains live authority, whole-root rollback and finalcommit.
The persisted comparison adds the intended corruption-denial behavior but does not
implement maintenance history admission or generation1 policy. Normal/public
signatures, ordinary post-query expiry clock, paired/executor and rekey unchanged.

Source IMPLEMENTED/UNVERIFIED; no tests/test authoring/review/import/compile/lint/
PG/Redis/network run. Final obligations: DB-trigger corruption of every identity/
nonce/AAD/key/schema/ciphertext/timestamp field, true requery despite identity map,
same-root sabotage, audit/commit failure rollback, no premature audit, strict typed
plaintext/canary failures, public compatibility and maintenance-owner composition.

Follow-up adds the real status-route active-credential verification dependency:

```python
_resolve_marketplace_credential_in_session(
    session: Session, account_identity: MarketplaceAccountCredentialOwner,
    kind: str, *, keyring: CredentialKeyring, now: datetime,
) -> DecryptedCredential
```

This is the ordinary active-row resolver, not the connected-only paired fetch
resolver. It uses existing account/active-row queries, missing/expiry checks and
one existing crypto decrypt implementation. No ORM row escapes; corrupt ciphertext
cannot become a successful status. Caller must not reveal the returned wrapper in
a status response. Exact keyring/owner class and aware time are required; it neither
loads keys/time nor creates/commits/rolls back a session. Existing public
`resolve_marketplace_credential(owner,kind)` keeps its own configured keyring,
short session, UTC clock and typed error mapping. A subsequent source correction
to `e2d51a5` restores exact legacy timing: public lookup runs BEFORE `_utc_now()`.
Two store-internal shared pieces avoid duplicate queries/crypto:
`_active_credential_for_resolution(session,owner,kind)` performs the existing scoped
account/active lookup and returns an internal row only within store;
`_decrypt_active_credential(row,*,keyring,now)` performs existing expiry/decrypt.
Public wrapper runs lookup→clock→decrypt; explicit-now in-session helper runs
the same lookup→decrypt with caller time. No callback/flag/default clock, second
query or second decrypt is introduced. Final authority remains the admin caller's
responsibility. Test expiry at supplied instant and public delayed-read ordering
in the final gate; no freshness across provider I/O is implied. The intermediate
e2d51a5 timing caveat is addressed in source, not independently verified.
Paired fetch/executor and reencrypt are untouched.
Follow-up source is also UNVERIFIED, with no tests/import/compile/review/PG run.

```python
_put_marketplace_credential_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    plaintext: Mapping[str, Any],
    *, keyring: CredentialKeyring, now: datetime,
    actor_user_id: str | None = None,
    expected_external_account_id: str | None = None,
) -> CredentialMetadata

_revoke_marketplace_credential_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str, reason_code: str,
    *, now: datetime, actor_user_id: str | None = None,
) -> CredentialMetadata

_get_marketplace_credential_metadata_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> CredentialMetadata | None
```

Metadata read has no time-dependent behavior and therefore no invented clock
argument. Put/revoke require trusted aware datetime; put requires the exact existing
keyring class. Inputs use exact owner class and existing positive internal-ID,
provider/kind validation. Shared private `_credential_transaction_inputs` validates
these participant inputs, not permission or a physical root security policy.

Caller supplies the actual Session, tenant context, authenticated live guard and
final commit/rollback. Primitives never create a session, load a key, consult a
global factory, read a clock, commit or roll back. They return only existing safe
metadata, provisional until the caller commits, not ORM/secret authority. No new
crypto implementation, duplicate writer, arbitrary credential fallback or API.

## Preserved operation semantics

Put locks account first, checks optional expected external account identity, then
locks active credential. Generation remains latest+1 or initial1. Replacement marks
the old row revoked and flushes before inserting a new encrypted row. Same existing
AAD/identity/encrypt/decrypt equality check is used, then row flush and put audit.
Revoke uses the existing reason allowlist, same account-before-active-row order,
missing error, timestamps/flush and revoke audit. Status requires the scoped account
and returns the existing latest row's metadata, including revoked rows.

Existing public signatures are unchanged. Wrappers keep configured key loader,
clock, own session, tenant context, commit/rollback and safe error mapping. Public
put validates expiry before loading keys as before; the reusable primitive repeats
expiry validation to remain safe when called directly. Public put/revoke retain a
reference to the exact row identified by their own returned metadata before commit
and apply `_metadata(row)` afterwards, retaining existing expire-on-commit refresh
semantics. That internal identity is a result of the scoped operation, not a new
user UUID-only lookup API. A fresh core-only caller may use provisional metadata
inside its same transaction and return it only after successful physical commit.

No connected-only requirement was introduced: credential setup on a newly created
disconnected account and revocation of a disconnected account remain possible in
the underlying existing data contract. Do not wrap these with the connected-only
fetch publication guard as a substitute for the missing admin authority contract.

## Error / authorization boundary

Existing local contract/missing/account-identity/reason and crypto errors remain.
Primitives do not own SQL/deferred-commit exception translation or transaction
recovery; public wrappers keep their current mapping. Future authenticated owner
must safely map SQL and commit errors without raw DETAIL, roll back all row/audit
writes, validate actor/owner permission and final same-physical-root authority.
Passing actor_user_id is not authentication. No permissions or guard semantics are
inferred here, and these functions alone do not make a credential admin API safe.

## Pending centralized acceptance

Run original public put/revoke/status compatibility (including configured SQLite
tests), plus caller-owned PG rollback/audit atomicity, simultaneous replacement,
generation preservation, external-account mismatch, invalid/expired payload,
wrong org/account/provider/kind, disconnected create/revoke, crypto equality failure,
aware-time/keyring requirements, no inner factory/keyloader/commit/rollback calls,
safe metadata/canary/error checks and final authenticated admin composition when
T1 provides its actual authority. None of those gates has run for this package.

Rollback restores public inline operations and removes these private primitives;
no migration, operational grant, provider action or persisted data was produced.
