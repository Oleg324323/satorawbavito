# Terminal 4 — результат независимого от schema slice

Дата: 2026-09-08. Ветка: `codex/arch-t4-experience`.
Исходный HEAD этого slice: `4a7d669` (committed Reviews schema request).
Commit реализации — commit, добавляющий этот отчёт.

## Что реализовано

По запросу пользователя продолжена работа, не требующая схемы первого терминала.
Изменены только новые pure domain modules, их тесты и локальные документы T4.
Существующие routes, workers, consumers и хранилища не подключены к новым модулям.

1. `app/reviews/decision_contract.py` — immutable draft/decision values и предикаты
   публикации, подтверждения и допустимости отправки. Binding `review-decision-v1`
   включает org/account/provider/external review ID, draft ID/revision/text checksum,
   source observation/checksum, policy ID/version/checksum/template/model versions.
   Проверяются актуальные draft и decision ID, source ambiguity, answer eligibility,
   права и состояние обоих actors, порядок timezone-aware timestamps.
   Новая редакция, новая policy/source, отзыв доступа, rejection и superseded decision
   блокируют использование старого подтверждения. Текст не выводится в repr/errors.
2. `app/notification_contract.py` — строго типизированная безопасная проекция события.
   Scope явный; потерянный account Reviews-события не превращает событие в org-wide.
   Отображаемый текст фиксирован, raw payload и recipient/route не принимаются.
   Identity `notification-event-v1` включает owner/scope/producer/entity/version/kind;
   dedupe digest не зависит от времени доставки или отображаемой формулировки.
   Org-wide допускается только для явного `system_attention`.

Модули используют существующий normalizer и стандартную библиотеку Python.
LLM не вызывается: draft builder принимает уже полученный текст, в тестах он synthetic.

## Проверка

- TDD RED: оба новых test modules первоначально падают на отсутствующем implementation
  module (exit 2). Дополнительный RED на latest decision predicate — exit 1.
- `tests/test_review_decision_contract.py`: **40 passed**, exit 0.
- `tests/test_notification_contract.py`: **31 passed**, exit 0.
- Совместно с неизменённым `tests/test_review_canonical_contract.py`:
  **109 passed**, exit 0; **0 попыток внешних действий**.
- `compileall` новых modules/tests и `git diff --check`: exit 0.
- Независимые read-only critic passes Reviews и Notifications: важных дефектов не найдено.
  Critic не запускал тесты; результаты выше получены основным исполнителем.
- Полный backend/frontend suite, DB outage/restart, PostgreSQL concurrency и browser E2E
  в этом slice не запускались. Baseline delta полного suite не заявляется.

Команда воспроизведения из backend этого worktree (существующий sibling venv — только
интерпретатор; `__file__` обоих modules проверен и указывает на worktree T4):

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin PYTHONPATH=.:backend_contracts \
  /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -c '
import sys
blocked = []
def deny(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen", "os.system"}:
        blocked.append(event)
        raise RuntimeError("TEST_EXTERNAL_ACTION_DENIED")
sys.addaudithook(deny)
import app.reviews.decision_contract as reviews
import app.notification_contract as notifications
print("IMPORT", reviews.__file__, notifications.__file__)
import pytest
result = pytest.main(["-q", "tests/test_review_decision_contract.py", "tests/test_notification_contract.py", "tests/test_review_canonical_contract.py"])
print("EXTERNAL_ACTION_ATTEMPTS", len(blocked))
sys.exit(result or bool(blocked))'
```

Audit denial — дополнительный guard для данного scoped прогона, не универсальный
OS sandbox для произвольного backend кода. Эти modules не импортируют DB/Redis/HTTP clients.

## Что намеренно НЕ реализовано

- Domain values не являются persistence, authentication или разрешением на external send.
  Actor projections должны собираться доверенным service из свежих membership/account данных,
  не из request body. Фактическая принадлежность account организации проверяется service/DB.
- CAS predicate не обеспечивает атомарность: future repository должен повторить проверки
  под транзакцией и выполнить conditional write. Два одинаковых прочитанных снимка могут
  оба пройти pure predicate; только БД решает, кто действительно записал revision.
- Нет durable approvals/audit, review shadow ingestion, send command/idempotency store,
  leases/dispatch markers, ambiguous reconciliation, outbox, receipts/preferences/delivery.
- Нет гарантии durable notification dedupe: SHA — identity для будущего unique constraint.
  Producer должен сохранить domain change + event/outbox одной транзакцией. Recipient
  authorization, cache isolation и mark-all high-water остаются отдельными gates.
- Не объявляются завершёнными этапы 2–5 или вся архитектура.

## Зависимости и следующий ход

1. T1 должен утвердить/реализовать request из commit `4a7d669`:
   `2026-09-08-t4-reviews-stage-2-schema-request.md`.
   Следующий доступный этап — scoped repository + shadow ingestion по committed schema,
   с replay/partial/source-order/account-revalidation и DB outage тестами.
2. После него нужны отдельные T1 schema contracts policies/drafts/decisions/commands,
   затем events/receipts/deliveries/preferences. Не создавать параллельные JSON/memory owners.
3. Подключить pure predicates внутри транзакционных services, проверить реальные CAS,
   restart, immutable audit и worker recovery с fake providers, до UI/capability switch.
4. Credential API T1 `2a2b765` прочитан; helper с `integrations:write` не является Reviews
   authorization. Последний наблюдаемый T1 HEAD `edd9d4b` всё ещё без Reviews schema.
   Доставка request через app message не подтверждена: tool вернул `already has an active writer`.
   Сам request закоммичен и доступен первому терминалу; отсутствие доставки не скрывается.

Production, GitHub, рабочие DB/Redis, реальные WB/Avito/OpenAI/Telegram, печать/export,
deploy/push и CodeRabbit не использовались. Flags не менялись; legacy не удалён.
