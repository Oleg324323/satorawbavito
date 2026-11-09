# Orders schema candidate: план реализации

Результат: проверенный SQL-кандидат для T1, вне активной цепочки Alembic.
База: T1 `5ba31a0`, текущий head `20260908_0061`; контракт T3 `f4d6d55`.
Разрешение пользователя: отдельная подготовка схемы и тестов; T1 включает её в общую миграцию.

- [x] Проверить baseline контрактов и миграций на временной БД PostgreSQL (см. ограничение cluster в HANDOFF).
- [x] Написать PostgreSQL acceptance tests; зафиксировать RED до SQL.
- [x] Создать upgrade.sql / downgrade.sql: tenant/account FK, нормализованные факты,
  неизменяемая история, версии проекций, полное сохранение строк снимка, FORCE RLS.
- [x] Проверить пустую БД, существующую схему, пустой rollback, отказ rollback с данными,
  runtime RLS, межаккаунтные ссылки, replay race, immutable history, snapshot retention.
- [x] Независимо сверить контракт, исправить замечания, оформить handoff и локальный коммит.

SQL-файлы не получают Alembic revision. Тестовый wrapper в временном каталоге
использует `orders_candidate_test` поверх единственного head; T1 назначает
реальный номер только при интеграции. Не менять shared ORM, router, grants script,
файлы T1/T3 или их рабочие копии. Catalog constraints включены только в кандидат.

Production work items, команды, аудит assignment, рендереры, API и collectors
не входят в Orders schema slice: остаются domain/integration задачами T3/T1.
Источник требований: T3 Stage 1 §8 + corrections из independent-preparation,
`app/orders/{ingestion,contracts}.py` и DB01–DB14 из UI/DB handoff.
