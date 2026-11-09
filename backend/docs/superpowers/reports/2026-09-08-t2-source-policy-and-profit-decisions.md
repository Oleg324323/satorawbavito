# T2: source revision policy и решения по финальной прибыли

Дата: 2026-09-08. Ветка: `codex/arch-t2-economics`.

Продолжение после `769f3cf44742622824233eefe8f81d4e360f8484`.
Пользователь разрешил выполнять задачи, не зависящие от новой schema T1.

## Что реализовано и что остаётся зависимостью

Добавлен чистый `app/modules/wb_source_revision.py`: сравнение двух уже
нормализованных runs в одном organization/account/source/semantic version/grain/
request context. Результат содержит точные added/removed/changed identities и
классификацию. Функция не сохраняет данные, не публикует snapshot и не вызывает
провайдера. Existing finance normalizer, checksum и formula services не менялись.

`SourceRun.complete` — входное утверждение проверенного source adapter, а не
доказательство пагинации, которое создаёт эта функция. `RevisionEvidence` — ссылка
на внешне проверенное evidence, связанная с двумя run IDs и manifest checksums;
проверку полномочий и достоверности evidence выполняет будущий service.
Нельзя передавать этот объект напрямую из пользовательского request и считать
непустую ссылку достаточным подтверждением WB revision.

Durable approvals, attempts, normalized settings и price/stock storage ожидают T1
schema. У T1 на момент проверки HEAD `c88a474`; незакоммиченные credential API
изменения не импортировались. Stage 2–5 не объявляются завершёнными.

## Основания и переиспользование

| Источник в repository | Что сохраняем |
|---|---|
| `app/platform/finance/service.py`, `normalize_operations` | Раздельные source identity и payload checksum; конфликтующие duplicates не выбираются по last-write-wins |
| `app/platform/finance/service.py`, `snapshot_checksum` | Существующий checksum algorithm не переписывается новой функцией |
| `docs/wb-prices-stocks-discovery-2026-09-03.md`, разделы 7–8 | Complete manifest, account ownership, явный grain, atomic publication после полной пагинации |
| `docs/wb-ktr-localization-source-discovery-2026-09-04.md`, разделы 5–10 | Reference version отдельно от localization observation; source pending, KRP не заменяет KTR |
| `docs/satorna-economics-versions-discovery-2026-09-04.md` | Существующие dated economics/cost services, запрет backdating и требование DB append-only |
| `app/modules/wb_reports/schemas.py` | `netProfitKopecks`, `profitClass`, `abcCode` остаются `None`; preliminary profit не переименовывается в final |
| `docs/superpowers/plans/2026-09-07-satorna-final-profit-blocker-avito-log-redaction.md` | Решение о final-profit blocker; historical production observations используются только как локальный документ, production заново не проверялся |

## Identity-level diff для T1

| Случай | Проверка | Результат и допустимое действие |
|---|---|---|
| Exact replay | Оба runs complete; одинаковые source identities и payload checksums | `exact_replay`; порядок строк и идентичные повторы значения не меняют |
| Объяснённая source revision | Complete; есть изменения; reviewed evidence связано с обоими immutable manifests | `source_revision`; сохранять новую revision с predecessor/evidence, отдельно пройти publication policy |
| Incomplete run | Хотя бы один run partial/failed | `incomplete_run`; delta только diagnostic, нельзя трактовать отсутствующие строки как удаление |
| Unexplained change | Complete, identities/payload изменились, объяснения нет | `unexplained_change`; сохранить discrepancy для разбора, current не заменять автоматически |
| Conflicting duplicate внутри run | Одна identity имеет два разных payload hashes | Validation error; ни одна версия не выбирается |
| Другой account/source/grain/request | Не совпадает context | Validation error, сравнение не выполняется |

Финансовый closed day может быть пересмотрен WB. Само наличие изменения не
доказывает сбой idempotency. Изменение source semantic version или периода — новый
контекст сравнения, а не revision, которую можно объявить по одинаковому total.

Синтетический пример с неизменным агрегатом:

```text
before: rrdId 101 -> 10000 коп.; rrdId 102 -> 20000 коп.; total 30000
after:  rrdId 101 -> 20000 коп.; rrdId 103 -> 10000 коп.; total 30000
diff: changed=[101], removed=[102], added=[103]
без reviewed evidence: unexplained_change
```

Историческое расхождение `120800` коп. остаётся discrepancy. Не создавать для него
synthetic `rrdId`, не менять checksum и не распределять разницу по SKU.

## Contract полноты price/stock runs для будущего adapter

Это acceptance contract до schema, а не реализованный collector.

1. Account/source/semantic version и request checksum фиксируются до первой страницы.
   Request context включает период или current-window, фильтры, stock scope и buyer
   context. Row identity содержит source-native product/offer/warehouse parts;
   `nmId` не используется как доказательство размера.
2. Manifest сохраняет запрос/ответ каждой страницы: cursor связи, безопасный request
   ID, raw checksum, row count, terminal marker и ошибки. Отсутствие ошибки само по
   себе не доказывает конец пагинации. Repeated/missing cursor, truncation, parse
   error, conflicting duplicate или незавершённая страница запрещают complete.
3. Разные источники имеют разные terminal rules. Известное число страниц/строк
   сверяется с provider total; неизвестный total не выдумывается. Legitimate пустой
   ответ становится complete только по контракту конкретного endpoint.
4. Payload hash сохраняет различие omitted / explicit zero / null. Нормализация
   денежных единиц и negative stock допустимость определяются source contract.
   Новая generic функция не вычисляет эти hashes за adapter.
5. Publication выполняется одной DB transaction только после complete validation;
   partial rows не смешиваются с предыдущим complete snapshot. Last-good можно
   показать stale с прежним observed time; чтение не обновляет freshness.
6. Stock daily ссылается на реально наблюдённый snapshot, timezone и selection rule.
   Today's stock не создаёт исторический daily. Replay не дублирует facts; source
   revision создаёт новую immutable membership/revision.

Обязательные adapter tests: terminal page, missing middle page, repeated cursor,
пустой terminal response, account/grain drift между страницами, identical/conflicting
duplicates, omitted/zero, partial после last-good, source revision и restart до
publication. Эти tests потребуют реализации конкретного adapter/storage slice.

## KTR: evidence gate сохраняется

В локальном discovery не доказаны approved KTR bands/effective dates и exact
warehouse/cluster local/all-orders source. Поэтому KTR остаётся `null` с blocker
`WB_ABC_KTR_TABLE_MISSING`; stock localization gate `WB-01` также остаётся открытым.
Product-period Sales Funnel localization не размножается на warehouses/offers.

Intake для owner: raw KTR artifact и checksum, metric units, scope, effective dates,
approved-by, плюс один account-owned localization sample с явно доказанным grain.
Для reference нужны непересекающиеся effective intervals и bands, ровно покрывающие
`0..10000` basis points на заявленной точности; граничное значение принадлежит
ровно одному band. При `all_orders=0` процент `null`, не 0/100. Требуется
`0 <= local_orders <= all_orders`; warehouse/cluster mapping нельзя угадывать.
`krp_table`, mock bands и file mtime не являются источником KTR/effective date.

## Decision package финансовому владельцу

Все примеры ниже синтетические, суммы — в копейках. Они показывают последствия
вариантов; ни один вариант не выбран и не реализован как формула.

| Решение | Что должен утвердить финансовый владелец |
|---|---|
| Состав расходов | COGS, комиссия, логистика/возвратная логистика, хранение, приёмка, штрафы/корректировки, loyalty, ads, налоги, эквайринг и OPEX: source, знак, inclusion/exclusion и защита от повторного вычитания |
| Operational/settlement | Какие события являются признанной выручкой/расходом; это две отдельные витрины или одна выбранная основа |
| Период признания | Дата заказа/выкупа/отчёта/корректировки; правила закрытого периода и поздних корректировок; timezone и half-open boundaries |
| 1C OPEX ownership | Сохраняется org-wide; прямой account/SKU только при source evidence; allocation отдельно от исходного расхода |
| Allocation | `none`, `revenue`, `orders`, `stocks`; eligibility set, sign/returns, basis period, source и missing-data behavior |
| Denominator | Что считается revenue/order/stock; нулевой/отрицательный total; full-population против фильтра; нельзя пересчитывать по странице |
| Rounding | Точность, half-even/half-up, на строке или сумме, правило распределения остатка копеек |
| ABC | Отдельные sales/profit thresholds, способ cumulative boundary, ties, zero/negative profit и отсутствие denominator |
| Версия | Полный formula document, formula version, effective date, approver и политика пересчёта исторических периодов |

### Operational и settlement / late adjustment

Один товар: заказ 30 сентября на `100000`; выкуп 2 октября, COGS `40000`,
подтверждённые marketplace fees `15000`; возврат/корректировка 5 ноября.

- Order-based operational view в сентябре показывает `100000` заказов, но это не
  доказывает признанную прибыль `45000`.
- При условно выбранном recognition-on-buyout октябрьская предварительная прибыль
  равна `100000 - 40000 - 15000 = 45000`.
- Если в ноябре доказаны refund `100000`, восстановление COGS `40000` и дополнительная
  return fee `5000`, ноябрьский delta = `-100000 + 40000 - 5000 = -65000`.
  Combined delta двух периодов = `-20000`. Fee refund не предполагается.
- Alternative restatement относит этот delta к октябрю, оставляя ноябрю 0 по этой
  операции. Выбор меняет оба monthly reports и требует approval; один cumulative
  total не разрешает молча менять recognition date.

### OPEX: четыре разных результата

Синтетические SKU A/B одной доказанной allocation population:
revenue `100000/50000`, orders `2/4`, average stock basis `50/50`, preliminary profit
`30000/20000`, org OPEX `12000`.

| Вариант | Allocated OPEX A/B | Условный profit A/B после этого OPEX | Unallocated |
|---|---|---|---|
| none | `0 / 0` (allocation отсутствует) | Final SKU profit остаётся неизвестным; preliminary `30000 / 20000` | `12000` |
| revenue | `8000 / 4000` | `22000 / 16000` | `0` |
| orders | `4000 / 8000` | `26000 / 12000` | `0` |
| stocks | `6000 / 6000` | `24000 / 14000` | `0` |

В каждом полном allocation варианте сумма условной прибыли `38000`.
При `none` org-level illustrative result также может быть `50000 - 12000 = 38000`,
если все остальные расходы доказаны, но это не устанавливает SKU attribution.
Cross-account allocation требует отдельного утверждённого population contract;
историческому OPEX не добавляется выдуманный marketplace account.

Denominator examples: revenue `100/-100` даёт net denominator 0; применение
positive-only `100/0` вместо net — другое бизнес-правило. При orders `0/0` нельзя
подменить правило equal split. Missing stock для B не означает stock 0.
До решения OPEX остаётся unallocated, final rows/summary несут completeness blocker.

### Rounding и остаток

`100` коп. на три равные доли: exact shares `100/3`; округление каждой вниз даёт
`33+33+33=99`. Возможный residual rule даёт `34+33+33=100`, но порядок получателя
остатка должен быть стабильным и утверждённым. Для `2.5` коп. half-even даёт 2,
half-up — 3. Rounding каждой строки и rounding total могут расходиться; API pages
не должны определять порядок или остаток.

### ABC, ties и отрицательная прибыль

Иллюстрация с условными thresholds `80/95` (это не approval profit thresholds):
profits `8000/1500/500`, cumulative `80/95/100%`. Политика inclusive upper boundary
даёт A/B/C; политика strict boundary переводит пограничные позиции иначе.

Ties: `4000/3000/3000`. После первой строки cumulative 40%, после второй 70%, после
третьей 100%. Row-order assignment может разнести одинаковые 3000 по классам;
tie-group policy обязана явно выбрать единый класс и boundary convention.

Negative case: `10000/-5000/0`, net total `5000`; первая доля 200%. Positive-only
denominator, отдельный loss class и отказ от классификации — разные варианты.
При total 0 cumulative share не определён. Никакой из этих вариантов не выводится
из старого rank-by-row поведения.

### Approval record, completeness и golden tests

Владелец утверждает каждый пункт таблицы, числовые ожидаемые результаты и effective
date в одном versioned decision record. Одного «продолжай» недостаточно для выбора
неуказанных финансовых правил. После approval формула реализуется в одном existing
canonical backend service; rows и summary содержат одинаковые source/formula
versions, completeness и blockers.

Golden matrix после approval: sale, return, late adjustment, loyalty, ads,
org-wide/allocated OPEX, missing cost as-of, rounding residual, ties/zero/negative,
custom periods, page independence, source revision и formula effective boundary.
`fullstats` не суммируется повторно с UPD/finance promotion; надо выбрать source
ownership расходов и сохранить reconciliation evidence отдельно.

До решения `netProfitKopecks`, `profitClass`, `abcCode` остаются `null`. Поздняя
себестоимость не backdate'ится. Существующие source checksums не подгоняются.

## Проверка и следующий доступный шаг

TDD: новый test сначала завершился `ModuleNotFoundError`, exit 2; после реализации
focused source/approval suite: `122 passed`, exit 0 (93 прежних + 29 новых).
Использован локальный isolated Python из worktree `wave1-repricer-approval-kernel`;
dependency metadata не менялась. Broad legacy baseline в этом slice не запускался.

Schema, flags, schedulers, runtime wiring и provider clients не менялись.
PostgreSQL concurrency/persistence и publication tests не заявляются пройденными.
Финансовые варианты требуют owner decision; T1 schema request остаётся в Stage 1
handoff. Отдельно можно продолжать characterization конкретных existing consumers,
но включать их на новые snapshots можно только после storage и one-writer gates.
