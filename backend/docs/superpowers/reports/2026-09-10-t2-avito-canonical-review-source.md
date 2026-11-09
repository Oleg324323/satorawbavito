# T2: существующая canonical Avito Review source boundary

## Доказанные компоненты, без нового metadata endpoint

| Existing path | Поддержка / граница |
|---|---|
| `canonical_contract.normalize_avito_review` | org/account/provider/exact external identity; точный text/null/empty; rating, answered, can_answer, source_status, created/updated, source/run/normalization versions |
| `review_fact_checksum` | Существующий hash включает scope и semantic fields; не включает observed_at/run id. Не менять алгоритм или source schema ради совпадения |
| `canonical_orm.CanonicalReviewObservationRow` | exact text + UTF-8 pair, rating/status/time/answered, immutable revisions; answer body/id/status не хранятся |
| `ReviewFactsRepository(ReviewOwner(...,'avito',...))` | generic org/account/provider-bound get/reserve/ingest, CAS version, revision, source-order conflict и historical binding fences |
| `local_service.read_local_review_context` | Уже поддерживает Avito. `reviews:read`, exact account context, shared guard до physical commit, без provider credentials |
| `local_http /api/v2/reviews/local/context` | Для известного факта требует одновременно internal review UUID и external_review_id; возвращает exact text/observation/checksum/answered/canAnswer |
| `local_repository.source` | Проверяет UUID↔external id и исторический account binding; ambiguous source отвергается, не становится current |
| `canonical_read /api/v2/reviews/wb/fact` | Только WB; не нужно копировать этот endpoint ради уже существующего Avito local context |
| `shadow_service`, `canonical_sync` | Действующий guarded producer WB-only: wb_api credential, WB DTO types, normalize_wb_review; Avito нельзя подставить через cast |
| `review_job_authority` | Send/attempt authority для WB+Avito, не permission публиковать source observations |

Ответ продавца намеренно не является canonical source body: существующий тест
`test_review_canonical_contract.py::test_received_wb_and_avito_dto_shapes_normalize_without_adapter_calls`
проверяет отсутствие `answer` в fact. Поэтому изменение только answer.text/id/status
при неизменном answered не меняет canonical content checksum. Нельзя выдавать это
за полную версию provider response или хранить source answer в local AI draft.

## Конкретный missing decoder

Raw Avito fixture в `tests/test_avito_reviews.py` содержит native `createdAt` как
Unix seconds и `item.id` вложенно. Canonical normalizer ожидает aware datetime/ISO
и flattened itemId. Legacy `LiveAvitoReviewsClient._review_row` выполняет bridge,
но одновременно подменяет missing text → empty, missing score → 5, clamps score,
coerces bool и time. Он не подходит для canonical facts без strict raw admission.

Минимальная pure граница: `canonical_avito_decode.decode_avito_review(raw, *,
organization_id, marketplace_account_id, source_run_id, observed_at)` возвращает
существующий `NormalizedReviewFact` через существующий normalizer, а не новый
repository или новую source-authority сущность. Native time → aware UTC; nested
item.id → itemId; text сохраняется побайтно в существующей semantic UTF-8 модели.
Никаких network/clock/repository/global state/serialization side effects.

Обязательные ограничения нового raw decoder:

- native IDs принимаются без bool/float/coercion; opaque strings/leading zeros
  сохраняются по existing identity contract, не считаются internal IDs;
- invalid rating/text/canAnswer не исправляются; missing nullable поля остаются null;
- `answer: null` — доказанное answered=false, объект с проверенным id — true;
  отсутствующее поле answer не доказывает отсутствие ответа и блокирует raw admission;
- existing normalized fixtures и normalizer defaults не изменяются;
- неподтверждённые owner/update aliases блокируются; источник updated time не
  синтезируется из observed_at/createdAt/порядка fetch;
- caller org/account/run/observed_at проходят existing contract validation, но
  сами по себе **не являются authenticated authority**;
- answer body, buyer name, title, images не проходят в canonical fact; это явная
  неполнота модели, а не потеря незаметно обещанных полей.

## Минимальное будущее подключение

```text
authenticated original actor + exact encrypted Avito account
  -> explicit reviews:write source owner/guard (ещё не подключён)
  -> durable reserve_run before fetch, exact paired binding
  -> bounded raw source fetch, outside transaction
  -> pure decoder -> existing normalize_avito_review
  -> same captured authority revalidation + generic repository ingest/CAS
  -> partial immutable observations/run receipt
  -> existing /reviews/local/context with reviews:read and both IDs
```

Это reuse plan, не реализованная публикация. Текущая работа не расширяет WB
ticket, не создаёт fake worker principal, не использует cabinet:read/preview как
publish permission и не выдаёт request metadata за guard.

Для полноценного list/table потребуется bounded projection существующих
review_observations.rating/source_status и scoped discovery internal UUID —
`ReviewFactSnapshot`/local context сейчас этих display fields не возвращают.
Для exact source answer body/version существующей колонки/semantic hash нет:
нужен отдельный ownership/schema slice T1+Reviews owner, не guessed JSON reuse.
Plain-text безопасный rendering/стандартная account isolation входят в уже
утверждённый продукт, отдельное бизнес-согласование на них не требуется.

Source updated time неизвестен: остаётся null. Repository использует свой
run_sequence и уже существующие known-time/history fences; receipt sequence не
называется provider freshness и не разрешает send по stale/ambiguous evidence.

## Проверки и ограничения текущего slice

Existing canonical contract: **38 passed, 0.20s, exit 0**, offline Python3.11.
Новый raw decoder реализован после explicit ROOT approval ровно в одном pure
module, с отдельным test file. RED `ModuleNotFoundError` до создания module;
GREEN **81 passed, 0.24s, exit 0** (43 new + 38 existing canonical contract).
Literal raw/native mapping сопоставлен с независимо составленной existing
normalized формой и тем же semantic checksum. Проверены null/empty/whitespace,
Unicode/NUL + existing UTF-8 codec roundtrip, source/run hash distinctions,
exact IDs, missing answer blockade, malformed native input, safe exception context.
Изменение только source answer text намеренно оставляет existing checksum тем же:
тест доказывает limitation, не новую answer revision guarantee.

Decoder принимает только dict/raw native shape, internal positive int4 owner data,
positive integer Unix seconds <=253402300799; conflicting normalized identity,
source-version и update aliases отклоняются. Это raw adapter admission, не
изменение existing canonical normalizer или historical keys/checksums.

Ruff/compile/diff проверяются перед commit. Первичный Ruff нашёл deliberate naive
datetime в negative test; тот же naive input построен через `replace(tzinfo=None)`,
защитная проверка не ослаблена. Внутренний critic pass: authority не создаётся,
new context endpoint не нужен, source answer не подменяется AI draft, missing
source time не восстанавливается из runtime clock. PostgreSQL/provider jobs не
запускались; schema/guards/local workflow/legacy/flags не менялись.

## Следующий согласованный slice: received-page publisher

Добавлены отдельные Avito definitions в `shadow_service.py`; девять прежних WB
definitions неизменны по AST. Нет HTTP caller, router registration, config,
новых grants или DDL. Pure decoder выше остаётся без изменений.

`begin_avito_review_shadow` получает original authenticated actor и paired fetch
binding, проверяет existing exact-org/account review-shadow gate и резервирует
run через existing repository. Authority — только public user-session guard с
фиксированным `reviews:write`, exact `avito_oauth_access` ID/generation/schema/
expiry. `cabinet:read` недостаточно; ticket — internal correlation data, не bearer
token, queue principal или доказательство разрешения.

```text
begin: original principal + paired binding + exact feature gate
  -> fresh root -> live guard/account lock -> reserve_run -> physical commit
  -> immutable ticket; transaction and account lock released
caller boundary: fetch is NOT implemented here
publish: same ticket/original principal, received tuple of <=50 native rows
  -> fresh root -> live guard + captured incarnation
  -> strict decoder -> existing repository/CAS -> partial manifest
  -> private incarnation fence -> shared final guard -> physical commit
  -> receipt returned only after commit
```

Coverage строго partial, даже для empty page; нет caller-supplied completeness,
inference об удалённых reviews или обещания полной pagination. Повторное содержимое
того же run проверяется existing manifest replay; другое содержимое — conflict.
После rollback публикации сохраняется ранее committed running reservation.
Автоматическое восстановление/повторная выдача authority после restart не реализованы.

### Physical commit fence: условный Core-only контракт

T1 подтвердил: shared credential guard удерживает account lock до commit, но
не сравнивает captured ingestion incarnation для ExpectedCredential. Поэтому
producer-private listener проверяет incarnation дополнительно. Он установлен
перед public acquire на private Session; shared hook не вызывается вручную,
не меняется и не переставляется. Effective dispatch обязан оканчиваться ровно
`(private fence, existing shared final)`. Pending ORM new/dirty/deleted блокируется
до SELECT; Session/callback не передаётся наружу. Shared final flush должен быть
пустым, иначе это не поддерживаемая композиция.

```text
late class listener -> incarnation changes -> private fence detects -> rollback
private fence -> unexpected listener -> shared final
  rejected by dispatch-tail check BEFORE unexpected listener executes
class listener queues ORM -> private fence rejects pending state -> no flush
other transaction changes locked account -> waits for current physical commit
```

Это узкое coupling к текущему shared hook identity/order, не generic registry.
Изменение listener topology требует повторной проверки, а не удаления fence.
Generic restricted-role fixture не доказывает готовность финального API-role:
его grants проверяет T1 отдельно. Runtime source route/fetch/activation остаются
за пределами этого commit; нельзя объявлять full Reviews cutover завершённым.

### Текущая проверка producer

RED: 8 missing page helper tests; затем 2 missing begin/publish tests при 8 PASS.
GREEN offline: **53 passed, 0.59s, exit 0** (10 producer + 43 decoder).
Ruff и compileall exact source/two new tests: exit 0. Старые WB definitions:
**9 unchanged**, AST comparison с parent. PostgreSQL suite: **17 collected,
0.80s, exit 0**; результат исполнения фиксируется отдельно после выделенного
ROOT serialized slot. Provider calls, реальные keys и production не используются.

Дополнительный bounded offline regression: producer, decoder, existing canonical
contract, review-shadow rollout и existing approval kernel — **229 passed, 0.77s,
exit 0**. Это не полный backend suite и не PostgreSQL evidence.

Первый allocated PG17: **17 setup errors, 5.33s, exit 1**, test bodies не
выполнялись. Новый synthetic fixture вызвал private resolver без tenant context;
RLS корректно скрыл account (`credential_account_not_found`). Existing production
wrapper задаёт context перед этим helper. Исправлен только fixture: exact actor
organization перед paired resolution; guards/RLS/source не ослаблены. Allocator
завершил exact owned DB/role cleanup и absence assertions без teardown errors.
Результат retry нельзя выводить из этого setup failure.

Второй PG17: **14 failed / 3 passed, 9.99s, exit 1**. Reservation блокировался
на SQL privilege boundary. Отдельный allocated diagnostic exact1: **1 failed,
3.77s, exit 1**, только `received-review-source SQLSTATE=42501`, без SQL/params/
raw exception. T1 подтвердил недостающие fixture-only EXECUTE для существующих
0068 PURE2: `review_binding_ascii_string(text)` и
`review_run_binding_bytes(integer,integer,text,text,text)`. Они выдаются только
owned disposable role; никаких trigger/all-functions/операционных grants.
Причина: migration выбирает уже имеющих права writers, reusable Stats fixture
выдаёт table rights позже. Обе попытки завершили explicit owned cleanup/absence.

Отдельно PG обнаружил реальный error-boundary дефект: raise внутри генераторного
context manager сохраняет exception context через `contextlib.throw`, даже после
внутреннего except. Pure regression: **1 failed / 10 passed, 0.52s**. Теперь
collector сохраняет только safe code; ordinary begin/publish frame поднимает
новую ошибку после выхода из `with`. GREEN: **11 passed, 0.48s, exit 0**.
Это не изменение shared guard и не подавление DB ошибки успешным fallback.

Следующий frozen PG17 после PURE2/error-boundary: **13 failed / 4 passed, 4.62s,
exit 1**, SQLSTATE42501 всё ещё на reservation. Cleanup/absence подтверждены.
Нельзя считать begin commit-failure test причинным доказательством: до callback
могла сработать ранняя ACL ошибка. Повтор PG17 приостановлен до полного exact
fixture dependency assessment Т1; broad grants и superuser fallback запрещены.

Catalog-only diagnostic exact1: **1 failed, 4.70s, exit 1**. EXECUTE false только
для двух 0065 helpers, true для двух 0068 helpers; run SELECT/INSERT и account
SELECT/UPDATE true. T1 подтвердил полный source dependency set PURE4. Fixture
дополнен только недостающими 0065 EXECUTE, без grants на trigger functions или
изменения operational ACL. В тех же PG17 отрицательные commit/class tests теперь
обязаны доказать вызов нужного callback, чтобы ранняя unrelated ошибка не дала
ложный PASS. Ещё один PG17 — только по отдельному ROOT slot.

PG17 с PURE4: **16 passed / 1 failed, 5.95s, exit 1**, все catalog privileges
true, happy reserve→publish→replay→existing local context прошёл. Remaining
incarnation fixture вручную менял защищённый version; 0076 trigger правильно
отверг UPDATE. Late class test также не доказывал private fence, если его UPDATE
раньше отверг trigger. Исправлены только stimuli: legitimate external binding
roundtrip с возвратом descriptor, assert version=captured+2, callback completion
и точный AUTHORITY_CHANGED outcome. Нет прямой записи version/отключения trigger.

### Финальный gate и handoff

**PG17: 17 passed, 3.86s, exit 0.** Legitimate incarnation roundtrip действительно
завершается (+2), затем private fence возвращает AUTHORITY_CHANGED; в базе нет
facts/observations. Intervening callback не выполняется; pending ORM и actual
physical commit failure откатываются. Committed reservation, exact-text publish,
same-run replay и existing Avito local context проверены реальным PostgreSQL.
Owned database `orders_test_f7900d6d577243839100e36cb9f97e1d` и role
`orders_exact_08a34eee92264272bbcdcb9500a1e456` удалены; allocator явно проверил
их отсутствие. PG slot освобождён ROOT.

Последний bounded offline набор: **230 passed, 0.65s, exit 0**. Полный backend
suite не запускался. PG test counts не включают прежние setup failures как PASS.

Команды из backend worktree (все pytest запускались через `env -i` и existing
offline sandbox, с закрытыми NETRC/PGPASSFILE/PGSERVICEFILE):

```text
/tmp/satorna-backend311-20260909/bin/python -m pytest -q --tb=short tests/test_review_avito_shadow.py tests/test_review_canonical_avito_decode.py tests/test_review_canonical_contract.py tests/test_review_shadow_rollout.py tests/test_wb_repricing_approval_domain.py
ORDERS_TEST_USE_LOCAL_CLUSTER=1 ...python -m pytest -q -rP --tb=short tests/test_review_avito_shadow_postgres.py
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check app/reviews/shadow_service.py tests/test_review_avito_shadow.py tests/test_review_avito_shadow_postgres.py
PYTHONPYCACHEPREFIX=/tmp/satorna-t2-account-discovery-pycache ...python -m compileall -q app/reviews/shadow_service.py tests/test_review_avito_shadow.py tests/test_review_avito_shadow_postgres.py
git diff --check
```

Внутренний отдельный critic pass: error context исправлен с causal regression;
ACL failure не принят за commit-failure proof; прямой version UPDATE не принят за
incarnation-fence proof; shared/WB definitions не изменены; роли fixture не
выдаются за final API-role. Указанный `preflight-critic/SKILL.md` локально отсутствует,
поэтому независимый внешний reviewer этим отчётом не заявляется.

Scope: additive Avito producer в одном existing module, два новых test files и
этот report. Не менялись schema/migrations/config/ops/flags/routers/tasks/legacy
WB flow. Нет decrypt/provider calls, новых credentials, network или production
mutations. Следующий разрешённый интеграционный slice требует ROOT/T1 wiring и
проверки реальных ограниченных role grants. Source answer body/list projection,
bounded fetch orchestration, crash recovery и activation этим commit не закрыты.

## Следующий bounded slice: one-GET orchestration

Этот раздел supersedes только прежний пункт о missing fetch orchestration;
final API-role, wiring, activation и crash recovery всё ещё не реализованы.
ROOT разрешил ровно пять файлов: новые `canonical_avito_fetch.py`,
`canonical_avito_sync.py`, два соответствующих test files и этот existing report.
Прежние WB/Avito clients, preview, decoder, publisher, config и routers не меняются.

Reuse: `LiveAvitoReviewsClient._get_json()` выполняет один точный GET/header path
через новый bounded HTTP facade. Не используется `fetch_reviews()` (он делает
два GET) или `_review_row()` (lossy defaults), не вызывается metadata preview.
Facade возвращает уже один раз строго разобранный native JSON без повторной
сериализации. Raw tuple поступает в existing received-page publisher/decoder.
Нет info GET, answer POST/DELETE, refresh/exchange, provider retry или cache.

```text
validate original actor + internal account + UUID4 request + offset + exact gate
  -> private resolution root, reviews:write/live session
  -> capture incarnation BEFORE paired credential SELECT/decrypt
  -> original account/access guard + private closing fence -> physical commit
  -> begin using SAME binding -> durable reservation/commit
  -> returned ticket must equal captured principal/account/access/incarnation
  -> SAME resolved credential -> exactly one GET outside all DB roots
  -> SAME ticket -> existing guarded publish -> physical commit -> receipt
```

No keyloader до проверки request/gate и live membership. Resolution root использует
ровно existing private Core-only guard contract, подтверждённый Т1: fresh private
Session, no pending ORM/callback exposure, exact private/shared final dispatch.
Нет fresh credential/actor substitution после resolution; несовпадение с ticket
блокирует GET. Если begin уже закоммитил run, тот остаётся running, а не удаляется.
Credential wrapper не копируется/сериализуется, его redacted binding — не authority.

Request checksum SHA-256 canonical sorted compact JSON v1 включает org/internal
account/provider/GET/path/offset/fixed50/decoder contract version. Request UUID4
задаёт `avito-page:<uuid>` source key, не provider review ID. Existing content
checksum/replay алгоритм не меняется. Same UUID+changed offset конфликтует до GET.
Явный повтор того же запроса может снова выполнить GET и затем replay/conflict;
нет автоматического retry, network deduplication или exactly-once GET promise.
Coverage всегда partial; total не превращается в provider-end/full-pagination.

Transport: 4MiB response limit, strict UTF-8/JSON, duplicate key и NaN/Infinity
denial, object/list<=50 shape, top-level unproven owner aliases denied. Только
200 application/json с identity encoding, проверяется Content-Length. Default
httpx client trust_env=False/follow_redirects=False, без retry. Timeout каждого
blocking operation ограничен min(20s, initial remaining elapsed budget); elapsed
30s проверяется на каждом реальном transport chunk и после stream. Это **не
жёсткий wall-clock deadline 30s**: блокирующая операция может пересечь порог,
а общий timeout не обещается как cancellation SLA. Удалено дополнительное 64KiB
накопление chunks, чтобы trickle stream не скрывал множество reads до проверки.
Ни raw exception context, ни request headers/token/provider payload не возвращаются
как diagnostic; consumer получает только allowlisted safe error.

### Evidence нового orchestration

- Initial pure RED: 41 missing-module failures; один затем найденный test-fixture
  `dataclasses.replace(ResolvedCredentialForFetch)` исправлен через явный constructor:
  wrapper по existing contract не dataclass и запрещает generic copies.
- Pure41 GREEN: 0.60s. Дополнительный trickle-stream regression RED1/0.60s:
  adapter потреблял следующий chunk после elapsed31 из-за 64KiB buffering.
  После перехода к native transport chunks **42 passed, 0.62s, exit 0**.
- Genuine PG causal RED1: **1 failed, 4.40s** — legitimate binding roundtrip во
  время paired resolution дошёл до запрещённого synthetic HTTP. Исправление:
  capture incarnation перед paired SELECT, не после. No trigger/guard weakening.
- Frozen PG13: **13 passed, 4.33s, exit 0**, включая этот causal regression,
  permission/credential/incarnation changes resolution→begin и begin→HTTP→publish,
  session revoke, physical resolution commit failure (callback reached, zero GET),
  locked-row NOWAIT из fake HTTP, committed intent до HTTP, replay/changed checksum,
  malformed raw leaves running intent/no facts и denied member/zero keyloader.
- Owned DB `orders_test_17b71754246b40e0844609743071116e` и role
  `orders_exact_a0d6edccc08b4de5bdef98ecdea5fd3d` удалены; absence verified,
  ROOT slot released. Никакой реальный provider/network не вызывался.

Тестовые команды — прежний env-i/offline sandbox/Python3.11, для pure
`pytest -q --tb=short tests/test_review_canonical_avito_fetch.py`, для allocated PG
`ORDERS_TEST_USE_LOCAL_CLUSTER=1 ...pytest -q -rP --tb=short tests/test_review_canonical_avito_sync_postgres.py`.
PG uses existing owned source fixture с exact PURE4; это не operational API-role.
Следующие gates: Т1 доказывает narrow credential/source table-column/sequence и
PURE4 privileges на финальной API-роли; ROOT владеет wiring. Source answer body,
list projection, crash-recovery jobs и activation не входят в этот slice.

Final bounded regression (new fetch + existing producer/decoder/contract/rollout/
approval kernel): **272 passed, 1.07s, exit 0**. Scoped Ruff, compileall (external
pycache), diff-check: exit 0. Critic pass отдельно проверил capture ordering и
trickle buffer по causal RED→GREEN; проверка не подменяет ROOT independent review.
Existing files вне пяти-file allowlist не менялись; preexisting untracked sandbox
сохранён. Full backend suite/actual final API-role/production не запускались.
