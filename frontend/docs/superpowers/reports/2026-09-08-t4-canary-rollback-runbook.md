# T4 — план canary и rollback, без выполнения rollout

Статус: подготовлен локально; **NO-GO сейчас**. Нет разрешения на deploy/production,
full frontend suite остаётся red, hosted checks и observation window не выполнены.
Основание: `2026-09-08-t4-stage-1-handoff.md`, фактический consumer
`src/features/wb-finance/canonicalAbcPnl.ts` и контракт backend ABC/P&L.
Не переносить этот runbook на Reviews send/Production print или другие capabilities.

## Gate 0 — до любого релиза

Release owner фиксирует immutable frontend/backend commits, artifact IDs/digests,
hosted route ownership, утверждённые org/account и период, наблюдателя и окно наблюдения.
Точные latency/error budgets утверждаются release owner до canary; отсутствие бюджета
или окна — NO-GO, а не повод выдумать SLA из исторического p95.
Полный suite/typecheck/build должен пройти либо иметь явно принятое владельцем исключение
с точными failure IDs. Исторические 29 failures не являются бессрочным разрешением релиза.

Нужен проверенный flag-off rollback artifact, совместимый с текущим backend.
Подтвердить возможность вернуть его до включения allowlist. Сохранить конфигурацию без
секретов в release evidence; bearer/cookie/сырые customer payload не сохранять.
Canonical read flag не разрешает API mutations и не заменяет backend authorization.

## Gate 1 — локальные проверки перед отдельно разрешённым deploy

1. Повторить команды focused/typecheck/full/build из Stage 1 handoff на release commit.
   Сравнить exact failure IDs с immutable base, не только число. Проверить отсутствие
   случайного generated Vella timestamp diff; не обновлять snapshots массово.
2. Missing/empty/malformed allowlist → off. Проверить duplicate org, unknown account,
   unsafe integer, trailing delimiter; валидный префикс не включает сломанный mapping.
3. В enabled режиме проверить fake 401/403/404/422/429/5xx/network/HTML/malformed JSON:
   явная ошибка, не demo/legacy fallback. Проверить account/session/period switch при
   transport, игнорирующем AbortSignal: старые данные скрыты уже на первом render.
4. Все pages одного account/period/formula/snapshot/meta/summary; duplicate nmId и
   повторные offset, drift и недогруженный результат не публикуются как complete.
5. Unknown money остаётся null. Final net profit/profitClass/abcCode не вычисляются в UI;
   preliminary profit подписан отдельно; blockers/evidence не удаляются.

## Gate 2 — только после отдельного разрешения production

Сначала deploy с пустым `VITE_CANONICAL_WB_ABC_PNL_ROLLOUT`. На hosted frontend проверить
rewrite `/api/v2` раньше SPA; GET достигает backend, сохраняются предусмотренные auth
semantics, JSON не подменяется HTML. Не посылать mutation requests ради проверки methods.
Flag-off должен сохранять прежний read route; отсутствие canonical запросов проверить
по безопасным network metadata без записи auth headers.

Затем отдельный frontend build для ровно одной утверждённой пары `organizationId:accountId`.
Остальные пары остаются off. До включения проверить, что account принадлежит org и backend
действительно проверяет finance.read/allowed-account, а не доверяет browser mapping.

## Gate 3 — read-only canary

Для одного фиксированного завершённого периода сверить browser и backend одного snapshot:

| Что | Требование |
|---|---|
| Scope/period | Exact org/account/dateFrom/dateTo и business UTC boundaries |
| Rows | Total совпадает, все offsets покрыты, duplicate nmId отсутствуют; null bucket не более одного |
| Snapshot | Finance snapshot/checksum/version и полная meta стабильны между страницами |
| Formula | Только поддержанная `wb-abc-pnl-fullstats-loyalty-v1` |
| Money | Integer kopecks, signed corrections сохранены, nullable не стало zero |
| Classification | Final profit/profitClass/abcCode остаются null при текущем контракте |
| Sources | Advertising source/evidence/checksum сохранены без повышения достоверности |
| Blockers | Все исходные blockers видимы; partial не представлен как ready/full |
| Summaries | Exact backend summary, не подмена суммой загруженной части |
| UX isolation | Смена account/session/period не показывает предыдущие значения |
| Performance | Samples/count, p50/p95/error rate и elapsed full pagination внутри заранее утверждённых budgets |

Исторические 457 rows, 2,283 kopecks и 90.19 ms — не текущие expected values и не SLA.
Не запускать collectors/warm-up/scheduler для получения желаемых цифр.
Backend не имеет отдельного source-stale state: не утверждать наличие freshness guarantee
по одному frontend TTL. Missing freshness policy остаётся отдельным blocker.

## Stop и rollback

Немедленно остановить расширение canary при tenant/account leakage, неверной формуле,
подмене null, drift/duplicates, частичной выдаче как complete, demo fallback или auth/rewrite
ошибке. При превышении budgets действовать по утверждённому release gate; не расширять allowlist.

`VITE_*` — build-time flag, **не runtime kill switch**. Удалить mapping и rebuild/redeploy
либо вернуть заранее проверенный flag-off artifact. Старый открытый tab может продолжать
работать на прежнем JS: проверить новый документ/asset version, reload и поведение открытых
сессий. Этот rollback сам по себе не отзывает backend permission у старого bundle.
При security incident требуется backend access-control response владельца T1, не UI-флаг.

После rollback подтвердить artifact ID, off mapping, восстановленный legacy read path,
auth, отсутствие новых canonical запросов из свежей сессии. Backend/schema не откатываются
в рамках frontend-only процедуры. Зафиксировать причину и read-only evidence, не удалять историю.

## Legacy retirement

Удаление только после functional parity, integration gates, завершённого approved observation
window и проверенного writer rollback. Для Orders/Production дополнительно нужны согласованные
contracts T3, единственный authoritative writer и parity XLSX/PDF/labels/print receipts.
Для Reviews/Notifications — durable storage/auth/CAS/recovery gates. Этот read canary
не разрешает live send, price apply, allocation, delivery, print confirmation или export.
