# Immutable Review run binding

2026-09-09. T1 storage design from exact T4 request `1a3d34477bc617aaad1e2731d2eb2b38ca82400a:backend/docs/superpowers/reports/2026-09-09-t4-review-historical-binding-request.md`. No revision is reserved, no DDL/consumer acceptance or activation is claimed.

## Problem and ownership

T4's disposable characterization writes a Review, completes an account rebind, acquires a fresh guard for the new binding and reads the old body. Its reported 1PASS is an OPEN defect witness, not safe-read acceptance. Current0063/0065 internal owner columns and in-flight publication guard cannot establish historical external-cabinet provenance.

Choose equivalent normalized immutable-from-INSERT fields on `review_sync_runs_v2`, rather than a separate binding table or mutable audit metadata. Observations already refer to the scoped source run. Co-locating the descriptor makes run reservation and binding one INSERT, rejects late promotion of an old run, and adds no second source of ownership truth. A separate table would add another atomic/FK/grant boundary with no needed independent lifecycle. Mutable audit JSON is not authoritative.

T1 owns only forward schema, helper grants and schema/RLS tests. T4 owns strict descriptor codec, repository/service admission and current/ambiguity read/publication checks. No T4 implementation is copied into T1, no old migration is rewritten, and Orders0067's positional codec is not reused for Review descriptors.

## Physical envelope

Add five nullable fields to the existing run:

| Column | Type / contract |
| --- | --- |
| account_binding_schema_version | SMALLINT, 1 when bound |
| account_binding_external_account_id | TEXT COLLATE C, exact nonempty when bound |
| account_binding_credential_ref | TEXT COLLATE C, NULL or exact text, including empty |
| account_binding_payload | BYTEA, exact canonical descriptor |
| account_binding_checksum | TEXT, lowercase64 SHA256 of exact payload |

All five NULL is legacy/unbound. Bound requires schema1, external/payload/checksum non-NULL; credential_ref NULL is valid and distinct from unbound/empty. Reject all partial shapes using explicit IS NOT NULL conditions so SQL three-valued CHECK logic cannot admit them. Do not add a lifecycle epoch or credential generation to this descriptor. Existing scoped run→account and observation→run FKs supply the internal owner relationship.

Every field is immutable from the original INSERT, including on running/empty runs and for privileged ordinary DML. An UPDATE cannot promote unbound, clear or change binding. Add a dedicated trigger rather than rewriting0065's existing run lifecycle/bytes guards. Do not lock account in UPDATE row triggers after a run tuple lock; no new mutable binding command is introduced.

For a bound INSERT, require READ COMMITTED, lock exact org/account/provider canonical account FOR UPDATE, reject immediately if no row was locked, then read its current provider/external/ref in a separate fresh statement and compare exact values. Retain the lock through the reservation transaction. A missing-account race must not overwrite FOUND and admit an unlocked new account. Runtime service user/membership/session authorization locks precede account; DB context and supplied metadata are not authentication.

Existing unbound historical writers remain representable while default-off rollout is closed, but they cannot publish into a bound/mixed historical unit through the activated T4 service. Storage compatibility must not be called safe consumer cutover.

## Canonical encoding

Provide new dedicated pure immutable SECURITY INVOKER, schema-qualified helpers with fixed search_path:

```text
review_binding_ascii_string(text) -> text
review_run_binding_bytes(integer, integer, text, text, text) -> bytea
# organization_id, marketplace_account_id, marketplace, external_account_id, credential_ref
```

Descriptor exactly six keys: credentialRef, externalAccountId, marketplace, marketplaceAccountId, organizationId, schemaVersion, in this sorted order. Canonical bytes equal Python compact sorted `ensure_ascii=True`, `allow_nan=False` ASCII encoding, no newline/BOM; schemaVersion is inside the object. IDs strict positive INT4, provider exact wb/avito. Escape quotes/backslashes/control characters and supplementary scalars exactly like Python; preserve Unicode normalization, all whitespace and null/empty distinctions. External must be nonempty, not nonblank. Do not borrow Orders' strip/length grammar.

CHECK requires exact reconstructed BYTEA equality AND SHA256 equality. Hash-only equality is insufficient. SQL TEXT has a native NUL/unpaired-surrogate representation limit and existing canonical-account VARCHAR has its current length limits. Do not silently trim, truncate or replace values. A bound SQL run must exactly match an actual representable canonical account; SQL cannot promise to admit an account containing U+0000. T4's pure descriptor accepts scalar strings under its own contract; native SQL inadmissibility is tested/disclosed, not presented as a new source-body restriction. Review body/source/coverage lossless0065 remains untouched.

Required literal vectors from1a3d344:

```text
{"credentialRef":null,"externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
71a7e4a446ba44c9ea993b2332736e6407490d7acbbd380d92661bbb62bc9c60
{"credentialRef":"","externalAccountId":"seller-A","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
c367232907c44843dbd7ac1ab6d9c3da34cd5c72c68cfcedaecac9ce576432e6
{"credentialRef":null,"externalAccountId":"seller-B","marketplace":"wb","marketplaceAccountId":11,"organizationId":1,"schemaVersion":1}
645db76e3ce7686e2233bdcd1a5f26a4751b3d8b01a9fc22ede0c255f200d92a
```

Exact committed T4 codec/literal vectors are now available at45e2ccbb165e6ddcc383311789b84b5d93ef2933: `app/reviews/historical_binding.py`, `tests/test_review_binding_codec.py`, `tests/fixtures/review_binding_descriptor_v1.json`, and handoff2026-09-09-t4-review-binding-codec-handoff.md. Main read all four fully; T4 also explicitly reviewed this physical direction against1a3d344/45e2ccb with no domain mismatch. Fixture has six literal vectors: originalASCII3, Unicode/maxINT4 with/without NULref and control/NULexternal. Four are candidates for actual SQL account admission; two NUL vectors are native-rejection/pure-codec evidence only. Reported61focused/283adjacentpurePASS is attributed T4 evidence, not main-run SQL parity. Do not manufacture/copy a foreign implementation; copy the exact committed fixture bytes only into a later authorized parity test path. Additional SQL expectations may use independent stdlib JSON. A concrete implementation plan/revision follows actual then-current sole head; this document does not reserve one.

## ACL, RLS and rollback

Preserve existing Review org-only FORCE RLS and existing multi-account query contract; do not silently convert it to account-GUC RLS. Schema tests prove no-context/wrong-org denial and scoped FK integrity. Same-org per-account permissions and historical binding are T4 guard/repository acceptance, not implied by this unchanged policy.

New columns must be insertable by the real intended run writer without granting UPDATE of identity/sequence or broadening limited readers. Existing0063/0065 column INSERT grants need explicit intersection-based extension only for holders of the complete required run INSERT authority; do not assume table-level INSERT exists. Pure helper EXECUTE only to entitled grantees, no PUBLIC/new default-ACL leakage. No trigger-only callable mutation API. Preserve exact old relation/column/function/default ACLs apart from the specifically approved new-column/pure-helper extension, and keep runtime script synchronized.

Downgrade takes run ACCESS EXCLUSIVE lock and verifies genuinely unfiltered absence of ANY non-NULL binding field before removing anything. Set row_security=off as a refusal defense, not a privilege grant. Nonempty bound evidence blocks downgrade; no erase/unbind/relabel/CASCADE workaround. Old binaries may run with the expanded schema while operational writer activation remains off. Removing trusted binding is not a valid rollback.

## Consumer gates and test matrix

T4 must reserve exact binding atomically before fetch; same-run replay with changed descriptor conflicts before I/O. After fetching, publication guard revalidates the paired live binding; publication/replay must also inspect the immutable source run of each existing fact/current head and relevant ambiguity evidence. Missing/malformed/different provenance blocks the entire requested unit with safe409, no silent omission, rewind, current-account backfill, provider GET refresh, legacy fallback or cross-cabinet merge. Mixed-source units unavailable. No automatic reset/delete/new-account strategy is inferred.

Required actual tests: previous-head valid run control and missing helper/column RED; three literal vectors plus Unicode/controls/null-empty; independent empty unstamped chain and populated old Orders/Reviews preservation; from-INSERT immutability/all partial shapes/unbound retention; wrong owner/provider plus both real account lock-winner rebind orders and missing-account race; exact ACL/default/limited-column grant behavior; runtime FORCE RLS and downgrade-hidden-data refusal. Run-schema tests alone do not close the reproduced completed-rebind defect.

T4 acceptance separately requires completed rebind, in-flight rebind both orders, null/empty ref change, unbound/mixed current+ambiguity rejection, same-run replay/change, publication history fences, rollback, safe shared ErrorEnvelope and no descriptor/ref exposure. ABA with identical final descriptor is not detected; no invented epoch claim.

Only synthetic own Unix-disposable PostgreSQL/runtime roles and scrubbed secret/IP-denying sandbox; no production/providers/real credentials/working Redis/flags/backfill/push/deploy. Native runtime limitation is not replaced with SQLite proof.

## Critical pass

Checked descriptor versus Orders codec mismatch, PostgreSQL NUL versus lossless provider-body distinction, immutable-before-observation requirement, existing column-grant writer admission, missing-lock FOUND race, UPDATE lock inversion, org-RLS versus account authorization, incomplete/mixed history publication, and hidden-row downgrade. No schema or consumer test result is inferred from this design. Revision and implementation remain pending actual graph/codec acceptance.
