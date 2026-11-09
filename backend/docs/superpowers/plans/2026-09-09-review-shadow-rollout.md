# Review shadow rollout implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** Supply T4 a default-off exact-account selector without enabling a consumer.

**Architecture:** Two non-secret Settings fields, strict parsing and one pure
membership predicate; permission/account/provider checks remain independent.

**Tech Stack:** Existing dataclasses/os/pytest, no new dependency or service.

**Spec:** backend/docs/superpowers/specs/2026-09-09-review-shadow-rollout-design.md; read fully.

## Global constraints

- T1 worktree/branch only. No real environment/.env/secrets, provider/network,
  application DB/Redis, GitHub/push/deploy, operational flags or host changes.
- Only own backend/.venv; approved alternate Ruff interpreter is lint-only.
- T4 owns Review implementation and frontend. No consumers/routers/tasks/registry,
  legacy parser/default changes or canonical-shadow flag reuse.
- All tests use synthetic values under scrubbed env and all-network/known-secret
  deny sandbox. No skips, new baseline or dependency install.

## Task 1: Add strict default-off pair selector

**Files:**
- Modify backend/app/config.py only dedicated fields/parser/predicate and get_settings wiring.
- Create backend/tests/test_review_shadow_rollout.py.
- Create backend/docs/superpowers/reports/2026-09-09-t1-review-shadow-rollout-handoff.md.

**Consumes:** complete existing config.py and spec; actual get_settings behavior,
not operational .env. T4 exact request does not require a domain import.
**Produces:** exact fields/environment names/predicate defined in spec.

- [ ] Step1 add missing-interface RED plus executable default/off/pair tests:

```python
settings = Settings(review_shadow_enabled=True,
                    review_shadow_account_pairs=((1, 11), (2, 22)))
assert is_review_shadow_enabled(settings, organization_id=1, marketplace_account_id=11)
assert not is_review_shadow_enabled(settings, organization_id=1, marketplace_account_id=22)
assert not is_review_shadow_enabled(Settings(), organization_id=1, marketplace_account_id=11)
```

Use test monkeypatch only within scrubbed process to exercise actual get_settings.
Invalid corpus: flag yes/1/empty; pairs `0:1`, `-1:1`, `+1:2`, `01:2`,
`1:02`, `1:2:3`, `1:2,` (empty trailing segment), Unicode digits,
duplicate pairs, huge/out-of-INT4 IDs, embedded
spaces and wildcard. Test direct tuple/list/bool/float malformations separately.
Enabled empty selects nobody. Disabled with malformed configured pairs still fails.
- [ ] Step2 run expected RED, then implement minimal dedicated strict parser and
predicate. Validate canonical ASCII decimal width before conversion; exact tuple/
bool types for direct Settings. Use one fixed safe error helper, no raw exception
interpolation/chaining or logging Settings. No new permission/policy class needed.

```python
return settings.review_shadow_enabled and (
    organization_id, marketplace_account_id
) in settings.review_shadow_account_pairs
```

The shown final expression follows complete validation from spec; do not substitute
Python truthiness for type checks or partially retain malformed CSV entries.
- [ ] Step3 GREEN all direct/parser/canary cases and unchanged unrelated config
fields. Exact own-runtime commands from backend:

```sh
.venv/bin/python -m pytest -q tests/test_review_shadow_rollout.py
.venv/bin/python -m compileall -q app/config.py tests/test_review_shadow_rollout.py
git diff --check
```

Prefix each with env-i + plan-owned network/secret deny sandbox. No runtime startup
or keyring access. Scoped Ruff newtest and changedconfig; disclose existing config
lint findings separately instead of broad cleanup. Preserve every old field's
default/parser behavior and verify get_settings succeeds with both new env names absent.
- [ ] Step4 self-review, commit `feat: add exact-account Review shadow rollout selector`;
handoff exact interface, parse/error semantics, tests/exits and default-off/no-auth
limits. Independent task review before ready delivery directly T4.

## Preflight

Task1 config→predicate→T4 service selection has one strict owner-pair contract.
No migration or cross-terminal file overlap. This is a selector, never a grant.
True+empty denies intentionally; malformed-config error differs from valid disabled.
No additional rollout policy values need to be invented or activated.
