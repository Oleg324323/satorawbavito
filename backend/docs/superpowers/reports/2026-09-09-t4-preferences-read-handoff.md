# T4 — typed read-only notification preferences

Base `b17638f`, branch `codex/arch-t4-experience`. Slice разрешён заданием этапа 5
и выделен как независимый в T1 `9438186:2026-09-09-t1-schema-amendment-feedback.md`.
Commit реализации — commit, добавляющий этот отчёт.

## Реализация и integration boundary

`app/notification_preferences_read.py:read_notification_preferences(session, *,
organization_id, membership_id, user_id)` читает существующую
`lk_user_preferences.notification_settings`. Единственный SELECT связывает preference
с exact IAM org/member/user и проверяет is_active membership и user. SQL возвращает
column value, не ранее загруженный ORM object из identity map.

Input identity — доверенный service context. Этот helper не становится HTTP authorization
boundary: public caller не может выбирать чужой user/member через query/body. Авторизующий
service передаёт проверенные identities, tenant context и Session. Account visibility,
verified destination и fresh authorization перед dispatch остаются отдельными gates.

Exact shape из `8f4a6c4`: email.enabled/dailyDigest/criticalAlerts и telegram.enabled —
строгие bool, telegram.chatId — nonempty string либо null, unknown keys запрещены.
Partial legacy settings отклоняются как PREFERENCES_INVALID; отсутствующие flags не
превращаются в permissive default. Нет автоматического исправления существующих данных.
Возвращается frozen NotificationPreferenceFlags из четырёх bool. Chat ID проверяется
только структурно и не возвращается; enabled не доказывает verified delivery binding.

Missing row/inactive/wrong scope → PREFERENCES_UNAVAILABLE. Bad scope types →
PREFERENCES_INVALID_SCOPE. DB failure → PREFERENCES_STORAGE_UNAVAILABLE, без SQL/parameters
в сообщении или исходной SQL exception context. Payload shape error → PREFERENCES_INVALID.
Read не вызывает create/commit/flush/default getter или memory fallback. No-autoflush
предотвращает запись незавершённых caller changes. Нет cache или нового persistence owner.

Adapter пока не подключён к routes, workers или delivery; существующий cabinet getter
с side-effect creation остаётся вне этой реализации. Настройки записывает прежний owner,
будущий canonical settings writer ожидает T1 CAS extension.

## Проверка

- RED: новый test module падает из-за отсутствующего adapter module, exit 2.
- GREEN: **21 passed, exit 0**, настоящие SQLAlchemy SELECTs на disposable SQLite in-memory.
- Вместе с существующими T4 pure/normalization/recovery tests: **173 passed, exit 0**,
  **0 external-action attempts** при Python audit denial socket connect/getaddrinfo/sendto,
  subprocess.Popen/os.system. Import path проверен внутри T4 worktree.
- Compileall новых module/test: exit 0. Независимый read-only critic без важных замечаний.
- Проверены scope mismatch, revoke при stale ORM objects, missing row без создания,
  malformed/partial settings, SELECT-only без autoflush/commit pending changes,
  DB failure и повторный read после изменения preferences.

Команда focused из backend (существующий sibling venv — только interpreter):

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin PYTHONPATH=.:backend_contracts /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -m pytest -q tests/test_notification_preferences_read.py
```

Combined использует audit-denial runner из `2026-09-08-t4-schema-independent-handoff.md`,
включая tests/test_notification_preferences_read.py, test_review_send_recovery.py,
test_review_provider_lifecycle.py и три предыдущих test modules.

SQLite доказывает read/query/type behavior; PostgreSQL RLS, isolation и concurrent revoke
не проверены. Caller обязан выбрать свежий transaction snapshot; read в долгой Repeatable Read
transaction сам по себе не гарантирует свежесть authorization. Перед delivery требуется
отдельная revalidation. Full backend/frontend suites здесь не запускались.

Schema, migrations, shared context/config, production/working DB/Redis/providers,
printing/export/deploy/push/flags/CodeRabbit не затронуты. No real secrets/customer data.
