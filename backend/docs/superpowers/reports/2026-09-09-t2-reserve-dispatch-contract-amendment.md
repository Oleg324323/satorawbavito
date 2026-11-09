# T2: exact reserve/dispatch contract для DDL T1

Дата: 2026-09-09. Ветка `codex/arch-t2-economics`, исходный HEAD `ee7bd6e`.
Ответ на committed T1 `d38e923` (`2026-09-08-t1-schema-contract-feedback.md`).
Также прочитан `2026-09-09-t1-schema-amendment-feedback.md`: он относится к T4;
из него учтена необходимость точных per-event actor/state/nullability matrices.
Это domain amendment на принятие T1. DDL и реальные persistence tests отсутствуют.

## 1. Ownership и команды

Canonical org/account/catalog/membership — положительные internal integers.
Проверка типа строго исключает bool, float и external account strings. Approval
ID сохраняет существующее exact text представление, не переводится в UUID.
Все команды имеют keyword-only `scope: ApprovalRepositoryScope(org, account, approval)`.
Account должен принадлежать org; CatalogSku, membership и account связи доказываются
composite FK + авторизацией сервиса. Bridge проверяет идентичности, но не наличие
membership, permission или разрешённого account в БД.

Точные сигнатуры находятся в `app/modules/wb_repricing_repository.py`:

| Команда | Вход кроме scope | Возврат после успешного commit |
| --- | --- | --- |
| get | — | snapshot или None |
| create_intent | request, actor, now | текущий snapshot; new pending V=0 |
| claim | expected_version, actor, now | applying snapshot V+1 |
| reject | expected_version, actor, reason_code, now | rejected snapshot V+1 |
| block | expected_version, actor, safe_blocker_code, now | blocked snapshot V+1 |
| reserve_attempt | expected_version, actor, now | ApplyAttempt reserved A=0 |
| mark_dispatch | attempt_id, expected_version, expected_attempt_version, actor, now | ApplyAttempt dispatched A=1; только winner |
| record_attempt_outcome | attempt_id, expected_version, expected_attempt_version, outcome, now | AttemptResult(approval, attempt) |

`now` — aware datetime из доверенного transaction clock адаптера, не JSON клиента.
Runtime implementation должен проверять authorization непосредственно перед
intent/claim/reserve/dispatch. Actor reserve/dispatch равен исходному claimant и
остаётся активным членом org с разрешением на этот account. Нельзя выдумывать
membership для scheduler. До согласованного authenticated execution principal
scheduler не получает автономное право apply; manual/scheduler используют одного owner.

`record_attempt_outcome` доступен trusted worker/recovery service, не public route
без проверки. Worker authorization связывает queue org/account/approval/attempt
с хранимым intent; не принимает credentials из queue. Аудит worker имеет NULL
membership, исходный claimant в approval/attempt остаётся неизменным.

Старый `insert` — только backfill, с imported audit и `request_format=legacy`.
Старый `record_outcome` оставлен для совместимости pure Protocol, но будущий
repository обязан делегировать в новый outcome path только для marked attempt A=1,
проверяя неизменность всех identity/intent/claim полей переданного snapshot. Он не
может служить обходом dispatch или вставлять произвольный terminal snapshot.

## 2. Attempt identity, version и transitions

Ровно 0 или 1 attempt на scoped approval за всю эту волну. Повторные попытки,
новые attempt после failed/ambiguous, leases, renew, automatic resend отсутствуют.
Нет lease expiry, освобождающего marker. Pre-marker работу можно продолжить на
той же reservation, конкурируя за один атомарный marker; post-marker неопределённость
закрывается как ambiguous. Внешний exactly-once не гарантируется.

Repository генерирует random UUID4 на стороне сервера внутри reserve workflow.
Canonical domain representation — lowercase UUID4 с дефисами, physical UUID допустим.
ID не выбирает browser, queue или pure function. Pure constructor получает его
параметром для детерминированных тестов. При гонке insert UNIQUE на scoped approval
оставляет один ID; проигравшая транзакция перечитывает существующую reservation,
проверяет неизменность binding и возвращает её только пока reserved. Возврат
существующей reservation не даёт права send и не создаёт второго audit.

Пусть pending имеет V. Claim переводит approval в applying V+1. Reserve и dispatch
оставляют **approval.version=V+1** и approval.updated_at неизменными, изменяя attempt.
Outcome меняет approval.version на V+2. `claim_version` attempt всегда равен V+1.
`expected_version` в reserve/dispatch/outcome — именно applying V+1, не pending V.

| Attempt state | A | dispatch_at | finished_at | safe_error | upload/result | Next |
| --- | --- | --- | --- | --- | --- | --- |
| reserved | 0 | NULL | NULL | NULL | NULL/NULL | dispatched или failed |
| dispatched | 1 | NOT NULL | NULL | NULL | NULL/NULL | applied/failed/ambiguous |
| applied | 2 | NOT NULL | NOT NULL | NULL | оба NOT NULL | terminal |
| failed, до marker | 1 | NULL | NOT NULL | из восьми кодов | NULL/NULL | terminal |
| failed, после marker | 2 | NOT NULL | NOT NULL | только WB_APPLY_REJECTED | NULL/NULL | terminal |
| ambiguous | 2 | NOT NULL | NOT NULL | из восьми кодов | NULL/NULL | terminal |

После marker код `WB_APPLY_REJECTED` означает **подтверждённое непринятие** всего
single-row запроса; generic HTTP error, timeout, 5xx, revoke или exception не являются
этим доказательством. Будущий adapter отвечает за классификацию ответа и проверяемое
непринятие; pure API не умеет аутентифицировать evidence. До marker failed означает,
что external call ещё не разрешался. Даже failed terminal не разрешает retry.

attempt.reserved_at NOT NULL и immutable; updated_at NOT NULL; dispatch_at write-once.
В reserved updated_at=reserved_at; в dispatched updated_at=dispatch_at.
reserved_at <= dispatch_at <= finished_at (с учётом NULL), updated_at не убывает,
в terminal updated_at=finished_at. Result поля выставляются один раз вместе с terminal.
Scope, attempt_id, action_key, request_checksum, claim_version, claimant неизменны.
approved result требует upload ID; термин applied сохраняет существующую семантику
kernel. Если есть только acceptance без доказанного выбранного result mapping,
adapter не должен изобретать подтверждение изменения цены.

Dispatch key — lowercase SHA256 от ASCII JSON массива без пробелов:
`["wb-price-dispatch/v1", org:int, account:int, approval_id:str, action_key:str, attempt_id:str]`.
Он вычислим уже при reserve, неизменен; наличие key не равно наличию marker.
Marker — `dispatch_at IS NOT NULL` и состояние dispatched/terminal с соответствующей A.
Unique scoped action_key в approvals, unique scoped approval в attempts и unique
scoped dispatch_key в attempts. Dispatch key не объявляется WB idempotency key.

## 3. Canonical request bytes, деньги и replay

`CanonicalApplyRequest` в `app/modules/wb_repricing_dispatch.py` задаёт single-row
WB upload intent. Schema literal `wb-price-apply/v1`, ровно следующие JSON keys:

`accountId, approvalId, articleId, catalogSkuId, discountPct, minPriceKopecks,
nmId, organizationId, priceKopecks, schema, sizeId`.

org/account/nm/price — required integers; catalog/size/min — integer или explicit
JSON null; discount — int 0..99. Bool/float/строковые числа запрещены. price/min
минимум 50 копеек: меньшая positive цена даёт нулевой integer-ruble upload и блокируется
при создании **нового v1 request**, не исправляется округлением kernel/backfill.
Верхней бизнес-границы цены не вводится; полный размер canonical bytes <=4096.
article — exact nonblank без surrounding whitespace/control chars. В single row
sizeId — native WB sizeID, не CatalogSku и не nmId. None sizeId означает product scope.

Serializer: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True,
allow_nan=False).encode("ascii")`; без BOM/newline; Unicode — стандартные JSON escapes,
без Unicode normalization. Request SHA256 вычисляется по **всем этим байтам**, а не
только цене или provider body. Bytes хранить неизменными (bytea), hash отдельно.
Запрещено менять checksum ради backfill/replay. Прежний `build_action_key` неизменен.
`bind_request` сверяет scope/catalog/nm/article/price/hash; stored bytes также
сравниваются byte-for-byte при replay, независимо от совпадения hash.

Provider bytes имеют только `{"data":[row]}`. Row: `nmID`, `price`, `discount` и
необязательные `sizeID`, `minPrice`. Price/min преобразуются positive integer
HALF_UP `(kopecks+50)//100`, как текущий `wb22_apply._build_upload_payload` через
`price_units.kopecks_to_wb_price_rubles`. Null optional fields в provider body
пропускаются. Та же canonical JSON encoding; request checksum относится к
внутреннему v1 envelope. Конвертация единиц не меняет formula или current flow.
Current one-request/one-approval send запрещено batch-склеивать без нового контракта.

Credentials, headers, URL, raw request/response dict и exception text отсутствуют
в typed request. Request содержит коммерческие article/price; он не попадает в
общие логи/ошибки. Audit ссылается на approval, не дублирует body. Safe exceptions
выводят название нарушения, не данные request. Retention не выбран этой реализацией.

Новый create_intent: pending V0 с v1 bytes/hash. Same scoped approval + same exact
bytes → вернуть current snapshot, ноль writes/audit. Любое различие immutable intent
→ conflict, даже если action key отличается из-за checksum. Key reuse с иным payload
также conflict. Replay после terminal не открывает approval и не создаёт attempt.

Legacy импорт: request_format=legacy, canonical_request_bytes=NULL, прежние
checksum/action key сохраняются. Даже pending legacy не проходит claim/reserve/dispatch
в этой волне. Unresolved internal identity вообще не импортируется в scoped canonical
rows; остаётся blocker с исходным legacy evidence. Схема не должна выдумывать identity.

## 4. Совместимость safe codes

В persisted approvals reason/safe_error/result сохраняется grammar kernel:
`[A-Za-z][A-Za-z0-9_]{0,127}`. SQL не сужает legacy codes до новой allowlist.
Kernel также допускает result_code=NULL у hydrated applied, хотя новый success command
требует result. Эти legacy строки допустимы в approvals, но не создают v1 attempts.

Новые attempt failures/outcome audit принимают только:

`INTERNAL_APPLY_ERROR`, `WB_APPLY_AUTHORIZATION_FAILED`, `WB_APPLY_RATE_LIMITED`,
`WB_APPLY_REJECTED`, `WB_APPLY_TIMEOUT`, `WB_APPLY_TRANSPORT_ERROR`,
`WB_APPLY_VALIDATION_FAILED`, `WB_RESULT_UNAVAILABLE`.

Дополнительное правило post-marker failed указано в матрице. New applied result
обязателен и удовлетворяет grammar; raw exception никогда не становится кодом.
Ambiguous terminal в kernel/attempt этой волны. Append-only reconciliation evidence
и resolution — следующий отдельно согласованный slice; текущий API не разрешает
ambiguous -> applied и не обещает automated history polling.

## 5. Точная audit matrix

`ApprovalAuditEvent`: scope, kind, actor_kind, actor_membership_id, before_status,
after_status, before_version, after_version, occurred_at и nullable attempt_id,
before_attempt_version, after_attempt_version, reason_code, safe_error_code,
wb_upload_id, result_code. Произвольного metadata JSON/text нет. T1 может назначить
физический audit PK. Все audit append-only, runtime UPDATE/DELETE запрещены.

M = membership: required positive ID + org composite FK. W = repricer_worker:
membership NULL. B = backfill: membership NULL, migration/import principal only.
`—` означает SQL NULL, а не empty string. Все неуказанные metadata поля — NULL.

| kind | actor | approval before→after | approval version | attempt ID / A | metadata |
| --- | --- | --- | --- | --- | --- |
| approval.created | M | —→pending | —→0 | — / —→— | все NULL |
| approval.imported | B | —→исходный status | —→исходная V | — / —→— | все NULL; ссылка на imported snapshot |
| approval.claimed | M | pending→applying | V→V+1 | — / —→— | все NULL |
| approval.rejected | M | pending→rejected | V→V+1 | — / —→— | reason required |
| approval.blocked | M | pending→blocked | V→V+1 | — / —→— | reason required |
| attempt.reserved | M | applying→applying | V→V | required / —→0 | все NULL |
| attempt.dispatched | M | applying→applying | V→V | required / 0→1 | все NULL |
| apply.succeeded | W | applying→applied | V→V+1 | required / 1→2 | upload + result required |
| apply.failed pre-marker | W | applying→failed | V→V+1 | required / 0→1 | safe_error required |
| apply.failed post-marker | W | applying→failed | V→V+1 | required / 1→2 | safe_error=WB_APPLY_REJECTED |
| apply.ambiguous | W | applying→ambiguous | V→V+1 | required / 1→2 | safe_error required |

Audit outcome/reason/upload/result равны результату соответствующей transaction.
created actor — фактический создатель; claim actor становится claimant;
reject/block actor становится decider; reserve/dispatch actor равен claimant.
Импорт не выдумывает автора; imported audit ссылается на строку, чьи original
claim/decision actor IDs сохранены только если canonical identity доказана.

Logical audit uniqueness: `(org,account,approval,kind,after_version,attempt_id,
after_attempt_version)` с NULLS NOT DISTINCT (или эквивалентный immutable normalized key).
No-op intent/reserve replay не создаёт event. Stale/closed/failed CAS не создаёт event.
Audit occurred_at равен trusted command transaction time. UI читает sanitized
projection; neither raw bodies nor tokens are audit fields.

## 6. Конкретное DDL поручение T1

Расширить исходный запрос approvals/attempts/audit следующими constraints, не менять
общую архитектуру и не добавлять state repository/outbox/event bus:

1. Approvals: исходные snapshot поля; `request_format IN ('legacy','wb-price-apply/v1')`;
   v1 требует bytea canonical_request_bytes 1..4096 и SHA256 equality, legacy требует
   bytes NULL. Unique `(org,account,approval_id)` и `(org,account,action_key)`.
   Immutable owner/intent/hash/key/created_at; version/status/metadata constraints
   kernel, включая legacy-compatible safe grammar и optional applied result.
   Новый v1 insert только pending V0. `catalog_sku_id` nullable internal composite FK.
2. Attempts: поля ApplyAttempt + stored computed dispatch_key; mutable только
   status/version/updated_at/dispatch_at/finished_at/safe_error/upload/result согласно
   матрице. Unique scoped approval (одна attempt), unique scoped attempt_id и
   dispatch_key; scoped approval FK с binding action_key/request_checksum/claim_version
   и claimant alignment. Реализацию cross-row invariant T1 выбирает сам; состояние
   approval applying + active attempt и terminal outcome + terminal attempt должно
   совпадать на commit. Legacy approvals не имеют attempts.
3. Audit: exact vocabulary/matrix выше, immutable append-only, scoped approval/attempt
   FK и membership composite org FK. Обязательное совпадение mutation/event в одной
   transaction; никакой async best-effort audit. Immutable event uniqueness выше.
4. FORCE RLS по org/account на всех трёх таблицах; runtime NOSUPERUSER/NOBYPASSRLS,
   scoped FK, grants после shared default grants. T1 владеет transaction-local
   context helpers. NULL/missing/foreign context fail closed. Helpers не заменяют auth.
5. Pending-claim index `(org,account,created_at,approval_id) WHERE status='pending'`
   для v1; recovery scan index account/status/updated_at для dispatched attempts.
   Никакого scheduler enablement этой миграцией. Миграционный номер выделяет T1.
6. Backfill synthetic-only тест сохраняет price/hash/key/status/IDs; unresolved
   identity блокируется, не исправляется guessed account. Downgrade empty-only
   guard всех трёх таблиц, не удалять evidence ради downgrade.

Денежные/version/nm поля: SQL должен сохранять область допустимых значений kernel
и импортов. Не сужать Python positive integers до int32; если T1 выбирает bigint,
нужен явный проверенный preflight на overflow, а не truncation. Numeric(без дробной
части через CHECK) позволяет сохранить широкий existing contract. Internal FK IDs
используют реальные типы canonical tables, exact positivity checks.

## 7. Atomic boundaries и acceptance tests (PostgreSQL: NOT RUN)

Intent+created audit → claim+claimed audit → reserve+reserved audit →
marker+dispatched audit → provider call вне DB locks → result+terminal audit.
Pure snapshots не доказывают durable commit и не являются send permission.

**Atomic mark predicate:** совпадают org/account/approval_id, applying status,
expected approval version, v1 immutable request/hash/action key, attempt_id,
attempt.claim_version, expected attempt version=0, reserved status, NULL dispatch_at,
claimant и свежая authorisation. UPDATE attempt 0→1 + audit в одной transaction;
approval должен быть защищён от concurrent terminal update row lock/CAS transaction.
Только успешный первый commit возвращает dispatch success. Повторный marker call
всегда conflict (не idempotent success). Uncertain commit → no send со стороны
caller, чтение/recovery; marker не снимается. `reserve_attempt` replay возвращает
reserved state, но не marker permission.

T1/T2 следующий этап обязан исполнить с двумя настоящими DB sessions:

| Test | Barrier/injection | Ожидание |
| --- | --- | --- |
| claim race | обе прочитали pending V | один CAS winner, один conflict, один claim audit |
| reserve race | обе видят applying без attempt | одна строка/UUID4, один reserved audit |
| dispatch race | обе видят reserved A0 | один marker winner, один conflict, один fake call |
| outcome/dispatch race | pre-marker failed vs marker | только один legal path; никогда failed без marker + send |
| stale/different scope | тот же approval UUID в двух accounts/org | 0 matched rows, 0 fake calls, 0 audit |
| payload/key reuse | изменить discount/size/min при same approval | conflict, stored bytes/hash/key неизменны |
| audit failure | ошибка INSERT audit в каждой mutation | rollback approval+attempt, 0 partial records |
| restart before intent | transaction не committed | 0 calls; authorized identical intent insert возможен |
| restart intent/claim/reserve | durable prefix без marker | same reservation; fresh scoped authorization и exclusive marker |
| crash after marker before POST | marker есть, исход неизвестен | ambiguous; 0 automatic resend |
| crash after acceptance before result | fake принял, commit результата нет | ambiguous; максимум исходный fake call |
| duplicate after result before ACK | terminal rows persisted | read+ACK, 0 новых calls/events |
| rollback result transaction | approval/attempt/audit failure | сохраняется marked prefix, recovery без resend |
| closed immutable | все commands на terminal | conflict/no writes, including ambiguous→applied |
| RLS runtime role | missing/foreign org/account context | read/write denied; FK нельзя обойти UUID-only lookup |
| synthetic legacy backfill | повтор/foreign/unresolved identity | exact preservation или blocker; no new v1 request |
| shadow | все shadow paths | 0 claim, 0 reserve/marker, 0 calls |

Неизвестный исход после marker нельзя автоматически признать непринятием. Exactly-once
external effect не обещается. Legacy writer fence и authorization/resolver integration
проверяются отдельно перед включением current consumers/concurrency.

## 8. Проверка этой поставки

TDD: новый test module сначала дал ModuleNotFoundError (exit 2), затем focused
new+existing kernel+repository suite: 143 passed (exit 0). Это pure/offline tests.
Расширенные negative hydration/audit cases дали RED 3 failed / 59 passed; после
исправлений combined suite — 155 passed, exit 0. Независимый critic подтвердил
enum/constructor gaps: strict enum и единые post-marker guards исправлены вместе
с regression tests. Compileall и diff-check прошли. Ruff отсутствует в используемом
wave1-integration venv (`No module named ruff`), lint pass не заявляется.
Повторный independent critic: оба замечания закрыты, новых blockers для передачи
domain amendment T1 нет. Финальный expanded pure group (dispatch/repository/kernel/
price-input/source-revision/source-grain): **232 passed, exit 0**.
Follow-up coordinator review предоставил Ruff runtime
`/tmp/satorna-backend-verify-20260908/bin/python`: новые import-order findings и
UTC-alias style исправлены без изменения domain semantics; targeted Ruff check
четырёх dispatch/size-test файлов — exit0, повторный kernel/repository/dispatch
group — 155 passed exit0. Legacy runtime-wide lint baseline этим не объявлен зелёным.
Runtime flow, flags, P&L formulas, migrations, DB/Redis/provider calls не менялись.

## 9. Requirement → commit → tests → remaining (T2)

| Исходный этап | Уже готово | Доказательство | Осталось |
| --- | --- | --- | --- |
| 1 writers/identity/schema contract | 769f3cf + этот amendment | 65 kernel + 28 bridge + 62 dispatch tests | Принятие точного DDL T1 |
| 2 durable approvals | Protocol + exact DDL request | pure only, PostgreSQL NOT RUN | Accepted migrations, repository/service, real sessions/CAS/rollback/RLS/backfill |
| 3 jobs/attempts/crash | этот amendment + ee7bd6e crash acceptance | pure transitions/metadata, no send | Durable marker service, jobs/resolver, recovery и fake-provider crash matrix |
| 4 settings/globals | 3ce5ee4 + 61add2e + 2761f73 characterization/context | 80 legacy characterization cases | Domain settings rows, immutable calculation context wiring, one-writer fence |
| 5 prices/stocks | 031c110 + 2761f73 + ee7bd6e | malformed-page, price guard, grain tests | Account-owned durable complete manifests/snapshots/daily и typed consumers |
| 6 KTR/revisions | 94b540f + ee7bd6e | SourceDiff tests, version/evidence contract | Proven local/all orders grain, reference ranges/effective dates; unknown remains null |
| 7 final profit | 94b540f decision package | synthetic options, no final formula chosen | Явный approval финансовых правил; netProfitKopecks/profitClass/abcCode остаются null |
| 8 test debt/handoff | 3ce5ee4,031c110,61add2e,5803e93,2761f73,ee7bd6e | предыдущий scoped group 412 passed | Новые DB/service gates и дальнейшая baseline regression по назначенным failures |

До принятия approvals DDL independently доступны точные price/stock manifest contracts,
safe source adapters и source/economics test debt. Orders DDL не разблокирует repricer.
Никакой ready-for-production или all-stages-complete статус этой таблицей не заявлен.
# PostgreSQL text amendment — 2026-09-09

All approval/kernel/scope/request/outcome text must be PostgreSQL UTF-8 representable:
U+0000 and U+D800..U+DFFF are rejected before action-key/request hashing or transition.
No normalization, stripping, replacement, truncation or new business length limit.
Ordinary Cyrillic, combining characters and supplementary Unicode remain exact;
existing valid canonical bytes/checksums/action keys are unchanged. Existing request
4096-byte and machine-code grammar limits remain unchanged. Invalid legacy identities
stay blocked; never sanitize then recompute their hash to force an import.

Attempt UUID4 uses exact lowercase canonical lexical validation before parsing can
echo malformed input. Validation errors contain no supplied text or chained parser
exception. This protects build_dispatch_key, reserve/hydration and audit boundaries.
New regression `tests/test_wb_repricing_postgres_text.py`: 50 expected failures /6pass
before text fix; critic UUID regression16fail/56pass; final72 tests pass, combined
kernel/repository/dispatch227pass. Ruff, compileall and diff checks exit0. These are
pure validation checks, not PostgreSQL migration/persistence acceptance.
