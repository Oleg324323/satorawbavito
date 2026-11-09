# T2 → T1: Stage 4A normalized settings/assignments/liquidation/context

Точный bounded DDL request, независимо от approvals и Orders. Исследованные owners:
`repricer_bff.py:741 DEFAULT_ALGORITHM_SETTINGS`, `:900–909` mutable overrides и
assignments, `:1190 LIQUIDATION_ACTIVE`, `:4227 _sku_cost_settings`, `:5795`, `:5945`;
`repricer_execution.py` читает globals во время calculation. Existing characterization:
80 cases в `test_repricer_calculation_characterization.py`. Этот request не меняет
формулы, production defaults, current writer или shared schema. Physical names ниже
предлагаются T1; logical fields/constraints обязательны. Номер миграции выбирает T1.

## 1. Bounded scope

Stage 4A переносит core calculation settings, SKU operational overrides, четыре
назначения (`baskets_orders`, `night_price_mode`, `plan_fact_interval`, `illiquid`) и
liquidation lifecycle. Other strategies/configs, notification preferences, economics
policies, templates и diagnostics не складываются в fallback JSON. Unsupported legacy
setting/assignment остаётся blocker и legacy evidence, не молча теряется. Полный
one-writer cutover запрещён, пока все реально используемые unsupported branches не
получат отдельный typed contract/parity. T1 может принять 4A до оставшихся расширений.

Настройки по существующему назначению org-owned; источник перед импортом должен
доказать org owner. Не клонировать shared module state во все org/accounts. SKU
operational overrides/assignments/liquidation — `(org,account,CatalogSku)`; canonical
SKU принадлежит org, marketplace mapping принадлежит account. Без разрешённого
mapping к SKU import/update blocked. Article — только отображение/evidence, не key.

## 2. Общий revision/head protocol

Immutable revisions отдельно от mutable head. Revision = positive integer, начинается
с 1 для каждого owner key; parent_revision NULL только для 1, иначе предыдущая head
revision. Head создаётся **только с первой revision в одной transaction**, пустых
head нет. Head.version начинается с 1, каждый успешный replace ровно +1; revision=head
version новой записи. Same-key/same-command replay возвращает original revision,
без нового audit. Same idempotency key + другое содержимое/actor/scope → conflict.

Command idempotency key — canonical lowercase UUID4, сгенерированный API application
для одной пользовательской команды, preserved через transport retries. Scope UNIQUE
в command owner; никакого UUID-only lookup. Клиент не выбирает revision/version.
Command checksum — SHA256 canonical typed patch encoding, содержит owner + command
kind + expected_version + membership + полный новый typed row + child rows sorted
by their logical key; bool/int/string/null различимы, NUMERIC values decimal strings
без exponent с сохранением mathematical value. Serializer фиксирует T2 repository
до его включения; до этого DDL может хранить проверенный lowercase checksum и
immutable command key, но не объявляет payload-equality proof реализованным.

Сохранение revision + children + head CAS + audit — одна transaction. CAS predicate
включает весь owner key и expected_version. Insert revision, проигравший head CAS,
полностью rollback. History не удаляется/не обновляется. Нет whole-JSON flush.

## 3. Org algorithm settings revisions

`wb_repricing_settings_versions` PK `(organization_id,revision)`;
`wb_repricing_settings_heads` PK org, current_revision NOT NULL FK same org,
version=current_revision >0, updated_at timestamptz. Version rows: parent_revision,
created_at timestamptz, actor_membership_id int composite org FK, command_id UUID,
request_checksum char64, formula_compatibility_version nonblank text. Все metadata
NOT NULL кроме parent. No server defaults for algorithm values: импортирует проверенные
значения existing organization; новые settings создаёт explicit authorized service.

Фиксированные колонки v1 (имена ниже — exact legacy field mapping, T1 может snake_case):

| Тип/проверка | Колонки |
| --- | --- |
| BOOLEAN NOT NULL | nightMedianEnabled, nightMedianCollectEnabled, nightMedianGlobal, nightMedianAutoApplyEnabled, workerAutoApplyPricesEnabled, minPriceSyncEnabled, priceJumpProtectionEnabled, discountStepEnabled, priceRoundingEnabled, liquidationAutoFlagEnabled |
| finite NUMERIC NOT NULL | targetMarginPct, priceStepPct, maxPriceChangeDailyPct, promoMarginThresholdPct, priceJumpStockValueMinPct, priceJumpSppMinPct, priceJumpStockQtyMinPct, csvMaxCostDropPct, csvMaxPriceDropPct, discountStepPct, nightMedianApplyDeltaPct, warmupMarginPct, warmupDailyLimitPct, liquidationStepPct, liquidationMinCogsPct |
| INTEGER NOT NULL >0 | syncIntervalMinutes, basketNormPeriodDays, cartComparisonDays, planFactFactPeriodDays, planFactIntervalHours, warmupDays |
| INTEGER NOT NULL >=0 | cartHighBasketsThreshold, cartLowBasketsThreshold, basketNormAutoMinOrders, warmupExitBaskets |
| INTEGER NOT NULL 0..23 | nightMedianWindowStartHour, nightMedianWindowEndHour |
| enum text NOT NULL | nightMedianMode: conservative/aggressive; basketSignalMode: matrix/thresholds; basketNormMode: fallback_by_type/auto/manual; planFactMetric: orders/revenue/margin |
| exact nonblank text NOT NULL | nightMedianTimezone (preserve old text, timezone normalization belongs to service) |

Finite NUMERIC исключает NaN/±Infinity. Не вводить новые business clamp/min/max для
percent во время state move: old guards/normalization остаются formula owner.
Actual unsupported enum значения import блокирует до отдельного compatibility amendment.
`nightMedianAutoEnableAllSkus` — alias `nightMedianGlobal`, не второй mutable column;
`nightMedianDefaultAllSkusVersion` — migration provenance, не новая operational policy.
`syncIntervalHours` — legacy alias/context adapter; не второй canonical scheduler owner.
Worker/auto-apply значения — сохранённые preferences, не разрешение включить flags:
deployment flag, approval/dispatch, current guards и one-writer fence обязательны.

`wb_repricing_basket_norm_defaults`: PK `(org,settings_revision,garment)`;
garment enum tshirt/hoodie/longsleeve; norm_units integer>=0; FK settings revision.
Все три rows обязательны для resolved fallback_by_type version, deferred final-row
check либо transactional service + acceptance test; no nested dict column.

Excluded scalars (`sppAccountingMode`, wbWalletType, marginCalcMode, acquiring/tax/
other expenses/logistics/localization, cogsByGarmentRub): financial/source compatibility
extension, не переносить сюда как guessed canonical economics. `CostsService`/
`EconomicsService` уже владеют dated values. Until explicit resolved context их
legacy planning defaults можно только читать в characterization, not activate new writer.
Telegram/digest fields → Notifications owner, cache/sync worker knobs → shared owner.

## 4. SKU operational overrides

`wb_repricing_sku_override_versions` PK `(org,account,catalog_sku_id,revision)`,
same metadata/parent protocol; head с тем же owner key. WB account discriminator,
account-org FK и `(org,sku)` FK обязательны. Fixed nullable override columns:

- boolean: automation_enabled, allow_negative_margin, night_median_enabled;
- integer >=0 kopecks: p_min_kopecks, p_max_kopecks, rrp_kopecks,
  min_margin_kopecks, max_margin_kopecks;
- finite numeric: min_margin_pct, max_margin_pct, price_step_pct;
- integer >0: price_step_minutes;
- integer >=0: basket_norm_manual;
- basket_norm_mode enum auto/manual/fallback_by_type, nullable.

NULL = inherit resolved org/default policy, explicit zero сохраняется и далее
интерпретируется existing formula guard. Не создавать price observation из pMin/pMax
или manual proposal. Scope-only `replace_overrides(expected_version, full typed values,
actor, command_id)` сохраняет новую full revision; разные SKU не разделяют head.
Cost/tax/logistics не дублировать здесь. COGS reference в calculation context указывает
на `catalog_cost_versions`, а не на mutable SKU override. Другие текущие override
поля требуют explicit extension: unknown keys блокируют migration/cutover.

## 5. Assignment revisions

`wb_repricing_assignment_versions` PK `(org,account,sku,revision)` + head. Metadata
как выше. `strategy_id` nullable: NULL — явное unassign; иначе один из четырёх v1
IDs выше. `assigned_at` timestamptz NOT NULL, `source` enum manual/legacy_import.
`interval_hours` integer>0 required только plan_fact_interval, для остальных NULL.
Произвольного config JSON нет. baskets/night/illiquid v1 parameter fields берутся
из immutable settings/override/context references; custom legacy configs вне этой
формы blocked, не discard. Assign/clear → new revision + head CAS + audit.
Первый explicit unassign допустим: revision1, strategy_id/interval_hours NULL,
event=cleared, before_revision NULL. Он фиксирует явный выбор, не создаёт фиктивное
предыдущее назначение. Повтор той же command не создаёт новую revision.
`typedStrategyId` derived registry mapping, не вторая mutable identity.

Independent pure encoding implementation: `app/modules/wb_repricing_assignments.py`
`AssignmentChange`; schema tag `wb-repricing-assignment/v1`, commandKind
`replace_assignment`. Full sorted compact ASCII JSON includes command UUID,
org/account/SKU, membership, expectedVersion as exact decimal string, strategyId
(explicit JSON null for clear), intervalHours, source and assignedAt normalized to
UTC ISO8601 microseconds with `Z`. Checksum is lowercase SHA256 of those bytes.
No clock/global/state access. IDs are strict internal INT4; strategy enums and
interval/null combinations follow this section, not guessed legacy defaults.
This fixes only assignment command bytes, not encoding for other Stage4A domains.
It does not prove historical replay lookup, auth, durable revision/head CAS or import
compatibility for unsupported configurations; those still require the real repository.

Cross_marketplace и bundles остаются disabled. schedule/group/plan_fact_period/daily/
optimal/metric/stockout/turnover требуют собственного typed config extension до
переноса соответствующих users; их нельзя объявлять migrated по существованию 4A.

## 6. Liquidation campaign и revision history

`wb_repricing_liquidation_campaigns`: scoped UUID4 server-generated campaign_id,
org/account/sku FK; immutable created_at, started_by_membership_id org FK,
start_price_kopecks>0. Один незавершённый campaign (active **или paused**) на
`(org,account,sku)`; partial UNIQUE slot включает оба состояния. Paused продолжает
занимать slot, resume не конкурирует с новым campaign. Finished campaign
не переиспользуется. `wb_repricing_liquidation_versions`: PK scoped campaign/revision,
head у campaign хранит current_revision NOT NULL/version=revision. Full typed revisions:

| Поле | Contract |
| --- | --- |
| state | active/paused/completed/cancelled |
| current_price_kopecks, target_price_kopecks | positive integers, explicit values |
| step_pct | finite NUMERIC >0; existing formula clamp применяется отдельно |
| hold_orders_to | integer>=0 |
| next_step_at | timestamptz required active, NULL paused/terminal |
| requires_negative_margin_confirm | BOOLEAN NOT NULL |
| confirmed_by_membership_id, confirmed_at | оба NULL либо оба NOT NULL; composite org FK |
| resulting_approval_id | nullable scoped FK к approvals после их DDL; ссылка, не send status |
| parent_revision/created_at/actor/command/hash | общий revision contract |

Creation active revision1. active→active(step result), active↔paused,
active/paused→completed/cancelled. Terminal никаких transitions. Resume не делает
price POST; создаёт/использует scoped approval owner. Negative margin требует
подтверждения для соответствующей campaign revision/target: изменение target/step,
если guard снова требует confirmation, создаёт revision с подтверждением NULL.
Не считать истечение next_step_at разрешением на send. Real apply идёт через
durable approvals, а current price history — через outcome, не memory mutation.

## 7. Immutable calculation context references

`wb_repricing_calculation_contexts`: scoped UUID4 context_id, org/account/sku,
as_of timestamptz, formula_version nonblank, context_checksum char64, created_at.
NOT NULL settings_revision; nullable override_revision/assignment_revision/campaign
revision (NULL означает отсутствие именно domain row). Explicit source manifests
через typed child references prices/stock/economics (после соответствующих DDL).
`cost_version_id` nullable FK `(org,catalog_cost_versions.cost_version_id)` плюс
SKU alignment; `cost_state` enum confirmed/assumed/missing. Economics policy IDs
должны ссылаться на существующие `EconomicsService` rows, типы/FK публикует T1.
Не backdate cost по поздней цене. Org-wide policy остаётся org-owned.

Mapping manifest/version — dependency Catalog owner: current tables не дают
immutable mapping token. До его появления context storage может быть согласован
отдельно, но context нельзя считать reproducible/eligible. Не использовать mutable
marketplace_product.updated_at как доказательство mapping revision.

## 8. Audit, isolation, acceptance

Для version/head domains append-only `wb_repricing_state_audit`: organization_id,
domain enum settings/sku_override/assignment/liquidation, command_id UUID4,
actor_membership_id required composite org FK, occurred_at required timestamptz.
Owner/reference columns и CHECK matrix:

| domain | account / catalog_sku_id / campaign_id | revision columns |
| --- | --- | --- |
| settings | NULL / NULL / NULL | settings_before nullable, settings_after required |
| sku_override | required / required / NULL | override_before nullable, override_after required |
| assignment | required / required / NULL | assignment_before nullable, assignment_after required |
| liquidation | required / required / required | liquidation_before nullable, liquidation_after required |

Все revision columns других families NULL. Каждая before/after ссылка имеет
составной FK на соответствующий scoped version PK; liquidation FK включает campaign
и проверяет его sku/account. after=coalesce(before,0)+1, before совпадает с parent
новой revision. UNIQUE по domain/full owner/command (NULLS NOT DISTINCT). Account
и SKU references также имеют composite org FKs. События settings/override: created
при before NULL, replaced иначе. Assignment: cleared при after.strategy_id NULL
(в том числе первое explicit unassign), created при before NULL и ненулевой strategy,
replaced иначе. Liquidation: created при before NULL/active; replaced active→active;
paused active→paused; resumed paused→active; completed/cancelled из active/paused
в соответствующий terminal. Terminal не имеет следующей revision.
Legacy import требует реально authenticated importing membership; не выдумывать
исторического автора и не использовать backfill actor для пользовательского действия.
No arbitrary metadata, no raw source/actor name. Expected state
и references соответствуют revision transaction. UNIQUE owner+command prevents replay.
Org settings FORCE org RLS; account entities FORCE org/account RLS. Runtime cannot
UPDATE/DELETE revision/audit history; head CAS grants только через accepted service.

Real PostgreSQL tests required (NOT RUN): two sessions same head CAS→one winner,
different SKU edits both survive, multi-table failure rolls back revision+head+audit,
same command replay no extra revision, foreign account with same SKU no access,
terminal campaign immutable, confirmation invalidated on changed intent, referenced
input revisions cannot be overwritten, old/new context calculation parity exact
kopecks/blockers on 80-case corpus. Schema downgrade empty-only. No auto-import
shared globals; no fence/cutover/replicas increase until all used branches proven.

**T1 acceptance requested independently:** fixed settings + children, bounded overrides,
four-strategy assignments, campaign revisions/heads/audit. Context FK-finalization
waits Catalog/source/economics references; it is not a blocker to the other tables.
This request explicitly lists unresolved extensions; it is not full Stage4 completion.

### Dormant override command bytes (T2 independent implementation)

`app/modules/wb_repricing_overrides.py` defines immutable full-row `OverrideValues`
and `OverrideChange`, not PATCH or persistence. All fourteen nullable fields must
be supplied; explicit zero/false differs from NULL. Strict positive internal INT4
org/account/SKU/member, canonical UUID4 command and nonnegative expected version
are included in sorted compact ASCII JSON (`wb-repricing-sku-overrides/v1`,
`replace_overrides`). Keys use camelCase for command ownership and snake_case for
the complete `values` row. Version/money are exact decimal strings; finite Decimal
percentages use context-independent canonical fixed-point strings, with trailing
zeros and signed zero normalized. No float coercion or new formula clamp.

`canonical_bytes(max_bytes=...)` and `checksum(max_bytes=...)` require an explicit
positive trusted resource budget; compact decimal exponents are checked before
expansion. SHA-256 covers complete command bytes. This is not authorization,
revision/head CAS, a backfill serializer or permission to activate legacy writers.
T1 DDL acceptance and real persistence tests remain required.

Dedicated SQL parity prerequisite: `tests/fixtures/wb_repricing_override_golden_v1.json`
pins six full typed commands, canonical ASCII bytes, byte count and SHA-256. Cases:
all NULL; explicit false/zero/signed decimal zero; all14 fields with negative/fractional
percentage normalization; 2**80 money and expected version; exact long Decimal
precision and exponent forms; changed full scope/actor/command plus fallback enum
and INT4 maxima. Fixture inputs store Decimal values as strings, money/version as
exact JSON integers; decode with arbitrary-precision integers, never JS Number.
Canonical output intentionally encodes money/version as decimal strings. Parse outer
fixture once, ASCII-encode canonical_ascii once; no second JSON encoding. Test
`test_literal_override_sql_vectors` reads pinned vectors, never regenerates them.
Literals were initially generated from the existing encoder and pinned; they are
not independently proven SQL output. The8192 budget is synthetic only. Python
unbounded ints/Decimal do not imply unlimited PostgreSQL NUMERIC representability;
DDL/service must fail explicitly outside native limits, never narrow/round or rehash.
No codec/formula changes in this fixture slice. Fixed mutation permission, live
mapping authorization, persistence/CAS/audit and empty-only downgrade remain gates.

### Org settings / liquidation exact command encoding

`app/modules/wb_repricing_state_commands.py` now implements the other requested
source encoders. These are pure inputs to T1 SQL parity and T2 repositories, not
DDL, executor authority or a one-writer cutover. All values remain explicit:

- `AlgorithmSettingsValues` has exactly the section3 typed scalar fields, in
  snake_case; no aliases, economic fields or arbitrary JSON extensions. Percentages
  are finite Decimal without new business clamps. `AlgorithmSettingsChange` owns
  org/member/command/expected_version/formula_compatibility_version/full values and
  typed `BasketNormDefault` children. All three garments are required in
  fallback_by_type, other modes retain explicitly supplied zero-to-three unique
  children. Input list is copied to a tuple sorted by garment, never a live view.
- Tag `wb-repricing-algorithm-settings/v1`, commandKind `replace_settings`; keys:
  organizationId, actorMembershipId, commandId, expectedVersion,
  formulaCompatibilityVersion, values, basketDefaults. Each basket child has
  garment and normUnits. INTEGER columns/IDs are JSON integers; finite NUMERIC
  values and unbounded expectedVersion are canonical decimal strings.
- `LiquidationCampaign` captures org/account/SKU/campaign UUID, immutable origin
  membership/created_at/start_price. `LiquidationChange` includes the full campaign
  identity/origin, actor/command/expected_version and full `LiquidationValues`.
  Tag `wb-repricing-liquidation/v1`, commandKind `replace_liquidation`; outer owner
  keys match override commands plus campaignId. `campaign` contains
  startedByMembershipId, createdAt, startPriceKopecks; `values` uses the exact
  section6 snake_case fields. Money, step_pct and version are decimal strings;
  optional references/timestamps remain explicit JSON null. UUIDs are exact UUID4;
  resulting_approval_id is exact nonblank logical TEXT, never UUID-coerced.
- All timestamp bytes are UTC ISO8601 microseconds with Z. Sorted compact ASCII
  JSON and normalized fixed-point Decimal use the existing override encoder's
  context-independent primitive. Explicit positive `max_bytes` bounds every
  canonical_bytes/checksum call, including before compact exponent expansion.
- `validate_liquidation_transition(previous, command)` consumes a fully scoped
  immutable `LiquidationRevision`, checks campaign/version equality and the exact
  section6 transition matrix. No previous row requires expected_version0 and active
  creation by the immutable origin member. Terminal and paused→paused reject.
  Changed target/step with a required negative-margin guard cannot carry a retained
  confirmation; a new revision clears it. Repository must perform authorized exact
  historical replay before this new-revision validation, under real row locks.

Safe exceptions: `StateCommandValidationError` and `StateCommandConflictError` carry
fixed messages, not input values. No authentication, DB references, one-active-or-
paused-campaign uniqueness, current-head CAS, audit or source truth can be proven
by these value types. Those remain required SQL/service gates. Missing mapping
version/source references still block canonical reproducible context publication.

`tests/fixtures/wb_repricing_state_golden_v1.json` pins five synthetic full command
inputs, canonical_ascii, byte_count and sha256: settings all false/signed-zero with
all3garments; changed owner/actor/large version/negative and precise Decimal; active
unconfirmed liquidation; paused confirmed; terminal with large exact money/version
and Unicode logical approval. Vectors were generated once from this Python encoder
and pinned; they are NOT independent SQL acceptance. The16384byte budget is a test
budget only, not an operational policy. SQL must also reject native NUMERIC range
overflow explicitly; never silently round, narrow or rehash to fit.
