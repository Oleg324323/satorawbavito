# T2: RNP period isolation и partial-ads test debt

## Root cause и изменение

`_period_cache` в `app/wb_api/rnp_runtime.py` в конце возвращал duration-only
fallback (`ads_7` / `baskets_7`), даже когда даты fallback не покрывали запрос или
daily rollup был невозможен. Aggregate другого периода становился входом текущего
RNP. `_covered_cache_from_daily` также позволял сумму одного присутствующего дня
объявить aggregate целой недели.

После исправления fallback используется напрямую только при точном совпадении
обеих дат. Более широкий cache должен иметь daily dictionaries для каждого дня
запроса. Отсутствие дня не является нулём. Иначе результат missing (`{}`), без
синтетического пересчёта и provider refresh. Exact-key contract сохранён; формулы
rollup не менялись.

RED: `8 failed, 1 passed` для mismatched/undated/unsliceable fallback; отдельно
`1 failed` для пропущенных daily observations. GREEN покрывает оба source prefixes,
точный legacy fallback, exact key, полный daily rollup с исключением лишнего дня,
неизменность исходного cache и missing day.

## Existing test debt

`test_build_rnp_snapshot_keeps_ads_metrics_unknown_when_ads_source_is_partial`
падал с AttributeError: mocked `build_ads_attribution_snapshot` больше нет в RNP.
Fixture переключён на существующие `_cached_ads_rows` / `_cached_funnel_rows`.
Все consumer assertions сохранены: partial source, неизвестные ads/organic metrics
равны None, organicEstimate=False, diagnostic reasons/counts сохранены.

Это test repair, не восстановление provider calls. Mock supplies validated boundary
values; тест вызывает реальную сборку RNP report. Ветка partial source не позволяет
подменить неизвестную рекламу нулевым расходом.

## Проверка и ограничения

Focused: `14 passed`, exit 0 (12 новых period tests + 2 existing RNP consumers).
Никаких network, DB, Redis или runtime file operations: cache boundaries заменены
synthetic callbacks. Периодическая загрузка и scheduler не включались.

RNP cache пока org-owned legacy; account migration, exact-key metadata drift,
source revision/freshness и достоверность daily manifest остаются отдельными gates.
Наличие daily dictionary проверяет coverage, но не доказывает, что underlying
provider pagination каждого дня завершена. Этот patch не объявляет весь source
pipeline canonical.

Для T1 ещё один candidate baseline removal после его group gate:
`tests/test_rnp_runtime.py::test_build_rnp_snapshot_keeps_ads_metrics_unknown_when_ads_source_is_partial`.
Shared allowlist не редактировалась.
