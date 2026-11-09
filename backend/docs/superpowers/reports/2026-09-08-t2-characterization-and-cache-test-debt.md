# T2: characterization repricer и cache test debt

Добавлен `tests/test_repricer_calculation_characterization.py`: 53 проверки
существующих вычислений, без изменения работающих formula functions.

Покрытие: все plan/fact band boundaries, kopeck half-even rounding, positive floor,
pretty-price limits и запрет разворота повышения цены, per-update/day clamp,
P_MIN/P_MAX, basket thresholds с explicit zero, масштабирование fact period,
missing SPP/COGS/commission/logistics/buyout/stock, OOS, negative margin,
stale/blocked source, discovery и margin/pmin blockers, точная граница шага,
24-hour history guard. Legacy margin `80000 - 16000 - 40000 - 5000 - 1000 = 18000`
при tax input `9000` зафиксирована как текущее поведение, а не одобрение final-profit
формулы. Налог в этом helper сейчас не вычитается; перенос состояния не исправляет
бизнес-формулы одновременно.

Тесты вызывают calculation/guard functions напрямую. Socket connections запрещены,
draft/apply/persistence functions заменены fail-fast boundary; settings/history и
readiness заданы синтетически. Глобальные настройки заменяются monkeypatch только
в тестовом процессе и восстанавливаются fixture. Runtime files/DB не используются.

## Исправленный test debt

Оба existing IDs воспроизведены RED по отдельному focused запуску:

- `tests/test_repricer_cache_store.py::test_source_cache_metadata_is_derived_without_mutating_payload`
- `tests/test_repricer_cache_store.py::test_list_source_cache_ranges_avoids_payload_and_parses_legacy_key`

Причина: expected metadata и fake SQL mapping отставали от существующих
`revenue_basis`/`finance_schema_version`. Поля уже определены в ORM, SQL SELECT и
миграции `20260825_0035_finance_revenue_basis.py`. Production fallback или удаление
полей ради теста были бы неправильным исправлением.

Fixtures приведены к существующему контракту. Полное сравнение metadata сохранено;
проверка неизменности payload усилена deep copy. SQL по-прежнему проверяется на
отсутствие payload/jsonb и правильный org scope. Добавлена передача both null и
non-null finance fields, отдельный тест metadata provenance. Tests не удалены,
skip/xfail не добавлены, baseline allowlist T1 не редактировалась.

## Проверка

Existing две failures: `2 failed`, exit 1 до исправления.
Combined characterization, 3 cache tests, approval kernel/bridge и source diff:
`178 passed`, exit 0. Из них 53 новых characterization и один новый cache test.

Broad suite не запускался; полное сокращение historical baseline на два failure
IDs не заявляется до group/full-suite gate. Для T1 передаются эти два IDs как
кандидаты на удаление из allowlist после его проверки. Formula code, schema,
config/flags, provider calls, production и shared wiring не менялись.
