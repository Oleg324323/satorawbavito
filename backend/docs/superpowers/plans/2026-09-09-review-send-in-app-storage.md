# Review send and in-app physical package

Spec: backend/docs/superpowers/specs/2026-09-09-review-send-in-app-storage-design.md.
Source-first user order: complete implementation now, final tests/review afterward.
No reserved migration number. Existing0071 local services stay compatible.

## Task 1: Implement additive send and account in-app schema

Read full spec. Read all original exact T4 documents named in its first paragraph
with git show, unchanged encode_review_send in reviews/storage_payloads.py,
send_payloads.py, notification_storage_payloads.py, notification_contract.py and
tests/fixtures/reviews/send-in-app-storage-v1-golden.json at ae7db1d. Read actual
0071 parent tables/keys/helpers and current runtime grants. Do not copy T4 Python
domain code or create a second codec implementation outside the physical SQL one.

Allowed exactly three source/handoff paths:

- One new backend/alembic/versions/YYYYMMDD_NNNN_review_send_in_app.py, exact
  revision/down_revision determined and supplied by controller on dispatch after
  checking actual sole source head and absence. Stop on mismatch/collision.
- backend/ops/runtime-db-role.sql, ONLY new objects/grants/exact helper signatures.
- backend/docs/superpowers/reports/2026-09-09-t1-review-send-in-app-handoff.md.

Implement eight relations from spec, actual parent FKs, immutable histories,
strict byte serializers/checks, all state/lease/evidence/nullability matrices,
partial one-CREATE invariant, exact NUMERIC versions, reciprocal graph witnesses,
enqueue and historical notification source references, first-write receipt
version fencing, dual FORCE RLS, narrow ACL scrub/grants and empty-only downgrade.
No old migrations/parent indexes/0071 writer hooks, no operational policy values,
generic bus, preference/destination/schema invention, tests, router/domain/runtime
service edits, production/provider/real secrets/.env/network/DB/Redis actions.

Account lock first for same-account source/storage mutation; immutable rows do
not require UPDATE grants merely to SELECT FOR UPDATE them. Existing user/account
publication guards and future worker guards precede domain locks. Graph integrity
is not live membership/evidence authentication. Preserve this distinction in docs.

Do not execute tests/compile/import/lint gates or reviewers during this package;
retain complete final test obligations in handoff, not unexecuted PASS claims.
Publish exact columns/types/PK/FK/index/checks/helpers/grants, all SQL-safe codes,
transaction/lock order, transition audit/authority requirements, consumer mapping
and compatibility. Explicitly identify remaining T1 shared multi-member/worker
authority and T4 runtime work. Never derive credential or approver auth from enums.

One commit: `feat: add Review send and account notification storage`.
Status IMPLEMENTED / UNVERIFIED, no amend/rebase or operational activation.
