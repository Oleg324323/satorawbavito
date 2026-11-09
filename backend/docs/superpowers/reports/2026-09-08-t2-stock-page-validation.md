# T2: stock page validation, без новой schema

## Доказанный дефект

`_load_stock_report_wb_warehouses` использовал общий `_extract_items`, который
отбрасывает не-dict элементы. Если полная страница содержит malformed элемент,
длина после фильтрации становится меньше limit: loader объявляет конец pagination
и возвращает `ok=True`. Также `setdefault(stockType)` менял raw provider payload,
что мешает воспроизводимому checksum/provenance.

RED: новые regression tests показали `7 failed, 6 passed`: malformed shapes
принимались за успешные terminal pages, raw payload мутировал.

## Исправление

Изменён только stock page reader в `app/wb_api/reports_sources_runtime.py`:

- malformed page отвергается целиком с `ok=False` и safe
  `STOCK_REPORT_INVALID_PAGE`; строки предыдущих страниц сохраняются;
- поддержаны прежние list/wrapped-list/singleton-stock shapes и пустой HTTP 204;
- HTTP transport status сохраняется, parse failure выражается через `ok/error`;
- `stockType` добавляется в новую row dict; исходные provider rows не изменяются;
- повторные read requests при 429 сохраняют прежний offset и не дублируют уже
  принятые строки.

Report consumer test доказывает `partial + WB-03` при malformed второй странице,
с сохранением quantity первых двух synthetic stocks. Raw omitted quantity и
explicit zero сохраняются различными на выходе loader. Legacy downstream stock
projection всё ещё может превращать omitted в zero; canonical typed consumer
остаётся отдельной задачей, этот patch не объявляет весь stock pipeline canonical.

Existing rate-limit test из baseline ожидал одну попытку, хотя reader давно делает
четыре. Исправлено ожидание `[0,1000,1000,1000,1000]`, сохранены assertions 429 и
1000 rows, добавлено `ok=False`. Новый recovery test проверяет переход
`[0,2,2]` и ровно три result identities. Retry policy не менялась.

## Проверка и ограничения

16 новых stock tests: PASS. Combined stock/price, characterization, cache metadata,
approval и source revision suite: `206 passed`, exit 0. Compileall и diff check:
exit 0. Провайдеры — исключительно synthetic clients; network запрещён в новых
тестах. Price apply не запускался. Config, migrations, flags, scheduler и formulas
не менялись.

Проверка row semantics/grain, conflicting duplicates, immutable manifests,
atomic current publication и restart требуют последующих canonical source slices.
Этот patch закрывает malformed-page false completion, не все completeness gates.
Full historical suite не запускался. Для T1 ещё один кандидат baseline delta:
`tests/test_reports_sources_runtime.py::test_stock_report_wb_warehouses_keeps_rows_when_next_page_is_rate_limited`.
Shared baseline allowlist не редактировалась.
