# Bounded runtime package discovery

2026-09-09, T1 Stage6 local build decision, not implemented or verified yet.

Current pyproject explicitly lists29 packages; metadata-only comparison finds27
actual app packages and no current missing package. T3's future app.orders is not
in this checkout or explicit list. Existing wheel test checks eight representative
imports and cannot prove future-package inclusion. Do not copy T3 domain code or
claim the current checkout already loses app.orders from a wheel it cannot build.

Use setuptools bounded discovery for Python packages under app and the
existing mapped reference package. Keep dependencies, project metadata, pytest
paths and package-data behavior unchanged. Installed setuptools/config/expand.py
was inspected: find_packages accepts multiple where roots and package-dir mappings
are filled for alternate roots. Existing app.reviews is an implicit namespace
explicitly present in the old package list. A real wheel regression proved that
namespaces=False loses its canonical_contract.py; preserve it with namespace-aware
discovery inside the same exact include roots. No domain __init__.py is added.

```toml
[tool.setuptools.packages.find]
where = [".", "backend_contracts"]
include = ["app", "app.*", "vella_wb_19_05", "vella_wb_19_05.*"]
namespaces = true

[tool.setuptools.package-dir]
vella_wb_19_05 = "backend_contracts/vella_wb_19_05"
```

Replace only the explicit packages list with this block. No repository-wide
unbounded discovery, setup.py/build hook, runtime path injection or app.orders
placeholder. Future regular app subpackages are included by the same rule without
inventing domain code. Existing implicit namespaces within those roots remain
supported; unrelated top-level namespaces and non-Python operational files are not
part of this contract. The controller's source-only finder comparison found the
sole current extra namespace app.reviews and no non-Python source-tree files.

Preserve clean-source staging in test_installed_wheel.py: only pyproject and
Python source in its two named roots, excluding existing caches/build/dist/venvs/
egg-info. Stale wheel/build files must never satisfy package proof. Build with
own .venv pip, --no-index/--no-deps/--no-build-isolation/--no-cache-dir; install only
into a newly allocated temporary target, never the worktree venv or another
project. Import from an outside-checkout cwd using -I and verify __file__ origins.
No network, env/credentials/provider imports or dependency resolution.

Add a synthetic nested regular package only in a temporary build source, never
app itself. Its __init__.py and module expose a fixed harmless sentinel. Existing
explicit-list build must miss it before the change; discovery must include it
and permit isolated installed import afterward. Also prove an unrelated top-level
synthetic package is not discovered by running actual configured setuptools
find_packages against the raw temporary source. Clean staging intentionally never
copies that unrelated root, so archive absence alone would not test the include
filter. Keep these two proofs distinct. Real new guard/fetch modules are safe import
representatives; validate their installed origins as well as archive presence.
Do not import all runtime modules, since the production Celery factory registers
business tasks and config side effects.

Adapt the stale-build regression to deliberately exclude app.wb_ads_cache through
the temporary find configuration, then seed its stale build/lib file. Assert the
clean wheel still omits it, while staged real source still exists. This preserves
the test's original negative-control meaning; do not delete it because its old
literal packages-list mutation no longer matches. Preserve forbidden-file archive
and clean-stage checks. No broad lint cleanup of the existing test is implied.

This slice closes prospective package discovery and installed-origin proof, not
full integrated T3 import/runtime compatibility. After domain merge, coordinator
must rerun the same wheel gate against actual app.orders plus full contracts/
suites. No build artifact, synthetic package, .env or operational data is committed.
