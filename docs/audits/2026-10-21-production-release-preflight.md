# Production release preflight — 21 October 2026

## Scope and status

The user authorized testing the current Satorna changes and deploying them to
`https://app.comcookie.store`. **Deployment has not occurred.** The known VPS is
109.69.22.214; the supplied screenshot identifies root, port 22, and a separate
SSH identity `~/.ssh/satorna_vps`. That identity file does not exist on this
computer. The existing default identity was rejected with Permission denied.
No private key contents were read or printed. No production data, configuration,
containers, keys or services were modified.

## Completed checks

- `npm run build`: TypeScript and Vite production build passed. Existing large
  chunk/Browserslist-age warnings remain; they are not build failures.
- Production readiness returned HTTP 200 with successful TLS verification from
  this machine. This confirms current service availability, not this release.
- 132 targeted backend checks passed. They cover disposable PostgreSQL migration
  and finance evidence behavior, restricted-role/security paths, WB extension
  HTTP/database roundtrips, browser-price snapshots and repricer projections.
- The migration/runtime subset was repeated: 7 passed. Fresh upgrade, downgrade,
  re-upgrade, the new classified-finance migration and its evidence-preserving
  downgrade guard use disposable databases, never the working or production DB.
- 65 focused frontend checks passed, including isolated Chrome date-switch,
  advertising/expense/margin rendering, extension connection and price states.
- 224 WB/Avito extension tests passed; no marketplace access was used.
- The actual production extension packager ran in an isolated temporary source
  copy for app.comcookie.store. Both ZIPs passed integrity, source-manifest version,
  origin binding and connection checks. WB 0.1.12 has one credential input and no
  manual server-address input; Avito is 0.2.21. Existing workspace ZIP deletion
  and other user changes were preserved.
- Publication scan found no matching raw credentials, local DBs or private
  runtime artifacts. `git diff --check` passed.

## Confirmed release-check defect repaired

`.github/workflows/vps-release.yml` compared the downloaded ZIP manifests against
obsolete hardcoded WB 0.1.11 and Avito 0.2.20 versions. It now compares each ZIP
against its source manifest, so version bumps do not incorrectly fail this gate.
The packaged current manifests were independently checked. This is not a claim
that the GitHub container workflow has run on the uncommitted working tree.

## Unverified / blocking

- Docker is unavailable locally: all 3 Compose-specific tests were explicitly
  skipped. Linux image build/start, production worker/beat configuration and
  production Python 3.11 behavior are **not confirmed** by the local Python 3.13
  checks. No guarantee of server-wide readiness is made.
- SSH access is required to inspect the existing production checkout, actual
  container/service configuration, Python/image version, database revision,
  backup/rollback procedure and preserved environment/key volumes before update.
- The current production database has not been migrated. Its revision and
  compatibility cannot be inferred from disposable migration tests.
- Existing genuine missing financial mappings/costs/observations remain missing;
  deployment would not create those source observations automatically.

To continue, place the existing authorized key at the supplied local path (or
provide its correct local path). Do not paste a private key or password into the
chat. After access is available, inspect and back up the actual existing
deployment before applying the tested source/artifacts, then verify readiness,
worker/beat, protected HTTP and the matching report periods after deployment.
