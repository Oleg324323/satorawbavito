# Avito release verification — 2026-10-05

Status: local implementation prepared; NOT a confirmed production release.

## 2026-10-07 customer-chat size follow-up

Existing Avito extension/AI pipeline extended with exact order/item/customer chat
binding, role/time-aware evidence, strict AI proof confirmation and no listing-size
fallback in chat mode. Confirmed size/review reason flows through durable snapshot,
Orders and saved-only XLSX. Details and activation limitations:
`outputs/AVITO-CHAT-SIZE-20261007.md`. Gates: 176 scoped backend / 89 extension /
42 focused frontend tests passed; TypeScript/build/manifest/ZIP checks passed.
Installed extension is now 0.2.9. No live account or live AI-service verification;
tested local AI configuration lacks a key. Shared API not restarted; WB untouched.

## 2026-10-07 Avito regression follow-up

This section supersedes the older collection-before-export behavior below. WB code, extension artifacts, marketplace data and workers were not changed for this follow-up.

- Both picking-list and returns-list buttons now download saved data directly. They neither invoke the extension nor open Avito tabs. The backend exporter reads saved images only and preserves current-credential authorization before cache access.
- The returns workbook has 11 columns: product fields, order/listing identifiers and return pickup location/deadline/code. Shipment and sticker columns and label images are removed only from this workbook. The regular picking list retains shipment numbers and original labels.
- Explicit return instructions now carry pickup place, deadline and exact collection code through collection, ingestion, saved snapshot, API rows and XLSX. Leading zeros remain text. Missing collection, failed collection, absent source fields and conflicting values have distinct export messages. Multiple codes are not guessed. Adjacent multiline headings cannot become field values.
- Terminal orders are not enriched through historical detail tabs. Minimal status updates reconcile only unique saved account/order identities, including the current-credential API queue, so received/canceled orders leave the operational export without losing saved details. Ambiguous account matches and unknown historical rows are rejected.
- Inventory pagination rejects repeated or malformed pages instead of claiming completeness. An optional statistics failure no longer hides otherwise valid listing inventory; unavailable metrics remain null. Existing statistics formulas and working behavior were preserved.
- Queue photos use saved account/listing photo IDs. Catalog enrichment uses account plus listing identity and rejects duplicate matches. Label verification checks only the requested missing-label identities.

Verification: 136 selected Avito backend tests passed, including HTTP to isolated SQLite to stored photo/original PDF to both XLSX files. All 255 frontend Avito tests passed, including actual browser export clicks with zero extension messages and zero popup tabs. TypeScript passed. Extension 83 tests, manifest checks and build passed. Source, unpacked build, installed folder and downloadable ZIP are byte-identical version 0.2.8.

Delivery: updated `/Users/bratishka/Downloads/satorna-avito-orders-extension`; previous version retained in `/Users/bratishka/Downloads/satorna-avito-extension-backup.CST58s`. Built ZIP: `frontend/public/downloads/satorna-avito-orders-extension.zip`.

Not verified: authenticated live collection of the user's current catalog and return details, live provider statistics coverage, shipment numbers unavailable from genuine source instructions, production behavior/deployment. The shared running local API was NOT restarted to avoid interrupting WB; server changes require its next coordinated restart. Chrome must reload extension 0.2.8 before a separate explicit return collection. Existing saved records are not claimed to have recovered new source fields. No deployment or marketplace business actions.

## Implemented locally

- One-action webpage/extension collection bridge before order/return XLSX download, visible partial/error outcomes, bounded waits.
- Durable order detail checkpoints, reuse of saved fields, five concurrent detail workers, pagination/hydration waits, bounded retry and security/429 stop.
- Active-only listing photo collection and repricer inventory; order/return photos remain eligible even for inactive listings. Stored database image bytes used by Excel before CDN.
- Original PDF label import/matching, multiple labels per PDF/page, missing-label context, persisted-save verification, repeat collection.
- Extension popup progress/settings layout, sidebar/overview cleanup, returns tab navigation, absolute plus percentage metric trends.
- Explicit order variant provenance: listing guesses cannot replace confirmed buyer selection.
- Export reconciles newer observed browser statuses with API cache; unknown/conflicting undated states cannot silently become shipping-ready. Checkpoints retain original timestamps for unvisited rows.
- Extension 0.2.7 build and downloadable ZIP share identical source. Frontend production build regenerates the extension archive.

## Verification performed

- 92 backend tests passed, including isolated real SQLite persistence through HTTP TestClient, photo import, original multi-label PDF import and both XLSX downloads.
- Created XLSX files inspected as ZIP containers: embedded photo and label media; barcode decoded from the embedded image with leading zeros preserved. Provider input was synthetic, not a live Avito account.
- 77 extension tests passed and manifest/JavaScript/build checks passed.
- 28 frontend tests passed earlier in this work, including fixture-based browser tests; TypeScript and Vite production build checked.
- Independent targeted code critic verified both status reconciliation regressions; nine targeted tests passed.
- Controlled benchmark against backup extension code: 20 synthetic delayed detail reads, old 3262 ms / current 173 ms (18.86x), cached 2 ms and zero detail calls. This is NOT a measured 10x improvement on live Avito.

## Runtime and delivery

- Source: `/Users/bratishka/Downloads/OMNIlos-satornawb-2bb102d` (ZIP checkout, no Git metadata).
- Updated unpacked extension: `/Users/bratishka/Downloads/satorna-avito-orders-extension`; previous files retained in `/Users/bratishka/Downloads/satorna-avito-extension-backup.Sm37HZ`.
- Chrome must reload the unpacked extension to activate the changed files. The store-installed extension is not updated by replacing this folder.
- Local frontend `http://127.0.0.1:5177`, read-only local backend on 58017. Database retained; restarting preview can require reauthentication.
- Default extension target verified: `https://satorna-wb.vercel.app`. Local test must explicitly use local server configuration; no fallback to another server on failure.
- Protected new collection endpoint returns local 401 (route present), production 404 (old deployment).

## Remaining blockers / unverified

- No authenticated live Avito → extension → database → XLSX run on the new version. Existing Chrome contains the user's session, but Chrome JavaScript-from-Apple-Events is disabled and no attached debugging connection exists. No security settings, cookies, or profiles changed. No new browser windows should be opened.
- User manual extension reload/run requested; no confirmed result yet.
- Backend/frontend production deployment not performed. Available Vercel session was not authenticated; no verified production deployment access/path established. Do not overwrite production or GitHub main from this ZIP checkout blindly.
- Live collection timings, provider layout compatibility for all orders/returns, and completeness of real customer workbooks remain unverified. Code tests alone do not close the task.
