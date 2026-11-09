# T1 runtime package discovery handoff

Date: 2026-09-09. Task 1 on `codex/arch-t1-platform`; implementation parent
`0ced916764f89feb2de3d0f7d987ed4c14f23988` (controller docs-only amendment
over the admitted clean base `c2281458e4ec8a30812973ad632b73f72249b36b`).

## Implemented contract

The static setuptools package list is replaced by bounded discovery in exactly
two roots: `.` and `backend_contracts`. Includes remain limited to `app`,
`app.*`, `vella_wb_19_05`, and `vella_wb_19_05.*`. Namespace-aware discovery
preserves the existing implicit `app.reviews` package. The mapped
`vella_wb_19_05` package directory, project metadata, dependencies, test extras,
pytest configuration, and package-data behavior are unchanged.

The wheel regression now constructs a future nested regular package only in a
temporary clean source and proves its module plus fixed
`synthetic-wheel-proof` sentinel survive archive build, `--no-index --no-deps`
installation into a fresh target, and `python -I` import from an outside-checkout
working directory. It also runs the actual configured setuptools finder against
the raw temporary source: the nested `app.*` package is discovered, an unrelated
top-level regular package is rejected, and a copied `include = ["*"]` positive
control proves that unrelated package was otherwise discoverable.

Archive and installed-origin representatives now include
`app.platform.integrations.credential_store` and
`app.platform.integrations.publication_guard`. Existing representatives,
including `app.reviews.canonical_contract` and mapped
`vella_wb_19_05.models`, remain asserted. Every imported `__file__` must resolve
beneath the exact disposable installation target, so neither the editable
checkout nor stale build output can satisfy the proof.

The stale-build regression now adds `exclude = ["app.wb_ads_cache"]` only to
its temporary parsed find configuration and proves that this is the sole TOML
mutation. Real source remains present in the clean stage while the deliberately
seeded stale build file and all forbidden cache/build/dist/venv/egg-info inputs
remain absent from the wheel/stage as applicable. The real project discovery
configuration has no such exclusion.

## Verification

All runtime/build commands used the worktree `backend/.venv`, a scrubbed
environment, and the plan-owned sandbox profile denying all networking and known
secret files/locations. Wheel build uses `--no-index --no-deps
--no-build-isolation --no-cache-dir`; wheel install uses `--target` with
`--no-index --no-deps --no-cache-dir`. No dependency or environment mutation
was performed.

The prospective RED against the old explicit list exited 1 with exactly the
expected missing archive member:
`app/wheel_probe_nested/deeper/value.py`. After the initial spec's
`namespaces = false` change, the complete gate correctly caught a real
regression: `app/reviews/canonical_contract.py` disappeared. The controller
amended the spec to `namespaces = true`; no representative assertion was removed
and no domain initializer or placeholder was added.

The corrected complete offline wheel gate passed: **3 passed in 3.78s**, exit 0.
It printed installed-target origins for all 11 representative imports, including
the two integration modules, implicit reviews module, synthetic sentinel module,
and mapped reference package. The stale-build/exclusion test independently
passed: **1 passed in 1.24s**, exit 0. `compileall`, `git diff --check`, and a
parsed full-TOML comparison against the implementation parent each exited 0;
the comparison proved only `tool.setuptools.packages` changed and that all 10
dependencies, 5 test requirements, and `package-dir` mapping are unchanged.

Scoped Ruff exits 1 for the same sole `I001` import-order finding present in the
base test file; a direct base-file stdin run reproduced that exact retained
baseline. No new Ruff finding is introduced, and no broad formatting cleanup is
included in this scoped change.

Exact commands, natural exits, installed origins, and self-critic notes are in
the ignored local execution report
`.superpowers/sdd/2026-09-09-runtime-package-discovery/task-1-report.md`.

## Limits and next gate

This change proves prospective bounded discovery and installed-wheel origins in
a synthetic clean source. It does not add or copy `app.orders`, domain code,
dependencies, flags, services, PostgreSQL work, runtime configuration, or real
credentials. It performs no provider/network call and reads no secret file.

The actual merged T3 `app.orders` source does not exist in this checkout and
remains a later integration gate. After T3 is merged, the coordinator must rerun
this wheel test against the real package plus the full contract/suite set.
Controller independent review is still required before acceptance.
