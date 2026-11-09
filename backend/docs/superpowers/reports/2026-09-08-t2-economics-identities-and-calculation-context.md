# T2: economics identities и immutable calculation context

## Реализованный identity fix

`CostsService` и `EconomicsService` принимали `True`/`1.0` как organization ID.
SKU bulk operations могли дедуплицировать `[1, True]` или `[1, 1.0]` в `[1]` до
валидации. Строковые IDs/None могли давать TypeError либо достигать SQL boundary.

Теперь organization и SKU IDs требуют `type(value) is int` и `value > 0`.
Проверка выполняется до set/sort/deduplication и до обращения к БД во всех затронутых
public read/write SKU paths. Ошибки — существующие `CostValidationError` /
`EconomicsValidationError`, без включения исходного значения в сообщение.

Это не меняет account ownership costs: cost по-прежнему принадлежит CatalogSku
в организации, а marketplace product/offer mapping разрешается в account scope.
Входная валидация не заменяет auth, composite FK, RLS или DB append-only privileges.
Поля actor/supersession и их DB constraints не пересматривались в этом slice.

TDD matrix: 63 случая для bool/zero/negative/float/string/None, constructor и
single/bulk/date/point/write paths. RED: `50 failed, 13 passed`. После исправления
вместе с existing economics policies, costs, finance normalization и ABC/P&L service:
`104 passed`, exit 0. Positive-path проверки используют отдельную in-memory SQLite;
это не PostgreSQL RLS/concurrency proof. Формулы и effective dates не менялись.

## Immutable calculation context: контракт следующего переноса

Контракт зафиксирован до появления settings/source schema. Код текущих формул не
подключается к новым rows, новый runtime container не создаётся. Repository/service
собирает context один раз; formula functions получают только immutable values.

| Часть context | Содержание и owner |
|---|---|
| Scope | Проверенные internal org/account/CatalogSku IDs; nmId/offer только из account-scoped mapping; mapping ambiguity остаётся blocker |
| Calculation instant | Явный timezone-aware `as_of`, переданный caller; clock singleton внутри формулы запрещён |
| Settings | Ссылка на immutable organization algorithm version плюс применимые SKU override/assignment versions и уже разрешённые typed values; отдельно versioned liquidation state |
| Mapping | Версия/manifest реально использованного product/offer→CatalogSku mapping; текущий catalog service не даёт готовый immutable mapping revision token — dependency для T1/catalog owner |
| Source observations | Account/source/semantic version/grain/run ID/request checksum/manifest checksum, observed time и completeness; точные price/stock values сохраняют missing/zero |
| Cost | Existing frozen `CostValue`, выбранный `CostsService` на `as_of`: cost version, value/evidence state, amount и effective_from; scope подтверждается service при загрузке |
| Economics | Existing frozen `EconomicsPolicy`: org policy/override IDs, tax/other-expense values, effective_from, value/evidence state; org-wide defaults остаются org-owned |
| Price history | Immutable список фактов, выбранных до calculation instant, с реальными source/action IDs; daily guard использует этот snapshot, а не `_MEMORY.price_history` |
| Formula provenance | Существующая formula version, точный набор input versions/checksums, blockers и полученный kopeck result |

Нельзя передать живой module dict через read-only view и считать context immutable:
исходный writer всё ещё смог бы менять его. Нужны frozen typed values и tuples,
отвязанные от mutable источника. Generic JSON state repository не нужен.

### Правила сборки и чтения

1. Resolve authenticated scope, затем загружать settings/assignments/mapping/source
   references в согласованном DB snapshot либо по явно закреплённым versions.
2. Last complete source run допускается для read-only stale presentation, но
   недостаточно для apply: источник/guard сохраняет blocker. Partial runs не
   объединяются с last-good. Historical SPP/Club/wallet не подменяют buyer current.
3. Costs/economics запрашиваются на фактический business instant. Позднее значение
   не распространяется назад. `missing` и `assumed` остаются явными состояниями,
   даже если предварительную метрику можно показать.
4. Context не вызывает hydrate/flush. В ходе расчёта невозможно повторно читать
   глобальные настройки, cache, DB или clock. Per-SKU calculation возвращает новый
   result; запись history/approval выполняет отдельный service.
5. Cache identity включает org/account, input versions, source manifests, period,
   formula version и filters. TTL не заменяет versions. Existing economics count
   revision пригоден только с доказанным DB append-only; это dependency T1.
6. Action request checksum/key фиксируется отдельно владельцем approval. Изменение
   context создаёт новый intent, не переписывает уже созданный approval snapshot.

### Доказательства до включения

- Characterization corpus `test_repricer_calculation_characterization.py` должен
  запускаться против old/new implementations на одинаковых frozen inputs и давать
  точные одинаковые kopecks/blocker codes. Сейчас доказан только legacy output.
- Две context сборки для разных accounts одной org не смешивают sources/history.
- Изменение исходных settings после сборки не меняет старый context/result.
- Новая settings/source/mapping revision меняет cache key и сохраняет старый result
  воспроизводимым; page order не меняет calculation.
- Concurrent settings edits используют row versions; audit append-only, изменения
  разных SKU не затирают друг друга.
- One-writer fence проверяется до cutover; shadow не claim/apply. Replicas и
  concurrency не увеличиваются, пока migration/persistence/fence gates открыты.

## Handoff

До schema готовы characterization, identity validation, source comparison и
безопасная stock-page validation. Для переноса состояния остаются реальные
PostgreSQL tables/repository/CAS/attempts/audit и тесты двух DB sessions. Полный
финансовый расчёт остаётся зависимым от утверждения decision package; final fields
по-прежнему null. Config/migrations/current price actions не изменялись.
