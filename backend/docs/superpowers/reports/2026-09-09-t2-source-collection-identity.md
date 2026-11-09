# T2: canonical collection request identity

Реализация envelope из schema request `907ec7b`, pure/unwired:
`app/modules/wb_source_requests.py`, `tests/test_wb_source_collection_request.py`.
Два explicit kinds: unfiltered seller prices и WB warehouse stock. Typed frozen
request содержит org/account/parserVersion/pageLimit; fixed method/path/body/query
не принимает arbitrary headers/credentials/filters. SHA256 canonical ASCII compact
sorted JSON совпадает с DDL request. Offset относится к page manifest, не identity
всего collection. Никакого provider client/import, clock, DB, current-head publication.

FBS/filtered nmList/transport fallback/buyer SPP — отдельные неподключённые extensions.
Price adapter должен явно подтвердить selected transport mode до live collection;
существующий BFF пробует несколько shapes, этот serializer не объявляет их одинаковыми.
Stock adapter использует body pagination, price — query pagination. Коллекция не
переключает текущий loader. Shared auth/account resolver обязан проверить scope;
SHA256 связывает параметры, но не является подписью или доказательством авторизации.
ParserVersion — trusted caller input: совпадение с реально выбранным parser проверяет
будущий collector; storage сохраняет exact value, не silently upgrades версии.

TDD: missing module RED;26 initialPASS. Critic нашёл Python int-to-string overflow
до envelope limit:3RED→safe typed serialization error→29PASS. Никакой coercion или
new business cap; oversized canonical bytes >65536 rejected as contract requires.
Price parser56 + source revision29 + collection29 + root independent16 =130PASS.
Ruff/compileall/diff checks exit0. Не заявляется DB persistence, full backend green,
provider pagination atomicity или source/canonical Catalog mapping readiness.

## Следующий разрешённый slice

Typed WB warehouse stock page parser/manifest без FBS ambiguity; затем actual
repository после принятого T1 DDL. Partial/current/daily publication и simultaneous
DB sessions остаются обязательными отдельными gates. Никаких новых формул/flags,
источников OPEX, финансовых approvals или scheduler activation здесь нет.
