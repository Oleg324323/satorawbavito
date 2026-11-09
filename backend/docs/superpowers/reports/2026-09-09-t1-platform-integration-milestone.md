# T1 consolidated platform integration milestone

## Frozen admission, not final acceptance

T1 branch `codex/arch-t1-platform`, original base `c88a474695569af905b294906ba604d61f3de62f`, frozen payload commit `e6d97c2ee97fbc320ba98c945a768a2b9a631a0e` (not a claim about the moving branch HEAD). Later commit `47ca9513244ea29f21aba47527cbd8807d31caed` is the separately released SKU permission export addition, explicitly outside this frozen payload. It passed independent review and main66PASS0.80s; root may explicitly admit it as an addition, not silently take the branch tip. This report adds no code or schema and does not activate any route, flag or credential writer. It is one consolidated input for coordinator-root's intermediate integration gate; it is not a claim that the whole original specification is implemented or verified.

The user's latest amendment **defers shared WB/Avito Production assembly and printing**. Preserve existing code/migrations/history and regression compatibility. Do not implement new Production work items, assignments, batches, scheduling, assembly sheets, XLSX/PDF labels, print archives or delivery receipts, or search for missing renderers/calendar sources. Existing 0069 storage and shared permission foundation remain available but have no accepted P1 service. This deferred module is not a blocker for the remaining active scope. Orders ingestion/storage/read/API/source progression, repricer/economics/settings/sources, Reviews/Notifications/frontend and their platform prerequisites remain active. KIZ/matcher remain excluded.

## Exact shared prerequisites

| Contract | Exact T1 commit(s) | Scoped evidence and boundary |
| --- | --- | --- |
| Publication guard | `8338ef77311266ec9943ef06fac4672efe948d40` | Main 304 PASS; actual PostgreSQL root, live user/session/account fence. Not worker authentication |
| Repricer approvals 0066 | `dbf8d31d9fc0b9035b9c8a84727e70713956afe0`; preservation proof `4e966805c0229c168f5c6e5a63afe95e1bc99978` | Main 283/390, later preservation 199 PASS; jobs/receipts/worker authority still separate |
| Orders immutable run binding 0067 | `d87e575f8fec32815dbeb459157339f8ad25220f` **plus mandatory** `c4e3e373f97be19171f80ebdad2ce919dd7c84dc` | Main 116 PASS/32.43s, natural exit and own resource absence; missing-account lock check fix included |
| Reviews immutable run binding 0068 | `4d95b24b549f0b1f773095ec583a85f18172fdbc` | Main unchanged 131 PASS/45.90s, natural exit, 16 own DB/21 role cleanup; previous 54 PASS/77 setup ERROR is retained failed evidence |
| Deferred Production 0069 foundation | `d57c54418762583c41829dabd80969775b4fdb56` **plus mandatory fixture fix** `bb7a958d99f1458d7e10877406eaf5dfe03ae6bc` | Main 173 feature/447 adjacent PASS and own cleanup; pure fixture review fix 6 RED → 15 GREEN. No P1 service acceptance |
| Deferred Production permission foundation | `a66b115c9f7eb1273c21d16593f48912e57e7d9b`; handoff `4067abd0925283064dd343cc6f655add8145d9f4`; documentation fix `e6d97c2ee97fbc320ba98c945a768a2b9a631a0e` | Main 265 PASS/14.42s and exact 2 DB/2 role cleanup; final four Python paths unchanged; scoped review PASS/APPROVED. No automatic grants or P1 activation |
| Packaging and duplicate report routes | `35b9f63f9801a68a58319320f98ad5acbc1f6587`; `3337c09b87a9fdadb2dd6f37336d6edc8b37974d` | Main installed-wheel 3 PASS/11 origins, route 19 PASS. This does not prove incoming domain packages or their registrations |

All prerequisites are ancestors of the frozen T1 payload. Migration order is the actual single linear head through `20260909_0069`; no 0070 is reserved by this report. Select schema prerequisites before domain commits; preserve dependency fixes, not just the headline migration. T4 has equivalent cherry-picked shared commits, so ancestry alone does not establish byte identity: inspect semantic overlap during integration and return domain conflicts to their owner.

## Owner-approved consumer candidates

| Owner | Frozen candidate | Included consumer evidence | Combined-gate limitation |
| --- | --- | --- | --- |
| T2 | `f3c61d7704813790245ec73096c753ee5dd93915` | User commands/participant `bd3bb60a0baadc8d7683c8efd8bf4eff444c9643`, owner-only legacy import `9c7e5baa6dc3b3b2f113671b03db6c301942b1e7`, 0066 merge `1464fcdfaab15b053678dd4f6d0337d958dab7f7`. Owner reports 93 real PG cases and separate 228 pure cases | 0067–0069 combined head not tested by this candidate; jobs/worker outcomes/settings/source services are not included |
| T3 | `35ea34e77d80920b0d8779a6b888e6f1d8e7bad0` | Decoder `8010112ed61ead1c345630da38618dd0ebc231ed`, read service `33c95c17c2580ddd3e1d66bfeb0bbd5a7cb679e7`, user-session browser sink `4c8b4c695d47569a2fa1c436ed59234a37a96e92`; actual owner 198 PASS plus 8 browser cases | Frozen candidate's current `read_db` fixture pins0067. Post-frozen test-only correction `be0329bf211f8a3af7b087efddbc6d1a63243854` now exists; admit its exact evidence separately and retain historical migration pins. Later schema merge `dc335d6a31e2e30e22b0b5cd36887b4bbafd021c` is outside domain payload selection, not a substitute for exact T1 schema prerequisites |
| T4 | `77abab0ae4a5bef1544bccfbeb92e728d8b7c4e6` | 0068 consumer `27c227e099494dbc6c49a05a011b768646beef6f`, canonical HTTP `835a82b38082b4428d467eb3899683e816515eeb`. Owner final 44 PG PASS after a failed expanded gate; full frontend 430 PASS/132 suites/133.91s, natural exit | Final consumer critic was owner-only; root independent integration review remains. Prior 900 HTTP PASS was before 0068 and is not current combined proof. Later parity work is not silently substituted for this frozen head |

These are owner-selected frozen candidates, not a request to merge a moving branch tip. They have scoped consumer acceptance only; **final-integrated = NO**. Root collects any subsequent exact corrective commit explicitly. T1 has not rerun domain-owner evidence or added those counts together to claim a full suite.

## Actual API and registration boundary

At the frozen T1 payload, `app.main` does **not** import/include either canonical router below. Do not add an optional/fake import to hide an absent dependency or copy domain implementation into T1.

- T3 export: `from app.orders.router import router`; existing prefix `/api/v2/orders`, GET empty suffix. Its `query_checksum` selects a prepublished view, not arbitrary client filters. `row_version`/`snapshot_id` are decimal strings, INT4 IDs/quantity are numbers. The real authenticated actor flows into the read service's live publication guard. Source progression, completeness and filter registry are still separate domain work.
- T4 export: `from app.reviews.canonical_router import router`; existing prefix `/api/v2/reviews/wb`, POST `/sync`. Existing default-off canonical gate remains. Canonical-only synchronization was an explicit safety amendment; it is not proof that original same-DTO shadow parity/reconciliation requirements are satisfied.
- Shared HTTP handling in `app.main` transforms standalone `detail.code` into `error.code` and strips validation input/context. Integration must exercise the actual main application, not only standalone routers, including unauthenticated/revoked/cross-org/cross-account and synthetic-secret responses.
- Shared router/task registration stays T1-owned; domain implementations remain T2/T3/T4-owned. Root can request a bounded registration change after their real modules are integrated. Legacy task factories must not execute business jobs merely to verify imports.

## Root milestone acceptance checklist

1. Separate integration worktree, exact schema prerequisites then accepted domain commits then frontend. Inspect ownership/semantic conflicts; no automatic overwrite or push by T1.
2. Actual single Alembic head and empty disposable PostgreSQL upgrade without stamp; current-schema/grants compatibility and historical upgrade/downgrade/preservation fixtures. Include existing deferred 0069 compatibility without new Production feature implementation.
3. Runtime non-owner/NOBYPASS RLS, no-context/wrong-org/account checks, actual canonical service tests and commit/failure/replay paths. Resolve the known T3 current-fixture pin before calling the combined suite green.
4. Installed wheel origins for actual incoming Orders/Reviews packages; actual `app.main` route/OpenAPI uniqueness, safe error contracts and generated frontend contracts/build under accepted offline/synthetic controls.
5. Record exact command, natural exit, duration, warnings and owned-resource cleanup. Full backend historical baseline was 894 PASS/105 FAIL with a post-summary hang: scoped natural exits and frontend success do not resolve it. No skip, baseline expansion, timeout widening or killing pytest as a success workaround.
6. Update the final integration handoff only after these checks. This intermediate milestone is not final acceptance of all remaining original requirements.

## Remaining active dependencies and missing inputs

Canonical SKU permission exports are separately released at `47ca9513244ea29f21aba47527cbd8807d31caed` (outside the frozen payload above); T2 accepted them as `5294fdb0860092b20c1fcc9c71b781b8666518a3`, reporting81purePASS, not authenticated service acceptance. Bounded SKU versions/heads/audit DDL for T2's real service is next. Jobs/receipts/trusted worker closing authority, immutable source settings/evidence, T4 draft/send/in-app notification persistence, extension token-only ingestion, actual credential consumer transport, synthetic maintenance/restore and live owner-published heartbeat remain active local work. No placeholder/fail-closed stub is marked as an implemented service.

Missing operational inputs must be recorded with impact, not invented: independently proven Avito external account mapping/registrar, key custody and maintenance principal assignment, production token TTL/abuse/body limits, mixed-version queue drain policy, rollout/observation approval and source completeness/chronology rules when not present in actual provider data. Synthetic/local implementation with explicit fixture inputs may proceed independently. Real backfill, key rotation, plaintext cleanup, encrypted-only production switch and deployment remain unauthorized.

New rollout/scheduler/allowlist gates stay default-off. Some unchanged legacy collector/price worker defaults are true; this report does **not** claim every historical flag is false.

## Evidence provenance and safety limitations

This continuation used local source and synthetic verification only: no GitHub/CodeRabbit, production/provider action, real credentials, `.env`, push or deployment. Earlier Stage 1 history contains a synthetic provider-403 incident; therefore no whole-branch-history provider-free claim is made. The permission implementer also ran an unnecessary read-only broad catalog prefix enumeration and returned empty; this was disclosed and prohibited from repetition. Main cleanup evidence uses exact own allocator targets, not that prefix query.

Permission review's documentation fence-order issue is fixed; the immutable feature commit's nonconforming subject remains an explicitly accepted metadata deviation. Historical RED chronology is attributed to contemporaneous implementer reports, not reconstructed from the final diff. Inherited `permissions.py` Ruff I001 and existing passfile/temp-cleanup warnings remain disclosed; new import diagnostics were fixed. None is a waiver of combined integration or final independent review.
