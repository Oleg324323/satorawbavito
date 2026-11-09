# T4 — canonical storage payloads

Implements the exact `review-policy-v1`, `review-generation-v1` and
`review-send-request-v1` representations from amendment `8f4a6c4`.
New module: `app/reviews/storage_payloads.py`; real behavior tests:
`tests/test_review_storage_payloads.py`.

Exact key sets, positive integer identities (not booleans), canonical UUIDs,
version labels, lower SHA-256, supported manual/fake modes, canonical UTC
timestamps and supported send operation are validated before UTF-8 encoding.
No normalization of external identity, Unicode or whitespace is performed.
Unknown fields, including raw text/token fields, are rejected. Error messages
are fixed safe codes; payload bytes are excluded from result repr.

Evidence:

- Initial missing-module RED preceded implementation.
- Independent critic found Unicode digits accepted in timestamps; regression
  reproduced **1 failed / 57 passed** before the ASCII-only fix.
- After fix: **231 passed**, exit 0, in combined serializers/preferences/recovery/
  provider-lifecycle/decision/notification/normalizer tests. Of these, 58 are
  serializer tests, including independent full golden bytes for all three types.
- Python audit hooks denied socket connect/DNS/sendto and process execution;
  observed external-action attempts: **0**.
- Independent re-review: scoped PASS, no remaining important findings.

Run from this worktree's backend with `PYTHONPATH=.:backend_contracts` and the
existing wave1-integration `.venv/bin/python`; imported code comes from T4.
Combined test paths: test_review_storage_payloads.py,
test_notification_preferences_read.py, test_review_send_recovery.py,
test_review_provider_lifecycle.py, test_review_decision_contract.py,
test_notification_contract.py, test_review_canonical_contract.py.

This is representation validation, not authorization, database ownership,
idempotency uniqueness or proof of installed storage. Repositories must compare
exact immutable bytes as well as checksums, validate scoped FKs, and execute
CAS/audit atomically. No DB migration, runtime API, send, provider or flag changed.

Coordinator reports T1 accepted `9284fe0`/`b17638f` and closed the five local/send
contract gaps. Actual committed DDL and PostgreSQL verification remain prerequisites
for persistence integration. Next independent work: classify and remediate frontend
failures against behavior, without inventing missing canonical HTTP contracts.

## Root review follow-up

Root independently reproduced 231 passing tests and reported Ruff violations on
`4b349ec`. Imports and 42 test-only `dict(keyword=...)` constructors were mechanically
fixed; timestamp comparison now explicitly attaches UTC after strict ASCII `Z`
validation. Accepted bytes and checksums are unchanged. Ruff using
`/tmp/satorna-backend-verify-20260908/bin/python -m ruff check` on both new Python
files: exit 0. Fresh combined tests after follow-up: 231 passed, exit 0.
