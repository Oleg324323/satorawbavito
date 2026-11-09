# T4 — детерминированный clock в тестах уведомлений

База: `e4818e3`, `codex/arch-t4-experience`; implementation commit добавляет этот отчёт.

Причина подтверждена локальным RED: synthetic feed датирован маем 2026, а тест фильтра
использовал реальный Date.now сентября. Получался пустой список вместо abc-cc.
Runtime filter правильно использует текущую дату; фиксировать её в runtime было бы ошибкой.

Изменён только `src/features/notifications/repository.test.ts`: scoped Date.now spy на
дату fixtures, afterEach restoreAllMocks, exact IDs вместо vacuous every на пустом массиве,
проверки inclusive boundary/на миллисекунду старше и продвижения clock за период.
Runtime repository, fixtures, schema, endpoints и flags не менялись. Это устранение
test debt, не реализация canonical notifications storage или исправление demo feed.

## Проверка

- До изменения focused: 1 failed / 5 passed, exit 1, ожидаемый календарный сбой.
- После: 8 passed, exit 0.
- Свежий полный baseline на `e4818e3`: **261 passed / 29 failed / 0 skipped**, exit 1.
- Свежий полный candidate: **264 passed / 28 failed / 0 skipped**, exit 1.
- Exact IDs: **0 новых failures, 1 устранённый**, 0 failed suites без failed assertions.
  Устранён: `src/features/notifications/repository.test.ts :: notifications repository
  filters by period, category, severity, manager, read state and search`.
- `npm run typecheck`: exit 0. Generator изменил только timestamp; эта наша побочная
  правка исключена, generated snapshot не входит в diff.
- Независимый read-only critic: важных замечаний нет; тест не подменяет filter/result,
  clock восстановлен после каждого test. Tests запускались основным исполнителем.

Команды из frontend T4, существующий node_modules, без загрузки dependencies:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /usr/local/bin/node node_modules/vitest/vitest.mjs run src/features/notifications/repository.test.ts --maxWorkers=2 --minWorkers=2
env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /usr/local/bin/node node_modules/vitest/vitest.mjs run --maxWorkers=2 --minWorkers=2 --reporter=json --outputFile=/tmp/satorna-t4-clock-after.json
env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin /usr/local/bin/npm run typecheck
```

Baseline запускался той же командой до patch с outputFile `/tmp/satorna-t4-clock-before.json`.
Сравнение по relative test path + fullName, не только counts. 28 failures из Stage 1 handoff
остаются; полный suite **не green**. Build/browser/canary в test-only slice не выполнялись.
Никаких production/real provider actions/printing/export/deploy/push/CodeRabbit.
