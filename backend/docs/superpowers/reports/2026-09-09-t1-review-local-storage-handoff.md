# T1 local Review storage — IMPLEMENTED / integration acceptance pending

Not READY. This handoff documents the implemented physical interface at migration
`20260909_0071`, parent `20260909_0070`, original dispatch BASE
`da89c676b2cf43de90d084b8fe4c8da55da79c4c`. Controller's docs-only measured compatibility
amendment is feature-review BASE `10c33410e5341bae75ebc250d53b6907eb4bbe35`.
No labels/dependencies, no provider
delivery, real-service authentication or deployment authority is conferred.

## Transaction ownership

The actual service supplies authenticated membership, expected provider/account
binding and decoded-source validation. Two GUCs (`app.organization_id` and
`app.marketplace_account_id`) are trusted scoped execution context, not authentication.
No provider GUC exists. Use READ COMMITTED and one caller-owned physical root:

1. Shared live membership/account guard and exact account `FOR UPDATE` fence.
2. Fresh full-owner command receipt lookup before current head/source/predecessor
   checks; compare actor, operation, exact request bytes and current descriptor.
   Matching historical receipt returns its original result, never latest pointers.
3. For a new action lock exact fact `FOR SHARE`, policy head `FOR UPDATE`, workflow
   head `FOR UPDATE`, in that order after the account. Policy head is always required;
   only workflow-head absence is permitted for first publication.
4. Write entity (where applicable), init/CAS head, audit, completed receipt. A head CAS
   must return exactly one row with expected version; otherwise roll back the root.
5. Live guard/final checks and physical commit. Unknown commit retries the identical
   command UUID/request. No internal commit, upsert-success fallback, cached absence,
   generation/network call or provider operation is part of this storage interface.

Statement triggers independently fence every new-table DML on the exact canonical
account, with immediate `FOUND` after the awaited account selection. Full scoped
account/provider FKs enforce row provider equality. Row guards validate new actions;
deferred callbacks validate captured historical transitions and reciprocal witnesses,
not old epochs against final current policy eligibility.

## Actual typed columns

All seven tables are in `public`. Every table begins with non-null owner triple O:
`organization_id integer`, `marketplace_account_id integer`, `marketplace text`.
Owners/members are positive INT4; provider is exactly `wb|avito`. UUIDs are nonzero.
All NUMERIC columns have no precision/scale typmod; positive finite integral values,
except expected head/draft counters explicitly permitting zero. Timestamps are finite
UTC-representable years1–9999; canonical bytes always use six fractional digits/Z.
All columns below are NOT NULL unless suffixed `?`; nullable fields obey closed
operation/event shape matrices, not arbitrary optional metadata.

| Relation | Columns after O |
| --- | --- |
| review_policy_versions | policy_id uuid; version numeric; approval_mode text; template_version text; model_version text; policy_payload bytea; policy_checksum text; actor_membership_id integer; created_at timestamptz; audit_event_id uuid |
| review_policy_heads | head_id uuid; version numeric; current_policy_id uuid; current_policy_version numeric; current_policy_checksum text; updated_at timestamptz |
| review_draft_revisions | review_id uuid; draft_id uuid; revision numeric; source_observation_id uuid; source_checksum text; policy_id uuid; policy_version numeric; policy_checksum text; text_utf8 bytea; text_checksum text; binding_payload bytea; binding_checksum text; generation_id uuid; generation_mode text; template_version text; model_version text; generation_started_at timestamptz; generation_completed_at timestamptz; previous_draft_id uuid?; generation_payload bytea; generation_checksum text; policy_head_id uuid; policy_head_version numeric; policy_selection_event_id uuid; actor_membership_id integer; created_at timestamptz; audit_event_id uuid |
| review_decisions | review_id uuid; decision_id uuid; draft_id uuid; draft_revision numeric; binding_checksum text; decision_kind text; actor_membership_id integer; decided_at timestamptz; audit_event_id uuid |
| review_workflow_heads | review_id uuid; head_id uuid; version numeric; current_draft_id uuid; current_draft_revision numeric; current_decision_id uuid?; updated_at timestamptz |
| review_local_audit | event_id uuid; aggregate_kind text; aggregate_id uuid; aggregate_version numeric; event_kind text; review_id uuid?; policy_id uuid?; policy_version numeric?; draft_id uuid?; draft_revision numeric?; decision_id uuid?; actor_membership_id integer; before_state text?; after_state text?; occurred_at timestamptz; local_command_id uuid; audit_payload bytea; audit_checksum text; policy_head_id uuid? GENERATED; workflow_head_id uuid? GENERATED |
| review_local_command_receipts | local_command_id uuid; actor_membership_id integer; operation_kind text; request_payload bytea; request_checksum text; account_binding_schema_version smallint; account_binding_external_account_id text; account_binding_credential_ref text?; account_binding_payload bytea; account_binding_checksum text; result_payload bytea; result_checksum text; completed_at timestamptz; audit_event_id uuid; review_id uuid?; expected_head_version numeric?; expected_draft_revision numeric?; expected_policy_head_id uuid?; expected_policy_head_version numeric?; result_policy_id uuid?; result_policy_version numeric?; result_draft_id uuid?; result_draft_revision numeric?; result_decision_id uuid?; result_head_id uuid?; result_head_version numeric? |

The two private audit columns are exactly:

```sql
policy_head_id uuid GENERATED ALWAYS AS
  (CASE WHEN aggregate_kind='policy_head' THEN aggregate_id END) STORED
workflow_head_id uuid GENERATED ALWAYS AS
  (CASE WHEN aggregate_kind='workflow_head' THEN aggregate_id END) STORED
```

They add no caller-controlled identity or public request/audit field. Public audit
`commandId`, `attemptId`, and `reasonCode` remain NULL. Private `local_command_id`
provides the receipt reference. UUID generated fields do not expand runtime rights.

## Declared keys, FKs and child-index inventory

Generated from the actual0071 declaration, not a second schema specification.
O expands to the owner triple above; all constraint names have `review_local_` prefix.
All FK actions are NO ACTION (no CASCADE); deferred means DEFERRABLE INITIALLY DEFERRED,
immediate means not deferrable. Constraint indexes retain uniqueness semantics.

| Constraint suffix | Child columns → parent columns | Timing |
| --- | --- | --- |
| policy_account_fk | review_policy_versions(O) → marketplace_accounts(O) | immediate |
| phead_account_fk | review_policy_heads(O) → marketplace_accounts(O) | immediate |
| draft_account_fk | review_draft_revisions(O) → marketplace_accounts(O) | immediate |
| decision_account_fk | review_decisions(O) → marketplace_accounts(O) | immediate |
| whead_account_fk | review_workflow_heads(O) → marketplace_accounts(O) | immediate |
| audit_account_fk | review_local_audit(O) → marketplace_accounts(O) | immediate |
| receipt_account_fk | review_local_command_receipts(O) → marketplace_accounts(O) | immediate |
| policy_actor_fk | review_policy_versions(organization_id,actor_membership_id) → iam_memberships(organization_id,membership_id) | immediate |
| draft_actor_fk | review_draft_revisions(organization_id,actor_membership_id) → iam_memberships(organization_id,membership_id) | immediate |
| decision_actor_fk | review_decisions(organization_id,actor_membership_id) → iam_memberships(organization_id,membership_id) | immediate |
| audit_actor_fk | review_local_audit(organization_id,actor_membership_id) → iam_memberships(organization_id,membership_id) | immediate |
| receipt_actor_fk | review_local_command_receipts(organization_id,actor_membership_id) → iam_memberships(organization_id,membership_id) | immediate |
| phead_policy_fk | review_policy_heads(O,current_policy_id,current_policy_version,current_policy_checksum) → review_policy_versions(O,policy_id,version,policy_checksum) | immediate |
| draft_fact_fk | review_draft_revisions(O,review_id) → review_facts(O,review_id) | immediate |
| draft_source_fk | review_draft_revisions(O,review_id,source_observation_id,source_checksum) → review_observations(O,review_id,observation_id,content_checksum) | immediate |
| draft_policy_fk | review_draft_revisions(O,policy_id,policy_version,policy_checksum) → review_policy_versions(O,policy_id,version,policy_checksum) | immediate |
| draft_predecessor_fk | review_draft_revisions(O,review_id,previous_draft_id) → review_draft_revisions(O,review_id,draft_id) | immediate |
| draft_epoch_fk | review_draft_revisions(O,policy_head_id) → review_policy_heads(O,head_id) | immediate |
| draft_selection_fk | review_draft_revisions(O,policy_selection_event_id,policy_head_id,policy_head_version,policy_id,policy_version) → review_local_audit(O,event_id,policy_head_id,aggregate_version,policy_id,policy_version) | deferred |
| decision_draft_fk | review_decisions(O,review_id,draft_id,draft_revision,binding_checksum) → review_draft_revisions(O,review_id,draft_id,revision,binding_checksum) | immediate |
| whead_draft_fk | review_workflow_heads(O,review_id,current_draft_id,current_draft_revision) → review_draft_revisions(O,review_id,draft_id,revision) | immediate |
| whead_decision_fk | review_workflow_heads(O,review_id,current_draft_id,current_draft_revision,current_decision_id) → review_decisions(O,review_id,draft_id,draft_revision,decision_id) | immediate |
| audit_policy_fk | review_local_audit(O,policy_id,policy_version) → review_policy_versions(O,policy_id,version) | deferred |
| audit_draft_fk | review_local_audit(O,review_id,draft_id,draft_revision) → review_draft_revisions(O,review_id,draft_id,revision) | deferred |
| audit_decision_fk | review_local_audit(O,review_id,draft_id,draft_revision,decision_id) → review_decisions(O,review_id,draft_id,draft_revision,decision_id) | deferred |
| audit_phead_fk | review_local_audit(O,policy_head_id) → review_policy_heads(O,head_id) | deferred |
| audit_whead_fk | review_local_audit(O,workflow_head_id) → review_workflow_heads(O,head_id) | deferred |
| audit_receipt_fk | review_local_audit(O,local_command_id,event_id) → review_local_command_receipts(O,local_command_id,audit_event_id) | deferred |
| receipt_audit_fk | review_local_command_receipts(O,audit_event_id,local_command_id) → review_local_audit(O,event_id,local_command_id) | deferred |
| receipt_policy_fk | review_local_command_receipts(O,result_policy_id,result_policy_version) → review_policy_versions(O,policy_id,version) | deferred |
| receipt_draft_fk | review_local_command_receipts(O,review_id,result_draft_id,result_draft_revision) → review_draft_revisions(O,review_id,draft_id,revision) | deferred |
| receipt_decision_fk | review_local_command_receipts(O,review_id,result_draft_id,result_draft_revision,result_decision_id) → review_decisions(O,review_id,draft_id,draft_revision,decision_id) | deferred |
| policy_audit_fk | review_policy_versions(O,audit_event_id) → review_local_audit(O,event_id) | deferred |
| draft_audit_fk | review_draft_revisions(O,audit_event_id) → review_local_audit(O,event_id) | deferred |
| decision_audit_fk | review_decisions(O,audit_event_id) → review_local_audit(O,event_id) | deferred |

| Constraint suffix | Relation and ordered columns | Kind |
| --- | --- | --- |
| policy_pk | review_policy_versions(O,policy_id,version) | PRIMARY KEY |
| policy_checksum | review_policy_versions(O,policy_id,version,policy_checksum) | UNIQUE |
| phead_pk | review_policy_heads(O) | PRIMARY KEY |
| phead_head | review_policy_heads(O,head_id) | UNIQUE |
| draft_pk | review_draft_revisions(O,draft_id) | PRIMARY KEY |
| draft_revision | review_draft_revisions(O,review_id,revision) | UNIQUE |
| draft_identity | review_draft_revisions(O,review_id,draft_id) | UNIQUE |
| draft_draft | review_draft_revisions(O,review_id,draft_id,revision) | UNIQUE |
| draft_binding | review_draft_revisions(O,review_id,draft_id,revision,binding_checksum) | UNIQUE |
| draft_generation | review_draft_revisions(organization_id,marketplace_account_id,generation_id) | UNIQUE |
| decision_pk | review_decisions(O,decision_id) | PRIMARY KEY |
| decision_draft | review_decisions(O,review_id,draft_id,draft_revision,decision_id) | UNIQUE |
| whead_pk | review_workflow_heads(O,review_id) | PRIMARY KEY |
| whead_head | review_workflow_heads(O,head_id) | UNIQUE |
| audit_pk | review_local_audit(O,event_id) | PRIMARY KEY |
| audit_aggregate | review_local_audit(O,aggregate_kind,aggregate_id,aggregate_version) | UNIQUE |
| audit_receipt | review_local_audit(O,event_id,local_command_id) | UNIQUE |
| audit_selection | review_local_audit(O,event_id,policy_head_id,aggregate_version,policy_id,policy_version) | UNIQUE |
| receipt_pk | review_local_command_receipts(organization_id,marketplace_account_id,local_command_id) | PRIMARY KEY |
| receipt_owner | review_local_command_receipts(O,local_command_id) | UNIQUE |
| receipt_audit | review_local_command_receipts(O,audit_event_id) | UNIQUE |
| receipt_witness | review_local_command_receipts(O,local_command_id,audit_event_id) | UNIQUE |

Additional nonunique btree indexes (24):

| Name | Relation and ordered keys |
| --- | --- |
| review_local_child_18 | review_draft_revisions(O,policy_selection_event_id,policy_head_id,policy_head_version,policy_id,policy_version) |
| review_local_child_19 | review_decisions(O,review_id,draft_id,draft_revision,binding_checksum) |
| review_local_child_21 | review_workflow_heads(O,review_id,current_draft_id,current_draft_revision,current_decision_id) |
| review_local_child_24 | review_local_audit(O,review_id,draft_id,draft_revision,decision_id) |
| review_local_child_31 | review_local_command_receipts(O,review_id,result_draft_id,result_draft_revision,result_decision_id) |
| review_local_child_12 | review_policy_heads(O,current_policy_id,current_policy_version,current_policy_checksum) |
| review_local_child_14 | review_draft_revisions(O,review_id,source_observation_id,source_checksum) |
| review_local_child_15 | review_draft_revisions(O,policy_id,policy_version,policy_checksum) |
| review_local_child_16 | review_draft_revisions(O,review_id,previous_draft_id) |
| review_local_child_22 | review_local_audit(O,policy_id,policy_version) |
| review_local_child_27 | review_local_audit(O,local_command_id,event_id) |
| review_local_child_28 | review_local_command_receipts(O,audit_event_id,local_command_id) |
| review_local_child_29 | review_local_command_receipts(O,result_policy_id,result_policy_version) |
| review_local_child_17 | review_draft_revisions(O,policy_head_id) |
| review_local_child_25 | review_local_audit(O,policy_head_id) |
| review_local_child_26 | review_local_audit(O,workflow_head_id) |
| review_local_child_32 | review_policy_versions(O,audit_event_id) |
| review_local_child_33 | review_draft_revisions(O,audit_event_id) |
| review_local_child_34 | review_decisions(O,audit_event_id) |
| review_local_child_7 | review_policy_versions(organization_id,actor_membership_id) |
| review_local_child_8 | review_draft_revisions(organization_id,actor_membership_id) |
| review_local_child_9 | review_decisions(organization_id,actor_membership_id) |
| review_local_child_10 | review_local_audit(organization_id,actor_membership_id) |
| review_local_child_11 | review_local_command_receipts(organization_id,actor_membership_id) |

Every table has `review_local_<alias>_scalars`: positive owners/members,
provider wb|avito, finite/integral NUMERIC, nonzero UUIDs, representable timestamp,
64-lowerhex checksum grammar. `policy_bytes`, `draft_bytes`, `audit_bytes`, and
`receipt_bytes` CHECKs tie exact typed canonical bytes to SHA256; draft also requires
nonblank valid UTF8 and generation-mode shape. `decision_kind` closes approved/rejected.
`audit_shape` fixes category/event and paired private refs. Receipt null/operation
matrices are enforced by fixed result bytes and deferred reconstructed request bytes.
The full graph validates reciprocal entity/head/audit/receipt identity, actor/time,
original policy-created aggregate version, and captured/final transition chains.


## Fixed helper signatures

All helper names below are `public` qualified; all SECURITY INVOKER with explicit
`search_path=pg_catalog,public`. Pure helpers are IMMUTABLE and read no business rows.
Native table composite arguments are internal typed projections, not authorized source
truth: deferred validation reloads actual scoped immutable rows before reconstruction.
Explicit ROW projections in CHECKs avoid PostgreSQL's prohibited whole-row references.

| Signature | Return |
| --- | --- |
| review_local_integer_text(numeric,boolean) | text |
| review_local_timestamp_text(timestamptz) | text |
| review_local_utf8_json_string(bytea) | bytea |
| review_local_nonblank_utf8(bytea) | boolean |
| review_local_string(text,boolean) | bytea |
| review_local_uuid(uuid,boolean) | bytea |
| review_local_number(numeric,boolean,boolean) | bytea |
| review_local_label(text) | bytea |
| review_local_checksum(text) | bytea |
| review_local_policy_bytes(public.review_policy_versions) | bytea |
| review_local_generation_bytes(public.review_draft_revisions) | bytea |
| review_local_decision_binding_bytes(public.review_draft_revisions,bytea) | bytea |
| review_local_audit_bytes(public.review_local_audit) | bytea |
| review_local_result_bytes(public.review_local_command_receipts) | bytea |
| review_local_request_bytes(public.review_local_command_receipts,public.review_policy_versions,public.review_draft_revisions,public.review_decisions,bytea) | bytea |
| review_local_account_lock() | trigger, VOLATILE |
| review_local_row_guard() | trigger, VOLATILE |
| review_local_validate() | trigger, VOLATILE |

Integer boolean means allow-zero; string/UUID boolean means nullable; number booleans
mean allow-zero then nullable. Fixed label/checksum helpers enforce their exact bounded
ASCII contracts. UTF8 strings preserve all valid scalar bytes including NUL escaped as
JSON, no Unicode normalization. Existing `review_strict_utf8` and
`review_run_binding_bytes(integer,integer,text,text,text)` remain dependencies, unchanged.
Physical account TEXT cannot represent NUL; BYTEA Review bodies/identities can.

## ACL and lifecycle boundary

All seven tables ENABLE and FORCE RLS with canonical org/account text comparisons in
both USING and WITH CHECK. Runtime is nonowner/NOSUPERUSER/NOBYPASSRLS/NOINHERIT.
Immutable tables permit SELECT/INSERT; heads SELECT/INSERT/UPDATE. No nonowner grant
options, DELETE, TRUNCATE, REFERENCES or TRIGGER. PUBLIC has no rights on new objects.
Trigger functions have no nonowner EXECUTE; entitled roles receive pure helpers only.
Function ACL changes use exact full regprocedure signatures/OIDs, not basename adoption.
Old overload/old-table/default rights remain outside new-object narrowing.

Each table has `review_local_account_first` BEFORE STATEMENT for I/U/D/TRUNCATE,
`review_local_guard` BEFORE ROW I/U, and `review_local_witness` deferred constraint
trigger AFTER ROW I/U. History update/no-op/delete/truncate fails; heads init1 then
same identity/+1 only. Draft revisions increment only on publication; decisions advance
head alone. Owner generation UUID uniqueness excludes review/actor/mode.

Errors emitted by new SQL are closed safe codes: `review_local_invalid`,
`review_local_context_invalid`, `review_local_account_missing`,
`review_local_isolation_invalid`, `review_local_immutable`,
`review_local_source_unbound`, `review_local_source_changed`,
`review_local_not_answerable`, `review_local_policy_changed`,
`review_local_predecessor_stale`, `review_local_downgrade_nonempty`.
Integrity23514, isolation25000, nonempty downgrade55000; native FK/unique/type/permission
details remain private and require actual service error translation.

Downgrade locks all seven in declared TABLES order ACCESS EXCLUSIVE, sets LOCAL
row_security=off, rejects any row before DDL. Drops exact triggers, trigger functions,
new FKs, scalar/byte CHECK dependencies, reversed pure helper dependencies, reversed
tables; never CASCADE or old-object DDL.

## Verification status

### Frozen source bytes

Own worktree SHA256 freeze after compatibility amendment, independently matched by
controller; paths relative to
`backend/`. Handoff is deliberately excluded from these twelve source hashes.

| Path | SHA256 |
| --- | --- |
| alembic/versions/20260909_0071_review_local_storage.py | fbfdf583973cb57f32735b63a5d318a371a5b8ceba176de3ac3a6e2cbf5fca95 |
| ops/runtime-db-role.sql | 97001a0057931b25553dc77518787015e83a22142d497a8377aaa90f1d6616b9 |
| tests/test_review_local_storage_schema.py | 195c44738ed236a5987ec0dae213cfd5517e4ec2f7573aeff7735d04c3b4b3f3 |
| tests/test_review_local_storage_codec.py | ed05b5e0d8a9848ef5ddcb07e49d5141686063d5ca7ae1eccff2026eaaa4c4a0 |
| tests/test_review_local_storage_lifecycle.py | 6ef66c942f2ac10cd0afc0858019df0a37bf2638bdbf9c21107ce86229313d4b |
| tests/test_review_local_storage_rls.py | e59f48f70cc190ff55aab7422a382546d793362437c6dca5f3bc9340178326c3 |
| tests/test_review_facts_schema.py | 47f32792551d9425171533cdf9a7dbac49f8df2f7ddee556d71be3042c46adf1 |
| tests/test_sku_override_schema.py | 84fc0a588bd279294e5300691c5141ab30d8721ce6616df8d8b3fa16381a35ef |
| tests/fixtures/t1_review_storage_v1_golden.json | 33bb1848950ecb5214b279c84869164bcf87af2606a1dcbf051496ec416537b2 |
| tests/fixtures/t1_review_local_audit_v1_golden.json | a6230858b279ba0f1cacc643c2e2533db2bdc33cd16383a05bf44270bb3eafa0 |
| tests/fixtures/t1_review_local_command_v1_golden.json | e8bf64f7ca4ba93aeda673bd27f62f36725601bf2ad00ea9309ddef5dc9b7169 |
| tests/fixtures/t1_review_account_binding_v1_golden.json | ee563c3fdd1e5c0799e7f42a33051c5fba3e7752e3a89db3a608a9cbe77fe00f |

The four literal files were copied byte-for-byte from immutable
`ad793f9c4672210b25fe05e35dc1ddb03cf676ac:backend/tests/fixtures/reviews/`:
`storage-v1-golden.json`, `local-audit-v1-golden.json`,
`local-command-v1-golden.json`, `account_binding_descriptor_v1.json`, respectively.
One-time identity was checked by implementation agent and independently by controller.
Actual T4 codec/matrix inputs through `176d243df4ef3349e03fa31f1d4231f3e9064271` were
read, not imported/copied as runtime implementation. Tests use committed local literal
files/hashes, never git history or T4 imports. Storage fixture's older audit entry is
retained unchanged; separate local-audit fixture is the accepted audit authority.

### Measured gates

Actual predecessor missing-relation RED, migration ACL setup failure, unrelated-overload
behavior RED and subsequent15-case green are recorded in the controller report.
First full386: 375 passed/11 test-harness failures,47.32s,natural1; missing fresh GUC,
native FK TRUNCATE precedence, and direct-driver percent placeholder setup corrected
without weakening checks. Aggregate/target-version complete-bundle regression observed
genuine RED (1 failed,4.92s), then narrow deferred equality added. Full498 passed43.04s.
Child-prefix live catalog regression then observed genuine RED (1 failed,3.56s), after
all FK coverage assertions passed; longest-prefix-first generation omits3 redundant
nonunique child indexes. Final499 passed in44.57s against the ten frozen hashes.
Every completed run had explicit admission, natural exit and exact own allocator
database/role cleanup/absence; no automatic retry, skipped test or kill-as-success.
Inherited `/dev/null` passfile warnings were never suppressed.

First exact10 adjacent gate: 1 failed,375 passed,241 setup errors in220.04s, natural1.
Existing facts CASCADE diagnostic lacks TRUNCATE grant on newly referenced tables;
existing SKU latest-runtime fixture upgrades0070 while current script requires0071.
All76 actual disposable databases/roles cleaned;3 additional cleanup lines were existing
mocked allocator controls. Controller approved the two bounded fixture edits in the
docs-only amendment; exact2 regression passed7.56s. Renewed499 passed47.97s and renewed617
passed222.77s. New runtime permissions are unchanged. At the user's revised sequencing,
parent independent501 was not run and independent review is deferred until the complete
integrated implementation package. Controller authorized the exact thirteen-path feature
commit; integration acceptance remains pending and no READY claim is made.
Full commands/failures/exact cleanup are retained
in `.superpowers/sdd/2026-09-09-review-local-storage/task-1-report.md` in this worktree.

Exact focused and adjacent commands, backend cwd, separately admitted:

| Gate | Actual outcome | Natural exit | Seconds |
| --- | --- | --- | --- |
| Predecessor absent-contract RED | 1 failed, required relation absent after successful0070 | 1 | 3.35 |
| Initial14 migration/literal smoke | 1 failed,13 setup errors, ambiguous ACL grantee | 1 | 6.72 |
| Unrelated-overload ACL RED | 1 failed, old ACL changed after upgrade/script | 1 | 4.98 |
| Migration/literal/overload15 | 15 passed | 0 | 10.59 |
| First full386 | 375 passed,11 harness failures | 1 | 47.32 |
| Aggregate-version RED | 1 failed, inconsistent bundle committed | 1 | 4.92 |
| Full498 | 498 passed | 0 | 43.04 |
| Child-index RED | 1 failed,3 redundant prefixes | 1 | 3.56 |
| Pre-amendment focused499 | 499 passed | 0 | 44.57 |
| First adjacent617 | 375 passed,1 failed,241 setup errors | 1 | 220.04 |
| Amended compatibility2 | 2 passed, exact old trigger/ACL restoration and SKU literal | 0 | 7.56 |
| Renewed amended-scope focused499 | 499 passed, nine owned DB/role cleanups verified | 0 | 47.97 |
| Renewed amended-scope adjacent617 | 617 passed, 8 unrelated pytest temp-cleanup warnings | 0 | 222.77 |

Renewed617 emitted79 cleanup diagnostics:76 actual owned PostgreSQL databases with
exact role absence verified, plus3 existing in-memory allocator control diagnostics.
Eight warnings concern sandbox-blocked removal of two unrelated existing pytest
garbage synthetic-source directories, not PostgreSQL cleanup. Their exact paths and
full warnings are retained in the task report; no unrelated manual cleanup was attempted.

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-local-storage/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_review_local_storage_schema.py tests/test_review_local_storage_codec.py tests/test_review_local_storage_lifecycle.py tests/test_review_local_storage_rls.py
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-local-storage/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_review_facts_schema.py tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py tests/test_sku_override_schema.py tests/test_sku_override_lifecycle.py tests/test_sku_override_rls.py tests/test_orders_schema_integration.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0071_review_local_storage.py tests/test_review_local_storage_schema.py tests/test_review_local_storage_codec.py tests/test_review_local_storage_lifecycle.py tests/test_review_local_storage_rls.py tests/test_review_facts_schema.py tests/test_sku_override_schema.py
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check --output-format concise alembic/versions/20260909_0071_review_local_storage.py tests/test_review_local_storage_schema.py tests/test_review_local_storage_codec.py tests/test_review_local_storage_lifecycle.py tests/test_review_local_storage_rls.py tests/test_review_facts_schema.py tests/test_sku_override_schema.py
git diff --check
```

The alternate Python is lint-only; no foreign test environment, installed package,
host configuration, TCP/provider request, production database or credential was used.

## Isolated manual Critic Pass

The requested preflight-critic skill file was absent (ENOENT); no child reviewer was
dispatched. Separate self-review read the brief/spec, actual diff and typed declarations,
literal fixtures, negative-test failure boundaries and measured gate/cleanup evidence.
Original scope audit found11 approved implementation paths; controller's measured
amendment adds exactly2 existing test paths, making13. Runtime diff stays44 additions,
no changes to old blocks. Old facts diagnostic adds9 lines only; SKU runtime fixture
changes one migration target to actual head. New Review runtime fixture likewise uses
head; all pinned migration tests stay unchanged. Both existing files had Ruff0 before
and after edits; no baseline lint drift or old-file reformat. Current twelve hashes
remained frozen through both renewed gates; controller's docs-only HEAD movement is explicit.

Review findings already resolved through actual RED/GREEN: unrelated overload ACL
adoption, policy-created aggregate/target version mismatch, redundant child-index
prefixes. Harness errors were distinguished from implementation defects; targeted
native/trigger/RLS assertions and rollback snapshots were retained, not weakened.

No additional defect identified in the frozen local contract. Acceptance remains
pending parent independent501 and independent review, both deferred until the complete
integrated implementation package by the user's revised sequencing. The earlier adjacent failure and
the measured amendment remain documented, not waived by focused passes. Actual T4 authentication/live guards, normalized
decoded-source validation, HTTP/replay error translation, retention/rollout and delivery
remain outside this physical-storage proof. No high-volume latency/SLO or broad
deadlock-free claim is made by finite synthetic concurrency tests.
