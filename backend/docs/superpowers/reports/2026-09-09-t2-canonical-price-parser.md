# T2: canonical seller-price parser и transport manifest

Независимый slice этапа 5, `app/modules/wb_price_snapshots.py`. Это работающий
локальный parser raw WB Prices JSON и assembler page prefix, без provider/persistence
calls и без переключения существующего repricer.

- Endpoint contract: `/api/v2/list/goods/filter`, explicit `data.listGoods` list.
  Missing wrapper, malformed row, duplicate JSON keys, duplicate product/known size
  identity, oversized page, explicit provider error/errorText отклоняют всю страницу.
- Fixed v1 units: RUB для seller/Club price, conversion по Decimal tokens без float
  arithmetic/context rounding. 100000 RUB = 10000000 копеек; никакого threshold guess.
  Fractional kopecks/NaN/Infinity/negative/string numbers/bools rejected. Signed bigint
  storage range проверяется, huge exponents не приводят к unbounded power allocation.
  Explicit currencyIsoCode кроме RUB rejected; endpoint v1 с отсутствующим currency
  трактуется как RUB по этому source contract. Legacy fixtures остаются в old adapter.
- PriceField хранит presence missing/null/value; explicit zero остаётся zero. Products
  и все sizes immutable. Product discount/Club discount не размножаются по размерам.
  sizeID сохраняется как native source key; отсутствие не заменяется nmId/index/CatalogSku.
  `source_sizes_identified` доказывает лишь наличие source IDs, не canonical mapping.
- Buyer aliases, historical SPP, wallet и неизвестные поля не становятся buyer facts.
  Raw response SHA256 вычисляется по оригинальным bytes до normalization. Source observed
  time остаётся None, receive time — explicit aware timestamp, без clock singleton.
- Assembler принимает только immutable непустую цепочку, offset=0, один org/account,
  limit и collection request checksum. Полная страница требует следующей, только
  explicit short terminal завершает transport run. После terminal нельзя добавить page;
  gap/backward receive time/cross-page duplicate product rejected. Неполный prefix
  возвращается complete=False. Нет фильтрации malformed rows ради ложного terminal.
- Manifest checksum: SHA256 ASCII compact JSON
  `["wb-goods-prices/v1",[org,account,limit,request_checksum],[[offset,limit,raw_checksum],...]]`.
  Collection request checksum задаёт trusted collector по request context без offset;
  parser не аутентифицирует его. Page/run/value constructors проверяют структуру,
  derived complete/products/checksum нельзя подать произвольными init parameters.

## Boundary и remaining

Transport completeness не доказывает immutable provider snapshot между страницами,
свежесть, mapping, полномочия caller или право на current publication. Published run
требует отдельного DB service, source context/manifest validation и identity gates.
Unknown size identity сохраняется для diagnostics, canonical offer projection blocked.
Новый parser не импортирован текущими WB fetch/price send paths. Формулы и flags прежние.

DB-dependent next slice: immutable run/page/product/size facts + scoped current pointer
CAS, account ownership/FORCE RLS и two-session publication tests. Exact source schema
request готовится отдельно, миграциями владеет T1. Никакой stock daily history по
этому price parser не создаётся.

## Verification

Новый module/API сначала дали RED missing import. Начальная green suite 41 tests;
critic выявил provider-error→empty-success и отсутствие hydrated-page validation.
Regression RED 12 failed / 42 passed; исправлено, **54 passed exit0**. Повторный
independent critic закрыл оба finding, новых blockers в parser/assembler не обнаружил.
Targeted Ruff pass; compileall/diff-check выполняются перед commit.

```sh
python -m pytest -q -p tests.repricer_offline_plugin tests/test_wb_canonical_price_parser.py
```

Python: local wave1-integration/backend/.venv; Ruff:
`/tmp/satorna-backend-verify-20260908/bin/python -m ruff`.
# Decoder correction — 2026-09-09

Root independent review reproduced Decimal.InvalidOperation for syntactically valid
JSON numeric tokens with positive/negative exponent beyond Decimal implementation
range. Two regression cases failed before the fix (54 original cases passed).
The raw decode boundary now translates DecimalException to PriceSourceValidationError
with suppressed context, alongside existing JSON/UTF-8 errors. No coercion, rounding
or payload reflection. Fresh parser56 plus root synthetic16 =72PASS; Ruff/compileall/
diff0. This is separate from approval Unicode227PASS and not a DB acceptance claim.
