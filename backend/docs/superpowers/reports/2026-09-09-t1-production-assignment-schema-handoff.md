# Production P1: dormant storage handoff

Статус: реализация и обязательные проверки завершены, готово к независимому
ревью. Feature gate173PASS; покрывающий adjacent gate447PASS. T3 service/permission
READY и активация не заявлены.
Схема `20260909_0069` после `20260909_0068`; controller review BASE
`f563d8a7a6c6f7b37e058c199d3ca1aad83168bc`. Accepted authority:
request `8a8722fb8ed03e2f45fdfb3e2db2173940170230`, amendment
`0aa2c483d98d321dcf7e41ff2d921267f9f99965`, codec/literal source
`72dfaccb9e6190234c025f3c96bcfef400ce43b0` read from local Git.
T3 domain implementation не импортирована и не скопирована.

## Контракт вызова

Три relations: `production_work_items`, `production_assignment_receipts`,
`production_assignment_history`. Ровно creation с frozen quantity/source version
и manual assignment. Новые helpers SECURITY INVOKER, immutable, fixed search_path:

```text
public.production_exact_text(text) -> boolean
public.production_ascii_json_string(text) -> text
public.production_assignment_bytes(bigint,bigint,integer,text,text) -> bytea
```

Порядок аргументов bytes: work_item_id, expected_version, catalog_sku_id,
idempotency_key, reason. Exact TEXT C без cap и текстового B-tree identity;
canonical compact ASCII bytes и SHA256 сравниваются независимо. Request JSONB
ровно5 полей, result JSONB ровно7. JSONB нормализует `1e0` в `1` до trigger,
но сохраняет `1.0`; исходная lexical admission — обязанность trusted decoder.
Bytes отвергают любую неканоническую spelling, даже при эквивалентном JSONB.
NUL/unpaired surrogate не представимы на client/PostgreSQL TEXT boundary.

Trusted consumer сначала проверяет live user/membership/login session, canonical
account и отдельно выбранное permission. Только затем ставит account context,
удерживает account FOR UPDATE; creation берёт точный Orders item FOR SHARE;
assignment берёт work item FOR UPDATE. Isolation только READ COMMITTED. После всех
ожиданий получает `clock_timestamp()` один раз и использует один finite instant
в work item updated_at, receipt created_at и history occurred_at. Schema доказывает
равенство/порядок времени, не происхождение произвольного SQL timestamp.

Replay: сначала повторная authorization и live source coherence, затем exact
scoped key lookup и byte equality; вернуть оригинальные7 result values до CAS
current-version check. Новый command выполняет atomic receipt/CAS/history;
same-SKU command всё равно увеличивает version. UPDATE требует нового distinct
current receipt witness. Captured deferred OLD/NEW проверяют каждое событие;
inverse continuous history исключает missing/extra/ghost receipts. Lifetime
history сканируется по bounded owner/workitem/version; O(1) не обещается.

Source gate отдельно: current Orders item → parent → immutable current run binding,
exact live account owner, source-version coherence. Unbound/mismatch fail closed
в runtime consumer. Физическая initial source quantity/version FK+lock проверка
не предоставляет provenance/auth/permission. Accepted T3 Orders33c95+decoder8010112
не является готовым Production service. Отдельное controller решение
`082149e58089bd2ef980cdd33f5678c5e87ec827` фиксирует explicit-only read/create/assign
permission contract: write требует read. Registry/shared-guard реализация и T3
service/replay acceptance поставлены в отдельную очередь; данная схема не выдаёт
эти права и не активирует consumer. Public writer, source refresh,
unassign/planning, automatic mapping, batches,
artifacts/print/calendar, provider/scheduler activation отсутствуют.

## Миграция и rollback

Все3 таблицы FORCE org+account RLS, statement-level account lock до tuple locks.
Missing account lock немедленно отвергается до следующего свежего read.
Runtime: workitems SELECT/INSERT/UPDATE; receipts/history SELECT/INSERT;
identity sequences USAGE; trigger helpers не callable mutation API.
Новые inherited ACL пересекаются с разрешёнными правами, PUBLIC/grant options
убираются; старые/default ACL не переписываются миграцией.

Downgrade фиксированно блокирует все3 relations ACCESS EXCLUSIVE, устанавливает
row_security=off как refusal defense и проверяет unfiltered emptiness до DDL.
История не удаляется. Если появилась production history, rollback — dormant old
binary на additive expanded schema; destructive downgrade запрещён. Empty
downgrade снимает named cyclic FK и собственные объекты без CASCADE.

## Проверки

Initial RED на previous0068:2 expected FAIL (UndefinedFunction/UndefinedTable),
1 native JSONB PASS; source positive control прошёл. Exact random database
`orders_test_e3fed1b1de6441f3b6f24d40c85f2805` удалена с проверкой отсутствия.
Первый focused GREEN attempt:32 setupERROR из-за SQLAlchemy JSON literal bind,
исправлена сборка литерала; все4 собственные базы/3 роли удалены. Последующий
PL/pgSQL CASE syntax issue также исправлен до успешного bootstrap. Ошибки тестовых
инъекций TEMP FK / GENERATED ALWAYS self-update / native NUL SQLSTATE отдельно
зафиксированы; ожидания предметного контракта не ослаблялись.

Финальный feature gate implementer:173PASS33.02s, natural exit0,7баз/6ролей
удалены с проверкой отсутствия. Независимый controller gate на том же frozen коде:
173PASS26.57s,natural0; это отдельный прогон. Проверены literal и stdlib Unicode/
long bytes, captured transitions и inverse history, malformed/ghost/result/actor/
time отказ, real-session CAS/idempotency и source/account lock order, independent
RLS policies, inherited/default/column/sequence ACL, distinct owner и hidden
downgrade, empty и populated previous-head roundtrip с exact old row/ACL snapshots.

Исходный обязательный adjacent gate:438PASS1FAIL,8warnings114.51s,natural1.
Единственный сбой — старый AST-тест credential fixture, уже воспроизводимый на
BASEf563: ранее53e0549 вынес migration bootstrap в `_bootstrap_postgres`, а тест
искал вызовы только в wrapper. Controller явно добавил седьмой allowed path —
узкая правка `test_orders_schema_integration.py`, без изменения credential fixture.
Pure RED1FAIL0.47s→GREEN9PASS0.47s: reachable local/native bootstrap chain и exact
0060→0061→0060→0061 sequence плюс8отрицательных контролей. Controller выполнил
покрывающий полный7-файловый adjacent gate:447PASS,12warnings118.49s,natural0;
все27собственных DB/23роли удалены с проверкой отсутствия. Исходный failed gate
сохранён в evidence, не waived.12warnings — автоматическая очистка pytest старых
synthetic wheel-source каталогов, запрещённая sandbox; каталоги не удаляли вручную,
ограничения не ослабляли. Libpq предупреждение о /dev/null passfile тоже раскрыто.

Историческая feature fixture закреплена0069 и использует узкие тестовые grants;
отдельная runtime-script fixture следует actual latest head. Fresh compileall и
Ruff для5Python файлов прошли; sole Alembic head0069; gitdiffcheck0. Предсдаточный
самостоятельный Critic Pass выполнен по AGENTS после отсутствия указанного skill
файла; controller подтвердил fallback, независимый review ещё впереди.
Полная execution evidence и exact owned resource names — plan-owned
`task-1-report.md` и `parent-verification.md`. Точные команды:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_production_assignment_schema.py tests/test_production_assignment_lifecycle.py tests/test_production_assignment_rls.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py tests/test_orders_schema_integration.py tests/test_review_lossless_migration.py tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
```

Все вызовы выполнены из backend через env-i PATH=/usr/local/bin:/usr/bin:/bin,
PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1,
ORDERS_TEST_USE_LOCAL_CLUSTER=1 и plan-owned `task-1-sandbox.sb`. Изменены ровно
исходные6путей плюс явно разрешённый Orders fixture-admission test; старые
миграции, credential fixture и domain consumers не менялись. Own commit SHA и
SHA256 каждого файла сохраняются в execution report после коммита.

Все PG проверки только disposable Unix allocator через scrubbed env-i и OS sandbox;
собственная backend .venv. Ruff-only interpreter согласован отдельно. No app DB,
real credentials, providers/network, Redis, host changes, flags, push или deploy.

## Исправление после review round 1 — I1

Независимое review нашло важный пробел только в pure credential-fixture admission:
старый visitor считал bootstrap после безусловного return и не замечал лишние
migration calls в local/native generators. До исправления добавлены6отрицательных
контролей: early return, внешний upgrade в head и неверную revision для обоих
generators. RED:6FAIL9PASS0.56s, все6сбоев — DID NOT RAISE AssertionError.

Admission теперь принимает ограниченный straight-line with/try setup shape:
единственный bootstrap → runtime assignment → единственный yield, без return;
никаких command calls вне общего блока из4точно закреплённых migrations.
Условные/loop/handler пути не заменяют обязательный setup spine. Это не общий
Python control-flow analyzer. Исходные8негативных контролей сохранены, реальная
credential fixture неизменна. Финальный covering pure gate:15PASS0.51s,natural0;
scoped Ruff, compileall и diffcheck прошли. PG gates не повторялись, исходные
173/447 результаты и failed438/1 history выше остаются отдельной evidence.

Изменены только Orders admission test и этот handoff;0069, runtime SQL и3feature
tests неизменны. I1 исправлен для scoped re-review, не объявлен независимо
принятым. M1 (12 inherited cleanup warnings) оставлен на отдельно разрешённое
исправление harness: suppression, sandbox relaxation и удаление чужих temp dirs
не выполнялись. T3 authorization/source/permission activation по-прежнему отдельно.
