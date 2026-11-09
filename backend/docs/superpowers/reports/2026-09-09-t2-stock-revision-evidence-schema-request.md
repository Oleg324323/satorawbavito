# T2 → T1: stock daily reviewed evidence — narrow schema request

Дополнение к907ec7b, только WB warehouse stock daily correction. Не блокирует
approvals0066 или immutable Prices/Stocks current facts. Это proposed persistence
contract, **не** реализованная авторизация, migration или доказательство WB revision.
Runtime/domain/schema не изменяются. Основания: `wb_source_revision.py` (94b540f,
неизменён), `wb_stock_snapshots.py` f7a5d62, source request907ec7b; shared guard
4860c53 прочитан. Physical migration/DDL/helpers/ACL остаются T1.

## 1. Что действительно доказывает текущий domain

`compare_runs` требует точного равенства SourceContext (org/account/source/semantic
version/grain/request checksum), связывает evidence с двумя run IDs/manifest hashes,
вычисляет added/removed/changed по полным identity tuples и payload hashes. Неполный
run всегда incomplete_run; полный без semantic deltas — exact_replay; полный с
deltas без evidence — unexplained_change. Любая непустая правильно привязанная
reviewed_evidence_reference меняет последнее на source_revision.

Последний переход **не** подтверждает reviewer, его permissions, существование
evidence, подлинность/полноту WB, допустимость исправления дня или факт удаления
товара. SourceRun доверяет caller.complete и adapter hashes; manifest SHA не подпись
провайдера. Нельзя передавать пользовательскую строку в RevisionEvidence и считать
это approval. Net-equal aggregates не заменяют identity-level diff.

## 2. Минимальная модель и альтернативы

Выбран bounded вариант: immutable proposal + normalized diff rows + один immutable
decision. Отсутствие decision означает awaiting_review; accepted/rejected terminal,
без mutable status, lease или универсального evidence repository. Reviewer decision
сам служит audit fact; не нужен дублирующий статус в proposal. Вариант «URL в daily»
не сохраняет просмотренные данные и автора. Вариант общего workflow/outbox framework
избыточен: решение и запись daily — локальные транзакции, external send отсутствует.

Имена ниже предложены T1; логические поля/инварианты обязательны. Все root IDs UUID4,
все canonical org/account/member IDs positive internal INTEGER, WB provider FK.
Составные ссылки всегда включают org/account. Нет lookup только по UUID.

### `wb_stock_revision_evidence`

| Поле | Обязательность / meaning |
| --- | --- |
| organization_id, marketplace_account_id, evidence_id | required scoped PK/UNIQUE; WB account composite FK |
| before_run_id, before_manifest_checksum | required scoped FK к sealed complete stock run с этим exact manifest |
| after_run_id, after_manifest_checksum | required scoped FK к другому sealed complete stock run с этим exact manifest |
| source_kind, parser_version, grain_version, request_checksum | required equal before/after context; v1 only wb_warehouse_v1, wb-warehouse-stocks/v1, grain below |
| stock_scope | required constant wb_warehouse; participates with request checksum in full daily owner FK |
| business_date_msk, before_daily_revision | required full scoped FK только к immutable daily revision PK; его source_run_id должен равняться before_run_id |
| proposed_by_membership_id, proposed_at | required org/member FK; trusted authorized proposer, aware finite DB timestamp |
| proposal_command_id, proposal_checksum | required UUID4 replay key scoped org/account; lowercase SHA256 immutable canonical proposal bytes |
| evidence_document_bytes, evidence_document_checksum | required sanitized UTF-8 bytes of the explanation actually reviewed; SHA256 must match |
| reviewed_evidence_reference | required exact nonblank identifier/reference, no credentials/signed URL query; pointer only, not authority |
| diff_checksum, added_count, removed_count, changed_count | required digest and exact nonnegative counts, total >0 |

No raw marketplace payload, credentials, arbitrary log/exception or live URL fetching
in this table. Immutable evidence_document_bytes is a **reviewer explanation artifact**,
not asserted provider-signed proof. Input service validates UTF-8 and rejects secrets
under explicit payload policy; CHECK/hash cannot identify secrets. No physical raw
TEXT identity B-tree dependence: refs use existing bounded scoped IDs. Before/after
context and daily linkage are deferred composite checks/FKs, not comments alone.
Never FK before_daily_revision to mutable head.current_revision. An optional head
reference may include immutable owner fields only. Historical evidence's original
revision link survives head advancement; only consumption CAS checks the current head.

The proposal, all diff rows and final manifest/count check commit atomically. No
append/UPDATE/DELETE after commit. If independent evidence is missing, there is no
acceptable proposal; do not manufacture an explanation or clone today's stock.

### `wb_stock_revision_evidence_diffs`

Scoped evidence FK; physical diff_row_id UUID4 PK plus UNIQUE
`(org,account,evidence_id,nm_id,chrt_id,warehouse_id)` NULLS NOT DISTINCT (or equivalent
uniqueness for nullable chrt; nullable chrt must not be part of a primary key). `nm_id` and
`warehouse_id` positive BIGINT; `chrt_id` nullable positive BIGINT, **not** nmId.
These columns are source identity, not CatalogSku resolution. `change_kind` is
added/removed/changed; before_payload_checksum and after_payload_checksum:

- added: before NULL, after required;
- removed: before required, after NULL;
- changed: both required, unequal.

Hashes lower64. Identity occurs once, across all three categories. Unchanged rows
are not copied. Completeness requires exact set equality against a newly computed
diff of both sealed run fact sets, not merely validating the listed rows/counts.
Removing one row from review, swapping hashes, changing grain or omitting a net-zero
change must fail. Identical duplicate raw rows are not additional identity changes;
current strict stock parser rejects duplicate identities rather than deduplicating.

### `wb_stock_revision_evidence_decisions`

PK/unique `(org,account,evidence_id)`, scoped proposal FK; decision_id UUID4 unique
within scope; outcome accepted/rejected; reviewed_by_membership_id required org FK;
decided_at required trusted aware finite timestamp >= proposed_at; decision_command_id
UUID4 scoped org/account; reviewed_proposal_checksum required exact proposal digest;
reason_code required safe grammar `[A-Za-z][A-Za-z0-9_]{0,127}`. No free-form actor.

One final decision per proposal; no UPDATE/DELETE. Decision concurrently inserted
after proposal row FOR UPDATE: one winner, second same-command/same-actor/same-bytes
replay reads existing decision, any contrary intent conflicts. Decision may reject a
proposal without daily mutation. Editing evidence/diff requires a new proposal ID.
For proposal or decision command replay, first resolve existing scoped command before
generating new server UUIDs; preserve original IDs/times. Compare immutable intent
including actor/reason/payload, not the newly generated UUID. No generic UUID-only lookup.
Retraction/appeal policy is not invented: no implicit rejected→accepted or accepted
→rejected. If invalidation of an unconsumed acceptance is required, authorize a
separate append-only invalidation contract before activation; do not silently delete.

## 3. Exact projection and checksum binding — new proposed v1, not existing proof

Current generic SourceRow does not define stock payload encoding. To make the schema
implementable, fix the following **proposed** narrow projection before evidence writer
activation; T2 must implement and test it separately against the accepted schema.
No change to compare_runs or existing hashes is authorized here.

SourceContext source=`wb_warehouse_v1`, semantic_version=`wb-stock-evidence/v1`,
grain=`nmId/chrtId?/warehouseId`, request_checksum=CollectionRequest.checksum;
org/account from validated run. Every row identity tuple is:
`("nmId",decimal_nm,"chrtId",decimal_chrt_or_"null","warehouseId",decimal_warehouse)`.
Decimal is canonical positive base10, no padding/sign/exponent. Literal `null` cannot
collide with a valid integer. Both run IDs use canonical UUID text.

Payload checksum = SHA256 of ASCII compact JSON array:
`["wb-stock-evidence-row/v1",[quantityPresence,quantityOrNull],
[toClientPresence,toClientOrNull],[fromClientPresence,fromClientOrNull]]`.
Presence missing/null/value is exact. Amounts are integer, not float/string; raw
warehouse names/ignored source attributes do not enter this **count-only** semantic
diff, but raw manifest still binds full accepted bytes. No new money/stock formula.
Changes only to ignored attributes give exact_replay, not permission to rewrite daily.

Diff checksum = SHA256 of compact ASCII JSON array
`["wb-stock-evidence-diff/v1",[[identityParts,changeKind,beforeHashOrNull,afterHashOrNull],...]]`.
Sort rows by identity tuple using Python lexicographic string order, matching
compare_runs; no locale collation or numeric reordering. Emission uses ensure_ascii,
no added whitespace. Proposal checksum hashes the fixed array:
`["wb-stock-evidence-proposal/v1",org,account,evidenceUUID,beforeRunUUID,beforeManifest,
afterRunUUID,afterManifest,sourceKind,parserVersion,grainVersion,requestChecksum,
businessDateISO,beforeDailyRevision,proposerMembership,proposalCommandUUID,
evidenceDocumentHash,reviewedEvidenceReference,diffHash,addedCount,removedCount,changedCount]`.
grainVersion stores the SourceContext grain literal above; parserVersion stores
wb-warehouse-stocks/v1; evidence semantic version is fixed by array tags. Timestamp
is excluded from replay bytes; replay preserves the originally committed timestamp.
Exact proposal/evidence bytes must be compared on hash matches, not assumed equal
solely because SHA256 matches. Unknown fields/encodings are rejected, never repaired.

## 4. Accepted evidence → daily correction, not a generic publication grant

Future service loads a scoped accepted decision and immutable proposal, verifies
its digest/reviewer fact and both manifests, rebuilds complete before/after SourceRuns
from domain rows and recomputes full diff. Only then construct RevisionEvidence with
reference `stock-evidence:<evidenceUUID>:<decisionUUID>`; never trust client-supplied
reference. source_revision classification is an input check, not authorization.

Daily correction transaction: fresh shared auth guard → account → proposal/decision
→ existing daily head, preserving T1 lock order. Require head revision/source run
equals proposal.before_daily_revision/before_run_id, both contexts unchanged and
after run eligible for **the same** business_date_msk under existing daily basis rules.
New daily revision must persist evidence_id + decision_id + proposal checksum,
supersedes revision, after_run_id, acting membership/reason/time. Add scoped FK to
decision; a deferred check requires accepted + exact before/after/daily binding.
CAS head, insert revision and existing daily.corrected audit share one transaction.
Second consumer after successful correction cannot reapply: expected prior head
is gone; exact command replay may read its original result after fresh authorization.

Acceptance must **not** override date eligibility. Current parser has no documented
source_observed timestamp; a new receipt today cannot correct a prior-day stock
snapshot. Before/after receipts crossing midnight are not daily eligible. A historical
source with proven timestamps requires its own adapter contract first. This evidence
schema neither declares provider deletion nor enables finance/price corrections.

## 5. T1 service-auth decisions / activation gates

These are deliberately **not guessed permission policy**:

1. Map proposal/read/accept/reject/daily-correct to trusted fixed permission constants
   and expose authenticated user/session+canonical membership principal. Existing
   `sync:run` for Orders is not automatically stock evidence approval. Do not accept
   client permissions or actor IDs as authentication.
2. Decide whether proposer may review/apply their own proposal (no invented four-eyes
   rule), and who may invalidate unconsumed acceptance. Without the decided policy,
   mutation API remains off; tables/current facts need not wait.
3. Use guard4860c53 with all live user/session/member/account checks through COMMIT.
   Evidence work on persisted facts may use explicit empty authorities as a trusted
   credential-independent operation; any external fetch requires every paired binding.
   No fake worker principal; automated review/acceptance not part of this request.
4. Decide safe explanation/reference payload limits and sanitization/retention owner;
   do not attach raw tokens, arbitrary fetched links or provider payloads. Schema
   hashes alone cannot enforce sensitive-data filtering. No guessed retention/deletion.

Past decision is an audit fact, not enduring permission for a later caller. Fresh
auth is required on proposal/decision replay, reads and daily apply. FK membership
proves ownership, not that the reviewer was authorized when acting. That needs actual
guarded service transaction tests and cannot be claimed by this schema.

## 6. Required DB/service tests (NOT RUN)

- Foreign org and same-org other account cannot read/decide/consume; both run/manifest,
  member, daily and decision composite bindings enforced under non-owner FORCE RLS.
- Incomplete, wrong-context, same-run-change, exact replay and empty diff cannot be
  accepted as changed complete evidence; arbitrary reference alone grants nothing.
- Missing/additional/swapped diff rows, zero-vs-missing and cancelling aggregates:
  freshly rebuilt full identity diff must match exactly; all children sealed.
- Two reviewers or two daily consumers race: one durable winner, no extra decision/
  daily revision/audit; stale head/version/revoked membership/expired session fails.
- Error on decision/daily audit/COMMIT rolls back the whole local mutation; input
  bytes and prior history survive restart, no File/memory fallback or rehash repair.
- Same-day receipt gate, midnight crossing, historical receipt substitution, changed
  document under same reference and changed run under old decision all reject.
- Runtime cannot edit/delete evidence/diff/decision/history; proposed/accepted/rejected
  rows block empty-only downgrade. No table helper may bypass account authorization.

Existing pure regression test_wb_source_revision.py checks classification/bindings,
not these auth/storage gates. This request adds no code, migration or test weakening.
Fresh existing source-revision/grain/stock parser group:87PASS in0.08s, exit0;
`git diff --check` exit0. No new executable behavior or PostgreSQL test was run.

## 7. Committed pure codec prerequisite (2026-09-09 continuation)

T1 requested exact domain bytes before SQL parity. Dormant implementation:
`app/modules/wb_stock_evidence_codec.py`; tests
`tests/test_wb_stock_evidence_codec.py`; literal fixture
`tests/fixtures/wb_stock_evidence_golden_v1.json`. The eight vectors are ASCII JSON
containing full typed inputs, canonical ASCII text, byte count and SHA-256. Decode
outer JSON with exact integers, then ASCII-encode canonical_ascii once; do not
JSON-encode that string again or parse BIGINT through JavaScript Number. Tests load
the literals; they never regenerate the fixture. Initial literals were generated
once from the proposed encoder and pinned, not independently proven SQL output.

`source_identity`/`row_bytes` accept existing WarehouseStockObservation. Count-only
bytes exclude native identity, which is carried separately. `EvidenceDiffRow` fixes
added/removed/changed hash shapes; `diff_bytes` rejects duplicate identities across
categories and sorts exact string tuples. Empty diff encoding exists for diagnostics,
but EvidenceProposal rejects empty evidence. Proposal derives diff/document hashes
and category counts rather than trusting separately supplied totals. Scope/member
are strict positive INT4, native stock IDs/counts retain existing BIGINT rules;
root IDs canonical UUID4, date exactly date, revision positive integer. UTF-8 and
exact nonblank PostgreSQL-compatible reference checked without Unicode normalization.

Every encoding takes explicit positive `max_bytes`; the fixture uses8192 solely as
a synthetic test budget, not operational policy. Document bytes must already be
sanitized and reviewed by the caller: UTF-8 validation does not detect credentials,
signed URLs or prove an explanation. No URL fetching, clock, DB, retained payload
store, repository or scheduler integration. Time is intentionally absent from
proposal bytes; the eventual service preserves original committed timestamps.
Codec validates shape, not complete before/after set equality, parent linkage,
manifest provenance, daily eligibility, auth or publication. All §§4–6 gates remain.

Rollback before wiring is a code/fixture revert only: no data or migration was added.
SQL parity and actual persistence tests remain T1/schema-dependent and NOT RUN here.
