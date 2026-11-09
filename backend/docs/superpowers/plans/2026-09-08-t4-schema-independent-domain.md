# T4 schema-independent domain implementation plan

> **For agentic workers:** Use superpowers:executing-plans with test-driven-development task by task. The user explicitly requested independent work while schema is pending.

**Goal:** Implement pure Reviews approval validity and safe notification projection without a database, routes, tasks, external calls or substitute persistence.

**Architecture:** Immutable domain values consume the existing normalized review identity/fact. Decisions are predicates over freshly loaded scoped source/draft/policy/membership values; a later transaction owner supplies and persists them atomically. Notification projection accepts enumerated reason codes and safe internal references, not arbitrary provider/customer text.

**Tech Stack:** Existing Python 3.11+ standard library and pytest; no new dependencies/packages.

**Spec:** User Terminal 4 Stages 3–5 plus 2026-09-08-t4-reviews-stage-2-schema-request.md. This implements independent rules only, not the stages' persistence/delivery acceptance gates.

## Global constraints

- No backend schema, migrations, shared config/registration, production, external actions, CodeRabbit or flag changes.
- No memory/file repository; pure values do not claim durable approval or atomic CAS.
- No raw customer/generated text in repr/errors/notification payload.
- Pure checks never replace live membership/account checks and transactional compare-and-swap.

## Task 1 — Immutable Reviews decision boundary

Files: app/reviews/decision_contract.py; tests/test_review_decision_contract.py.

Interfaces: ReviewSourceEvidence wraps NormalizedReviewFact + observation UUID + source ordering state; ReviewPolicyVersion binds exact org/account/provider/policy UUID/version/checksum/template/model version; ReviewDraftRevision captures those fields and exact output checksum. ReviewActorScope is a trusted, freshly loaded membership projection, never a request-body authorization object. ReviewApprovalDecision binds an immutable draft and approver.

Functions: build_review_draft(...), decide_review_draft(...), validate_review_send(...). Inputs and result values are frozen, strictly validated; timestamps require timezone. Draft revision must equal expected_version + 1. Callers retain the DB write CAS; evaluating two old snapshots can never itself reserve a revision.

- [x] Write tests for exact approved send eligibility, new source observation/checksum, changed policy/template/model, changed current draft ID/version, revoked or wrong-scope approver/sender, rejected approval, answered/cannot-answer/unknown and ambiguous source, malformed input, immutable output and redaction.
- [x] RED: run tests/test_review_decision_contract.py before implementation; first failure is missing domain module.
- [x] Implement scoped comparisons and safe ReviewDecisionError codes. The essential gate is:

```python
if current_draft_id != draft.draft_id or current_draft_version != draft.revision:
    raise ReviewDecisionError("REVIEW_STALE_DRAFT")
if source.observation_id != draft.source_observation_id:
    raise ReviewDecisionError("REVIEW_SOURCE_CHANGED")
if source.fact.answered or source.fact.can_answer is not True:
    raise ReviewDecisionError("REVIEW_NOT_ANSWERABLE")
```

- [x] GREEN: run new tests plus existing test_review_canonical_contract.py with a scrubbed environment and socket audit denial.
- [x] Independent read-only review of this domain module/tests. Commit together with the second pure module as one schema-independent slice.

## Task 2 — Safe notification projection

Files: app/notification_contract.py; tests/test_notification_contract.py. No notification storage owner is created.

Interfaces: immutable NotificationSourceEvent with explicit org/account scope, internal UUID entity/version and event kind; SafeNotificationDisplay derives producer and stable dedupe checksum and provides only product-authored title/details/severity. No free-form payload, recipient IDs, customer text, auth material or provider error is accepted.

- [x] Write tests that same event identity is stable, org/account/event kind/version changes are distinct, org-wide scope is explicit, extra/raw payload input is rejected, projection contains only fixed copy and safe fields, and unsafe reason codes fail closed.
- [x] RED: run tests/test_notification_contract.py before implementation.
- [x] Implement a strict enum of Reviews lifecycle notifications initially: approval_required, send_blocked, send_ambiguous, plus explicit system_attention. Display copy is fixed; dedupe is SHA-256 of canonical typed identity, not mutable presentation copy or timestamps.
- [x] GREEN: run both new test files and existing normalization tests; no DB/Redis/network.
- [x] Independent read-only review. Persistent events, receipts, atomic mark-all and delivery attempts remain blocked on separate schema prerequisites.

## Verification and handoff

- [x] Compile new modules; run combined targeted tests from this worktree, never sibling application code.
- [x] Verify imports/tests with socket and process-launch audit denial; zero external-action attempts. Source inspection confirms no schema bootstrap, task enqueue or repository.
- [x] git diff --check; exact changed-file/ownership inventory; no secrets or generated snapshot diff.
- [x] Record tests and explicitly list remaining DB/restart/concurrency/API integration gates in 2026-09-08-t4-schema-independent-handoff.md. Full backend suite was not run for these isolated pure functions.
