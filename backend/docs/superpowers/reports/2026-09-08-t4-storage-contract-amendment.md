# T4: уточнение storage contract после T1 d38e923

Ответ на committed `2026-09-08-t1-schema-contract-feedback.md`. Дополняет requests
`4a7d669` и `e4818e3`; физическая schema, migrations, grants и shared infrastructure — T1.
Review Facts уже приняты T1 отдельно. Этот документ не задерживает их DDL и не означает
утверждение T1 последующих таблиц. ORM `app/reviews/canonical_orm.py` создаёт T4 после DDL handoff.

## 1. Persisted lifecycle и nullability

Command immutable поля: scoped review/draft/decision references, operation, request bytes/checksum,
binding/text checksum, idempotency key, creator membership, created_at. Mutable поля:
state, version, current_attempt_id, completed_at, safe reason. Version начинается с 1,
каждый успешный lifecycle update увеличивает её на 1; replay и failed CAS не меняют version.
Expected version относится к command до операции, response возвращает version после неё.

| Command state | current_attempt_id | completed_at | Разрешённый следующий state |
|---|---|---|---|
| queued | null | null | leased, blocked, cancelled |
| leased | non-null | null | queued только после expired undispatched attempt; ambiguous, sent, conflict, blocked |
| ambiguous | non-null, с dispatch marker | null | sent или conflict только по append-only verified evidence |
| sent | non-null | non-null | нет |
| conflict | non-null | non-null | нет |
| blocked | nullable; если non-null, attempt без marker | non-null | нет |
| cancelled | null | non-null | нет |

Cancelled относится только к ещё queued command. Post-dispatch отозванный доступ не превращает
command в cancelled/blocked: он остаётся ambiguous до разрешённого reconciliation. Queued после
reclaim очищает current_attempt_id, но сохраняет исторические attempts. Новая claim создаёт новый
attempt. Blocked command не оживает после восстановления права: требуется новое явное действие.

Attempt — **mutable lifecycle row + append-only audit events**, не immutable row. Immutable:
UUID, owner, command FK, sequence, claimed_at и lease token identity. Mutable:
state, lease_expires_at, dispatched_at, finished_at, result evidence FK и safe reason.

| Attempt state | dispatched_at | finished_at | Переход |
|---|---|---|---|
| claimed | null | null | dispatched, abandoned, blocked |
| dispatched | non-null | null | ambiguous, sent, conflict |
| ambiguous | non-null | null | sent, conflict |
| abandoned | null | non-null | нет |
| blocked | null | non-null | нет |
| sent | non-null | non-null | нет |
| conflict | non-null | non-null | нет |

claim сохраняет lease_expires_at > claimed_at. Marker допускается только для текущего token,
command version/attempt и пока DB now < lease_expires_at. dispatched_at >= claimed_at.
finished_at >= dispatched_at, если marker есть, иначе >= claimed_at. История marker не очищается.
Renewal допустим только текущему владельцу при ещё живой lease, увеличивает command version;
heartbeat после expiry не возвращает lease. Значение lease duration задаёт утверждённая policy,
в этой волне production default не назначается.

Pure `RecoveryAction` — предложение, не SQL enum: wait/keep_terminal ничего не пишут;
reclaim атомарно делает attempt abandoned + command queued; ambiguous переводит обе строки;
confirm_sent → sent; conflict → conflict. RecoverySnapshot покрывает только выделенное подмножество
persisted states; queued/blocked service обрабатывает до его построения. Lease действует также
в ambiguous. Все updates fenced current attempt/token/version, audit в той же transaction.

## 2. Policy, provenance, canonical bytes

Локальный storage contract первой волны: `review-policy-v1`, только manual approval.
Payload — exact объект с полями schemaVersion, organizationId, marketplaceAccountId, marketplace,
policyId, version, approvalMode=`manual`, templateVersion, modelVersion. Других ключей нет.
Owner IDs — positive integers, UUID — lowercase canonical string, version positive integer;
template/model version соответствуют существующей grammar `[A-Za-z0-9_.:/-]{1,128}`.
Payload не содержит prompt/customer text. Расширение policy на tone/автоматическую generation
потребует новой версии контракта, не произвольных дополнительных JSON keys.

Canonical bytes: UTF-8 JSON, sorted keys, separators `,` и `:`, ensure_ascii=false,
allow_nan=false; без floats, whitespace normalization и Unicode normalization. SHA-256 lower hex.
Повтор unique(owner,policyId,version) принимает только те же canonical bytes, иначе conflict;
immutable policy не перезаписывается. Head switch — CAS с audit, не side effect GET.

Provenance `review-generation-v1`: schemaVersion, generationId(UUID), sourceObservationId(UUID),
sourceChecksum(SHA256), policyId(UUID), policyVersion, policyChecksum, templateVersion,
modelVersion, mode=`fake|manual_edit`, actorMembershipId, startedAt, completedAt.
Для manual_edit дополнительно required previousDraftId; для fake это поле отсутствует.
Время canonical UTC RFC3339 с фиксированными шестью знаками microseconds и Z, completed>=started.
В этой волне нет real LLM mode. Draft exact UTF-8 text/checksum хранится отдельно в scoped storage.
Generation ID replay сравнивает provenance bytes и text checksum; различие → conflict.
Identity snapshot source/policy ещё раз проверяется при publication CAS.

Retention остаётся owner decision. Не добавлять TTL cleanup job и не удалять evidence до утверждения
policy; это ограничение разработки, не назначение бессрочного production retention. Активация
production writers требует approved retention policy reference/version и процедуры удаления,
сохраняющей dispatch/idempotency/audit обязательства. T1 не должен придумывать значения.

## 3. Send identity, uniqueness, reconciliation

Единственный operationKind: `review.answer.create.v1`. Immutable request bytes:
schemaVersion=`review-send-request-v1`, operationKind, organizationId, marketplaceAccountId,
marketplace, externalReviewId, draftId, draftRevision, decisionId, bindingChecksum, textChecksum.
Канонизация из раздела 2; externalReviewId сохраняется как normalized string canonical_contract.
Нет token, generated text, auth flag, server timestamps или current lease в request checksum.
Idempotency key — отдельный client UUID, canonical representation, scoped unique
(org,account,operationKind,key). Same key+bytes → existing command после fresh authorization;
same key+different bytes →409. Compare bytes вместе с checksum; digest не единственное доказательство.

Partial unique review predicate включает `queued,leased,ambiguous,sent,conflict` для operationKind.
Таким образом sent/conflict навсегда блокируют ещё один CREATE в рамках этого контракта,
даже с новым draft или ключом. Будущие edit/delete/reopen требуют отдельного approved contract.
Blocked/cancelled освобождают возможность нового явного действия после fresh eligibility checks.
Нельзя создать новую revision, чтобы обойти ambiguous. Внешний existing answer блокирует create
независимо от наличия нашего sent command.

Evidence immutable: exact owner/review/command/attempt, read UUID, reconciliation_started_at,
observed_at, optional provider answer ID и exact answer checksum, verifierVersion.
Read start создаётся сервером перед fresh read; dispatch<=read_start<=observed<=server now.
Нельзя восстановить read start из старого evidence. Repeated read UUID сравнивает все canonical
evidence bytes; mismatch conflict без изменения command. Нет confirmed sent без обоих answer ID
и checksum и доказанной scoped provider identity; incomplete/empty остаётся ambiguous.
Verified different answer → conflict. CAS command version/current attempt, evidence и audit — одна
transaction. Результат sent подтверждает наличие желаемого ответа, а не causal attribution вызову.

## 4. Audit и enqueue intents

Audit vocabulary v1: policy.created, policy.selected, draft.published, decision.approved,
decision.rejected, send.created, send.claimed, send.lease_renewed, send.dispatched,
send.reclaimed, send.ambiguous, send.sent, send.conflict, send.blocked, send.cancelled.
Envelope: schemaVersion=`review-audit-v1`, org/account, eventId(UUID), aggregateId(UUID),
aggregateVersion, eventKind, occurredAt, actorKind=`membership|worker`, actorMembershipId nullable,
commandId/draftId/policyId/decisionId/attemptId nullable explicit references as applicable,
beforeState/afterState nullable enum, reasonCode nullable closed enum.
Membership actor ID обязателен для policy/draft/decision/send.created/send.cancelled;
worker outcomes используют actorKind worker, membership null, attemptId non-null, кроме
pre-claim send.blocked (attempt null). Пользователь reconciliation имеет membership actor.
Нет свободных metadata, raw exceptions, IP/userAgent, text или JSON before/after snapshots.

Closed reason codes v1: ACCESS_REVOKED, SOURCE_CHANGED, POLICY_CHANGED, NOT_ANSWERABLE,
LEASE_EXPIRED_UNDISPATCHED, RESULT_UNKNOWN, VERIFIED_EXACT_ANSWER, VERIFIED_DIFFERENT_ANSWER,
USER_CANCELLED, CREDENTIAL_UNAVAILABLE. Unknown error не сохраняется verbatim; service отображает
его на RESULT_UNKNOWN после marker либо CREDENTIAL_UNAVAILABLE только при доказанной credential
ошибке до marker. Другие pre-dispatch ошибки не выдумывают success/terminal result: transaction
откатывается либо текущая lease истекает. SQL и typed consumer используют один этот enum.

Unique audit identity `(owner,aggregateId,aggregateVersion,eventKind)`; replay принимает exact
canonical payload, иначе rollback. Все перечисленные state changes требуют audit transactionally.
Enqueue intent schema `review-enqueue-v1`: owner, commandId, commandVersion, eventKind=`send.ready`;
dedupe по всем этим полям, queue delivery содержит IDs/version. Reclaim создаёт новый ready version.
Дубликаты допустимы на transport, claim CAS отсекает проигравших. Physical outbox решает T1;
нет generic bus и нет network внутри transaction.

## 5. Notifications: payload, receipts, preferences и deliveries

Event `notification-event-v1` использует существующий `app/notification_contract.py`:
org/account/scope/producer/entityId/sourceVersion/kind, stable dedupe, occurred_at,
fixed title/details/severity. Entity UUID references: approval_required → draft UUID/revision;
send_blocked/send_ambiguous → command UUID/version; system_attention → registered platform event
UUID/version. Missing/unknown platform registry закрывает создание этого kind до отдельного
producer contract. Не использовать arbitrary UUID для внешнего provider object.
Reviews kinds только account scope. Explicit org-wide system event разрешается только после
platform producer authorization, не выводится из отсутствующего account. Source change/event
atomic; exact event replay сохраняет original occurred_at и сравнивает payload, не перезаписывает.

Mark-all choice: **явный список visible event UUIDs**, без high-water варианта в этой волне.
Каждый UUID проверяется в текущем org/account scope и fresh membership; один недоступный UUID
отклоняет всю transaction. Только переданные IDs, никаких newly arrived events. Receipt unique
(org,event,membership); read_at/dismissed_at first-write-wins отдельно, merge сохраняет другое поле.
Оба действия idempotent; GET не пишет. Unread/reopen не реализуются этим контрактом.

Authoritative preferences: `lk_user_preferences.notification_settings`,
`app/cabinet/orm.py:LkUserPreferenceRow`, user_id. Existing shape:
email {enabled,dailyDigest,criticalAlerts} booleans; telegram {enabled:boolean,chatId:string|null}.
Existing `get_preferences` создаёт row и имеет memory fallback; canonical read/delivery НЕ может
использовать этот helper. Нужен read-only typed DB adapter с fail-closed отсутствием/invalid shape,
без изменения authoritative owner. User preference — opt-in, не account authorization.
Версия existing row отсутствует: T1 должен согласовать CAS extension existing owner до settings
writer; новая competing notification_preferences relation из прежнего request больше не предлагается.

В первой волне durable in-app events/receipts независимы от внешней delivery. Единственный
планируемый external channel `telegram`; email delivery не реализуется из-за default email.enabled.
Recipient — fresh membership UUID/ID в org, destination — versioned verified binding reference
от platform owner, не raw chatId из event или одного legacy preference. Пока такого binding нет,
delivery не создаётся; schema его FK требует отдельного T1 committed contract.

| Delivery state | current_attempt_id | completed_at | Переход |
|---|---|---|---|
| pending | null | null | leased, suppressed |
| leased | non-null | null | pending, ambiguous, sent, suppressed, exhausted |
| ambiguous | non-null, marker required | null | нет в этой волне |
| sent | non-null, marker и scoped provider receipt required | non-null | нет |
| exhausted | non-null, последняя abandoned undispatched attempt | non-null | нет |
| suppressed | null до claim; non-null после claim, только без marker | non-null | нет |

Delivery attempts — mutable lifecycle rows и append-only audit. Exact states: claimed
(marker/finished null), dispatched (marker required/finished null), ambiguous (marker required/
finished null), abandoned (marker null/finished required), suppressed (marker null/finished required),
sent (marker/finished/provider receipt required). Claimed→dispatched/abandoned/suppressed;
dispatched→ambiguous/sent; остальные states terminal для attempt в этой волне.
Все имеют immutable identity/command-delivery FK/sequence/claimed_at, mutable lease и lifecycle
fields. Provider receipt — отдельный scoped reference, а не user read receipt.

Expired/no-marker attempt становится abandoned: если budget permits, delivery pending и current
pointer очищается; иначе delivery exhausted сохраняет FK последней abandoned attempt. Обе операции
сохраняют историю всех attempts. Suppression до claim: pending→suppressed, attempt остаётся null;
после claim до marker: claimed→suppressed и leased→suppressed с сохранением current attempt FK.
После marker opt-out/revocation не очищают историю и не возвращают в pending/suppressed.
Network uncertainty после marker всегда ambiguous, даже если retry budget остался. Ambiguous
не auto-retry; дальнейшая resolution требует отдельного provider reconciliation contract.

Immutable delivery policy snapshot reference обязателен: owner-approved maxAttempts positive int,
backoffSeconds positive-int list длины maxAttempts-1, leaseSeconds positive int, policy version.
Следующая попытка разрешена только до maxAttempts и not-before previous finished_at + соответствующий
backoff. Production значений здесь нет. Policy отсутствует → no external delivery activation.
Unique(event,recipientMembership,channel); destination version snapshot не меняется при replay;
rotation/revocation revalidated перед dispatch, never silently reroute historical intent.
Delivery audit — отдельный envelope `notification-delivery-audit-v1`, не Reviews envelope.
Exact fields: schemaVersion, eventId(UUID audit identity), organizationId, marketplaceAccountId
(nullable только explicit organization scope), scope, deliveryId(UUID), deliveryVersion positive int,
notificationEventId(UUID), recipientMembershipId, destinationBindingId(UUID), destinationVersion,
deliveryPolicyId(UUID), deliveryPolicyVersion, channel=`telegram`, eventKind, occurredAt,
actorKind=`worker`, actorMembershipId=null, attemptId(nullable), beforeState(nullable), afterState,
reasonCode(nullable). Все refs проверяются по owner; recipientMembership — адресат, не actor.
Canonical bytes/time из раздела 2. Unique(org,deliveryId,deliveryVersion,eventKind), exact replay
не меняет audit. State change+audit атомарны; никакого free payload/chat ID/customer content.

Enum eventKind: delivery.created/claimed/dispatched/reclaimed/ambiguous/sent/exhausted/suppressed.
Created: beforeState null, afterState pending, attempt null. Claimed/dispatched/ambiguous/sent/
exhausted: attempt required. Reclaimed: afterState pending, attemptId указывает завершённую
abandoned attempt, хотя current pointer уже null. Suppressed: attempt null только при beforeState
pending, иначе required и beforeState leased. Outcome actor всегда worker; receipt read/dismiss
не используют этот envelope и остаются membership actions. Closed reason codes:
OPTED_OUT, ACCESS_REVOKED, DESTINATION_REVOKED, LEASE_EXPIRED_UNDISPATCHED,
BUDGET_EXHAUSTED, RESULT_UNKNOWN, PROVIDER_RECEIPT_CONFIRMED. Created/claimed/dispatched reason null;
остальные outcomes выбирают доказанный code из списка; неизвестная raw ошибка не сохраняется.

## Открытые внешние решения и проверка

Не назначены production retention/lease/retry values, verified destination binding и platform
system-event registry. Их отсутствие блокирует соответствующую активацию и FK handoff, но не
Review Facts, review local schema или in-app receipt tests. T1 может выпускать эти slices отдельно.
Состояния/bytes/enums выше — предложение domain owner для принятия T1, пока не installed contract.
Проверка: сопоставление с T1 d38e923, pure modules и фактическим cabinet preferences owner;
документ не заявляет schema/DB/concurrency/provider tests. Все прежние PostgreSQL gates сохраняются.
