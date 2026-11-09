# T3: чтение товаров WB

База: `966dcc5ba64697b52f596a0abd755323c23e36e2`.
Ветка: `codex/wb-live-t3-reads`, worktree `arch-t3-operations`.
Source commit: `3f367be7d8093e848c7ed500af635b96d20e3536`.
Зависимости T1 импортированы без изменений: `7550eb6`, `a225bea`, `ab0a51b`.
T3 не менял schema, ORM, config, bootstrap, frontend или пользовательские данные.

## Реализация

`app.wb_live.products_router.router`: GET `/api/v2/wb/accounts/{id}/products`.
Регистрацией владеет координатор. Флаг `wb_live_sync_enabled` остаётся default-off.
Свежие session/membership, `catalog:read`, exact organization/account проверяет
реальный `acquire_read_context` T1; приложение не подменяет эту проверку токеном
пагинации. RLS и явные predicates используются независимо.

Параметры: `limit` 1..200 (default 50), `sort` nmId/vendorCode/title/brand,
`direction` asc/desc, `q` literal case-insensitive substring (до 100 символов),
`brand` exact (до 200), `cursor`. Неизвестные и повторные параметры отклоняются.
Period/status/manager/financial filters здесь не поддержаны и не игнорируются.

Единственный SQL statement читает страницу, вложенные размеры и источники
в одном MVCC snapshot. Cursor MAC привязан к actor/session/account/query и
вектору `(source, job_id, run_id, revision)`. Изменение вектора даёт 409
`WB_PRODUCTS_CHANGED`: UI начинает новую первую страницу. Это не долговечный
исторический snapshot. Исходный ключ сортировки восстанавливается по scoped PK
в том же statement; усечённый display text не становится cursor identity.
Обязательная зависимость: writer T1 увеличивает revision атомарно с каждой
публикацией страницы. Тесты T3 не доказывают сам provider/writer pipeline.

## Wire И Границы

Модель `products_http.py` определяет `{data:{marketplaceAccountId,items,nextCursor,
readVersion,readiness,sources}}`. Account ID integer, native IDs и kopecks
decimal strings; неизвестные цены null, доказанный ноль остаётся `"0"`.
Цены сохраняют native size grain, нет выбора случайной общей цены товара.

На товар возвращаются первые 5 размеров по chrtId с `sizesTruncated`.
Содержимое barcode/SKU arrays в list не передаётся: неизвестное null/false,
известное пустое []/false, известное непустое []/true (`skusTruncated`).
Нового detail endpoint или механизма экспорта не добавлено.
SQL до JSON aggregation ограничивает title/vendorCode/subjectName до 128 символов,
brand до 64, techSize до 32. Поля перечисляются в `truncatedFields`.
URL длиннее 512 символов заменяется null с явным флагом, не обрезанным URL.
SQL сортирует/фильтрует исходные значения. Предельный escaped JSON для 200 строк
проверен отдельным тестом: менее 2 MiB, без передачи произвольных payload/баркодов.

Content отображается независимо от ещё не загруженных prices. Readiness:
partial при незавершённых источниках, ready при обоих completed с результатом,
empty при обоих completed без совпадений, error при failed без результата.
Отсутствующий source представлен idle и никогда не означает complete.
Безопасные ошибки: 400 cursor/query, 403 access, 409 changed, 503 unavailable;
стандартная FastAPI validation остаётся 422. Ответ страницы имеет no-store.

Подключаемый экран: существующая таблица товаров/SKU, её content поля и цены
размеров. Stocks, baskets, orders, manager, strategy, period economics, ABC/P&L
и исторические отчёты не выводятся из этих двух источников. Существующие
canonical financial routes не изменены. Общие totals/KPI не выдуманы.

## Проверки

- Первые pure/query/HTTP tests: 36 PASS, 2 warnings, 0.62 s, exit 0.
- Первый PG запуск: 13 fixture ERROR из-за отсутствующих algorithm/aad/schema
  metadata синтетической credential. Исправлена только fixture, не ограничения.
- Затем actual head 0080 / runtime role: 13 PASS, 2 warnings, 35.56 s, exit 0.
  Проверены независимый RLS, запрет чужого account той же org и другой org,
  source revision conflict, восемь sort/direction combinations и nested limits.
- После compact SQL: 6 affected pure PASS / 31 deselected, 1.55 s, exit 0;
  затем 9 affected PG PASS / 4 deselected, 15.10 s, exit 0. Успешные независимые
  проверки не повторялись; полного backend suite в этом пакете не было.
- Ruff, compileall и git diff checks: exit 0. Самостоятельный критический проход
  выявил и устранил размер вложенного ответа, missing-vs-empty SKU arrays и
  использование display sort values. Отдельный внешний review не заявляется.

PG запускался последовательно по слоту координатора, только через существующий
allocator disposable_database и sandbox Unix socket. Все созданные DB/roles
удалены с проверкой отсутствия; T3 освободил слот T1, процессов не осталось.

Финальный synthetic замер: 10 000 товаров, page 50, включая товар со 102 размерами
и длинными списками SKU: **140.09 ms** на guarded service read, **21 648 bytes**
на page JSON, **1 data statement + 28 постоянных authorization/setup statements**.
Это одиночный локальный замер, не p95, не HTTP latency и не скорость WB sync.
Первоначальный EXPLAIN до compact SQL: 85.11 ms; product Seq Scan + sort,
sizes используют PK. Новый EXPLAIN не запускался. Запрошенные T1 индексы:
scope+nmId PK и scope+brand/vendorCode/title+nmId. Substring search остаётся
потенциальным scan; extension/cache/partitioning не добавлялись без нужды.
Peak memory, живой WB и время загрузки всех источников здесь не измерены.

Команды из backend, approved Python 3.11 runtime:

```sh
python -m pytest -p no:cacheprovider -q tests/test_wb_live_products.py
python -m pytest -p no:cacheprovider -q -s tests/test_wb_live_products_postgres.py
python -m pytest -p no:cacheprovider -q tests/test_wb_live_products.py -k 'maximum_list or null_keyset or bounded_query'
python -m pytest -p no:cacheprovider -q -s tests/test_wb_live_products_postgres.py -k 'real_guard or actual_sql'
python -m ruff check app/wb_live/products_*.py tests/test_wb_live_products*.py
python -m compileall -q app/wb_live/products_http.py app/wb_live/products_read.py app/wb_live/products_router.py tests/test_wb_live_products.py tests/test_wb_live_products_postgres.py
git diff --check
```

Запуски используют `ops.release_gate.safe_environment()`, отключённый plugin
autoload, `ORDERS_TEST_USE_LOCAL_CLUSTER=1`, sandbox с запретом внешней сети
и чтения .env/credential files; не запускайте против application/live DB.
Следующий шаг координатора: соединить completed T1/T2/T3/T4 commits и проверить
реальный общий путь. Notifications ждут отдельного подтверждения первого пути.
КИЗ/standalone matcher/production printing остаются вне этого пакета.
Никаких real marketplace calls/actions, production, push, deploy или CodeRabbit.
