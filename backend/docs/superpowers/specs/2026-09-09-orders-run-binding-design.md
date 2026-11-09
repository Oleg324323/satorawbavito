# Orders run: immutable account binding

2026-09-09. T1 narrow forward-schema design, не implementation/activation.
Authority: `0b8ae9dab5c5f8a97d31bb3fc7f9cb3e7e8e2cc1` schema request и
local `1e25e71` полный `app/orders/bindings.py`, updated source-binding request,
`tests/fixtures/orders/account_binding_golden_v1.json` (5 vectors).
Существующие Orders0062/0064, guard8338ef7 и T3 multiaccount read contract прочитаны.
T3 прямо подтвердил: binding frozen FROM INSERT, legacy unbound сохраняется,
existing org RLS не меняется на single-account GUC. Новые Production tables —
отдельный dual-RLS contract; не смешивать две границы.

## Threat / выбранное решение

Numeric account ID переживает external/ref rebind. Текущая membership и совпадение
account ID не доказывают seller старого snapshot. Guard защищает fetch→commit race,
но не превращает изменяемый `lk_audit_events.details` в immutable provenance.

Выбрано additive nullable binding в существующем order_sync_runs; отдельная таблица
не нужна. Audit-only stamp не даёт DB immutability, а backfill из current account
ошибочно переименует старую историю. Никакой автоматической relabel/migration reads.
Контракт доказывает сохранённый account binding, не подлинность ответа provider или
историческую A→B→A эпоху, для которой отсутствует source contract.

## Поля

Все новые поля nullable для old rows, без DEFAULT backfill:

```text
account_binding_schema_version SMALLINT
account_binding_external_account_id TEXT COLLATE "C"
account_binding_credential_ref TEXT COLLATE "C"
account_binding_payload BYTEA
account_binding_checksum TEXT
```

Unbound означает ВСЕ пять SQL NULL. Bound означает schema1, external required,
payload/checksum required, ref legitimately nullable. Частично заполненная shape
отклоняется CHECK. Parent run org/account/marketplace уже существуют и входят в
canonical bytes; новые duplicate owner columns/FKs не создаются.
External ID nonblank1..128 Unicode codepoints; ref NULL или nonblank1..255.
Nonblank соответствует bool(Python strip), но содержимое НЕ trim. Допустимы
leading/trailing whitespace и interior control characters; NUL/lone surrogate
отклоняются, допустимый Unicode сохраняется. Это границы guard metadata, не cap
для order/item external IDs. Existing account provider wb/avito.

## Точный v1 producer

Per-run ровно один account, canonical positional ASCII:

```text
[organization_id,[[marketplace_account_id,provider,external_account_id,credential_ref]]]
```

Version хранится снаружи bytes, не добавляется в checksum. IDs canonical positive
INT4 numbers, provider/text JSON strings, ref JSON null или exact string. Compact
JSON, ensure_ascii True, lowercase escapes, UTF16 pairs для supplementary Unicode,
без normalization. SHA256 BYTEA lowercase64. Не заменить named object, jsonb::text
или stringified account ID. Коллизия hash не означает равенство: проверять bytes.

Dedicated immutable helpers с fixed search_path и typed positional contract:

```text
orders_binding_text(text,integer)->boolean
orders_binding_ascii_string(text)->text
orders_run_binding_bytes(integer,integer,text,text,text)->bytea
# organization_id, marketplace_account_id, provider, external_id, nullable ref
```

CHECK payload = reconstructed bytes и checksum=encode(sha256(payload),'hex').
Missing/extra/alternate lexical fields, floats/bools/string IDs, whitespace,
duplicate/multiaccount input не пройдут exact comparison. Отдельный JSONB projection
не нужен: typed external/ref + existing owner fields уже дают независимую проверку.
Нативное ограничение PostgreSQL storage явно остаётся, произвольный новый byte cap
не придумывается. Source codec bounds ограничивают размер нормального payload.

Literal fixture копируется byte-for-byte из1e25e71 с provenance; outer JSON decode
один раз, canonical_ascii.encode('ascii'). Первые четыре vectors — SQL helper parity;
two_accounts vector — отрицательный per-run insertion, не менять shared aggregate
codec ради single-run модели. Additional boundary vectors через stdlib json не
выдаются за фактический запуск T3 producer в T1.

## Insert / immutability / concurrent rebind

Новый BEFORE INSERT trigger действует только на bound rows. READ COMMITTED;
canonical account FOR UPDATE по exact run org/account, затем fresh metadata read
сравнивает provider/external/ref (NULL точно) с замороженными полями. Missing/mismatch
фиксированный safe error. Existing0064 INSERT уже account-serialized; повторная
проверка не заменяет/не переписывает его uniqueness semantics. INSERT не владеет
чужой existing run tuple до account lock; не вводить run→account UPDATE inversion.

Новый BEFORE UPDATE trigger всегда сравнивает пять binding fields OLD/NEW и запрещает
любое изменение, включая unbound→bound, bound→unbound, staging modification и sealed
modification. Existing0062 terminal guard остаётся без rewrite. Old unbound inserts/
updates по старому контракту продолжают работать; нет универсального mandatory flag.

Trusted new service получает paired fetch evidence и live guard, создаёт bound staging
и seals в той же owned root. Account lock удерживается до commit. Если rebind победил
до INSERT — mismatch; если INSERT/guard победил — commit с исходным binding, затем
rebind возможен, и будущий read под новым binding отклоняет историю. Schema не выдаёт
auth для отдельного staging/seal сервиса: current live authorization остаётся guard.
Нет дополнительного account lock из UPDATE trigger; source stamp immutability не
объявляется универсальной auth-проверкой любых SQL writers.

## RLS / ACL / errors

Существующая FORCE org RLS сохраняется. Multiaccount Orders read проверяет каждый
account through guard. Same-org unrelated-account denial — application guard proof,
НЕ новый single-GUC database SELECT policy. Payload wrong account/provider/org
отклоняется reconstruction/FK/current canonical INSERT comparison; wrong org runtime
RLS тестируется отдельно. Это явная граница, не заявление о DB account RLS.

Новые helper функции SECURITY INVOKER, fixed search_path, revoke default PUBLIC
execution/grant options; narrow required execution для существующих entitled
non-owner grantees. Не переписывать old table/default ACLs. Runtime script добавляет
только required helper grants. Column data доступна по existing table SELECT:
binding/ref — metadata, не credential/ciphertext/nonce/verifier. Не логировать её как
inventory. Все custom exceptions fixed safe codes, service sanitizes DB exceptions.

## Forward migration / verification / rollback

Revision выбирается по actual sole head перед implementation, явно отражается в
плане. Нет reserved number или edit0062/0064. Upgrade добавляет nullable columns,
checks/два триггера/helpers и узкие grants в одной транзакции. Synthetic old unbound
rows/counts/values/ACLs сохраняются byte-for-byte; не читается production.

Actual disposable runtime tests: four golden parity + multiaccount rejection,
strict null/Unicode/whitespace/limits, wrong typed ownership/provider/hash/bytes,
old unbound compatibility и запрет read-time stamp, staging/terminal field mutation,
two-session account rebind↔bound INSERT winner orders, exact replay changed binding
не переиспользуется в T3 consumer. Fresh own nonowner role org RLS и permissions;
no new same-org account-RLS claim. Current runtime-script latest fixture и historical
feature pin separate; graph future-successor regression.

Downgrade ACCESS EXCLUSIVE exact order_sync_runs, genuine all-row visibility;
любое non-NULL new binding field запрещает downgrade ДО drop/alter. Hidden rows/
visibility error fail closed; no CASCADE/data deletion. Empty/unbound-only downgrade
roundtrip сохраняет исходные rows/old schema. Rollback decoder-capable binary на
expanded schema, unbound histories failclosed, не возвращение к mutable audit proof.

Own venv, scrubbed env, secret/IP-denying sandbox, authorized Unix-only random
disposable DB/runtime roles, exact finally cleanup/absence. No app DB/workingRedis/
provider/production/realcredentials/.env, no flags/router activation/push/deploy.

## Critical pass / dependencies

Checked positional bytes versus named-object sketch; null/ref semantics; guard
whitespace preservation; arbitrary hash identity; stale external mapping; UPDATE
lock inversion; forced RLS scope overclaim; partial NULL shape; destructive
downgrade. Tests are required, not executed by this document. T3 owns codec/ORM/
repository/freeze/read/HTTP acceptance after exact migration is ready; immutable
source stamp is mandatory before new router activation. Old audit stamp is not
promoted to security authority and no derived backfill is performed.
