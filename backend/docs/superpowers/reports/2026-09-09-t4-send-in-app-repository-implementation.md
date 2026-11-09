# T4 actual0074 persistence package — IMPLEMENTED / UNVERIFIED

Source implementation only. No imports/compile/lint/tests/PG/provider/queue or
independent review gate run. No shared registration, credentials/config/schema
authorship, UI activation, production, push or external actions. Final acceptance
remains pending, including tests for the new repositories themselves.

## Actual dependencies, not proposed tables

T1 full0074 handoff and actual908-line migration read before implementation.
Necessary unchanged source lineage consumed locally (no migration execution):

```text
b274a50 →65dff20  actual0072, parent0071
fdf99d9 →7fb6aa3  actual0073, parent0072
d1e90a9 →605a91e  actual0073 creation-authority fix
40d178b →5fa9ba5  actual0073 lock-order fix
a21a1cf →c611c4c  actual0074, parent0073
```

Earlier actual0071 prerequisite4f196cd remains. Imported migration/grant files
are T1-owned unchanged code, not new T4 schema or an acceptance claim. Historical
Production migration chain remains intact without resuming deferred Production.

## Source implementation and signatures

`app/reviews/send_tables.py`: explicit private SQLAlchemy Core mappings of all
eight0074 tables, no reflection or create_all. Versions use unbounded NUMERIC;
credential generation preserves the physical BIGINT distinction. All bytes are
BYTEA; no JSONB/schema substitution. Runtime grants still require actual T1 setup.

`app/reviews/send_repository.py`:

```text
ReviewSendRepository(connection, binding: ReviewBindingDescriptor)
create(command_id, review_id, idempotency_key, actor_membership_id, intent, authority)
transition(command_id, expected_version, expected_attempt_id, lease_token,
           event_kind, actor_kind, actor_membership_id, reason=None, evidence_id=None)
append_evidence(payload)
get(fixed_table, lock=False, **exact_scope_keys)
```

All method arguments after self are keyword-only except get's fixed table and
append_evidence's payload. Constructor checks active PostgreSQL root, physical
READ COMMITTED and exact org/account context; then locks the matching canonical
account. Caller MUST hold actual principal/member/session/credential guards
before construction. The binding object/GUC/actor enum/token are NOT authority.
No internal commits, savepoints, queue/provider I/O or secret resolution.

Before each send write, compare actual runtime unicodedata.unidata_version with
SQL review_send_unicode_version() and the bounded14.0.0 contract. Missing SQL
helper/storage fails, mismatch raises REVIEW_SEND_UNICODE_INCOMPATIBLE. This is
not a legacy app/migration ban or evidence that Unicode SQL parity was tested.

Creation validates canonical intent and exact owner, then replay matches original
creator/review/bytes/checksum/current binding. Replay returns the original immutable
creation projection plus created_audit_event_id, not a mutable latest lifecycle
masquerading as the original receipt. A new candidate command UUID cannot replace
the existing one. For new creation, authority accepts exactly the real0074 fields;
the future T1 guard must supply their evidence, not a client HTTP payload.

Creation order is audit INSERT/RETURNING→command→immutable authority→send.ready.
Transition order is audit INSERT/RETURNING→attempt INSERT or exact CAS→command CAS
→send.ready on reclaim. The returned DB time, random claim ID/token, version,
lease, marker, attempt sequence/state and completion are the ONLY projection
input. Caller timestamps are never assumed to survive a trigger. Canonical audit
bytes/hash are revalidated after RETURNING. Numeric comparison bind types stay
NUMERIC, and NULL attempt predicates use IS NULL rather than SQL =NULL.

Claim/renew/dispatch/reclaim/ambiguous/sent/conflict/blocked/cancelled consume
the actual SQL event protocol. No new state, retry policy or lease default is
introduced. Evidence insertion validates canonical bytes, exact owner and exact
replay ID/bytes/checksum; SQL supplies marker/time/outcome-reference integrity.
Authentication of the evidence remains the trusted adapter/service's job.

Every returned row is PRIVATE domain data, not a public response. Raw SQL/driver
errors must be mapped by the future trusted root boundary without exposing
parameters. Commit-time failures require rollback of the whole physical root.
No successful-return-before-commit API is provided by this participant.

`app/notification_repository.py`:

```text
NotificationRepository(connection, binding)
publish(review_id, entity_id, source_version, kind, source_audit_event_id=None)
read_visible(event_ids: list[str], recipient_membership_id: int)
mark_visible(event_ids: list[str], recipient_membership_id: int, action: read|dismiss)
```

Methods use keyword-only arguments. Same exact root/context/account serialization
contract; recipient membership MUST be derived from a live caller guard, never
accepted as authentication from a request body. No list/filter/pagination HTTP
contract is invented here: reads take explicit IDs only, preserve requested order
and perform no INSERT/UPDATE/receipt creation.

Publishing derives fixed safe projection/dedupe from the committed pure contract,
uses DB time/new UUID only for a genuinely new dedupe key, and preserves original
UUID/time/bytes on replay. Source_review/draft/command/audit physical witnesses
must match. The new producer must call this in the same root as its source
mutation. There is no hook on old0071 writer, backfill or inferred second owner.
Historical source identity metadata is checked against current account binding
without reading customer text. A stale identity fails closed; post-marker source
rebinding/notification composition still needs the actual closing service policy.

Visible receipt actions validate the closed explicit UUID-list intent, check ALL
events/source bindings before the first write, lock existing receipts in sorted
UUID order, then INSERT/ON CONFLICT UPDATE only the selected read_at/dismissed_at
column. The already held account lock serializes absent receipt insertion too.
Immutable events have no UPDATE grant: they use SELECT, not FOR UPDATE. This
avoids requesting a forbidden history-update privilege for locking convenience.

DB triggers derive first timestamps and physical version; RETURNING returns both
independent timestamps and exact int version. Canonical receipt bytes deliberately
exclude that version. Repeated action retains existing timestamps and does not
invent an earlier first time, reopen receipt or mark any future/unlisted event.
No cache, JSON snapshot, preferences rewrite, Telegram or external delivery.

## Actual remaining service work, not waived by repositories

- T1 concrete multi-member/executor/closing authority handoff, then T4 guarded
  root composition. Fresh sender reviews:send/current approver reviews:approve;
  user reconciliation reviews:read AND reviews:send. Never fake a worker session.
- New source/notification atomic service wiring; recipient live authorization and
  default-off capability policy before any dormant HTTP registration or UI switch.
- Fake-provider marker-commit-before-I/O chain, lost-commit readback, restart and
  duplicate-safe enqueue scan. Repository return is not a committed dispatch marker.
- Trusted fresh reconciliation read and evidence authenticity; lease-expired ACK
  cannot be promoted to reconciliation. No live-send authority implied.
- Final synthetic PG/golden/Unicode/RLS/revocation/concurrency/rollback/receipt
  merge/ghost-audit gates, then combined frontend/backend integration. NOT RUN.

The earlier workflow-history/typed HTTP-client implementation is retained in the
same worktree/package with its own explicit pending checks. Its pre-transport
45frontendPASS/TypeScript0 does not verify these repositories or the new transport.
