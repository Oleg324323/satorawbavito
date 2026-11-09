# T1: повторное шифрование с точным владельцем аккаунта

Дата: 2026-09-09. BASE: `17028f5667599c0ef2ca2f2536d3334426354afd`; исходный worktree чистый. Только шесть согласованных tracked paths. Проверено четыре прежних test calls и отсутствие application callers; оба PostgreSQL owner-only подменных writer factory удалены. Production crypto/schema/grants/config/consumers/helpers не менялись.

## Контракт и реализация

`reencrypt_credential(credential_id, expected_generation, target_key_version, *, account_identity=None)` возвращает `CredentialMetadata`. Старый вызов без владельца теперь выдаёт `credential_contract_invalid` до загрузки ключей, создания Session и SQL. Валидация owner/provider/UUID и диапазонов INT4/BIGINT локальна этому entry point; max INT4 — допустимая форма, не обещание наличия ключа.

Свежая принадлежащая store транзакция PostgreSQL требует Engine bind, фактический READ COMMITTED и DBAPI nonautocommit. В ней устанавливается tenant, блокируется точный account через существующий `_account(lock=True)`, затем credential. SELECT и CAS повторяют org/account/provider/UUID/nonrevoked, CAS дополнительно generation. После ожидания блокировок проверяется expiry. Сохранена цепочка decrypt → freshnonce encrypt → decrypt exact equality → один CAS → safe audit → commit; отказ откатывает транзакцию. SQLAlchemy failures, включая factory и audit, переводятся в безопасный код с suppressed context.

## Доказательства

- Настоящий runtime: каталог подтвердил nonowner, NOSUPERUSER/NOBYPASSRLS, ENABLE+FORCE RLS и Unix connection; неизменённый disposable allocator создаёт случайные DB/roles и проверяет их отсутствие после завершения.
- Синтетические ключи 7/8 для rekey находятся только в памяти. Проверены точный payload, новый nonce/ciphertext при разных и одинаковой версии ключа, сохранение UUID/owner/provider/kind/schema/expiry и один audit на успешный rekey.
- Другой account той же org, другая org с собственным активным credential, provider mismatch и неверная UUID/account пара: безопасный отказ и полное равенство сохранённых encrypted rows/audits.
- Missing/malformed owner и поля, boolean, нулевые/отрицательные/overflow IDs, generation и key_version отсекаются до key/session factory.
- Stale generation, revoked/expired/corrupt row, отсутствующий old/target key, verify mismatch, синтетические DB/factory/audit failures: rollback всей строки и audit; canary отсутствует в error/repr/captured logs.
- Реальное ожидание account lock наблюдается через `pg_blocking_pids`; удерживающий account A получает credential lock без цикла и фиксирует replacement/revoke либо trusted clock пересекает expiry. B видит committed состояние и отказывает.
- Два rekey с одной generation: наблюдаемое ожидание, один success, один `credential_concurrent_update`, одна прибавка generation/audit.
- Исходные paired-fetch snapshot assertions и оба publication lock winners сохранены под runtime factory. SQLite — только контракт/CAS/crypto-проверки, не RLS/concurrency evidence.

## Команды и результаты

Все команды запускались из `backend` этого worktree. Ниже точные scrubbed sandbox команды, с собственным `backend/.venv`. Альтернативное окружение использовано только для Ruff. Ошибки и успешные прогоны завершились естественно; процессы не убивались, timeout не считался успехом.

### Исходный RED (до реализации)

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py::test_reencrypt_missing_owner_denies_before_io
```

exit 1; 2 failed in 2.98s. Runtime old three-position call: credential_missing after successful put/resolve; missing-owner contract: forbidden key loader AssertionError.

### Tests-first расширение RED

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py -k reencrypt
```

exit 1; 64 failed, 1 passed, 12 deselected in 25.20s. New owner keyword absent; race observers could not reach locks. This additional contract run is not substituted for the initial real runtime RED.

### Focused GREEN

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py -k reencrypt
```

exit 0; 65 passed, 12 deselected in 6.46s.

### Усиленные SQL/replacement проверки

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py
```

exit 0; 24 passed in 3.45s.

### Обязательный scoped gate

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py
```

exit 0; 220 passed in 17.68s; no skips/xfails.

### Соседние crypto/migration

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_marketplace_credential_crypto.py tests/test_marketplace_credential_migration.py
```

exit 0; 53 passed in 0.49s.

### Compile

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb .venv/bin/python -m compileall -q app/platform/integrations/credential_store.py tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py
```

exit 0.

### Scoped Ruff

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check app/platform/integrations/credential_store.py tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py --output-format concise
```

exit 1, exactly the eight inherited BASE diagnostics below; no expansion.

### Ruff новых/затронутых PG tests

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py --output-format concise
```

exit 0; All checks passed!

### Точное сравнение Ruff с BASE

```sh
git show 17028f5667599c0ef2ca2f2536d3334426354afd:backend/app/platform/integrations/credential_store.py | /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check --stdin-filename app/platform/integrations/credential_store.py - --output-format concise
git show 17028f5667599c0ef2ca2f2536d3334426354afd:backend/tests/test_marketplace_credential_store.py | /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-account-scoped-credential-reencryption/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check --stdin-filename tests/test_marketplace_credential_store.py - --output-format concise
git diff --check
```

Baseline Ruff: exit 1 и 1; ровно те же позиции/коды/сообщения: production UP017 на 143:25, 150:38, 151:29, 189:78; SQLite I001 1:1, UP017 29:42/138:50, F841 178:5. `git diff --check`: exit 0. Новые I001/C408 и ставший неиспользуемым PG import исправлены до итогового gate.

## Cleanup

Для каждого PG прогона allocator вывел `Orders cleanup verified` после DROP и SELECT-проверок отсутствия ровно созданных ресурсов:

| Прогон | Database | Runtime role |
| --- | --- | --- |
| Initial RED | orders_test_c56fe2546ecc43949aabc2d005274e0b | fetch_authority_f5726a5582834cbdb2978d58df9f2015 |
| Contract RED | orders_test_0ec3cc0b4ee242a19531800632dc6fb1 | fetch_authority_472da020480749a0a47a3ee28e39f393 |
| Focused GREEN | orders_test_ea772f5f54304475aae5f63b0d236433 | fetch_authority_a4c87780fc2c4c2096a21f872b053e83 |
| Strengthened PG | orders_test_f46d4ce4c97b474085761732c77a2705 | fetch_authority_d175f819c5ea4c9088000c7f532944d3 |
| Gate: rekey | orders_test_030c5808c8de47c99febddb9995a9310 | fetch_authority_342bad0fcf824e41a0eac3ca36b4d17b |
| Gate: fetch | orders_test_7f37cdd12bc1455285352d645f1cac40 | fetch_authority_9f20d86ea5fe4abdad87bb73ede0461a |
| Gate: publication | orders_test_deec1137e917446d89d0e935fbc94493 | fetch_authority_c3a5fbbfd20643faaee2511474dc9f31 |

Signal release / mutator rollback выполняются в finally до executor shutdown. Новые изолированные admission engines disposed в finally; общий fixture закрывает runtime/observer engines перед allocator cleanup. Унаследовано предупреждение libpq о `/dev/null` не plain file; passfile не менялся.

## Изолированный Critic Pass и ограничения

Применены executing-plans, test-driven-development и verification-before-completion. Указанный `/Users/bratishka/.codex/skills/preflight-critic/SKILL.md` отсутствует; поиск local skills/plugin cache не нашёл замену. По прямой процедуре brief выполнен самостоятельный изолированный проход: исходный запрос/spec → шесть diff paths → predicates/transaction/rollback/resource lifetimes → команды и фактические результаты. Другие агенты/ревьюеры не запускались. Исправлены слабая replacement-модель в новом race (теперь A фиксирует полноценный encrypted replacement) и новые Ruff-замечания; финальный gate выполнен после этих исправлений. Открытых Blocker/Important в этом self-review не выявлено; независимое ревью контроллера всё ещё требуется перед Stage3 CLI.

Account scope не является authentication. Entry point предназначен только для trusted explicitly authorized administration. Нет нового публичного API/worker principal, operational key policy, key lifecycle/backfill/rotation activation, providers/production/working Redis/flags/deploy/push. Не заявляются full-suite, native-cluster или production доказательства. Injected pre-COMMIT failures не доказывают восстановление после неизвестного сетевого COMMIT. Исходные соседние configuration tests могут создавать свои synthetic temporary key files; новые rekey tests не открывают key files и не используют реальные credentials.
