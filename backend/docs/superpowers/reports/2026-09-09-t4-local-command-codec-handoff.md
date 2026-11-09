# T4 → T1: local Review command/result canonical bytes

Implements the pure representation boundary requested by T1 after accepted
`a35b657` and `b7c3bb1`. No schema, imports of schema candidates, ORM, routes,
providers, account authorization, generation or persistence integration added.

## Inputs for the local DDL owner

- `app/reviews/local_command_payloads.py`:
  `encode_review_local_request` and `encode_review_local_result`.
- `tests/fixtures/reviews/local-command-v1-golden.json`: eight literal cases,
  request and result for each of the four operations. Independent synthetic
  examples, not one combined fixture import sharing the same request UUID.
- Existing contract: `2026-09-09-t4-review-storage-exact-matrices.md`, local replay
  and policy-selection epoch sections. Existing policy/generation/audit/binding
  serializers and their canonical bytes are unchanged.

Request operations: `review.policy.create.v1`, `review.policy.select.v1`,
`review.draft.publish.v1`, `review.decision.record.v1`.
Closed exact top-level/nested keysets; no defaulted omitted fields. Policy and
generation use the existing encoders. Owner/member remain positive INT4; nested
policy owner and generation actor must equal the envelope. Head initialization
requires both draft/head expected versions zero, or both positive. Policy epoch
requires nonzero canonical head UUID and positive exact version. Decisions require
positive expected head/revision, explicit approved/rejected, exact source/binding
references. Prepared text must be nonblank valid UTF-8 but is never normalized.

Result refs are explicit null exactly where the operation does not use them.
Non-null IDs/versions are validated. Decision headVersion is at least2 because
its input requires an existing draft/head; completedAt has six UTC fractional
digits. Result has no text, owner/binding payload or inferred current eligibility.
Request/result cross-row equality, expected+1 arithmetic and witness ownership
belong to transactional consumers, not these standalone representation checks.

## How to consume the golden fixture

Parse the outer file, take each `canonicalUtf8Json` string and encode it directly
as UTF-8. That is the fixed expected BYTEA. `sha256` is a frozen literal calculated
independently from these hand-authored bytes, not produced by the encoder under
test. For typed inputs parse this string with a lossless JSON integer parser.
Do not decode it through JS Number or reserialize using PostgreSQL `jsonb::text`.

Cases preserve policy/draft/epoch/result versions beyond signed BIGINT, canonical
UUIDs, leading-zero Unicode external Review ID, composed and decomposed accents,
emoji, quote, newline, tab, NUL and surrounding spaces in draft text. No generated
secret/provider data. For SQL storage preserve bytes and exact integers; never
substitute a VARCHAR cast that strips NUL or truncates version values. A SQL
implementation cannot manufacture authority from successfully decoded input.

Receipt account-binding descriptor bytes remain separate and private, encoded by
the existing historical-binding codec (its ensure_ascii=true format is unchanged).
Generation/witness preparation, fact/binding/source checks, current policy epoch,
RLS/FKs, immutable receipt uniqueness, physical CAS and atomic audit are still
required in T1 DDL and T4 services. Serialization proves none of those gates.

## Verification

TDD initial8 literal cases failed with `Local command codec missing`, then new
module60PASS. Main critical pass identified that decision result headVersion1
was incorrectly accepted: dedicated case1RED/5PASS, then reject this impossible
initial result. Final new61 plus existing storage payload/golden/local audit/
decision tests: **218PASS,0warnings,0.20s**, naturalexit0. Exact bytes, all missing/
extra keysets, invalid fields/null refs, actor/owner mismatch, no input mutation,
key reordering, manual-edit and rejection variants exercised. Scoped Ruff/diff0.

All tests ran with scrubbed environment under the existing all-network-denied
sandbox, without PostgreSQL/Redis/provider access or the shared heavy slot. Main
critical pass checked no authority claims, no lossy integers/text, preservation
of existing formats, result refs and delayed-replay contract limits. Root final
independent integration review remains. No full backend/frontend rerun or schema
READY claim; frozen77abab0 milestone remains separate from this follow-up.
