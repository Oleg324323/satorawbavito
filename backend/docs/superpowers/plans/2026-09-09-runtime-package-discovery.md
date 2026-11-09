# Runtime package discovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** Include future regular app subpackages, preserve existing implicit app.reviews, and prove installed-wheel origins without copying domain code.

**Architecture:** Bounded setuptools discovery replaces the static package list;
clean temporary build fixtures prove future nested packages and negative exclusions.

**Tech Stack:** Existing setuptools/wheel/pip/pytest and stdlib, no dependencies.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-runtime-package-discovery-design.md`, read fully.

## Global constraints

- Only T1 worktree/branch; no production, working services, provider/network,
  GitHub/push/deploy, secrets/.env reading, flags or host changes.
- Own backend/.venv build/test runtime; alternate Ruff lint-only. No dependency
  installation. Wheel installation solely --no-index --no-deps into exact new
  disposable target is an explicitly required verification step, not venv mutation.
- No T3 source copies, app.orders placeholders, other domain/task/frontend edits.
- No existing baseline/skip expansion. Current metadata scan finds no missing app
  package; prospective synthetic test RED must not be called actual T3 failure.

## Task 1: Discover bounded regular packages and prove clean installation

**Files:**
- Modify `backend/pyproject.toml` only package discovery configuration.
- Modify `backend/tests/test_installed_wheel.py` only packaging regression/representatives.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-runtime-package-discovery-handoff.md`.

**Consumes:** existing complete pyproject/wheel test, installed setuptools find
implementation and new safe credential_store/publication_guard modules. No live
configuration or keyring is required for their module imports.

**Produces:** exact TOML find block in spec, future-package and mapped-reference
archive/import proof, unchanged clean-build exclusion regression.

- [ ] Step1 extend the temporary synthetic build fixture with a nested regular
  package. Write test expecting its files and fixed sentinel from installed target.
  Run RED before changing pyproject, under denied network/secret-file sandbox.

```python
probe = synthetic_source / "app/wheel_probe_nested/deeper"
probe.mkdir(parents=True)
(probe.parent / "__init__.py").write_text("", encoding="utf-8")
(probe / "__init__.py").write_text("", encoding="utf-8")
(probe / "value.py").write_text("VALUE = 'synthetic-wheel-proof'\n", encoding="utf-8")
# Build from this temporary source using existing _build_wheel; assert archive
# contains app/wheel_probe_nested/deeper/value.py, then install/probe with -I.
```

- [ ] Step2 replace the static list with exact spec TOML. Namespace-aware
  discovery with namespaces=true preserves existing implicit app.reviews; never
  remove its representative assertions or add domain package placeholders. The
  initial namespaces=false plan produced a real missing canonical_contract.py
  failure and is superseded by this correction, not accepted as a baseline.
  Adapt temporary
  stale-source pyproject mutation by adding `exclude = ["app.wb_ads_cache"]`
  inside packages.find, asserting the mutation changed only that test config.
  Do not add this exclusion to the real project. Preserve mapped reference root.
- [ ] Step3 expand safe representative archive/import sets with
  app.platform.integrations.credential_store and
  app.platform.integrations.publication_guard. Add unrelated top-level synthetic
  regular-package negative case against actual configured discovery, not only
  archive absence after staging already excluded it. Assert all import origins under exact target;
  never let editable checkout path or stale build satisfy them.

```python
from setuptools.config.expand import find_packages
config = tomllib.loads((synthetic_source / "pyproject.toml").read_text(encoding="utf-8"))
discovered = find_packages(root_dir=str(synthetic_source),
                          **config["tool"]["setuptools"]["packages"]["find"])
assert "app.wheel_probe_nested.deeper" in discovered
assert "synthetic_unrelated_package" not in discovered
```

Create synthetic_unrelated_package/__init__.py in that raw temporary source
before this probe. An include-filter mutation to `include=["*"]` in the probe's
copied dictionary must discover it, proving the negative test has a positive
control. Never write that broad filter into real pyproject.
- [ ] Step4 GREEN all existing/new wheel tests; independently inspect archive
  exclusion assertions and unchanged dependency/metadata configuration. Tests use
  no-index/no-deps temporary installation only, no project environment mutation.

```text
cd backend
env -i PATH=/usr/bin:/bin TMPDIR=/tmp PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-runtime-package-discovery/offline.sb \
  .venv/bin/python -m pytest -q -s tests/test_installed_wheel.py --tb=short
```

The plan-owned profile denies all networking and known secret files/locations;
copy the prior verified profile via apply_patch but remove both UnixPG allow rules,
then read/prove the resulting denial profile before execution. No service required.

- [ ] Step5 scoped Ruff test file (report any retained exact baseline separately),
  compileall test file, git diff --check and TOML metadata comparison. Self-review,
  bounded commit `build: discover bounded runtime packages`; handoff exact tests,
  wheel temporary installation origin proof and integration limitation. Independent
  task review before acceptance; actual merged T3 wheel remains later gate.

## Preflight

One task changes config and its own proof. Exact include roots match existing
package-dir contract; no runtime import path mutation. Current eight-import gate
is extended, not replaced. Synthetic future-package and stale-exclusion cases
exercise opposing sides of discovery. All fixture writes occur only in owned
temporary build source; real application packages remain untouched.
