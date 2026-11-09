# Изолированный PostgreSQL для credential / ingestion security gates

2026-09-09. Test-only design, не production database или runtime migration.
Полностью прочитаны test_marketplace_credential_rls.py (330 строк) и
test_avito_ingestion_token_store.py (1029 строк), actual candidate.cluster /
disposable_database / migrate, текущий paired-fetch PostgreSQL fixture.

## Причина и решение

Два старых fixture запускают свой initdb/TCP cluster, создают фиксированные roles и
проверяют runtime URL на127.0.0.1/random port. На этом host native bootstrap ранее
получал ENOMEM. Разрешённый Unix-only fallback уже доказывает guard/account/schema
на свежих random DB/roles, но эти два файла его не используют. Их прежние native
проверки нельзя выдавать за заново пройденные или заменить SQLite/skip.

Варианты: чинить host/server settings (не разрешено); удалить native proof или
подменить DB mocks (теряет gate); **выбрано добавить explicit local mode**, сохраняя
native default и исходные assertions отдельной веткой. Использовать существующий
tracked allocator, не новую URL/env connector абстракцию. Никакой app URL в fixture.

## Scope / интерфейс

Меняются только два указанных test files, новый
tests/test_security_postgres_harness.py и handoff. Existing helper, runtime code,
models/migrations/grants/dependencies/CI не меняются этим slice.

`ORDERS_TEST_USE_LOCAL_CLUSTER == '1'` выбирает existing candidate.cluster и
candidate.disposable_database. Любой другой режим остаётся native default; не
считать произвольный URL или truthy typo разрешением использовать чужой сервер.
В local mode wrapper запрашивает cluster через pytest request только в этой ветке;
не запускает второй native fixture ради неиспользуемого аргумента. Существующие
startup-cleanup tests ingestion сохраняются с их exact failure/interrupt/stop
assertions на вынесенном private native generator, не удаляются как obsolete.

Каждый test module имеет свой random role name с безопасным фиксированным prefix
и uuid4hex suffix. Candidate allocator проверяет отсутствие exact DB/role, создаёт,
finally удаляет и подтверждает отсутствие. Никогда CREATE ROLE IF NOT EXISTS/reuse
старого фиксированного runtime role на shared local server. Local metadata содержит
actual generated name, mode, Unix socket and DB provenance, не маскируется под native
temporary port/data directory. Runtime URL получается из exact allocator URL через
SQLAlchemy URL.set(username=role), не строковый host replacement.

Common-within-each-file schema bootstrap выделяется из старого fixture и используется
native/local ветками; не копировать большие DDL blocks второй раз. Local role уже
создан allocator: bootstrap выполняет только grants в своей DB, не дублирует CREATE
ROLE. Native generator создаёт свой random role в принадлежащем ему cluster.
Owner/runtime engines disposed в finally при setup/test failure, до DB cleanup.
Только созданная DB и exact role в scope maintenance connection. Нет pg_ctl/server
restart/config/defaultACL/appdata queries на существующем local server.

## Сохранённые feature semantics

Оба existing historical fixtures создают synthetic production-shaped minimal
previous schema, stamp0060, upgrade0061→downgrade0060→upgrade0061. Сохранить этот
тестовый контракт и минимальные synthetic seeds/grants (включая права, позволяющие
проверить именно RLS, не только отсутствие DELETE privileges). Это НЕ empty DB
bootstrap proof; добавить отдельный fresh empty upgrade до0061 без stamp через
existing migrate helper в новом test file. Не заменять historical0061 на latest
head: последующие unrelated constraints не должны менять эту feature fixture.

Сохранить все credential org-RLS/FK/one-active tests, token verifier/redaction,
issue/revoke/expiry и actual two-session lock races. RUNTIME_ROLE assertion получает
actual random name, не прибитый старый constant. Не ослаблять исходные predicates
или превращать failures в skips. Существующие credential cases используют порядок
module seeds; этот slice не обещает исправить их отдельный dependency design.

## Proof environment

Native mode: exact owned tmp cluster dir, random loopback port !=5432, expected
native URL, owner pg_ctl startup/stop; старая cleanup evidence сохраняется.
Local mode: parsed URL host exactly /tmp или /private/tmp Unix socket, allocated
DB name exact helper name and fresh random prefix, role exact owned random name;
actual inet_server_addr IS NULL/current_database/current_user checks через свою
DB, nonowner/NOSUPERUSER/NOBYPASSRLS attributes. Не требовать fake native port!=5432
и не объявлять собственным существующий server process.

Использовать ownbackend/.venv, env-i, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null и
plan-owned OS sandbox deny IP/all other network, allow только exact local UnixPG.
Известные secret/.env/keychain/config reads запрещены. Не выводить ambient env/
URLs/secret fields. `/dev/null` passfile warning остаётся disclosed minor; не
совмещать root-cause adapter с warnings suppression или реальным password file.

## Required RED / GREEN / cleanup

- Новые tests до реализации проверяют local dispatch не вызывает native runner,
  требует exact explicit mode и использует allocator/actual generated role; RED
  missing local interface или старый native-only assertion, не production probe.
- Unit doubles проверяют routing и exact finally behavior, не заменяют runtime RLS.
- Actual local combined запуск всех existing credential-RLS/ingestion cases и
  нового harness module: no skips, natural exit, actual locks/RLS/canaries.
- Fresh separate empty0061 bootstrap без stamp; historical shaped cycles остаются
  отдельными fixtures. Final namespace cleanup/absence для всех own resources.
- Setup failure после role/DB allocation и при bootstrap/grants: finally закрывает
  engines и allocator убирает только exact names; failure injection synthetic.
- Положительные контрольные tests демонстрируют реальные операции и deny cases;
  никакой generic DBAPIError ловушки вместо проверки успешного fixture setup.
- Native start/stop tests остаются unit verified; actual native runtime gate здесь
  всё ещё NOT_RUN/host ENOMEM, не выдаётся за local-mode результат.

Сначала focused RED и routing GREEN, затем combined actual suite once; scoped
Ruff/compile/diff, отдельный reviewer. New files не переписывают соседние test modules.
Нет production/network/provider/realcredential/workingRedis/flag/push/deploy actions.

## Critical pass / limits

Проверены риски fixed global roles, silent local fallback, duplicated bootstrap,
helper source metadata против доказанного фактического URL, native false-proof,
startup cleanup regression, RLS→ACL substitution, stamped fixture→emptybootstrap
подмена и contaminated ambient URLs. Все остаются проверяемыми obligations, не
утверждениями об уже выполненной адаптации. Полный backend shutdown/baseline и CI
release gate — следующие отдельные проверки, не автоматически закрытые этими файлами.
