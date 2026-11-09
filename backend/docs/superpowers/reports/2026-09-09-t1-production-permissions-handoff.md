# T1: общий контракт Production permissions — handoff

Дата: 2026-09-09
Base: `28258e3f39eb388179fe0b27686d0b8840cfa8dc`

## Результат

- В `app.cabinet.permissions` зарегистрированы четыре точных неизменяемых `frozenset`: общий набор ключей, read, create и assign.
- `acquire_publication_guard` до любого соединения/SQL отклоняет `production:create` или `production:assign` без `production:read` кодом `publication_context_invalid`.
- Существующие `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, нормализация профилей, union профиля с явными membership grants, SQL/lock/final-commit механика guard не изменены.
- Ни один профиль или legacy alias не получил Production permission автоматически. Регистрация capabilities не выполняет operational grant.

## Точный resolver contract для T3

T3 импортирует эти четыре exports без aliases или локального пересчёта:

```python
PRODUCTION_PERMISSION_KEYS = frozenset(
    {"production:read", "production:create", "production:assign"}
)
PRODUCTION_READ_PERMISSIONS = frozenset({"production:read"})
PRODUCTION_CREATE_PERMISSIONS = frozenset(
    {"production:read", "production:create"}
)
PRODUCTION_ASSIGN_PERMISSIONS = frozenset(
    {"production:read", "production:assign"}
)
```

Только trusted service выбирает константу: read использует `PRODUCTION_READ_PERMISSIONS`, create и его replay — `PRODUCTION_CREATE_PERMISSIONS`, assignment/change и его replay — `PRODUCTION_ASSIGN_PERMISSIONS`. Request data, role label, idempotency key и receipt не выбирают permissions.

Guard вызывается с фактическим аутентифицированным `UserSessionPrincipal`, точным frozen `ExpectedAccountBinding` текущего выбранного аккаунта и пустыми `authorities` для credential-independent Production commands:

```python
from app.cabinet.permissions import PRODUCTION_CREATE_PERMISSIONS
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    UserSessionPrincipal,
    acquire_publication_guard,
)


principal = UserSessionPrincipal(
    organization_id=authenticated_organization_id,
    user_id=authenticated_user_id,
    membership_id=authenticated_membership_id,
    session_id=authenticated_session_id,
)
account = ExpectedAccountBinding(
    marketplace_account_id=selected_marketplace_account_id,
    provider=selected_provider,  # exactly "wb" or "avito"
    external_account_id=expected_external_account_id,
    credential_ref=expected_credential_ref,
)

with session.begin():
    guard = acquire_publication_guard(
        session,
        principal=principal,
        required_permissions=PRODUCTION_CREATE_PERMISSIONS,
        accounts=(account,),
        authorities=(),
    )
    # Existing-receipt branch: this read occurs only after guard acquisition.
    existing_receipt = ...  # T3's read-only receipt lookup
    if existing_receipt is not None:
        result = existing_receipt
    else:
        # New-mutation branch: T3 may now acquire its domain locks and validate.
        ...
        # This call must be immediately before every domain/receipt/audit write.
        guard.revalidate_before_write()
        result = ...  # T3's domain write follows immediately; repeat per write.
# Return only here, after the guard's final before-commit validation and COMMIT.
return result
```

Не создавать fake user/background principal и не подменять binding данными запроса. Acquire должен происходить до чтения receipt и до domain locks; root transaction сохраняется через всю операцию. Existing-receipt branch остаётся read-only. В new-mutation branch `guard.revalidate_before_write()` вызывается непосредственно перед каждой domain/receipt/audit записью — никогда после неё. Replay заново требует текущую operation+read permission даже для ранее успешной команды. Guard автоматически выполняет дополнительную final revalidation в `before_commit`; T3 возвращает receipt/result только после успешного commit. Committed revocation до acquire отказывает; revocation, сериализованный после уже захваченной операции, не отменяет её ретроактивно.

Публичные safe codes guard ограничены точными значениями: `publication_context_invalid`, `publication_access_denied`, `publication_binding_changed`, `publication_authority_invalid`, `publication_expired`, `publication_persistence_failed`. Неизвестный input/canary не отражается в ошибке.

## Проверенное поведение

- Точные exports, тип `frozenset`, их union и полный literal snapshot прежних profiles/aliases.
- Pre-I/O rejection для create, assign, обоих write permissions и смесей с посторонними permissions без read.
- Полные read/create/assign requirements продолжают прежний путь admission.
- Реальный PostgreSQL: явные grants для custom/viewer/admin, WB и Avito account bindings, отсутствие неявных grants у всех профилей и alias `owner`/`production`, mismatch операций и неизвестный permission.
- Scope и tenant fences: неверный selected account, foreign principal/account, inactive membership, revoked/expired session и stale ORM identity map.
- Удаление read или command grant до acquire и в той же транзакции до commit отклоняет create/assign и откатывает synthetic proof rows.
- Synthetic replay-shaped путь заново проверяет create/assign grant до чтения ранее сохранённых proof rows.
- Обе стороны реальной гонки revocation проверены через наблюдаемый `pg_blocking_pids`: committed revoker-first отклоняет requester; requester-first завершает уже удерживаемую операцию, revoker блокируется до commit, следующая операция отклоняется.

## TDD и проверки

Команды выполнялись в `env -i` с `backend/.venv`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `ORDERS_TEST_USE_LOCAL_CLUSTER=1`, `PGPASSFILE`/`PGSERVICEFILE`/`NETRC=/dev/null` и task-owned Unix-only sandbox.

- RED, pure: `backend/.venv/bin/python -m pytest -q backend/tests/test_production_permissions.py` — natural exit 1, `7 failed, 5 passed in 0.65s`. Ожидаемые причины: отсутствующие exports и шесть write-without-read вариантов дошли до instrumented `Session.connection()`.
- GREEN, pure: та же команда — natural exit 0, `12 passed in 0.44s`, без warnings.
- Первый PostgreSQL запуск не считается behavior RED: natural exit 1, `67 errors in 0.65s`; fixtures не исполнялись, база/роль не создавались. Причина — неэкспортированные transitive fixture aliases. После точечного исправления `--collect-only` завершился natural exit 0 с 68 tests; затем добавлен отдельный unknown-permission case, итоговая коллекция — 68.
- GREEN, PostgreSQL focused: `backend/.venv/bin/python -m pytest -q backend/tests/test_production_permissions_postgres.py` — natural exit 0, `68 passed in 5.16s`, без test warnings.
- GREEN, mandatory combined: `backend/.venv/bin/python -m pytest -q backend/tests/test_production_permissions.py backend/tests/test_production_permissions_postgres.py backend/tests/test_publication_guard.py backend/tests/test_publication_guard_postgres.py` — natural exit 0, `265 passed in 17.74s`, без test warnings.
- Отдельная main verification тем же combined gate (не agent run): natural exit 0, `265 passed in 14.42s`; allocator сообщил точный cleanup databases `orders_test_56669aac9fb24600862a1349a8cc3afc`, `orders_test_71f81708a3da44098ffddcc3f5681821` и roles `fetch_authority_315cb9536a044801aa9147339c83e797`, `fetch_authority_32212fa2317c48699dfebe1f36112a49`. Main также получил compile exit 0 для четырёх code/test paths.
- Syntax compile — natural exit 0. `git diff --check` — natural exit 0.
- Scoped Ruff из backend CWD: новые tests и guard без diagnostics; единственный `permissions.py:I001` воспроизведён без изменений на base commit и не расширен этой задачей. Отдельная main-проверка обнаружила и до commit исправила различие root/backend import classification в новом PostgreSQL test; повторный backend-CWD Ruff не показал нового diagnostic.

## Cleanup и ограничения доказательства

Каждый successful pytest process использовал новый `orders_test_<random UUID>` и `fetch_authority_<random UUID>` из существующего allocator. Его `finally` удаляет именно созданные database/role и сразу утверждает отсутствие этих точных имён; quiet pytest capture не сохранил случайные имена в видимом выводе, поэтому они не реконструируются и не заявляются здесь как известные. После combined gate была дополнительно выполнена read-only каталог-проверка широких synthetic prefixes; она вернула `remaining_databases=[]` и `remaining_roles=[]`, natural exit 0, с предупреждением psycopg `password file "/dev/null" is not a plain file`. Эта дополнительная проверка подтверждает отсутствие остатков по префиксам на тот момент, но не подменяет точные teardown assertions allocator и не должна повторяться вместо проверки конкретных собственных targets.

Тесты используют только synthetic proof rows и existing shared guard. Они не доказывают фактический T3 receipt/audit service, provider transport, unassign, background principal, production credentials или operational activation. Провайдеры, сеть, Redis, миграции, DDL, runtime grants, flags и production не затрагивались.

## Isolated critic pass

Сопоставлены все пункты brief/spec с пятью owned paths и фактическими командами. До commit исправлены test-only fixture registration, iterator extraction, отсутствовавшая synthetic replay-shaped проверка и backend-CWD import ordering. После исправлений новых Blocker/Important замечаний не осталось; прежний `permissions.py:I001` явно остаётся baseline, а отсутствие actual T3 receipt/audit acceptance — намеренная граница этой общей задачи.

## Откат

Пока consumers dormant, откат code-only: удалить guard import/conditional и четыре exports. Если consumer уже импортирует constants, сначала откатить его wiring; не восстанавливать writer в обход текущей авторизации.
