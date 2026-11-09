# T3 -> T1: immutable source-run account binding

Найдено на read preflight: numeric account ID недостаточен после завершённой
перепривязки external_account_id/credential_ref. Guard ловит изменение между fetch
и commit, но не доказывает, к какому external binding относится старая история.

T3 consumer теперь fail-closed:

- stamped publication receipt связывает run с fingerprint org/account/provider/
  external_account_id/exact nullable credential_ref;
- freeze проверяет selected coverage run и каждый current projection run;
- immutable snapshot high-water mark v2 содержит content и account-binding hashes;
- latest/explicit/cursor read сверяет frozen binding под live guard;
- unstamped runs и unbound snapshots не принимаются; ничего не backfill из current
  account metadata, поскольку это могло бы переименовать чужую старую историю.

**Остаточный blocker для activation:** сейчас run stamp хранится в `lk_audit_events.details`.
Эта legacy relation не является immutable/unique Orders provenance contract.
Это service-level проверка, не DB-enforced гарантия. Не выдавать её за завершённое
tenant hardening. Нужен narrow forward schema amendment владельца T1.

## Предлагаемый минимальный storage

Предпочтительно добавить versioned binding payload в существующую `order_sync_runs`,
не создавать отдельную domain table. Точные имена/representation выбирает T1:

- account_binding_schema_version=1;
- canonical binding payload (BYTEA + JSONB projection либо доказанный exact JSONB
  representation), exact fields organization_id, marketplace_account_id, provider,
  external_account_id, credential_ref (NULL сохраняется как NULL);
- account_binding_checksum SHA256 canonical bytes, не unique identity и не замена
  exact comparison. Payload содержит только metadata/ref, не secret/credential values.

Payload принадлежит exact org/account run, provider совпадает с marketplace;
positive integer types и nullable ref проверяются строго. Producer bytes могут
быть вынесены T3 в versioned codec после согласования representation с T1.
Состояние sealed run immutable уже должно защищать новые поля; проверить trigger
против их UPDATE явно, а не предполагать покрытие.

Existing unbound rows сохранить без подстановки. Consumer отказывает в публикации
derived view до доказанного исходного binding. Новый writer должен записывать
binding в staging run из уже проверенного guard и seal его в той же transaction.
Legacy writer compatibility/mandatory insert constraint согласовать с dormant
consumer rollout; никаких изменений live writer или feature flags в этой заявке.

Exact replay сравнивает frozen binding с actual guarded binding до возврата receipt.
Перепривязка не разрешает переиспользовать старые source observations под новым seller.
Snapshot v2 является derived fingerprint, не provider high-water mark и не auth token.

## Acceptance

Actual runtime SQL: UPDATE sealed binding denied; cross-org/account/provider rejected;
NULL отличается от non-NULL ref; exact Unicode unchanged. Two sessions rebind vs publish:
либо исходный guarded commit, либо отказ, никогда relabel. Старый sealed unbound run
не становится bound посредством read. Different binding exact replay rejected.
Wrong account FK/RLS отдельно от application guard. Downgrade с ненулевыми binding
данными отказывает без потери provenance; не удаляет новые поля с историей silently.

Статус: schema request, DB acceptance **NOT_RUN**. T3 не меняет shared ORM/migrations,
не подключает provider/production и не активирует новый HTTP router.

## Exact v1 codec и golden vectors

`app/orders/bindings.py`: ACCOUNT_BINDING_SCHEMA_VERSION=1,
serialize_account_bindings(org, tuple[ExpectedAccountBinding,...])->bytes,
deserialize_account_bindings(bytes)->(org, sorted tuple), account_binding_checksum.
Representation сохраняет bytes уже committed866d1a1 consumer, не меняет его hashes:

```text
[organization_id,[[marketplace_account_id,provider,external_account_id,credential_ref],...]]
```

Это positional v1 array, НЕ предложенный named JSON object из раннего design sketch.
Schema version хранится отдельно, не добавляется внутрь checksum bytes. Account tuples
sort by numeric account ID, IDs distinct, positive INT4 exact integers (bool запрещён).
Per-run binding содержит ровно один tuple совпадающего run account; multi-account
форма нужна aggregate snapshot, не разрешает multi-account source run.

Canonical encoding: Python JSON ensure_ascii=True, separators comma/colon без spaces,
UTF8 ASCII bytes, Unicode escapes lowercasehex; supplementary scalar -> surrogate pair
escape; без Unicode normalization/trim. Guard metadata rules сохраняются: provider wb/avito,
external account ID nonblank <=128 codepoints, ref NULL или nonblank <=255, NUL/lone
surrogate запрещены, допустимые leading/trailing whitespace и embedded tab/newline
сохраняются exact. Это existing account guard bounds, НЕ лимит external order/item IDs.

Checksum SHA256 этих bytes lowercase64hex. Decoder требует побайтово canonical input:
дубликаты account IDs, alternate numeric types, unsorted/whitespace encodings отвергаются.
NULL ref не становится empty string; empty ref не принят existing guard.

`backend/tests/fixtures/orders/account_binding_golden_v1.json`: пять synthetic vectors
null/ref/Unicode+supplementary+combining+control/INT4max/reversed two-account input.
Для SQL извлечь `canonical_ascii` после ОДНОГО decode outer fixture JSON и взять ASCII
bytes; не сериализовать эту строку вторично. Каждая vector содержит ожидаемый SHA256.
Fresh pure codec + assignment + wire tests:41PASS0.42s, exit0. Actual schema parity
остаётся T1 gate; producer RED missing exportedcodec -> GREEN записан в task.
