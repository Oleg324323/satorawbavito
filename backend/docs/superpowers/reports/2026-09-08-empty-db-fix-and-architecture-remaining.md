# Satorna: empty-DB migration fix и оставшиеся архитектурные работы

Дата: 2026-09-08. База: Wave 1 integration `960d8e5`.
Ветка исправления: `codex/fix-empty-db-migrations`.

## 1. Исправленный bounded slice

Причина: `20260717_0017` создаёт `rv_review_sync_settings.ai_prompt`,
а `20260717_0019` повторно добавляла ту же колонку. Её downgrade также ошибочно
удалял колонку, принадлежащую схеме `0018`, вместе с операторскими данными.

Исправление ограничено `0019`, regression tests и локальным release gate:

- существующая корректная колонка сохраняется без изменения значений;
- для исторической схемы без колонки добавляется `TEXT NOT NULL DEFAULT ''`;
- несовместимая существующая схема вызывает явную ошибку, а не скрытую нормализацию;
- downgrade `0019 → 0018` сохраняет колонку: удаление принадлежит `0017`;
- release gate выполняет `upgrade head → downgrade -1 → upgrade head` напрямую,
  без прежнего обхода через `stamp 0019`.

Новая `0062` не нужна: она не смогла бы исправить сбой раньше в цепочке.
`0017`, `0061`, runtime code, данные пользователя и production не изменялись.
Базы, уже прошедшие `0019`, не выполнят её повторно при обычном `upgrade head`;
принудительный downgrade/stamp на рабочей базе для применения фикса не нужен.

### Проверки

Реальный локальный PostgreSQL в отдельном временном кластере, случайный loopback
port, новая БД на каждый тест; подключение из пользовательского окружения не используется.
Тесты проверяют полный пустой bootstrap до текущей head, последний migration roundtrip,
переходы `0018 ↔ 0019` с сохранением prompt, историческую схему без колонки,
уже stamped `0019` и отказ при несовместимой схеме с сохранением revision и данных.

Команда из `backend`:

```sh
.venv/bin/python -m pytest -q tests/test_empty_database_migrations.py tests/test_release_gate.py
```

Первый red run: 4 failures воспроизвели duplicate-column и потерю колонки на downgrade.
Целевой run: **10 passed in 9.26s**, нормальное завершение.
Полный backend run: **883 passed / 105 failed / 16 warnings in 303.30s**;
JUnit failure IDs точно совпали с предыдущим integration run
`878 passed / 105 failed`: added = 0, missing = 0, errors = 0.
После записи JUnit и итоговой сводки Python завис в shutdown фонового потока;
точный test process завершён SIGINT. Это не чистый зелёный release gate и не
замалчиваемый success exit. Нестабильный shutdown входит в test-infrastructure debt.
Compileall изменённых Python files, `git diff --check` и один Alembic head
`20260908_0061` также проверены.

Локальные диагностические артефакты (не часть repository):
`/tmp/satorna-empty-db-red.txt`, `/tmp/satorna-empty-db-green.txt`,
`/tmp/satorna-empty-db-full.txt`, `/tmp/satorna-empty-db-full.xml`.
Полный Docker/frontend release gate и production canary в этом bounded slice
не запускались; migration часть того же release gate проверена настоящим PostgreSQL.

## 2. Что действительно завершено, а что пока только подготовлено

| Направление | Проверенное локальное состояние | Что это НЕ означает |
|---|---|---|
| Credentials | crypto, account-owned storage/forced RLS `0061`, store; три commits Wave 1 | текущие WB/Avito consumers ещё не переведены на encrypted-only |
| Repricer | `app/modules/wb_repricing.py`: чистый approval lifecycle/version/action-key contract | durable repository и действующие price paths ещё не подключены |
| Orders | `app/modules/orders.py`: identities и явная WB/Avito status mapping | общей persisted очереди, ingestion и production API ещё нет |
| Reviews | `app/reviews/canonical_contract.py`: normalization/checksum contract | tenant-owned shadow ingestion и безопасный live-send ещё не подключены |
| Canonical finance | backend projections, account-scoped ABC/P&L, evidence/blocker semantics уже существуют | финальная прибыль и классификация не утверждены; legacy UI не считается переключённым |
| Frontend ABC/P&L | кандидат `edc5439` на `codex/satorna-frontend-abc-pnl-canonical` | этих frontend изменений нет в Wave 1 integration `960d8e5` |

Это не повторный live-аудит: production и внешние действия не выполнялись.
Состояние production из handoff от 2026-09-07 — историческое evidence, не новая проверка.
Оценку «77%» нельзя автоматически повышать за добавленные чистые контракты.

## 3. Приоритетный backlog до завершения архитектуры

Каждая строка ниже — направление, которое разбивается на указанные bounded slices,
а не разрешение реализовать всё одним изменением.

| ID / приоритет | Независимые implementation slices | Критерий завершения |
|---|---|---|
| A1 / P0 | Зафиксировать единую integration/release lineage; вернуть отсутствующие architecture spec и исходники prototype/renderer; отдельно интегрировать проверенный frontend candidate и discovery docs | один воспроизводимый checkout, полный перечень компонент/владельцев, никакого вывода «все ветки уже объединены» |
| A2 / P0 | Credentials Tasks 4–9: account-owned APIs/adapters → IDs вместо секретов в Celery → scoped ingestion tokens → idempotent backfill → key mounts/rotation/recovery → encrypted-only и поздний plaintext cleanup | exact account binding, corrupt ciphertext fail-closed, секретов нет в API/logs/broker, rotation и restore проверены; legacy cleanup только после parity |
| A3 / P0 | Repricer: PostgreSQL approval repository/CAS → durable draft/job/attempt/intent → account-owned settings вместо globals/whole-JSON flush → audit/reconciliation → runtime cutover | один sender, stale version = conflict, crash-after-provider-accept = ambiguous/reconcile, запрет слепого resend; API/Celery работают с одним owner, второй writer разрешён только после concurrency gates |
| A4 / P0 | Reviews: account-owned storage/RLS и shadow ingestion → policies/AI provenance → draft/approve/read → отдельно sender/outbox/lease/reconciliation | изоляция одинаковых external IDs разных accounts, re-generation инвалидирует approval, send требует актуального backend approval, restart не теряет результат; live-send отдельный rollout |
| A5 / P0 | Убрать опасный DB-error → memory/file/defaults fallback в критических mutation paths; аудит membership/account boundary и legacy caches по модулям | отказ БД не создаёт вторую правду; cross-tenant/account negative tests, cache keys включают scope, audit не раскрывает чужие данные; не затрагивать чужой FoundHub workload |
| A6 / P1 | Orders: schema/RLS на уже готовом contract → Avito complete observations → отдельное подтверждение WB fulfillment source → catalog resolver → canonical read API | order/item identities стабильны, replay идемпотентен, partial fetch не отменяет заказы, returns/cancellations явные; WB statistics не выдаются за FBS readiness/deadlines |
| A7 / P1 | Production: вернуть/зафиксировать prototype fixtures → work items/mapping/CAS → batches/groups/frozen sheets → отдельно XLSX, A4 PDF, stickers → archive/delivery/operator actions → typed UI | SKU mapping, ordering/quantities, >48 rows, Cyrillic, 120×75/58×40, selection, authenticated audit, 409 UX и restart parity; prototype не удалять до полного parity |
| A8 / P1 | KIZ/PDF: восстановить полный matcher source/lockfile → synthetic characterization → PostgreSQL allocation/leases → stateless Node inspect/render → durable jobs/artifacts → reconciliation → one-writer cutover | код нельзя выделить дважды; ambiguity блокирует; download не расходует код; rollback не возвращает assigned/printed в доступный пул; shadow не резервирует/расходует коды |
| A9 / P1 | Notifications: durable events + per-user receipts → preferences/Telegram deliveries → transactional outbox/retries → frontend | событие не теряется на commit/crash, параллельные read/unread не затираются, user/account scope соблюдён, повтор не дублирует доставку |
| A10 / P1 | Frontend ABC/P&L: интеграция отдельного candidate → default-off build/release → read-only org/account canary → расширение allowlist | `/api/v2` routing, typed pagination/drift guards, exact/custom periods, loading/empty/blocked/error; null не превращается в 0, предварительная прибыль не называется финальной; проверенный frontend rollback |
| A11 / P1 | Финальная экономика: утверждение formula/OPEX allocation/classification → scoped 1C expense domain/RLS → доказуемые dated costs/economics → formula implementation и golden reconciliation | без выдуманного распределения OPEX, backdating costs и synthetic raw IDs; согласованы tie/zero/negative rules; финальные поля остаются null при blocker; историческое расхождение 120800 коп. отдельно закрыто evidence либо явно принятой сменой golden gate |
| A12 / P1 | Facts: price snapshot → stock snapshot/daily → подтверждённый KTR/localization source и versioned reference → typed report consumers | полный snapshot публикуется атомарно; account/offer/warehouse grain сохранён, missing ≠ zero, current SPP не подменяется историческим/Club, `krp_table` не заменяет КТР |
| A13 / P1 | Scheduler: identity-level source-diff gate → read-only reconciliation диагностика → отдельный ограниченный canary; observability: shared worker/beat heartbeat → freshness/backlog/latency alerts | реальные ревизии WB отделены от duplicates, unexplained diff блокирует; worker liveness действительно измерена; rollout/kill switch/rollback проверены до постоянного включения |
| A14 / P1 | Авито multi-account: полный inventory auth/adapters/tasks/caches/orders/reviews/repricing/XML/stats/wallet → закрытие оставшихся org-only lookups по capability | credential/account ownership сквозное от HTTP до worker и storage; два account одной org не смешиваются; готовый Orders/Reviews contract не засчитывается как покрытие остальных capabilities |
| A15 / P0→P1 | Test debt и release engineering: устранить 105 backend baseline failures по triage и зависание shutdown, отдельно frontend baseline → воспроизводимый CI/contracts/migration/RLS/build → backup/restore и canary gates → позднее legacy retirement | не расширять allowlist вместо исправления; полные suites зелёные и штатно завершаются, contract generation drift отсутствует, чистый bootstrap и production-shaped restore проходят; удаление legacy лишь после capability parity и rollback-window |

## 4. Как параллелить следующую волну

Не запускать четыре владельца migrations одновременно. Сначала назначить один schema
owner, согласовать IDs/FK/версии контрактов; DDL сериализовать на integration-ветке.

- Terminal 1: Credentials API/adapters (Task 4), без включения encrypted-only и backfill.
- Terminal 2: Repricer repository/service поверх согласованной схемы, tests со stub provider;
  не трогать формулы и реальные price flags.
- Terminal 3: Orders/Production characterization fixtures и catalog-resolution contract;
  никакой реальной печати/export, никаких правок runtime prototype.
- Terminal 4: Reviews shadow repository/ingestion поверх согласованной схемы,
  без send/OpenAI/Telegram. Пока DDL не согласован — fixtures и contract tests.

Финальная последовательная интеграция обязательна. Frontend candidate и устранение
baseline debt — следующие отдельные slices, не скрытая пятая задача этой волны.
Во всех runtime cutovers: **expand → backfill → shadow → canary → switch → поздний cleanup**.
Там, где есть external action или allocation, не допускаются два независимых writer.

## 5. Основания и границы достоверности

- `SATORNA_ARCHITECTURE_HANDOFF.md`, особенно Open blockers и next slice: состояние до Wave 1.
- `SATORNA_SPEC_AUDIT.md`: исторический список замечаний; уже исправленные RLS/finance
  boundary и новая crypto foundation не записаны повторно как полностью отсутствующие.
- Текущий код: `app/modules/{orders,wb_repricing}.py`, `app/reviews/canonical_contract.py`,
  `app/platform/integrations/{credential_store,wb_credentials}.py`, `app/repricer_bff.py`.
- `docs/superpowers/plans/2026-09-08-marketplace-credentials-encryption.md`: Tasks 1–3
  подтверждены кодом/commits, несмотря на исторические unchecked checkboxes; Tasks 4–9 впереди.
- `docs/wb-prices-stocks-discovery-2026-09-03.md`,
  `docs/wb-ktr-localization-source-discovery-2026-09-04.md`,
  `docs/superpowers/reports/2026-09-06-satorna-legacy-test-failures-triage-ddaf087.md`.
- Orders/Production discovery и Wave 1 plan: `edc5439`, файлы
  `backend/docs/satorna-orders-production-architecture-discovery-2026-09-08.md`
  и `backend/docs/superpowers/plans/2026-09-08-satorna-four-terminal-wave-1.md`.
- Repricer discovery: `5d7dde6`,
  `backend/docs/superpowers/reports/2026-09-08-wb-repricer-module-global-state-audit.md`.
- Reviews/Notifications discovery: `4b1b4fc`,
  `backend/docs/superpowers/reports/2026-09-08-reviews-notifications-discovery.md`.
- KIZ/PDF discovery: `6020d59`,
  `backend/docs/superpowers/reports/2026-09-08-kiz-pdf-pipeline-discovery.md`.

Последние четыре источника прочитаны из локальных Git refs; они не автоматически
включены в integration tree. Исходный architecture-design от 2026-08-26 и полный
production prototype/Node matcher отсутствуют в исследованном integration checkout.
Их отсутствие нельзя трактовать как разрешение переписать либо удалить работающий
production; перед соответствующим implementation slice требуется восстановить sources.
