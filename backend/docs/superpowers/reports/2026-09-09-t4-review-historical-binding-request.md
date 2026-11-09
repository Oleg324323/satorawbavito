# T4 → T1: immutable Review source binding

## Reproduced gap, not safe-read acceptance

On835a82b, test_review_historical_binding_gap.py writes a synthetic Review while
account91103 points to synthetic-c, commits a change to synthetic-new-cabinet, then
acquires a fresh real reviews:read guard for the new binding. The scoped repository
returns the old body. Actual Unix-only disposable result:1PASS3.08s, cleanup checked.
The assertion characterizes the defect; PASS does not establish tenant isolation.

Current0063/0065 owner columns contain internal organization/account/provider only.
_command verifies current MarketplaceAccount metadata, not historical source binding.
The in-flight rebind check835a82b and shared publication guard cannot close this gap.
No public Review reader is being wired until the historical boundary is enforceable.
Default-off router registration remains a separate step, never activation approval.

## Proposed minimal grain and write contract (T1 approval required)

One immutable binding row per review_sync_runs_v2.sync_run_id, with exact composite
organization_id/marketplace_account_id/marketplace/run FK. Preserve run owner identity
and schema-versioned canonical binding bytes plus checksum, or equivalent normalized
columns with an exact lossless decoder. No mutable audit JSON and no redundant
per-observation body copy. Every observation already references its immutable source
run; current fact and ambiguity pointers must be checked through their referenced
observations/runs rather than inferred from the fact's account ID.

Trusted service derives binding from the guarded paired fetch, never request JSON.
Reserve run plus binding must be atomic, before provider I/O. Initial/replay admission
holds account lock after user/member/session locks and verifies the exact current
account binding. Same run plus changed descriptor conflicts before fetch. Once any
run item/observation is admitted, its binding cannot change or disappear; runtime
cannot UPDATE/DELETE/TRUNCATE it. Missing binding cannot be promoted to bound by an
untrusted replay or an observation write. RLS/FKs/grants remain T1-owned.

Descriptor keys exactly: schemaVersion=1, organizationId, marketplaceAccountId,
marketplace, externalAccountId, credentialRef. IDs strict positive INT4, never bool;
marketplace exact wb/avito; externalAccountId nonempty Unicode scalar string, exact
no normalization/trim; credentialRef exact Unicode scalar string or null, null and
empty distinct. Descriptor is non-secret metadata, not credential payload/token.
The consuming public API must not expose credentialRef or the descriptor itself.

Canonical bytes: Python-compatible json.dumps(...,sort_keys=True,ensure_ascii=True,
separators=(',',':'),allow_nan=False).encode('ascii'), no newline/BOM. Decode rejects
duplicate/extra/missing keys, wrong types, invalid Unicode scalars and noncanonical
byte spellings by exact re-encoding. SHA256 hashes those exact bytes; a hash alone is
not authorization or a substitute for comparing the decoded exact descriptor.
ASCII golden vectors below were calculated independently with Node crypto; future
codec/SQL implementation must validate them and add Unicode/control-character cases.

```text
{"credentialRef":null,"externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
sha256 71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60

{"credentialRef":"","externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
sha256 c367232907c44843dbd7ac1ab6d9c3da34cd5c72c68cfcedaecac9ce576432e6

{"credentialRef":null,"externalAccountId":"seller-B","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
sha256 645db76e3ce7686e2233bdcd1a5f26a4751b3d8b01a9fc22ede0c255f200d92a
```

## Read/current-head/legacy rules

- Old missing stamp is explicitly unbound. Never backfill it from current metadata,
  provider-ID resemblance, credential/token equality or the rollout allowlist.
- Guarded reads compare every selected current observation and relevant ambiguity
  evidence's immutable run descriptor to the exact live guarded binding. Missing,
  malformed or unequal provenance blocks the entire requested unit with a safe409;
  no partial silent omission, old-data fallback or provider refresh during GET.
- A mixed-binding page/snapshot is unavailable as a unit. A single mismatched
  current head cannot silently rewind to an older matching observation.
- Publication/replay must not advance or merge an existing fact head whose source
  binding is missing/different. Otherwise the same external review ID in two cabinets
  merges histories. Missing/different history needs an explicitly approved isolation
  migration/new-account strategy, not automatic relabel/delete/reset.
- This captures exact binding at each run, not a new account lifecycle epoch. It
  does not detect an unobserved A→B→A transition with identical final descriptor.
  If lifecycle generations are required, T1 must define that versioned contract;
  T4 does not manufacture an epoch from timestamps or credentials.

Acceptance requires completed rebind and both in-flight race orders, exact nullable
ref changes, unbound history rejection, mixed current/ambiguity run rejection,
same-run replay/change, atomic rollback, cross-owner FK/RLS/ACL and actual consumer
decoding. No existing migration rewrite. Orders0067 must not be assumed applicable
to Review tables. This document requests a bounded design/storage decision, not
permission to enable providers, alter production or erase old observations.
