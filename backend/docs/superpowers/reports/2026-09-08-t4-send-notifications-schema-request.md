# T4 → T1: требования к storage этапов 3–5

Статус: **request, не установленная schema и не API contract**. Migration/ORM,
revision allocation, RLS/grants и app registration принадлежат T1. Реализация T4
начинается только после committed handoff и проверки фактического контракта.
Этот request дополняет `2026-09-08-t4-reviews-stage-2-schema-request.md`, не заменяет его.

## Общие требования

Все account-owned записи несут organization_id и marketplace_account_id. References
между такими записями — composite FK с owner, а не один UUID. Marketplace согласован
с account и review identity. membership также привязан к организации. RLS обязателен,
но service отдельно проверяет свежий membership, permission и allowed accounts.
Свободный UUID или frontend approved=true не означает authorization.

Immutable записи не обновлять при replay. Время — timezone-aware server/database UTC;
версии — positive bigint, expected_version обязателен на commands. Audit хранит
внутренние IDs/reason codes и версии, не customer text, prompt, credentials/provider errors.
Никакого JSON/file/memory fallback при outage. Не добавлять универсальную event bus
поверх billing outbox. Предлагаемые имена ниже T1 может уточнить в committed handoff.

## Reviews — минимальные владельцы состояния

| Relation | Обязательные поля / ограничения |
|---|---|
| review_policy_versions | scoped policy UUID, version, checksum, validated policy content, template/model version, actor, created_at; unique(owner, policy UUID, version), immutable |
| review_policy_heads | owner, current policy-version FK, version; одна текущая policy на account, conditional update |
| review_draft_revisions | owner, review fact FK, UUID, monotonic review-local revision, observation FK/checksum, policy-version FK/checksum, template/model versions, exact text/text checksum, safe generation provenance, actor, created_at; unique(owner, review, revision), immutable |
| review_decisions | owner, UUID, draft FK/revision, binding checksum, approved/rejected, actor membership FK, decided_at; immutable |
| review_workflow_heads | owner, review FK, current draft FK, current decision FK nullable, version; scoped FK prevents pointing at a different review's draft/decision |
| review_send_commands | owner, UUID, review/draft/decision FKs, immutable request checksum and idempotency key, approved binding/text checksum, state, version, current attempt FK, created_at |
| review_send_attempts | owner, command FK, UUID, sequence, lease owner token and expiry, dispatch marker, result/evidence references, safe error code; unique(command, sequence), fenced current attempt |
| review_reconciliation_evidence | owner, command/attempt/review FKs, reconciliation_started_at, observed_at, provider answer ID when available, exact answer checksum when provable, evidence kind/version, safe outcome; immutable, no raw response in generic audit |

Text хранится только в scoped domain storage; queue содержит IDs, не text/prompt/token.
Идентификаторы template/model обозначают версии, не произвольный customer input.
Policy canonical serialization/checksum и lifecycle deletion должны быть утверждены
до writer; не угадывать default policy и не backfill-ить неизвестный actor/account.

## Транзакции и lost-update gates

1. Generation читает observation/policy/head version. После fake/LLM результата новая
   revision становится текущей только conditional write по той же head version,
   source observation/checksum и policy. Проигравший результат не перезаписывает победителя.
2. Edit/regeneration очищает current decision pointer; старые revisions/decisions остаются
   immutable. Source ambiguity, changed source/policy и revoked actor блокируют send даже
   если pointer ещё существует. `review-decision-v1` из pure module — binding, не подпись.
3. Approval/rejection и audit — одна транзакция. Current decision — authoritative pointer;
   более старый approved не оживает после rejection. CAS conflict →409 без auto-retry approval.
4. Send idempotency uniqueness: `(organization_id, account_id, operation_kind, idempotency_key)`.
   Exact replay возвращает тот же command только после текущей authorization; разные request
   checksums →409. В checksum входят review/draft/decision/action и approved binding, не secret.
5. Partial unique constraint запрещает второй active send для review, включая queued/leased/
   ambiguous. Это сильнее защиты только одной draft revision: новая редакция не обходит
   неопределённую предыдущую отправку. Sent тоже не разрешает повтор: current review eligibility
   и отдельная явная action policy обязательны. Edit/delete answer не входят в send.
6. Intent + audit + enqueue intent/outbox фиксируются до network. Worker claim CAS-ит
   state/version/current attempt, выдаёт новый lease token. Все дальнейшие записи fenced этим
   attempt/token; истёкший старый worker не сохраняет dispatch/result поверх нового.
7. Dispatch marker durable до provider call. Не удерживать transaction во время network.
   Между marker и вызовом остаётся crash window: он намеренно приводит к ambiguous, не retry.
8. Expired lease без dispatch marker допускает только CAS reclaim с полной повторной проверкой
   approval/account/credentials перед dispatch. Marker без результата → ambiguous; duplicate queue
   delivery не отправляет снова. Старый worker обязан пройти fencing непосредственно перед marker.
9. Recovery CAS-ит тот же command version и current attempt ID, сохраняя evidence+audit атомарно.
   Границу reconciliation_started_at фиксировать перед текущим read, не восстанавливать из старого
   evidence. Сохранить её и проверить dispatch ≤ read_start ≤ observed_at ≤ server now для exact
   command/attempt; локальный CHECK read_start ≤ observed_at плюс transaction check scoped dispatch.
   Это сохраняет проверяемую границу для audit/restart; adapter отдельно исключает cached response.
   Empty/stale/partial provider response не доказывает отсутствие отправки. Только exact scoped
   проверенные answer checksum + provider answer ID могут подтвердить желаемый результат;
   иной проверенный answer — conflict. Недостаточная capability провайдера оставляет ambiguous.
10. После dispatch не «откатывать» command удалением marker/attempt или сменой idempotency key.
    Сетевой timeout, 5xx и crash после provider success одинаково требуют reconciliation.

`app/reviews/send_recovery.py` — только pure proposals для пунктов 8–9, не repository,
worker, гарантия exactly-once или готовый provider verifier. Матч текста подтверждает наличие
желаемого ответа, а не причинность конкретного сетевого вызова.

## Notifications — четыре отдельные сущности

| Relation | Обязательные поля / ограничения |
|---|---|
| notification_events | UUID, org, account nullable, explicit scope, producer, entity UUID/version, kind, dedupe key, schema version, occurred_at, safe display; unique(org, dedupe key), immutable |
| notification_receipts | org, event FK, authenticated membership FK, read_at nullable, dismissed_at nullable, version; unique(org,event,membership), atomic upsert |
| notification_deliveries | org, event FK, recipient membership/destination reference, channel, state/version, retry count/next attempt, current attempt FK; unique(event,recipient,channel) |
| notification_delivery_attempts | delivery FK, sequence, lease/fencing, dispatch marker, safe result code, provider receipt reference, timestamps; immutable history |
| notification_preferences | existing authoritative preferences owner reused after typed validation; versioned CAS; do not create competing settings storage |

Account-scoped event requires account. Org-wide разрешён явно и только для разрешённого
system kind; потерянный account не расширяет доступ. `notification-event-v1` задаёт identity.
Domain change + event либо outbox — одна транзакция. Duplicate event сохраняет исходный
occurred_at и payload; digest не заменяет unique constraint и payload comparison.

GET не создаёт receipts и не меняет read state. read/dismiss отдельные monotonic поля:
конкурентные upserts не стирают друг друга. Mark-all ограничивается authorized visible IDs
или фиксированным high-water sequence + тем же filter/scope; более новые события не затрагивает.
Поздняя потеря membership/account доступа блокирует read/action/delivery; общий org ID недостаточен.
Cache key включает org/account/membership/filter/version, cache очищается при смене auth scope.

Telegram payload строится только safe projection. Chat destination берётся из доверенного
scoped config, не event body. Нет network внутри DB transaction. Max attempts/backoff/cap
нужны как явная delivery policy до worker, не бесконечный retry. Transport без provider
idempotency: dispatched-without-receipt → ambiguous, без автоматического повтора; receipts
и delivery state не заменяют per-membership read receipt.

## Acceptance T1 → T4

- Один Alembic head, clean bootstrap/upgrade/rollback на disposable PostgreSQL; без рабочей БД.
- Composite FK negative tests cross-org/account/review/actor; RLS с реальной non-owner role.
- Concurrent CAS и uniqueness: два generators/approvals/sends, same-key exact/conflicting replay.
- Kill/restart до/после commit, lease claim, marker, provider success и result persistence.
- Старый lease owner не может записать marker/result; ambiguous не разблокирует второй send.
- Notification receipt upsert/read-dismiss races и mark-all/new-event race без lost updates.
- Outage fail-closed, no fallback; fresh membership revoke и recipient/account isolation.
- Immutable history и rollback без потери dispatch evidence; нет live/provider calls.

Нужен committed schema/ORM interface + test evidence + handoff; текущий документ не означает,
что T1 уже принял request. Integration и capabilities остаются default-off до всех gates.
