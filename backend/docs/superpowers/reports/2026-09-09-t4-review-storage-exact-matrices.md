# T4 → T1: exact Review storage matrices

Ответ на `9438186:2026-09-09-t1-schema-amendment-feedback.md`, уточняет `8f4a6c4`.
Domain proposal для принятия T1; физическая schema и runtime не меняются. Review Facts
остаются отдельно принятыми. Подтверждённое чтение T1 amendment — commit `9438186`.

## Lease и head initialization

Lease token — UUIDv4, генерируется доверенным worker service при claim из OS random,
не принимается из HTTP/body/queue. Unique(owner, lease_token), immutable на attempt;
он является fencing identity, не credential и не заменяет authorization. Attempt UUID
также server UUIDv4; claim создаёт оба в transaction, возвращает их только winning worker.
Каждый новый attempt получает новый token. Queue передаёт command ID/version, не token.

Renewal разрешён только при command=leased, attempt=claimed, dispatched_at=null,
current attempt/token/version match и DB now<lease_expires_at. Dispatched/ambiguous
никогда не renew. Dispatch меняет attempt claimed→dispatched; command остаётся leased,
version V→V+1. Audit tuple leased→leased. Result write потребляет именно V+1, либо
актуальную version после допустимого CAS read; любой failed CAS требует reread, не blind overwrite.
Delivery fencing использует тот же predicate с delivery version/current attempt/token/live DB lease;
renewal только claimed/pre-marker, не ambiguous. Это дополняет прежние delivery matrices.

Policy head: отдельный server UUID, unique(owner), создаётся только с первым выбранным
policy-version FK, version=1. Committed empty policy heads запрещены. При select существующего
head expectedVersion>=1, при первом select expectedVersion=0 означает отсутствие строки,
unique race проигравшего даёт409. Immutable policy creation само по себе head не создаёт.

Workflow head: отдельный server UUID, unique(owner,review_id), создаётся с первой опубликованной
draft revision, current_draft_id non-null, current_decision_id=null, version=1.
Committed empty workflow head запрещён. Первая publication expectedVersion=0 проверяет absence;
последующие publication/decisions CAS-ят head version. Draft revision — отдельный монотонный
счётчик по review; approval increments head version, но не draft revision. Pure publication
predicate использует current draft revision, не workflow-head version: repository проверяет оба.
Decision устанавливает current_decision_id, draft остаётся прежним. Новая draft очищает decision.
Scoped FK гарантирует, что current decision относится к current draft этого же review.

## Result evidence и reason nullability

Review evidence хранит explicit discriminator `evidenceKind=dispatch_ack|reconciliation_read`,
`evidenceVersion=review-answer-evidence-v1`, verifierVersion (nonempty version label).
Stored `outcome=incomplete|exact|different`; immutable записи имеют UUID, owner/review/command/
attempt, observed_at и optional provider_answer_id/answer_checksum. Для reconciliation_read
required readId(UUID) и reconciliation_started_at: dispatch≤read_start≤observed≤server now.
Для dispatch_ack оба read поля null; observed>=dispatch; authenticated direct response должен
доказать exact review/account и answer, а не просто transport HTTP200.

Incomplete: отсутствует хотя бы answer ID или checksum; допустимы оба null. Exact/different:
оба обязательны, outcome соответствует сравнению checksum с immutable command text checksum.
Dispatch ack без достаточного содержимого становится incomplete/ambiguous. Если provider не
предоставляет доказуемый exact answer checksum/ID, success не угадывается из отправленного request.
No raw provider response/customer text в evidence. Read identity или direct response evidence UUID
replay сравнивает exact bytes; разные bytes → conflict без изменения history.
Pure recovery принимает только projection verified reconciliation_read; direct ack обрабатывается
отдельным worker transaction gate, не поддельным read timestamp.

| Command state | result_evidence_id | reason_code |
|---|---|---|
| queued, leased | null | null |
| ambiguous | null либо incomplete evidence | RESULT_UNKNOWN |
| sent | required exact evidence | VERIFIED_EXACT_ANSWER |
| conflict | required different evidence | VERIFIED_DIFFERENT_ANSWER |
| blocked | null | ACCESS_REVOKED, SOURCE_CHANGED, POLICY_CHANGED, NOT_ANSWERABLE, CREDENTIAL_UNAVAILABLE |
| cancelled | null | USER_CANCELLED |

| Attempt state | result_evidence_id | reason_code |
|---|---|---|
| claimed, dispatched | null | null |
| ambiguous | null либо incomplete evidence | RESULT_UNKNOWN |
| sent | required exact evidence | VERIFIED_EXACT_ANSWER |
| conflict | required different evidence | VERIFIED_DIFFERENT_ANSWER |
| blocked | null | те же пять blocked codes |
| abandoned | null | LEASE_EXPIRED_UNDISPATCHED |

Для current attempt sent/conflict/ambiguous/blocked command и attempt result/reason совпадают.
Reclaim exception: command queued очищает result/reason, historical attempt abandoned сохраняет
LEASE_EXPIRED_UNDISPATCHED; audit описывает причину transition. Все result FKs scoped по owner,
review, command и attempt. Evidence exact/different не может ссылаться на чужой attempt.
Incomplete reads могут добавляться без lifecycle change; они не заменяют существующую evidence
и не переводят sent/conflict обратно. Их immutable insert атомарно завершается сам по себе,
без send transition audit; состояние ambiguous остаётся прежним.

## Exact Review audit matrix v1

Общий envelope из `8f4a6c4` сохраняется. Audit state enum расширяется только для head-проекций:
policy_selected, draft_current, decision_current. Null указан явно; никаких free JSON snapshots.
Owner/org/account, event UUID, occurredAt и aggregate identity/version обязательны у всех rows.
Все не перечисленные ниже entity refs null. Worker actorMembershipId null; membership actor
required canonical **positive INTEGER membership ID**, проверенный по organization composite FK.
Notification recipientMembershipId также canonical INTEGER, не UUID и не user_id.

Обозначения refs: P=policyId, D=draftId, Q=decisionId, C=commandId, A=attemptId.
Local immutable events и head updates выполняет membership; send worker lifecycle — worker.

| Event | Aggregate / version | before→after | Required refs | Actor | reason |
|---|---|---|---|---|---|
| policy.created | policy UUID / policy version | null→null | P | membership | null |
| policy.selected | policy-head UUID / new head version | null→policy_selected при init, иначе policy_selected→policy_selected | P | membership | null |
| draft.published | workflow-head UUID / new head version | null при init, иначе draft_current либо decision_current → draft_current | P,D | membership | null |
| decision.approved | workflow-head UUID / new head version | draft_current либо decision_current → decision_current | D,Q | membership | null |
| decision.rejected | workflow-head UUID / new head version | draft_current либо decision_current → decision_current | D,Q | membership | null |
| send.created | command UUID / 1 | null→queued | C,D,Q | membership | null |
| send.claimed | command UUID / new version | queued→leased | C,A | worker | null |
| send.lease_renewed | command UUID / new version | leased→leased | C,A | worker | null |
| send.dispatched | command UUID / new version | leased→leased | C,A | worker | null |
| send.reclaimed | command UUID / new version | leased→queued | C,A исторической abandoned attempt | worker | LEASE_EXPIRED_UNDISPATCHED |
| send.ambiguous | command UUID / new version | leased→ambiguous | C,A | worker | RESULT_UNKNOWN |
| send.sent | command UUID / new version | leased либо ambiguous → sent | C,A | worker для direct ack/read; membership только для authorized reconciliation_read | VERIFIED_EXACT_ANSWER |
| send.conflict | command UUID / new version | leased либо ambiguous → conflict | C,A | как send.sent | VERIFIED_DIFFERENT_ANSWER |
| send.blocked | command UUID / new version | queued либо leased → blocked | C; A required если leased, иначе null | worker | пять blocked codes из таблицы |
| send.cancelled | command UUID / new version | queued→cancelled | C | membership | USER_CANCELLED |

No membership-supplied 'verified' flag: reconciliation evidence читает доверенный adapter/service,
membership только инициатор разрешённого действия. При user result CAS service повторяет свежие
membership/account permissions. Worker actor нельзя присвоить из HTTP. Result evidence reference
берётся из scoped command/attempt для audit review; envelope не дублирует payload evidence.
BeforeState определяется фактически прочитанной locked row, а не аргументом клиента.

Unique audit `(owner,aggregateId,aggregateVersion,eventKind)` и exact replay как раньше.
Init создаёт head+target+audit одной transaction. Policy.selected того же target с прежним
idempotent command не создаёт новую version; новое явное select требует expected version и
считается новым head update даже при том же target. Draft/decision/current pointers и audit
согласованы при commit. Cross-row CHECK/trigger/transaction mechanics выбирает T1.

## Сохраняемые отдельные gates

### Дополнение 2026-09-09: durable replay локальных команд

T1 правильно обнаружил пробел: предыдущие документы требовали exact retry после
смены head, но не определяли durable identity запроса. Существующий local audit
serializer сохраняется: его `commandId=null`; это поле зарезервировано для send.
`eventId`, head ID, generation ID и request UUID не взаимозаменяемы. Этот раздел —
новый domain contract для принятия T1, не утверждение уже существующей таблицы.

Выбран отдельный узкий immutable receipt локальной Review-команды рядом с audit
(предлагаемое имя `review_local_command_receipts`). Не generic bus/outbox и не send
command. Вариант «искать только latest head» теряет replay после следующей команды;
переиспользование audit.commandId нарушает действующий serializer. Receipt решает
оба случая без изменения `review-audit-v1` или добавления pending/worker state.

**Identity и хранение.** Canonical nonzero client request UUID `local_command_id`,
unique(organization_id,marketplace_account_id,local_command_id), без actor/kind в
unique key. Provider фиксируется scoped account FK. В рамках одного owner повторное
использование UUID для другого kind/actor тоже конфликт, не новая операция.
В разных аккаунтах UUID независимы; это не разрешает обращаться к чужому owner.
Actor — canonical positive INT4 membership ID, полученный сервером из live session,
не утверждение клиента. Session ID не входит в intent: тот же member может повторить
команду после перелогина, если снова имеет нужные права.

Receipt хранит typed owner/actor/operation/request UUID; canonical request BYTEA и
SHA256; immutable account-binding descriptor bytes/checksum текущего physical binding
по существующему `historical_binding.py`; canonical result BYTEA/checksum; completed_at
(DB time); ровно один scoped audit-event FK; типизированные result entity/head refs.
Полная строка immutable, UPDATE/DELETE запрещены runtime. Все target/member/audit FKs
scoped. Audit-event FK также unique в owner: разные completed команды не разделяют
один audit event. Audit event kind, actor, target/head/version обязаны соответствовать
receipt; request/result checksums проверяются по exact bytes. Никакого сырого HTTP body,
bearer, credential value, exception, generated text
в audit/result. Request publication может содержать текст: это restricted domain
evidence, хранится lossless bytes, не логируется и не выводится через repr/API receipt.
Descriptor может содержать credential reference, но не сам секрет; он не публичен.

**Exact request envelope `review-local-command-v1`.** Только schemaVersion,
organizationId, marketplaceAccountId, marketplace, localCommandId,
actorMembershipId, operationKind, input. UUID lowercase canonical/nonzero; owner/member
INT4 как текущая identity; input — закрытый объект следующей таблицы, без extra keys.
Canonical UTF-8 JSON как `storage_payloads.py`: sorted keys, separators comma/colon,
ensure_ascii=false, allow_nan=false. Typed integers, не bool/float; никакой Unicode/
whitespace/NUL normalization. Canonical bytes сравниваются вместе с digest, не только
hash. Existing policy/draft versions выше signed BIGINT остаются валидными: нельзя
сузить их до BIGINT/INT4 или пропустить через JS Number; physical representation
обязан сохранить exact integer. Head expected versions >=0; revision expectations
>=0; остальные versions >0. expected=0 означает absence только при разрешённом init.

| operationKind | Все и только поля input | Meaning/result audit |
|---|---|---|
| review.policy.create.v1 | policy (exact объект review-policy-v1) | Immutable policy creation, без head; policy.created |
| review.policy.select.v1 | policyId, policyVersion, policyChecksum, expectedHeadVersion | Явный CAS policy head; policy.selected |
| review.draft.publish.v1 | reviewId, externalReviewId, draftId, expectedHeadVersion, expectedDraftRevision, expectedPolicyHeadId, expectedPolicyHeadVersion, generation (exact review-generation-v1), text | Publication подготовленного immutable output; draft.published |
| review.decision.record.v1 | reviewId, externalReviewId, draftId, draftRevision, bindingChecksum, sourceObservationId, expectedHeadVersion, expectedPolicyHeadId, expectedPolicyHeadVersion, decisionKind | decisionKind approved/rejected; соответствующий decision audit |

В draft input generation уже содержит source observation/checksum, policy
ID/version/checksum, template/model, generation ID, mode, trusted actor и времена.
Её actor обязан совпасть с envelope actor. text — exact string из подготовленного
output, валидируется текущим draft predicate, сравнивается побайтно, не только hash.
Начальная draft: expectedHeadVersion=expectedDraftRevision=0; при existing head
оба >0. expectedPolicyHeadVersion всегда >0. Для decision обе expected head versions
и draftRevision >0. expectedPolicyHeadId — canonical nonzero UUID, не ID policy.
BindingChecksum и sourceObservationId сверяются с immutable draft,
reviewId/externalReviewId — с exact scoped Review Fact. Нельзя выбрать чужой generation
или подменить actor/binding из HTTP. Policies используют reviews:write, draft publication
reviews:write, decision reviews:approve; все требуют live scope и active account.

Это контракт **publication подготовленного результата**, не exactly-once generation.
Generation ID/provenance/timestamps должны повторяться как тот же prepared output;
новый output под уже committed localCommandId конфликтует. Запуск fake/LLM generation,
его durable preparation и HTTP mapping — отдельная service orchestration; нельзя
регенерировать данные до lookup receipt и выдавать их за исходный retry. Ни real LLM
credentials, ни provider I/O для этой локальной transaction не нужны.

**Result `review-local-command-result-v1`.** Exact fields: schemaVersion,
localCommandId, operationKind, auditEventId, completedAt, policyId, policyVersion,
draftId, draftRevision, decisionId, headId, headVersion. Все не относящиеся к kind
refs/versions explicit null. create: policy fields nonnull, остальные entity/head
fields null; select: policy/head nonnull, draft/decision null; publish: policy/draft/
head nonnull, decision null; record: draft/decision/head nonnull, policy fields null.
IDs, versions и timestamp исходные, не вычисляются из latest head. completedAt равно
audit occurredAt; UUID audit/decision/head при init генерирует сервер, они не часть
request и не заменяют localCommandId. HeadVersion после операции = expected+1;
draftRevision при publish = expectedDraftRevision+1; decision не меняет revision.
Receipt не утверждает, что этот head всё ещё current, и не даёт send authority.

**Transaction/replay порядок.** Fresh authentication → shared metadata guard/locks
и exact historical binding fence → lookup receipt по owner+localCommandId **до**
проверки текущих head/source для нового исполнения. При existing receipt текущий
actor/kind/request bytes и сохранённый binding обязаны совпасть. Чужой actor или
changed bytes/kind — typed conflict409 без старого payload; denied current rights —
403; смена account binding — conflict, никакого relabel. Если всё совпало, возвращаем
original result, даже когда policy/draft/source/head позднее изменились. Это receipt
прошлого действия, не повторная проверка его нынешней send-eligibility. Никаких новых
head versions, entities, audit или notifications при retry. Fresh guard снова
проверяется перед завершением root. Не отказываем exact receipt лишь из-за superseded
head, но никогда не обходим текущую авторизацию или account rebind.

При отсутствии receipt: locked current source/policy/workflow eligibility + CAS;
immutable entity/head mutation + audit + completed receipt (+ applicable in-app event
после его DDL) в одной caller-owned root transaction. Error/failed CAS откатывает всё;
committed pending/failed receipt не создаётся. Concurrent duplicate после metadata
lock заново читает receipt в READ COMMITTED, не использует cached pre-lock absence.
Uniqueness/CAS остаются DB fences; конфликт unique нельзя маскировать как success без
новой transaction и fresh guarded exact replay. Новый UUID при том же policy target
— новая явная команда и CAS, не неявный no-op. Existing immutable policy ID/version с
другими bytes конфликтует; создание той же policy другими command IDs не фабрикует
новое policy.created audit: оно конфликтует, exact retry требует исходный UUID.
Это уточняет command-level поведение; прежний low-level immutable policy byte-replay
не превращается в разрешение выпускать повторный policy.created под новым command ID.

При неизвестном результате COMMIT повтор с исходным UUID разрешает неопределённость
через receipt lookup; отсутствие receipt проверяется после живой transaction fence,
не предполагается из timeout. Нельзя автоматически выдать новый UUID. Для HTTP
success только committed receipt; generic failed-response caching здесь отсутствует.
Retention/cleanup не вводятся, пока не определён безопасный lifecycle evidence.

**Required local acceptance cases.** 1) selectA→selectB→retryA возвращает originalA,
head остаётсяB, counts неизменны; 2) publishA→decision/newdraft→retryA аналогично;
3) approve→reject/newdraft→retryapprove не оживляет approval; 4) same UUID changed
text, expected version, policy, decision kind, actor или operation409; 5) revoked
permission/session/account и rebind deny до выдачи receipt; 6) first-head race при
different keys ровно один winner, loser без entity/audit/receipt; 7) same-key concurrent
requests дают один набор entity/audit/receipt и одинаковый result; 8) exception после
head/audit до commit откатывает все строки; 9) fresh-process exact retry после successful
commit/lost response не повторяет generation/publication; 10) independent tenants/
accounts и identicalUUID не пересекаются; 11) all optional refs/nulls, >BIGINT versions,
NUL/Unicode output и canonical-byte drift; 12) same immutable policy different key
conflicts без второго audit. Это gate requirements, не уже выполненные PG tests.

Проверка amendment: сопоставлен с actual storage_payloads/decision/historical_binding
и текущими golden bytes, отдельный main-agent critical pass на actor/rebind/late-replay,
atomicity и private output. Runtime/schema/serializers не менялись. T1 acceptance и
физическая DDL обязательны до consumer implementation; этот пробел не блокирует Reads.

### Policy-selection epoch: P1→P2→P1 не восстанавливает старый approval

Это дополнение отвечает на отдельный T1 ABA admission вопрос. Существующий
`decision_contract._context` сравнивает только policy ID/version/checksum и сам
по себе не доказывает актуальность выбора policy. Новое physical/service условие
добавляется явно; existing `review-policy-v1`, `review-generation-v1`, decision
binding checksum и `review-audit-v1` bytes не переписываются задним числом.

Каждая immutable draft сохраняет **policy_head_id + policy_head_version** выбранной
policy (captured selection epoch), дополнительно к существующим policy ID/version/
checksum. Head ID — scoped FK на account policy head; captured version >0, exact
integer без сужения валидных versions. Ссылка на historical policy.selected audit
этого же head/version обязательна как immutable witness; соответствующие target
policy, actor-independent owner и version сверяются при publication. Head later
изменяемый: captured version не FK к его current version, не ON UPDATE CASCADE.
Нельзя вывести captured epoch из latest head при чтении старой draft или backfill.
Draft без trusted capture/witness fail-closed для новых decision/send; read history
может показать её без утверждения eligibility. Автоматическая relabel/backfill нет.

До подготовки generation сервер фиксирует scoped selected head ID/version вместе
с policy snapshot. Internal prepared publication intent несёт expectedPolicyHeadId
и expectedPolicyHeadVersion (добавлены выше). В transaction публикации locked head
должен совпасть с обоими значениями и immutable generation policy. Только после
этого новые draft capture/witness + workflow CAS + audit + receipt публикуются одной
root transaction. Head change во время fake generation отклоняет output; нельзя
подставить свежий head version к уже подготовленному output. Manual edit также
требует нового явного prepared intent/current selection и новой draft revision;
не обновляет policy epoch прежней draft или decision.

Для новой approve/reject команды submitted expected epoch, captured draft epoch и
locked current policy head ID/version должны совпасть; также проверяются policy
target/checksum и прежние source/draft/actor predicates. Клиентское значение само
по себе ничего не разрешает. Decision хранит immutable scoped draft reference,
поэтому наследует её epoch; повторять epoch в decision не обязательно. Если T1
дублирует поля, DB обязан запретить расхождение с draft. Будущий send intent/claim/
pre-dispatch service дополнительно сравнивает epoch immutable approved draft с
current head, не полагается только на старый `validate_review_send`/binding checksum.
Mismatch до dispatch блокирует новое действие с existing POLICY_CHANGED reason;
после dispatch остаются действующими ambiguous/reconciliation rules, историю не
превращаем в cancelled и не очищаем marker. Этот документ не реализует send service.

Каждый новый явный policy select увеличивает head version, даже при том же target.
P1/v1 selection epoch1 → P2/epoch2 → P1/epoch3: draft/decision epoch1 остаются
неактуальными, хотя policy content снова совпал. Нужно создать новую draft под
epoch3 и подтвердить её заново. Никаких массовых updates всех workflow rows,
background invalidation jobs, JSON/memory fallback или автоматического approval.
Точное повторение уже committed localCommandId по прежнему возвращает historical
receipt без нового действия: его результат не заявляет current eligibility и не
обходит проверку epoch при новом decision/send command. Новый UUID требует checks.

Дополнительные gates: (a) P1→P2→P1, прежняя draft + новый approval command rejected;
(b) прежний approved draft + новый send denied до provider; (c) same-target select
new key тоже меняет epoch, exact old key replay не меняет; (d) current target тот же,
но head UUID другой — deny; (e) selection switch между prepare и publication —
rollback без draft/audit/receipt; (f) forged client current epoch при old capture
deny; (g) historical decision receipt replay после switch сохраняет old result,
не переустанавливает current pointer; (h) concurrent select/decision lock winners
и revoke rechecks; (i) missing/wrong-owner audit witness и null legacy capture deny;
(j) новая draft/current epoch + новое explicit approval проходят локальные fake
gates. Эти случаи требуют будущей DDL/service проверки, не считаются уже пройденными.

### Manual edit: только непосредственный текущий predecessor

Уточнение по запросу T1 после `ad793f9`. Existing generation codec проверяет только
форму previousDraftId; ни он, ни legacy mutable approve/reject service не доказывают
current predecessor. Для canonical **нового исполнения** manual_edit выбираем exact
immediately current draft этого owner/review, не произвольную историческую revision.
Это explicit service/storage admission rule, не изменение existing generation bytes
или ужесточение уже опубликованного pure codec задним числом.

После fresh guard и отсутствия exact completed receipt: workflow head существует;
expectedHeadVersion и expectedDraftRevision >0 и равны locked current values;
generation.previousDraftId = locked workflow.current_draft_id. Ссылка scoped по
organization/account/provider/review и immutable. Новая draftId отличается от
previousDraftId, новая revision = current revision+1; запись старого текста/epoch не
обновляется. DDL сохраняет typed nullable previous-draft FK: required для manual_edit,
null для fake, совпадает с generation canonical bytes; enforcement currentness только
в publication transaction, не FK к постоянно меняющемуся current pointer.

First-draft manual_edit запрещён: отсутствие head не заменяется фиктивным predecessor.
Хотящий начать с ручного текста требует отдельного будущего mode/contract, а не
поддельного fake provenance. Historical restore/branch/merge тоже вне этой волны;
старый same-review previousDraftId отклоняется как stale, даже с подставленными
свежими expected versions. UI должен reload/rebase на current draft после409, не
blind retry с обновлённой version поверх прежнего intent. Cross-review/account FK
reject без чтения/выдачи чужого текста. Same-key exact historical receipt после
более новой draft возвращается по правилам выше, не повторяет predecessor check
как новую mutation и не возвращает head назад.

При изменившейся policy/source новая явно подготовленная manual edit может создать
новую draft от текущего текста с **новым** current source/policy-selection capture;
все generation/current source/policy/epoch checks обязательны. Нельзя автоматически
приписать свежий capture старой draft. Существующее approval очищается при любом
успешном новом draft publication, даже если text bytes те же. Approval новой draft
требует отдельной новой команды и current eligibility.

Сохраняем существующие pure semantics: publication требует current/unambiguous
source и current policy/context, но не требует answerable; approve дополнительно
требует unanswered и can_answer=true; reject не требует answerable, однако fresh
scope/current source/policy/epoch/draft checks не пропускаются. Не подменяем false/
unknown can_answer выдуманным разрешением; publication не даёт права отправки.

Нужные PG/service cases: current predecessor success; old same-review predecessor
со свежими expected versions denied; first manual edit denied; foreign predecessor
denied; stale versions denied; same text new revision clears decision; two editors
дают одного CAS winner без loser audit/receipt; historical exact receipt replay
не меняет head; source/policy смена требует new prepared capture. Данный docs-only
commit не заявляет эти DB tests выполненными; codec и golden bytes неизменны.

### Generation identity: один опубликованный output на owner/generation

По запросу T1 уточняется фраза `8f4a6c4` о generation replay. Для fake и manual_edit
одинаково: UNIQUE(organization_id,marketplace_account_id,generation_id) среди immutable
published drafts. Provider согласован с account; generation закрепляется за exact
same-review draft и prepared provenance/text, а не переиспользуется для другого review.
Generation ID — не UUID local command, не draft ID и не authorization capability.

Исходный localCommandId + exact intent после fresh auth/binding lookup возвращает
оригинальный receipt до новых generation/head checks, даже после следующих revisions.
**Новый** localCommandId с уже опубликованным generationId всегда conflict409, даже
если provenance bytes и text checksum совпадают. Никаких второго draft/revision,
head update, audit, receipt или notification. Changed bytes также conflict, не
перезапись. Это command-level уточнение replay; pure generation codec не меняется.
Unique включает owner, но не review/actor/mode: смена review, исполнителя или mode не
обходит уже занятый generationId. Foreign owner остаётся недоступным; одинаковый UUID
в разных разрешённых аккаунтах независим, не основание cross-owner lookup.

Уникальность generation и публикация draft/head/audit/receipt атомарны в той же root.
Concurrent different command keys с одним generation дают одного winner; loser не
оставляет partial rows. Полный rollback не занимает ID. Для нового намеренного
generation output нужен новый generationId и новый localCommandId с актуальным CAS;
это не способ оживить прежнее approval. Таблица неподтверждённых generation jobs/
leases не добавляется этим контрактом. Для timestamps остаётся существующее
completedAt>=startedAt и exact UTC representation; новых TTL/retention/clock-skew
ограничений здесь нет.

Дополнительные будущие service/PG cases: same original key replay stable; new key
same generation/same bytes409; new key changed bytes409; reuse across same-owner
reviews/modes denied; concurrent keys single winner; failed publication rollback
leaves no reservation; same UUID independent authorized account isolated. Эти gates
не исполнены данным docs-only изменением; existing encoder/golden bytes неизменны.

External notification destination/receipt/policy relations и org-wide system registry остаются
platform contracts T1; их FK/production values здесь не выдумываются. Account-scoped in-app
events/receipts от них не зависят. Policies/drafts/send schema может выпускаться отдельно.
Retentions/leases/retries production values остаются owner decision; локальные test values
не являются разрешением активации. T1 сообщил ENOMEM при disposable PostgreSQL bootstrap;
этот документ не устраняет environment blocker и не доказывает DB gates.

Проверка этого изменения: docs-only сопоставление с T1 feedback, фактическим canonical identity
и pure predicates; новых runtime/test/schema changes нет. Следующий gate — review/acceptance T1,
committed DDL и реальные disposable PostgreSQL tests. Не заявляется installed storage.
