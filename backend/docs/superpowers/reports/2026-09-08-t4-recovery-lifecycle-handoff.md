# T4 — recovery и независимая подготовка этапов 3–7

База slice: `98d1331`, branch `codex/arch-t4-experience`. Без БД и runtime wiring.

## Результат

- `app/reviews/send_recovery.py`: pure recovery proposals. Expired undispatched lease
  допускает только попытку CAS reclaim; live lease (включая ambiguous state) сохраняется.
  Dispatch без достаточного результата остаётся ambiguous, empty response не разрешает retry.
  Evidence привязано к exact org/account/provider/review/command/attempt и текущему read boundary;
  exact answer checksum + provider answer ID подтверждают desired answer, другой answer — conflict.
  Terminal commands не открываются заново. Никакой отправки, lease mutation или memory store.
- `tests/test_review_send_recovery.py`: synthetic recovery cases, оба marketplaces.
- `tests/test_review_provider_lifecycle.py`: реальные WB/Avito normalizers на существующих
  synthetic DTO fixtures, cross-account same ID, changed source, late generation и replay checksum.
- `2026-09-08-t4-send-notifications-schema-request.md`: следующий конкретный request T1,
  включая scoped FKs, CAS, send uniqueness across revisions, outbox, receipts и fencing.
- Frontend `2026-09-08-t4-canary-rollback-runbook.md`: локальный runbook, текущий NO-GO,
  без deploy; build-time rollback и старые browser sessions описаны явно.

## Границы доказательств

Provider verifier ещё не реализован: trusted evidence нельзя получить из request body.
Caller фиксирует reconciliation_started_at перед новым чтением; adapter доказывает exact
account/review и отсутствие stale/cached response. Произвольный freshness TTL не придуман.
Провайдер без достаточного answer ID/checksum остаётся ambiguous. Pure function не обеспечивает
exactly-once, DB locking, restart, transactional audit или фактическую свежесть внешнего API.

Existing normalization tests/fixtures уже покрывают answered и missing eligibility.
Partial runs и поздний ingestion требуют будущего repository: сценарии зафиксированы в Stage 2
request (partial omission ничего не удаляет; seq1 A → seq3 A → late seq2 B не регрессирует;
equal-time conflicting source durable ambiguous). Здесь не создан fake repository, который
выдавался бы за доказательство PostgreSQL поведения. До schema эти acceptance gates открыты.

## Проверка

Первый RED recovery: отсутствует новый module, exit 2. Первый GREEN: 32 passed.
Critic нашёл live ambiguous lease bypass и отсутствие current read boundary; добавлены tests,
RED 17 failed / 18 passed (новый параметр и missing rejection), затем GREEN 35 recovery tests.
Provider lifecycle: 8 passed на существующих normalizers/predicates, без изменения их runtime.
Итоговая combined verification: **152 passed, exit 0**, Python audit hooks на socket
connect/getaddrinfo/sendto и subprocess/system зафиксировали **0 external-action attempts**.
Проверен __file__ recovery module внутри T4 worktree. Команда как в предыдущем
`2026-09-08-t4-schema-independent-handoff.md`, с добавлением двух новых test files.
Compileall новых Python files — exit 0. Независимый critic подтвердил закрытие двух code
замечаний; также добавлена persistence read boundary в schema request по его замечанию.
Full frontend/backend suite в этом recovery slice не запускался. Commit — добавляющий этот отчёт.

Никакие schema/migrations/config/app registration, production/working DB/Redis/providers,
GitHub/deploy/push/flags/CodeRabbit/printing/export не затронуты. Frontend runtime не изменён.
Следующий разрешённый schema-dependent шаг — scoped Reviews repository/shadow ingestion по
committed T1 schema. Frontend test debt остаётся отдельным slice, не объявляется исправленным.
