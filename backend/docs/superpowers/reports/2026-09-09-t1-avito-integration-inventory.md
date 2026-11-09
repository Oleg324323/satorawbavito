# T1 current Avito integration inventory — local source evidence

Inspected T1 edd9d4b, no provider requests. Findings are remaining cutover obligations,
not claims of exploit verification. Line numbers refer to this baseline.

| Surface | Evidence | Consequence / owner |
|---|---|---|
| Auth | app/avito/auth.py:62 resolves user cache, writes cache_user_avito_access_token | T1 encrypted OAuth adapter must resolve exact owner and never cache plaintext |
| Listings | routers/avito_listings.py:141 user-or-org credentials; cache filter list not canonical ownership | T1 resolver boundary; domain account filtering must be preserved, never infer mapping |
| Orders | routers/avito_orders.py:593 credentials resolver, :822 org browser snapshot | T1 router cutover / T3 client and canonical facts |
| Returns | avito/returns_tasks.py:119 org credential, :186 org iteration; returns_orm.py:14 unique(org,identity_key), nullable external account | T1 transport; T3 requires canonical account-owned facts, not column guesses |
| Reviews | routers/avito_reviews.py:105,:238 user-or-org resolver; avito/reviews.py:134 str(exc) result | T4 domain, T1 exact adapter imports/grants; new schema request 4a7d669 |
| Repricer | repricer_tasks.py:805 org resolver; routers/avito_repricer.py:644 price POST and :646 raw error logging | T2 domain; require durable account-scoped intent/attempt, safe errors, IDs-only task |
| Messenger | routers/avito_chats.py:164 message POST uses caller accountId, :174/:187 raw exception response | No account-bound authorization proof from credential selection; T1 integration boundary, domain send ownership must be coordinated before wiring |
| Statistics | routers/avito_stats.py:231 user/org credentials; avito/stats.py:751-757 logs request/body diagnostics | T2 analytics/domain + T1 safe adapter boundary; secret/PII canary tests required before cutover |
| Overview aggregation | routers/avito_overview.py:35 filters cache by caller account IDs, :65 returns str(exc); imports user/org credential stores | T1 exact resolver and safe error boundary; account-list cache partition is not allowed-account authorization; preserve explicit stale/partial read-cache labeling |
| Notifications | routers/avito_notifications.py:88 org/user resolver, :38 org read-state cache, :81 raw error | T4 normalized recipient-owned delivery/read state; T1 credential boundary |
| XML | avito/notifications.py:215 only blocked-actions metadata in inspected Avito namespace | No verified XML publisher found; XLSX XML files are not an Avito feed adapter |
| Wallet | No wallet/balance API implementation found in app/avito and routers/avito*.py | Do not invent a client or claim wallet migration complete; wider product source recovery if required |
| Shared caches | repricer_cache/store.py:79 SQL errors -> None; :735 save_source_cache returns unsaved payload at end | T1 new ingestion mutation must not use this success fallback; T2 owns legacy repricer persistence changes |
| Background transport | canonical_shadow_tasks.py:137 carries wb_token in local dispatch kwargs; returns tasks org only | Trace each transport before claiming Celery safe; token appearing in local dict alone is not proof it was broker-published |

Static inventory command: rg legacy credential resolver names in backend/app/*.py,
then follow referenced Avito routers/clients and cache writer. XML/wallet search
limited explicitly to app/avito and routers/avito*.py; absence outside that scope is
not asserted. No real caches, logs, snapshots or credential rows were inspected.

2026-09-09 source refresh at ba08f02: file inventory includes all named Avito router
and app/avito modules, including orders AI/XLSX and overview. A case-insensitive
co-occurrence search for Avito and XML/wallet/balance over backend/app Python files
returned no additional matches (rg exit1); this is a bounded search result, not
proof a product feature cannot exist elsewhere. Clients remain unmodified: preserve
valid single-account adapters and move authorized fan-out outside them. Chat client
selects the first caller account when supplied (avito/chats.py:209-219); messages
are keyed by chat ID inside that account fetch, so cross-account aggregation must
carry the account identity explicitly rather than reuse that map globally.

Current shutdown candidates (not a diagnosis): infra/health.py owns a global
ThreadPoolExecutor; repricer_sync.py:1194 owns another executor reachable from daemon
onboarding threads in routers/wb_repricer_bff.py:4832. Need stack evidence under an
offline synthetic test before choosing a fix. Never hide with daemonization/kill.
