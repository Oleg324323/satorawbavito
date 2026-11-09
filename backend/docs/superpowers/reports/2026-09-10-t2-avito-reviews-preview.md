# T2: account-owned Avito Reviews metadata preview

## Scope и статус

ROOT согласовал только dormant metadata preview: новые service/adapter, router factory,
focused tests и этот отчёт. Legacy Reviews/AI/drafts/approve/send/delete, canonical
Review domain, cache, schema, main/bootstrap/config и frontend не изменены.
Это **не cutover**, не canonical ingestion и не доказательство готовности отправки.

Конкретный старый путь: `app/routers/avito_reviews.py:223` использует org-only
source cache и user/org credentials с OAuth resolver. Новый preview не обращается
к этому пути, не читает plaintext fallback и не записывает cache. Старый путь
сохранён работающим; его удаление здесь не заявляется.

## Переиспользование и граница

| Компонент | Решение |
|---|---|
| `LiveAvitoReviewsClient.fetch_reviews(..., http_client=...)` | Существующая последовательность двух GET и parser сохранены через bounded injected edge |
| `/ratings/v1/info`, `/ratings/v1/reviews` | Только фиксированный `https://api.avito.ru`, второй GET limit=50, offset из запроса |
| Legacy score=5/clamp/bool/total=len defaults | Не являются данными preview: сначала strict raw projection, parser получает только безопасную внутреннюю форму; наружу идут только проверенные raw metadata |
| Paired encrypted credential resolver | `avito_oauth_access`, schema1, исходные ID/generation/expiry/binding; без client secret, refresh или обмена OAuth |
| Public publication guard | Использован как live read authority до и после I/O, не как право на запись Review facts |
| Canonical Review identity | Сохранена лексическая семантика exact opaque IDs; canonical rows/checksums/run IDs не создаются |

Локальные evidence: `tests/test_avito_reviews.py` (raw numeric id, nested item/answer,
Unix seconds, metadata); `tests/fixtures/reviews/avito_reviews_synthetic.json`
(opaque IDs и invalid numeric-like IDs); `app/reviews/canonical_contract.py`
(`_external_id`). Синтетические fixtures — свидетельство контракта проекта,
не утверждение о всех возможных ответах production Avito. Public docs/provider
не вызывались.

## Замороженный HTTP контракт

`make_account_avito_reviews_router(*, service_dependency)`

`GET /api/v2/avito/accounts/{account_id}/reviews/preview?offset=0`

`account_id`: обязательный internal positive int4 в canonical decimal text.
`offset`: обязательный nonnegative int64 в canonical decimal text. Дубликаты,
ведущие нули, дополнительные query keys и невалидные числа — 422, до service.
Limit всегда 50. Успех `{data: ...}`, все ответы `Cache-Control: no-store`.

```text
data:
  marketplaceAccountId: internal int4
  provider: "avito"
  externalAccountId: canonical positive decimal string
  offset: nonnegative decimal string
  limit: 50
  coverageState: "partial" (всегда)
  total: nonnegative int64 decimal string | null
  rating:
    isEnabled: boolean | null
    score: plain decimal string in [0,5] | null
    reviewsCount: nonnegative int64 decimal string | null
    reviewsWithScoreCount: nonnegative int64 decimal string | null
  rows[]:
    reviewId: exact string
    score: integer 1..5 | null
    stage: exact string | null
    usedInScore: boolean | null
    canAnswer: boolean | null
    createdAt: YYYY-MM-DDTHH:MM:SSZ | null
    itemId: exact string | null
    answerId: exact string | null
    answerStatus: exact string | null
    accountEvidence: "credential_scope"
```

`canAnswer` — только metadata провайдера, не permission на действие. Missing/null
не превращается в `false`, `0`, `5`, `unknown` или размер текущей страницы.
Неправильные присутствующие типы/диапазоны отклоняют ответ целиком.
Отсутствующий `reviews` list не считается пустым успешным ответом.
Нет `hasMore`/complete/freshness, выдуманных из total или длины страницы.

Review/item/answer IDs: positive JSON integer → точная десятичная строка;
строки сохраняются без trim/числовой нормализации, включая `0001` и opaque IDs.
Bool/float/пустая строка/numeric-like exponent rejected. Строго числовой account
ID — другая сущность. Preview дополнительно ограничивает строки 512 символами
без control/surrogate, numeric JSON IDs <10^128; это defensive preview bounds,
не ограничения canonical identity или утверждение о provider schema.

Rating decimal парсится непосредственно из JSON через Decimal, без float roundtrip;
выход `format(..., 'f')`, без exponent, с исходным fractional scale до 20 знаков.
Integer `0` → `"0"`, `4.30` → `"4.30"`, `4.3e0` → `"4.3"`.
Negative zero нормализует только знак: `-0.0` → `"0.0"`.
Source time принимается только из положительного JSON integer Unix seconds,
переводится в UTC; missing/invalid/out-of-calendar → null, не текущее время.

Text ответа/отзыва, buyer name, item title, URLs/images, reject reasons,
diagnostics и raw exceptions не включены. Rating/row/top-level неподтверждённые
owner aliases `accountId/userId/sellerId` отклоняются, не трактуются как buyer
или seller identity. `sender` не используется для account attribution.

Ошибки: `AVITO_REVIEWS_PREVIEW_{UNAVAILABLE,DISABLED,ACCESS_DENIED,INVALID_REQUEST}`.
401 сохраняется; access denial 403, invalid input 422, unavailable/disabled 503.
Нет raw payload/SQL/key/token/exception chain в публичной ошибке.

## Transaction и transport proof obligations

```text
request actor + exact account
  -> fresh owned PostgreSQL root
     require_live_actor(cabinet:read, account)
     lock connected account / capture ingestion_binding_version
     resolve original encrypted access credential
     own final incarnation check + shared public guard last
  -> physical commit, Session closed
  -> at most two fixed GETs, no transaction held across HTTP
  -> strict immutable metadata projection
  -> second fresh root with SAME captured principal/account/credential/version
     no authority replacement/decrypt/key reload
  -> physical commit, Session closed -> response

credential replacement / permission loss / session revoke / account rebinding
during HTTP -> second root fails -> fetched payload discarded
initial commit fails -> no HTTP
final commit fails -> no response data
```

No DML in the read service. Default-off predicate requires literal True for exact
org/account. Constructor requires PostgreSQL Engine. No memory/file fallback.

Transport: fixed origin/path/method/order, max2 GET, no retry/redirect/env proxy,
identity encoding, JSON200 only, 4MiB per response, content-length and actual
stream length enforced. 20s per-operation httpx timeout and shared 30s elapsed
checks before requests/during/after stream. Это не hard process deadline;
блокирующий read ограничен timeout операции. Duplicate JSON keys/nonfinite
numbers отклоняются. Никаких follow-up images/AI/answer POST/delete/refresh.

## Проверки

- TDD RED: missing preview module/router, затем missing service API при collection
  без запуска PostgreSQL fixtures.
- RED integer zero formatter: 1 failed / 61 passed; исправлен `format(int,'f')`.
- Negative-zero RED: producer отдавал `-0.0`, собственный wire validator отвергал;
  sign-only fix проверяется отдельной literal regression.
- Offline: **133 passed, 2 existing deprecation warnings, 10.30s, exit 0**:
  67 новых transport/DTO/HTTP tests + 1 existing parser test с synthetic recording
  transport + 65 existing pure approval-domain tests.
- PostgreSQL: **15 passed, 11.25s, exit 0**, exact frozen file
  `tests/test_account_avito_reviews_postgres.py`. Fresh roots/NOWAIT account lock
  во время fake I/O, default-off, четыре initial denial, пять concurrent authority
  changes, обе commit failures, cross-org/account. Happy read trace не содержит DML.
  Fixture завершил engine disposal и cleanup своего DB/role; слот возвращён ROOT/T1.
- Ruff и compileall четырёх новых Python files — exit 0. Staged diff/allowlist
  проверяются перед commit; существующие файлы в этом пакете не изменены.
- Отдельный внутренний critic pass: проверены null/zero distinction, exact opaque
  IDs, negative-zero self-consistency, bounded bytes/decimal output, no actual
  Review write authority. Локальный файл skill `preflight-critic` отсутствует;
  отдельная внешняя review-сессия не заявляется. ROOT review также выявил
  negative-zero consistency одновременно с новой regression.

Среда: `/tmp/satorna-backend311-20260909/bin/python`, `env -i`, network-deny
`task-1-offline.sb`; PG только `ORDERS_TEST_USE_LOCAL_CLUSTER=1` и существующий
`migrated_database` allocator, synthetic keys/accounts. Никаких production DSN,
`.env`, реальных marketplace credentials или внешних requests.

## Rollout / остаточные gates

1. ROOT принимает source и restricted-role evidence; T4 может независимо готовить
   typed client по этому контракту, без UI cutover.
2. ROOT/T1 отдельно владеют default-off wiring/config, final API-role grants и
   assembled HTTP/role test. Generic test runtime — не окончательная API-role proof.
3. Activation, real provider compatibility, rate-policy, canonical source jobs,
   Review text privacy policy и любые send workflows — отдельные разрешения/gates.
4. Rollback dormant slice: не регистрировать factory/не включать gate. Legacy не
   менялся; никаких backfill/schema/down-migration/credential изменений нет.

Никакой готовности всех Avito consumers, полноты Review sync, exactly-once
отправки или удаления старого plaintext/cache path этим результатом не заявляется.
