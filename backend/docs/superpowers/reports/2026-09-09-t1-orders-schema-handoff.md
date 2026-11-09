# T1 → координатор / T3: активная схема Orders

Дата: 2026-09-09. Граница результата: DDL/Catalog anchors/runtime ACL.
Точный schema commit — коммит, добавляющий этот handoff и revision
`20260909_0062`; его SHA передаётся координатору отдельно, без циклической
ссылки внутри собственного коммита. База реализации `3ffb738`, согласованное
дополнение по ACL `f771244`. Единственная head при проверке — `20260909_0062`,
`down_revision = 20260908_0061`. До независимого PASS координатора schema-ready
не означает разрешения включать collectors/readers/writers или production.

## Установленный SQL интерфейс

Авторитет runtime — `alembic/versions/20260909_0062_orders.py`:
`UPGRADE_SQL` и `DOWNGRADE_SQL` дословно содержат проверенный кандидат
`09cf383ca7419b147ebb63a7b749a7ebacc99b73` (T1 cherry-pick `c4c78fe`).
Миграция не читает файлы кандидата, не использует временный Alembic wrapper.
После DDL, в той же транзакции, выполняется дополнительное сужение inherited ACL.

| Relation | Identity PK | Права runtime |
| --- | --- | --- |
| order_sync_runs | sync_run_id | SELECT, INSERT, UPDATE только staging по trigger |
| marketplace_orders | order_id | SELECT, INSERT, UPDATE с увеличением version |
| marketplace_order_items | order_item_id | SELECT, INSERT, UPDATE с увеличением version |
| order_observations | observation_id | SELECT, INSERT |
| order_status_observations | status_observation_id | SELECT, INSERT |
| order_lifecycle_events | lifecycle_event_id | SELECT, INSERT |
| order_deadlines | deadline_id | SELECT, INSERT |
| order_sync_coverage | coverage_id | SELECT, INSERT |
| order_sync_memberships | membership_id | SELECT, INSERT |
| order_read_snapshots | snapshot_id | SELECT, INSERT |
| order_read_snapshot_rows | snapshot_row_id | SELECT, INSERT |

Все 11 relations имеют FORCE RLS по transaction-local `app.organization_id`.
Без контекста и при чужой организации строки скрыты, запись чужого tenant
отклоняется. Это не account authorization. Identity sequences — только USAGE;
нет DELETE/TRUNCATE/DDL/sequence UPDATE, ownership, SUPERUSER или BYPASSRLS
у проверенной runtime роли.

Существующие зависимости: `lk_organizations(organization_id)`;
`marketplace_accounts(organization_id,marketplace_account_id,marketplace)`;
`catalog_skus(organization_id,catalog_sku_id)`. В Catalog SQL и ORM добавлены ровно:

- `uq_orders_product_account` на marketplace_products:
  `(organization_id,marketplace_account_id,marketplace_product_id)`.
- `fk_orders_offer_product_account` на marketplace_offers: та же тройка
  ссылается на account-qualified product key.
- `uq_orders_offer_product_account` на marketplace_offers:
  `(organization_id,marketplace_account_id,marketplace_product_id,marketplace_offer_id)`.

Order identity уникальна внутри org/account/external_order_id; item — внутри
org/account/order/source_line_key. Внешние ID остаются точным TEXT. Run source,
adapter, org/account связаны составным FK observation→run. Catalog resolution:
resolved требует product+SKU; manual_override допускает SKU-only; offer требует
product того же account и той же пары. Неверные прежние пары Catalog останавливают
upgrade атомарно; миграция не удаляет и не исправляет данные автоматически.

История защищена от UPDATE/DELETE/TRUNCATE триггерами даже при ошибочных broad
grants. Terminal run неизменяем, но append дочерней истории требует протокола
сервиса. Current projection trigger требует version+1; сервис обязан добавить
`WHERE version = expected_version` и проверять rowcount. Deferred snapshot checks
проверяют число и позиции 1..N, scope и item/observation binding; header и полный
payload строк вставляются одной транзакцией и после commit не дописываются.

## ACL и эксплуатация

При expand перечисляются только non-owner ACL grantees 11 новых relations и
их собственных identity sequences. Разрешается только пересечение прежних
прав с таблицей выше; SELECT-only роль не получает INSERT/UPDATE, grant options
не восстанавливаются. Owner, старые Catalog objects и database-local default
ACL не меняются. Никакие runtime role names в миграцию не зашиты.

`ops/runtime-db-role.sql` проверяет Orders FORCE RLS, после прежних общих grants
явно сужает Orders ACL и identity sequences. Весь script теперь BEGIN/COMMIT:
ошибка после broad GRANT откатывает расширение прав. Запускать как самостоятельный
psql input с ON_ERROR_STOP, без внешней транзакции и без `--single-transaction`.
Existing unrelated final ACL policy сохранена; это не общий аудит grants.
Реальное применение к application/production DB в этой задаче не выполнялось.

Rollback до записей: owner transaction, блокировки всех Orders relations,
`row_security=off`, проверка полной пустоты, затем удаление без CASCADE.
После записей downgrade отказывает; все relations, version и количество строк
сохраняются. Непривилегированный owner с FORCE RLS получает ошибку вместо ложного
«пусто». После появления данных нужен отдельный forward-fix/retention план.

## Принятый T3 binding и ещё не реализованные service gates

Прочитан T3 amendment `2f1724a9e3a8f1d0a644a2a3700ac2f0bacc553f` и его
`app/orders/serialization.py`; domain code в T1 не перенесён.

- Первый writer сохраняет целый order-level observation: SQL order_item_id и
  source_event_id NULL. Stable line/unit evidence находится в normalized JSON.
  item_count означает число validated item identities, не сумму quantity.
- Checksum `orders-observation-v1`: exact serializer JSON, исключается только
  receipt observed_at; provider revision и полный semantic evidence входят.
  Replay нового run использует membership, не изменяет observation/run ID.
  При hash conflict сравниваются semantic bytes; snapshot использует сохранённый
  receipt. Manifest hash — отдельный контракт, не observation checksum.
- row_payload = полный serialize_read_row v1, не join к изменяемому Catalog.
  Пустой authorized account scope возвращается без persisted header.
- Любой append/finalize writer берёт одну и ту же run row FOR UPDATE и заново
  проверяет staging. Terminal retry только читает. SQL trigger не заменяет это.
- Quarantine typed payload, return/partial_return event payload и item-specific
  deadline binding пока не определены; соответствующие writers остаются off.
  SQL unique deadline `(org,account,observation,deadline_kind)` не расширяется
  выдуманной семантикой. WB Statistics остаётся observation-only.

Согласованный будущий publication protocol: одна свежая READ COMMITTED transaction,
transaction-local org; user FOR SHARE → membership FOR SHARE → canonical accounts
FOR UPDATE по возрастающим ID → exact credential/token metadata FOR SHARE → run
FOR UPDATE → projection CAS/immutable facts/coverage/sealed snapshot/terminal commit.
После ожиданий перечитать active state, permissions, scope, exact external binding,
generation/revocation и срок по DB time. Account/auth/run locks держать до commit.
Не читать/decrypt secret для metadata authorization. Worker authority требует
отдельного явного контракта; нельзя подставлять admin principal.

Это совместимо с текущим account-before-credential/token порядком в
`app/platform/integrations/credential_store.py` и `ingestion_tokens.py`,
account binding/invalidation в `app/platform/integrations/wb_credentials.py` и membership mutation в
`app/cabinet/store.py`. Existing public store методы открывают собственные sessions;
`require_marketplace_account_credential_access` выполняет обычные SELECT и не является
publication guard. T1 ещё должен предоставить session-participating metadata/auth
guard. Не вызывать resolver с собственной session под уже удержанным account lock.

DB08 ещё должен реально соревновать publication с T1 revoke/rebind в обоих порядках
победителя locks и проверять полный rollback publication. DB04/05 должны соревновать
append с terminal transition. Atomic publish/rollback, account reauthorization,
partial absence not cancellation, monotonic reconciliation, полный semantic replay,
stable pagination/cursor authorization остаются сервисными PostgreSQL gates T3.
Schema replay/CAS гонки не являются их заменой.

Orders не создаёт T2 approvals/attempts/audit и не разблокирует repricer persistence.
T2 reserve/dispatch amendment уже получен: `ccaed3410e32c50b3604f02e5b1171fd7243ce49`,
`2026-09-09-t2-reserve-dispatch-contract-amendment.md`. Он задаёт exact signatures,
attempt IDs/states, version semantics, immutable request/checksum/replay, safe errors,
audit vocabulary и exclusive dispatch marker. Отдельный T1 DDL для трёх relations
approvals/attempts/audit и их PostgreSQL acceptance ещё не реализованы этим slice.
Production work items/assignment audit/command receipts/sheets/KIZ/WB fulfillment
authority отсутствуют: DB09–DB13 production portions этим коммитом не готовы.

## Проверка и ограничения

101 passed: сохранены все 39 исходных candidate acceptance cases (адаптированы к
0062), 16 integration cases, 46 pure Orders contract cases. Feature fixtures явно
pin 0062; graph test требует одну head и 0062 в ancestry, поэтому будущая 0063
не ломает bounded fixture. Сейчас 0062 и есть фактическая единственная head.
Нативный credential RLS fixture только закреплён на 0061 (два upgrade targets);
его собственный PostgreSQL suite не запускался; static target regression не выдаётся
за credential acceptance. Полный backend/service suite не запускался.

PostgreSQL 16.15 Homebrew x86_64, Unix `/tmp`; это новые disposable DB на существующем
локальном сервере, не отдельный server process. До CREATE выполнен READ ONLY запрос
к maintenance `postgres`: database/user/version и inet_server_addr IS NULL.
Сервер не перезапускался; существующие DB/roles/application data не менялись.
Создавались только случайно именованные DB и NOSUPERUSER/NOBYPASSRLS/NOINHERIT роли.
Cleanup зарегистрирован до CREATE и проверяет отсутствие точных созданных ресурсов.
Failure-injection покрывает частичный CREATE DB/ROLE, исключение тела и pg_ctl start.

Команда из backend (профиль sandbox в ignored scratch задаёт deny network* и
единственное outbound Unix исключение `/private/tmp/.s.PGSQL.5432`):

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null \
  PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec \
  -f ../.superpowers/sdd/2026-09-09-orders-schema-integration/orders-tests.sb \
  .venv/bin/python -m pytest -q -s tests/test_orders_schema_candidate.py \
  tests/test_orders_schema_integration.py tests/test_orders_contract.py
```

IPv4 и IPv6 probes получили PermissionError errno=1; внешняя сеть запрещена.
libpq печатает ожидаемое предупреждение, что обязательный `/dev/null` passfile
не обычный файл; pytest warnings отсутствуют. .env, реальные credentials и provider
не использовались. Ruff в runtime отсутствует; это явно непроведённая проверка.
Compileall и git diff --check выполнены отдельно, exit 0.

Self-review выполнен по исходному brief и реальным DDL/ORM/grants/tests. Указанный
глобальный preflight-critic SKILL.md отсутствует (поиск альтернатив не нашёл),
поэтому проведён самостоятельный Critic Pass по checklist AGENTS.md; отдельного
subagent/reviewer по поручению не запускали. Независимый review выполняет контроллер.

## Controller acceptance, 2026-09-09

Implementation `f9c7401d946063465e0576a689eb247c368eed73`; controller plan amendments
`665bb71ff6c1ac1155e3af7a5d427976c1f6023b`; style-only followups
`d5e990106007d7b8661e026b49b445b9b5289d39` and
`1514425516d5f24b53e5956912e93da1e2682ede`. SQL constants remain byte-identical.

Controller independently repeated the exact Unix-only command above: **101 passed
in94.68s, exit0**, with exact allocated DB/role absence checks. Fresh independent
committed-code/spec review `3ffb738..665bb71` found no Critical/Important defects.
Separate coordinator independently reproduced101PASS145.64s at immutablef9c7401.
These are schema gates only, not T3 repository/publication acceptance.

Adjacent hermetic T1 checks (credential API/crypto/store, release/bootstrap/wheel,
heartbeat/health/scheduler, OpenAPI/contracts, static runtime/migration) returned
**272 passed,6warnings22.99s, exit0**, versus the preceding265-case set plus7static
checks. No full-suite baseline claim. Four duplicate OpenAPI IDs remain an owned
followup; two dependency deprecations are unchanged. Controller compileall
app/tests/alembic/release_gate and diff-check returned0.

The new server-issued Review Facts sequence, separate T2 three-table DDL and
session-participating user/session/membership/account/credential publication guard
are independent following prerequisites, not provided by0062. Worker delegation
and domain publication permission must not be guessed. KIZ/matcher allocation is
excluded by the latest explicit user scope; no unrelated deletion performed.
