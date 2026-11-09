# Review shadow exact-account rollout selector

2026-09-09. T1 shared-config prerequisite requested by T4 after actual0065
decoder/writer acceptance dab11ec/0ce0517. Configuration only; no current consumer,
request, provider fetch, DB writer, Celery registration or operational flag is enabled.

## Decision and scope

Keep Review shadow separate from canonical_shadow_collection_*: those fields select
another collector and cannot authorize Review work. Independent org/account lists
are less explicit than a literal pair allowlist. Choose one default-off boolean,
one immutable tuple of exact internal owner pairs and one pure predicate:

```python
Settings.review_shadow_enabled: bool = False
Settings.review_shadow_account_pairs: tuple[tuple[int, int], ...] = ()
is_review_shadow_enabled(settings: Settings, *, organization_id: int,
                         marketplace_account_id: int) -> bool
```

This predicate selects eligible rollout targets only. It does not authenticate,
grant reviews:write, prove canonical account/provider ownership, resolve/decrypt
credentials, authorize a provider call or replace publication guard. T4's service
must independently check its exact WB account, live permissions and all fetch
bindings. No implicit organization-wide enable or fallback to another flag.

## Non-secret configuration contract

Environment names are VELLA_REVIEW_SHADOW_ENABLED and
VELLA_REVIEW_SHADOW_ACCOUNT_PAIRS. Missing enabled means False. The flag accepts
only case-insensitive true/false after surrounding whitespace; any other value
raises RuntimeError with exactly review_shadow_configuration_invalid, from None.
Do not reuse a parser that silently converts every typo to false.

Pairs are comma-separated org:account canonical decimal IDs; surrounding whitespace
around a whole pair is permitted, but not inside its IDs. Both are strict positive
INT4 values; no zero, sign, padding/leading zeros, Unicode digits, bool, duplicates,
empty segment, extra colon or wildcard. Missing or wholly whitespace pair input
means empty tuple. Validate length/range before unbounded int conversion. Preserve
input tuple order; no operational policy depends on order. One account may appear
under different orgs in non-secret configuration but only the canonical pair can
pass the separate ownership check; selector never infers or repairs that ownership.

Malformed provided configuration fails even if disabled; it is not silently
partially accepted. Enabled with empty allowlist is valid but selects nobody
(empty allowlists never mean all). Parsing is pure except reading these two env
names inside get_settings. No file/keyring/env-file loader or config inventory log.

The predicate validates the exact Settings boolean and tuple-of-exact-tuples
contract also for directly constructed Settings; malformed stored policy raises
the same fixed RuntimeError. Valid disabled/empty/unlisted returns False. Invalid
requested IDs return False; strict positive INT4 required, not bool/coercion.
Only enabled and exact listed pair returns True. No string/str(settings) output.
Errors never expose raw config, account inventory or values from other Settings.

## Compatibility and verification

Add only these fields, minimal dedicated parsing/validation and predicate in
app/config.py. Existing config values/parsers/security validation remain unchanged.
No rollout switch in .env/Compose/CI, no registry/router/domain changes.

RED missing fields/predicate and actual malformed-parser canaries before code.
GREEN direct Settings plus isolated-env get_settings roundtrips, default-off,
exact-pair versus cross-pair, strict types/INT4/duplicates, all invalid flag/pair
forms, valid true+empty deny, disabled malformed rejection, and synthetic secret
sentinel absent from str/repr/traceback errors. Snapshot existing unrelated fields
under scrubbed environment before/after, including canonical shadow and send flags.
Only synthetic IDs/config strings; no real environment values read into logs.

This task needs no DB/Redis/provider process. Own backend/.venv under all-network
and known-secret-file deny profile; no installs. Scoped Ruff/compile/diff checks.
Deliver exact ready import/arguments to T4 after independent review; no whole
Reviews stage or production activation claim.
