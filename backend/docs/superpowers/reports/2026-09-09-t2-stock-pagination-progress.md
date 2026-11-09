# T2: stock pagination progress guard

Независимый runtime slice этапов 5/8 после dispatch amendment `ccaed341`.

## Дефект и изменение

`_load_stock_report_wb_warehouses` принимал повторную полную страницу как новые
остатки: дублировал quantity и продолжал offset loop. Повтор мог быть перестановкой
строк. Oversized page также принималась, хотя следующий offset увеличивался только
на requested limit. Invalid limit=0 молча подменялся default, другие invalid values
достигали provider boundary или ломали сравнение после запроса.

Теперь limit — strict positive integer до I/O; page size > requested limit возвращает
`STOCK_REPORT_INVALID_PAGE`. Для полной страницы строится локальный run fingerprint:
SHA256 каждой canonical JSON row, сортировка фиксированных 32-byte hashes с сохранением
multiplicity, SHA256 concatenation. Повтор любого ранее принятого полного page multiset
возвращает `ok=False`, `STOCK_REPORT_PAGINATION_STALLED` **до append**, сохраняя prefix.
Non-JSON full page также invalid. Ошибка не включает payload. Existing caller уже
маркирует failed envelope как partial/WB-03; новый путь не публикует complete run.

Fingerprint — транспортная защита прогресса, не canonical source checksum. Raw provider
payload не меняется. Retry после 429 на том же offset не считается repetition, пока
page не принята. Empty terminal page остаётся success. Set hashes локален одному call;
module-global state не добавлено. Формулы, stock aggregation и feature flags не менялись.

## Ограничения

Частичное пересечение разных страниц не обнаруживается этим guard. Page multiset
`[A,A,B]` отличается от `[A,B,B]`; identity-level duplication и complete source manifest
по account/offer/warehouse остаются отдельным ingestion slice. PostgreSQL current
pointer/daily history отсутствуют; production completeness не заявляется.

## Проверки

RED: 8 failed / 16 passed (exit1). GREEN первоначально: 27 passed (24 page tests +
3 existing pagination/default-limit/rate-limit tests). Независимый critic: blockers
нет, отдельный page module 24 passed exit0. Затем добавлены три regression cases:
nonconsecutive repetition, duplicate multiplicity и invalid non-JSON page.
Финальный focused group: **30 passed, exit 0**; compileall/diff-check exit 0.

Команда из backend, Python wave1-integration/backend/.venv/bin/python:

```sh
python -m pytest -q -p tests.repricer_offline_plugin \
  tests/test_wb_stock_page_validation.py \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_paginates_until_short_page \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_uses_official_max_page_limit_by_default \
  tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_keeps_rows_when_next_page_is_rate_limited
```

Provider заменён synthetic client, sleep отключён fixture. No DB/Redis/provider/
production/deploy/push actions. Не зависит от Orders или approvals schema T1.
