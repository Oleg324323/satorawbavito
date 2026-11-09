# Orders exact TEXT identity amendment

2026-09-09, explicit root-authorized release blocker followup to accepted0062.
This design does not rewrite0062, change user data, enable providers or alter T3
source files. T1 issues a new migration after the actual sole head; T3 implements
the corresponding repository interface change. Previous101PASS is bounded evidence,
not proof of unbounded-key storage.

## Evidence and choice

Own disposable PostgreSQL16.15 reproduced SQLSTATE54000 for4096-byte incompressible
synthetic external_order_id, source_line_key, source_run_key and source_event_key;
the pure identities and SQL exact-text validator accept them. Eight ordinary/ON
CONFLICT inserts failed; rollback preserved all11table counts, four short-key
insert/replay controls passed.
T3 `8f5603f47de305b0575cb5e65ed2e24f56485475:app/orders/evidence_repository.py:88`
depends on the original raw external-order conflict target. Exact account lock
visibility probe sees the prior winner after wait under RC, but not RR.

Choose exact comparison under canonical-account-serialized READ COMMITTED inserts,
without a hash identity or business length cap. A separate key registry would add
unneeded tables/interfaces; raw B-tree preservation would require a proven upstream
byte cap that does not exist. A nonunique hash accelerator may be introduced later
only with collision-safe exact comparison and its own proof. Initial implementation
uses existing owner-leading indexes and exact scans; contention/lookup cost is an
explicit tradeoff, not a throughput claim.

## Complete indexed-text inventory and target contract

The migration must inspect all0062 raw TEXT unique/index keys, not just first repro.
The following full raw-text keys lose their oversized B-tree arbiter and gain an
exact logical uniqueness check; UUID/BIGINT/narrow enum/checksum keys remain:

| Relation | Exact logical equality inside organization/account |
| --- | --- |
| order_sync_runs | source_kind, source_run_key |
| marketplace_orders | external_order_id |
| marketplace_order_items | order_id, source_line_key |
| order_observations, order level | order_id, NULL order_item_id, source_kind, adapter_version, payload_checksum |
| order_observations, item level | order_id, exact nonnull order_item_id, source_kind, adapter_version, payload_checksum |
| order_lifecycle_events | order_id, observation_id, source_event_key |
| order_deadlines | observation_id, deadline_kind |
| order_sync_coverage | sync_run_id, coverage_kind |

Run UNIQUE `(org,account,sync_run_id,source_kind,adapter_version)` is also oversized
when adapter_version is long. Its observation FK must become the existing bounded
owner/run-ID FK plus an explicit immutable source_kind/adapter_version comparison
on observation insert. The existing run UPDATE guard already makes those parent
fields immutable. Do not remove source/provider alignment or rely on a UUID-only
lookup. Preserve bounded `(org,account,marketplace,source_kind,sync_run_id)` target:
source_kind is restricted to three finite literals by the existing provider CHECK.
Checksums remain fixed lowercase64hex; VARCHAR state/status fields have finite
checks. All other indexed text must be classified and tested before acceptance.

New insert guards, before any row insertion, require actual transaction isolation
`read committed` (otherwise safe25000), acquire exact canonical account FOR UPDATE,
then use a fresh statement snapshot for exact `COLLATE "C"` comparisons. Values are
never truncated, trimmed, normalized or replaced by a digest. Exact duplicate raises
safe23505 with a documented logical constraint name and no input fragments. The
scope remains the old key's exact column/null semantics; changed payload under a
different checksum is not collapsed into an old observation. Existing immutable
guards prevent UPDATE from changing keys. No account data update is used to force
serialization, no bypass-RLS/SECURITY DEFINER shortcut.

## T3 repository amendment, not automatic conflict-target substitution

Removed raw-column/partial-index arbiters can no longer be ON CONFLICT targets.
T3 must acquire fresh authorization/account locks before its run/projection locks,
read exact existing key under the held account lock, compare every immutable replay
field/semantic payload, then insert only when absent. An exact no-op returns stored
identity/evidence; same invocation/key with changed immutable payload is a conflict,
not overwrite, ambiguous merge or new authority. Under concurrency the waiter
refreshes after lock acquisition, sees the committed winner and validates replay.

Observation replay lookup includes owner/order/item-nullability/source/adapter/
checksum and compares the exact normalized semantic evidence as already required.
Receipt/manifest membership still binds a new run to existing evidence. Bounded
membership and internal-ID arbiters not changed by this migration may retain their
ON CONFLICT usage. Generic DO NOTHING without the exact comparison is not a replay
contract. No service implementation is copied into T1.

Database triggers are a last defense; they cannot retroactively reorder domain
locks acquired by an incorrect caller. Session guard and T3 service tests remain
separate obligations. RR/SERIALIZABLE are explicitly unsupported for the new key
inserts, not silently accepted based on RC tests. Creation writers/importers must
use the documented fresh RC protocol. Provider network stays outside locks.

## Migration, downgrade and verification

New migration only. In one owner transaction, validate expected old constraints/FKs,
lock affected tables, replace only named/key-matched arbiters and the source-binding
FK, install guards and preserve all rows, RLS, ACLs, history/snapshot invariants and
unaffected indexes. Existing0062 data cannot contain duplicates under its original
constraints; explicit parity checks should nevertheless fail atomically on drift.
No migrations stamp, data rewrite, epoch/registry table, owner-role change or backfill.

Downgrade is guarded: refuse nonempty affected Orders state, including rows hidden
by RLS, before dropping guards or recreating limited B-tree arbiters. Empty-only
downgrade restores exact original constraints/FK/indexes without CASCADE. Do not
silently drop long rows or “repair” values to regain index compatibility.

Actual disposable PG tests: every unbounded indexed field long/distinct/exact;
provider/source/adapter and cross-org/account FK negatives; exact replay versus
changed evidence; concurrency with both snapshots established before the account
wait; RC fresh winner and RR/SERIALIZABLE safe rejection; full rollback on failure;
populated0062 upgrade parity; empty roundtrip; nonempty/hidden-RLS downgrade refusal.
No hash is used, so no hash-collision identity or fake hash proof is needed. If a
hash accelerator is added, forced same-bucket distinct-value acceptance becomes
mandatory. Preserve former101tests on bounded0062 and verify new amendment at the
actual new revision/latest chain. T3 must additionally prove its updated repository
without obsolete conflict targets. Release blocker closes only after both owners'
commits and independent migration/repository verification, not this design file.
