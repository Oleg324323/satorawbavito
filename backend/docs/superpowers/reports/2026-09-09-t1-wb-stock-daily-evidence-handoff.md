# T1 → T2: ежедневная история WB stock и reviewed evidence

2026-09-09. **IMPLEMENTED / UNVERIFIED**. Новый source-only Alembic
`20260909_0078`, точный predecessor `20260909_0077`. Чистая dispatch-база
`b21f2166886ade7637b1b9594b5b28c1e38d3e80`; фактические 631 строка миграции0077
и полный handoff прочитаны до реализации. Текст всего revision graph показал
единственную вершину0077, исторический merge0040 и отсутствие0078. Последующие
disjoint controller/import commits сохраняются; существующие миграции не меняются.

Binding inputs: §§7–8 `907ec7b` price-stock schema request; полный evidence request,
`wb_stock_evidence_codec.py` и восемь literal vectors в
`wb_stock_evidence_golden_v1.json` на точном T2 commit
`0c8fca9c5f8bdf62e630398cb1bd663acfb2cee0`. Все прочитаны через локальный `git show`.
SQL parity не запускалась. Это хранилище, не готовый authenticated evidence service,
не разрешение на correction, не доказательство подлинности WB и не завершение Stage5.

## Физические родители и общее владение

Новые отношения только: `wb_stock_daily_revisions`, `wb_stock_daily_heads`,
`wb_stock_revision_evidence`, `wb_stock_revision_evidence_diffs`,
`wb_stock_revision_evidence_decisions`, `wb_stock_daily_audit`. На каждом:

- `organization_id INTEGER NOT NULL >0`, `marketplace_account_id INTEGER NOT NULL >0`,
  `marketplace TEXT COLLATE "C" NOT NULL = 'wb'` и FK ровно на
  `marketplace_accounts(organization_id,marketplace_account_id,marketplace)`.
- ENABLE + FORCE RLS с одновременным точным равенством обоих owner IDs в decimal
  text и `app.organization_id` / `app.marketplace_account_id`. Неправильные, отсутствующие
  или сменённые до deferred checks GUC закрывают транзакцию.
- Root/command IDs UUID4; source run IDs ссылаются на UUID4 родителей0077.
  Membership — positive INT4 плюс реальный FK
  `iam_memberships(organization_id,membership_id)`. Это ownership, не доказательство
  действующей сессии, роли, permission или состояния участника.
- Revision, head version и категории diff — точные `NUMERIC` без typmod, только
  конечные целые, без BIGINT/float преобразования. Revision положительная, category
  count неотрицательный. Native nm/chrt/warehouse остаются positive BIGINT.

Сокращение `owner` ниже означает `(organization_id,marketplace_account_id)`;
`day` означает `(organization_id,marketplace_account_id,stock_scope,request_checksum,
business_date_msk)`. Поля day: `stock_scope TEXT C = 'wb_warehouse'`, lower64 SHA256
`request_checksum TEXT C`, `business_date_msk DATE` в диапазоне0001..9999.
Идентификаторы не разрешаются только по UUID.

Реальный run parent: `wb_stock_runs(owner,source_kind,parser_version,request_checksum,
run_id)` уже имеет UNIQUE0077. Обе fixed строки — `wb_warehouse_v1` и
`wb-warehouse-stocks/v1`. Schema0078 не добавляет родительских колонок/индексов,
не переписывает source audit и не обновляет source lifecycle/current head.
Manifest equality и complete/received eligibility проверяются по реальным полям
`state`, `version`, `request_bytes`, `request_checksum`, `manifest_checksum`,
`received_at`, `finished_at`, `page_count` и реальным `wb_stock_pages`.

## Шесть отношений

| Отношение | Контракт |
| --- | --- |
| `wb_stock_daily_revisions` | PK `day,revision NUMERIC`. UUID4 `daily_revision_id` и `command_id` по отдельности UNIQUE с owner. Fixed `source_kind`, `parser_version`; `source_run_id` с точным context FK0077; `request_bytes BYTEA` с SHA равным owner checksum; `basis TEXT C='received'`; `effective_observation_at TIMESTAMPTZ` точно run.received_at; DB `created_at TIMESTAMPTZ`; `actor_kind TEXT C` worker/membership; nullable positive INT4 `actor_membership_id`; nullable safe `correction_reason TEXT C`; nullable positive NUMERIC `supersedes_revision` с FK `day,revision`; nullable UUID `evidence_id`, `decision_id`, lower64 `proposal_checksum`. Owner/evidence и owner/decision UNIQUE обеспечивают однократное потребление. Scoped FK owner/evidence/decision на фактический terminal decision. |
| `wb_stock_daily_heads` | PK `day`; required `current_revision NUMERIC` с FK к immutable day/revision; `version NUMERIC=current_revision`; DB `updated_at TIMESTAMPTZ`. Пустого head нет; изменяемы только current_revision/version, время выставляет trigger. |
| `wb_stock_revision_evidence` | PK owner/UUID4 `evidence_id`; UNIQUE owner/UUID4 `proposal_command_id`. Полный day и fixed `source_kind`, `parser_version`, `grain_version TEXT C='nmId/chrtId?/warehouseId'`; exact `request_bytes BYTEA`; разные UUID `before_run_id`, `after_run_id` с двумя реальными context FK0077; lower64 `before_manifest_checksum`, `after_manifest_checksum`; positive NUMERIC `before_daily_revision` с immutable day FK, чей source точно before_run_id. Required positive INT4 `proposed_by_membership_id`, DB `proposed_at TIMESTAMPTZ`; `proposal_bytes BYTEA`, lower64 `proposal_checksum`; `evidence_document_bytes BYTEA`, lower64 `evidence_document_checksum`; exact `reviewed_evidence_reference TEXT C`; lower64 `diff_checksum`; NUMERIC `added_count`, `removed_count`, `changed_count`, сумма>0. |
| `wb_stock_revision_evidence_diffs` | PK owner/UUID4 `diff_row_id`; scoped `evidence_id` FK. Positive BIGINT `nm_id`, nullable positive BIGINT `chrt_id`, positive BIGINT `warehouse_id`. UNIQUE NULLS NOT DISTINCT(owner,evidence_id,nm_id,chrt_id,warehouse_id); nullable chrt никогда не PK. `change_kind TEXT C`: added/removed/changed; nullable lower64 `before_payload_checksum`, `after_payload_checksum`. Added NULL/hash; removed hash/NULL; changed два разных hash. |
| `wb_stock_revision_evidence_decisions` | PK owner/evidence_id плюс proposal FK; UUID4 `decision_id`, `decision_command_id` по отдельности UNIQUE с owner; дополнительный UNIQUE owner/evidence_id/decision_id для потребления. Required `outcome TEXT C` accepted/rejected; positive INT4 `reviewed_by_membership_id`; DB `decided_at TIMESTAMPTZ >= proposed_at`; exact lower64 `reviewed_proposal_checksum`; safe required `reason_code TEXT C`. Один terminal decision, UPDATE/DELETE запрещены. |
| `wb_stock_daily_audit` | PK owner/DB UUID4 `audit_id`; UNIQUE day/positive NUMERIC `revision`, immutable revision FK, scoped `source_run_id` FK; `event_kind TEXT C` daily.created/daily.corrected; DB `occurred_at TIMESTAMPTZ`; worker/membership `actor_kind`, nullable member FK; typed composites `before_revision`, required `after_revision` (`wb_stock_daily_revisions`), `before_head`, required `after_head` (`wb_stock_daily_heads`). Один автоматический свидетель каждого реального head transition. |

Все строки immutable, кроме двух разрешённых колонок head. Timestamps finite с
общим bounded UTC validator0077. Safe reason в correction/decision:
`[A-Za-z][A-Za-z0-9_]{0,127}`. Документ хранится именно BYTEA, SHA считается от
исходных байтов. Схема не хранит provider payload, URL response, exception или токен.

## Received date и исправление

Initial revision=1: worker, NULL member/reason/parent/evidence/decision/checksum.
Manual initial creation внеv1. Revision и head1 создаются одним root transaction;
без head revision не может закоммититься. Trusted initial worker authority всё ещё
требует отдельного явно утверждённого executor contract. Переданный actor enum
не удостоверяет worker и не разрешает запуск публичного writer.

V1 принимает только `received`. Effective time точно равен0077 run.received_at,
то есть receipt последней принятой страницы. Каждая принятая страница обязана иметь
ту же дату Europe/Moscow, равную business_date_msk. Page count сверяется с реальными
страницами; run должен быть complete/version1 с exact request bytes/hash и fixed
source/parser.0077 отдельно обеспечивает sealed page graph, manifest и все facts.
Cross-midnight complete source остаётся корректным current source, но daily не eligible.
NULL-only source_observed_at не превращается в provider timestamp. Будущий adapter
с настоящим source timestamp требует нового контракта до source_observed basis.

Correction: revision=parent+1, membership/reason обязательны, accepted immutable
proposal/decision и exact checksum. Source нового daily — именно after_run_id.
Перед вставкой revision под account lock читаются immutable proposal и decision,
затем блокируется head. Он должен оставаться proposal.before_daily_revision;
его immutable revision.source_run_id должен быть proposal.before_run_id. Проверяются
полные day/request bytes/context, accepted outcome и reviewed checksum.
Изменение head — `40001 wb_daily_head_conflict`, полный ROOT rollback.
Новая revision с тем же непосредственным source run недопустима. Ранее использованный
run не получает blanket запрета: отдельное допустимое evidence должно объяснять
новое реальное изменение от текущего parent; service replay не создаёт revision.

Head advance требует создания целевой revision в том же top-level transaction,
через сравнение xmin с pg_current_xact_id. Не используйте nested-savepoint insertion
для нового proposal+diff или revision+head. Вставка diff после commit proposal
отклоняется по такому же seal. Не выполняйте ранний SET CONSTRAINTS IMMEDIATE до
завершения полного графа. По окончании root deferred checks сверяют всё взаимно.

До и после correction оба source run должны оставаться eligible для одного
точного business_date_msk. Today's received run не заменяет прошлый день даже
при accepted decision. Не вводятся day-close cutoff, TTL, retention, reserve,
финансовое правило или автоматическое заполнение отсутствующего дня.
Missing coverage остаётся unknown, approval не создаёт отсутствующее независимое
evidence и не доказывает удаление товара провайдером.

## Канонические байты и полный diff

`wb_stock_evidence_row_bytes` кодирует только три presence/value пары, сохраняя
missing/null/value0. Native identity, display names и ignored raw attributes
не входят в count-only row hash. Raw source manifests остаются отдельной связью.

```text
["wb-stock-evidence-row/v1",[quantityPresence,quantityOrNull],
 [toClientPresence,toClientOrNull],[fromClientPresence,fromClientOrNull]]
identity = ["nmId",decimal_nm,"chrtId",decimal_chrt_or_"null","warehouseId",decimal_warehouse]
["wb-stock-evidence-diff/v1",[[identity,changeKind,beforeHashOrNull,afterHashOrNull],...]]
```

SQL FULL OUTER JOIN обоих scoped наборов `wb_stock_observations` сравнивает exact
row payload BYTEA, затем computes hashes. Nullable chrt сопоставляется NULL= NULL;
локальный join sentinel0 не записывается в таблицу и не попадает в identity/codec.
EXCEPT в обе стороны требует совпадения полного множества native identity/category/
before hash/after hash: omitted/additional/swapped/unchanged rows и cancelling
aggregate changes не могут пройти count-only проверкой. Counts и diff checksum
пересчитываются по всем детям; пустая proposal запрещена.

Сортировка diff — Python lexical STRING tuple order, SQL C collation по decimal
nm, decimal chrt либо literal null, decimal warehouse. Поэтому10 предшествует2.
Точная compact ASCII serialization вручную, без JSONB re-encoding и float casts.
`wb_daily_ascii` реализует Python ensure_ascii, lowercase escapes, quotes/backslash,
controls, DEL и non-BMP surrogate pairs. Unicode normalization отсутствует.
NUMERIC revisions/counts сериализуются `trunc(v)::text`, сохраняя большие целые.

Proposal helper воспроизводит fixed22-field array из EvidenceProposal.canonical_bytes:
tag, org, account, evidenceUUID, beforeRunUUID, beforeManifest, afterRunUUID,
afterManifest, sourceKind, parserVersion, grainVersion, requestChecksum,
businessDateISO, beforeDailyRevision, proposerMembership, proposalCommandUUID,
evidenceDocumentHash, reviewedEvidenceReference, diffHash, addedCount, removedCount,
changedCount. Здесь tag плюс21 значение; времён и physical diff row UUID нет.
DB сравнивает proposal_bytes byte-for-byte с пересчитанными typed данными,
сверяет SHA и document SHA; совпадение supplied digest само по себе не принимается.

UTF-8 document разрешает codec-valid bytes, включая embedded NUL, сохраняя исходный
BYTEA. Existing `review_strict_utf8` и `review_local_nonblank_utf8` проверяют UTF-8
и Python whitespace-only shape; TEXT decoding всего документа не выполняется.
Reference — nonempty PostgreSQL TEXT, без NUL по типу, без Python strip whitespace
на первом/последнем code point; остальной текст остаётся точным. Эти проверки
не идентифицируют credentials, PII или signed-URL secrets. Approved sanitization,
payload limits и retention отсутствуют и блокируют включение mutation API.
Synthetic fixture budget8192 не переносится в production schema как политика.

Восемь закреплённых vectors (SQL outputs ещё НЕ сверены):

| kind/name | bytes | SHA256 |
| --- | ---: | --- |
| row/presence | 71 | `f4ea6867f4fccadb8abc16a70810cf9cd6c505a09624cd9cfa3cdb972c277159` |
| row/bigint | 84 | `819c505db61829978ad518521fdf024a9cef2d70181bfe55a3848211f1732373` |
| row/all_zero | 64 | `aa51b376d372c5b6c012c6a15ce675c20d842ab242a4b9fe86d99a8ad443c801` |
| diff/empty | 32 | `696ae55f24d671ba6c46bfc479c7f6c7bfd3f83e7202401891d5ddc9e4f43d8e` |
| diff/lexical | 420 | `a5a05ae535279dee22a00464d19519aca5eb72c6ddcd8c1f2eb6b8f114dc4f7f` |
| diff/all_changes | 488 | `874da4647880427411c66f317ab0fc74ebef2e5680096a1d33063bd74ecab69d` |
| proposal/basic | 644 | `883dc3b64b4a184ff47eb7152e3da1b196783d88c6480dfdb55e25194033ccc8` |
| proposal/unicode | 673 | `3296b49d682f7571221d7cf1d3ed43c5a984c8898d49056497729cbc14a1e942` |

## Транзакции T2, replay и physical lock order

READ COMMITTED, один ROOT transaction. Live user/member/session locks (когда
применимы) → canonical WB account FOR UPDATE → immutable proposal/decision SELECT
→ mutable daily head FOR UPDATE. Строки proposal/decision имеют SELECT+INSERT,
не фиктивный UPDATE ради FOR UPDATE. Statement trigger нового DML берёт account
до tuple mutation, но read/replay сервис обязан сам взять account ДО первого
proposal/decision/head чтения. Locks живут до COMMIT. SQL RLS не удостоверяет caller.

1. Initial: future trusted worker guard → account → scoped existing command/run
   lookup и immutable intent comparison → при отсутствии generate UUID4 → INSERT
   daily revision1 (без created_at) → INSERT head1 (без updated_at) → COMMIT.
2. Proposal: fresh authorised user guard → account → existing owner/proposal_command_id
   lookup до генерации evidence UUID → exact existing intent/document/reference/run/
   bytes equality либо new UUID → load immutable before revision и оба complete run →
   compute full native diff → INSERT proposal без proposed_at → INSERT всех diff rows
   в том же top-level transaction → COMMIT с deferred equality.
3. Decision: fresh guard → account → existing scoped decision_command_id и proposal/
   existing decision SELECT → return original только при совпадении command/actor/
   outcome/reason/reviewed bytes intent; иначе INSERT immutable decision без decided_at.
   Один winner по owner/evidence UNIQUE, contrary intent конфликтует. Accepted или
   rejected terminal, без reopen/retract/appeal/expiry defaults.
4. Apply: fresh acting user guard → account → scoped daily command lookup/replay →
   proposal и accepted decision SELECT → exact head FOR UPDATE → INSERT next revision
   без created_at → head CAS по full day и прежним current_revision/version → COMMIT.
   Revision хранит acting member, который не обязан быть прежним reviewer: это
   policy decision сервиса, не переиспользование исторической reviewer authority.

Пример формы head CAS (параметры — только уже проверенные typed values):

```sql
UPDATE public.wb_stock_daily_heads
SET current_revision = :before_revision + 1, version = :before_revision + 1
WHERE organization_id = :org AND marketplace_account_id = :account
  AND stock_scope = 'wb_warehouse' AND request_checksum = :request_checksum
  AND business_date_msk = :business_date_msk
  AND current_revision = :before_revision AND version = :before_revision;
```

Rowcount!=1 или любой deferred/commit error должны выйти через ROOT rollback;
не коммитить decision/diff/revision после CAS loss в savepoint. Не делать upsert,
который выбирает новый parent или перезаписывает историю. Reapply принятого evidence
невозможен по exact head и UNIQUE consumption. Уже выполненный exact replay после
fresh auth возвращает original UUID/time/history, без INSERT audit/decision/diff/
daily. Миграция предоставляет ключи/неизменяемость, а не реализует T2 replay domain.
Hash match требует exact immutable intent/bytes equality. Не генерировать новый
UUID до scoped command lookup и не сравнивать original intent с новым UUID.

Только future authenticated T2 service реконструирует SourceRuns/RevisionEvidence
из реальных accepted rows и использует reference
`stock-evidence:<evidenceUUID>:<decisionUUID>`. Клиентский explanation reference
не является authority. SHA raw source и explanation не являются WB signature.

## Typed audit, privileges и rollback

Head INSERT/UPDATE автоматически вызывает `wb_daily_emit_audit`: DB audit UUID/time,
exact before/after revision/head, worker/NULL initial или acting membership correction.
Прямой audit INSERT отклоняется origin guard; SECURITY INVOKER audit INSERT grants
нужны только для этого trigger. На каждом новом relation deferred graph:
daily проверяет непрерывную revision/audit/head lineage и final head, evidence
проверяет оба sealed parent/full diff/bytes/decision. Audit-only или unaudited
transition не проходят commit. Proposal/decision сами immutable audit facts;
дополнительного mutable status, notification или queue не создаётся.

Точные новые scalar/read/codec EXECUTE signatures:

```text
wb_daily_integer(numeric,boolean)
wb_daily_whitespace(integer)
wb_daily_reference(text)
wb_daily_ascii(text)
wb_stock_evidence_row_bytes(public.wb_stock_observations)
wb_stock_evidence_diff_bytes(public.wb_stock_revision_evidence_diffs[])
wb_stock_evidence_proposal_bytes(public.wb_stock_revision_evidence)
wb_stock_evidence_expected_diff(integer,integer,uuid,uuid)
wb_daily_run_eligible(integer,integer,uuid,bytea,text,date)
```

Последние два — STABLE SECURITY INVOKER scoped reads под RLS; они не authenticate
caller. Остальные IMMUTABLE codecs/checks. Все search_path закреплены.
Existing exact dependencies: `wb_current_uuid(uuid)`, `wb_current_time(timestamptz)`,
`review_local_nonblank_utf8(bytea)`, `review_strict_utf8(bytea)`; родителей не меняли.
Trigger-only, без runtime/PUBLIC EXECUTE:
`wb_daily_lock()`, `wb_daily_immutable()`, `wb_daily_insert_guard()`,
`wb_daily_head_guard()`, `wb_daily_emit_audit()`, `wb_daily_audit_guard()`,
`wb_daily_graph()`, `wb_stock_evidence_graph()`.

Migration и новый0078 section `runtime-db-role.sql` scrubs nonowner table/column/
точные helper ACL, включая defaults при создании. Runtime получает SELECT шести
relations; INSERT только перечисленных в script колонок; generated created_at,
proposed_at, decided_at, updated_at, audit_id/occurred_at исключены. UUID4 корней
и commands подаёт trusted service после replay lookup. UPDATE только
`wb_stock_daily_heads(current_revision,version)`. Нет DELETE/TRUNCATE/history UPDATE,
SECURITY DEFINER bypass, новых operational roles, broader parent privileges,
backfill или source/current writer activation. SQL script не исполнялся.

Downgrade empty-only: row_security=off, sorted ACCESS EXCLUSIVE lock всех шести
relations, проверка любой строки, включая RLS-hidden proposed/rejected/audit/history.
Executor без BYPASSRLS получает отказ вместо filtered empty. Любые данные блокируют
downgrade; без CASCADE и без старых schema edits. До activation rollback оставляет
writer выключенным и immutable history сохранённой; legacy overwrite fallback нет.

## Открытые решения и финальные gates

Mutation APIs должны оставаться закрыты: не определены fixed proposal/read/accept/
reject/apply permissions, proposer self-review/four-eyes, acceptance invalidation,
explanation sanitization/limits/retention и trusted initial worker authority.
Не присваивать stock evidence существующий Orders sync permission, profile,
global account scope или caller-provided actor. Поздняя auth не выводится из
сохранённого reviewer FK. Independent source/storage work продолжает выполняться.

Не запускались tests/test-authoring/RED, review/critic, import/compile/lint,
PostgreSQL/Redis/Alembic execution, provider/network/production calls. По явному
source-first порядку пользователя gates отложены, не отменены:

- SQL byte/hash parity по всем восьми pinned inputs, включая non-BMP/combining
  Unicode/quotes/backslash, BIGINT, missing/null/zero и >BIGINT NUMERIC revision.
- Synthetic disposable PG FORCE RLS для всех операций/обоих account scopes,
  canonical owners/UUID/native identities, immutable sealing, UTF-8/NUL/whitespace,
  full diff omissions/additions/swaps/net-zero и complete source linkage.
- Same-day и cross-midnight/last-page date matrix, historical receipt substitution,
  две reviewer races и две head consumer races, fresh authority/revocation.
- Commit/audit failure atomicity, restart/readback/replay, stale CAS rollback,
  denied grants/direct audit/history writes, hidden-history downgrade refusal.
- Empty и production-shaped synthetic migration cycle, final source guards/
  compatibility, authenticated T2 service composition, отсутствие provider calls.

Ни один ранний test count другого пакета не подтверждает0078. Full Stage5, API
activation, provider completeness и final release readiness здесь не заявляются.
