from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.requirements import Requirement

from ops.release_gate import safe_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_RUNTIME_FILES = {
    "app/cash_flow/__init__.py",
    "app/platform/funnel/__init__.py",
    "app/report_rules/presets.py",
    "app/wb_ads_cache/__init__.py",
    "app/security/__init__.py",
    "app/platform/integrations/__init__.py",
    "app/platform/integrations/credential_store.py",
    "app/platform/integrations/publication_guard.py",
    "app/reviews/canonical_contract.py",
    "app/reviews/canonical_router.py",
    "app/reviews/canonical_read.py",
    "app/reviews/local_http.py",
    "app/reviews/canonical_sync.py",
    "app/orders/router.py",
    "app/orders/read_service.py",
    "app/main.py",
    "app/infra/celery_app.py",
    "app/avito/returns_tasks.py",
    "app/modules/orders.py",
    "app/orders/job_publication.py",
    "app/orders/ingestion_publication.py",
    "app/platform/integrations/user_orders_jobs.py",
    "app/platform/integrations/ingestion_publication_guard.py",
    "app/platform/integrations/ingestion_api.py",
    "app/platform/integrations/worker_identity.py",
    "vella_wb_19_05/models.py",
}

REPRESENTATIVE_IMPORTS = (
    "app.cash_flow",
    "app.platform.funnel",
    "app.report_rules.presets",
    "app.wb_ads_cache",
    "app.security",
    "app.platform.integrations",
    "app.platform.integrations.credential_store",
    "app.platform.integrations.publication_guard",
    "app.reviews.canonical_contract",
    "app.reviews.canonical_router",
    "app.reviews.canonical_read",
    "app.reviews.local_http",
    "app.reviews.canonical_sync",
    "app.orders.router",
    "app.orders.read_service",
    "app.main",
    "app.infra.celery_app",
    "app.avito.returns_tasks",
    "app.modules.orders",
    "app.orders.job_publication",
    "app.orders.ingestion_publication",
    "app.platform.integrations.user_orders_jobs",
    "app.platform.integrations.ingestion_publication_guard",
    "app.platform.integrations.ingestion_api",
    "app.platform.integrations.worker_identity",
    "vella_wb_19_05.models",
)

EXCLUDED_BUILD_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=safe_environment(),
    )


def _stage_build_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")
    for relative_root in (
        Path("app"),
        Path("backend_contracts/vella_wb_19_05"),
    ):
        for source_file in sorted((source / relative_root).rglob("*.py")):
            relative_file = source_file.relative_to(source)
            if any(
                part in EXCLUDED_BUILD_PARTS or part.endswith(".egg-info")
                for part in relative_file.parts
            ):
                continue
            staged_file = destination / relative_file
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, staged_file)


def _build_wheel(source: Path, wheelhouse: Path) -> Path:
    clean_source = wheelhouse.parent / f"{wheelhouse.name}-source"
    _stage_build_source(source, clean_source)
    wheelhouse.mkdir()
    _run(
        "-m",
        "pip",
        "wheel",
        ".",
        "--wheel-dir",
        str(wheelhouse),
        "--no-build-isolation",
        "--no-deps",
        "--no-index",
        "--no-cache-dir",
        "--disable-pip-version-check",
        cwd=clean_source,
    )

    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_test_extra_declares_offline_wheel_build_requirements() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    build_requirements = set(metadata["build-system"]["requires"])
    test_requirements = set(metadata["project"]["optional-dependencies"]["test"])
    assert build_requirements <= test_requirements


def test_wheel_build_does_not_reuse_stale_build_output(tmp_path: Path) -> None:
    synthetic_source = tmp_path / "synthetic-source"
    synthetic_source.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", synthetic_source / "pyproject.toml")
    for source_root in (
        PROJECT_ROOT / "app",
        PROJECT_ROOT / "backend_contracts" / "vella_wb_19_05",
    ):
        for source_file in source_root.rglob("*.py"):
            destination = synthetic_source / source_file.relative_to(PROJECT_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)

    pyproject = synthetic_source / "pyproject.toml"
    original_metadata = pyproject.read_text()
    mutated_metadata = original_metadata.replace(
        "namespaces = true",
        'namespaces = true\nexclude = ["app.wb_ads_cache"]',
    )
    assert mutated_metadata != original_metadata
    original_config = tomllib.loads(original_metadata)
    expected_config = copy.deepcopy(original_config)
    expected_config["tool"]["setuptools"]["packages"]["find"]["exclude"] = [
        "app.wb_ads_cache"
    ]
    assert tomllib.loads(mutated_metadata) == expected_config
    pyproject.write_text(mutated_metadata)

    stale_file = synthetic_source / "build/lib/app/wb_ads_cache/__init__.py"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text('"""Stale build output must not reach the wheel."""\n')

    excluded_files = (
        synthetic_source / ".env",
        synthetic_source / "app/operational-snapshot.json",
        synthetic_source / "app/nested/build/leak.py",
        synthetic_source / "app/nested/dist/leak.py",
        synthetic_source / "app/nested/.venv/leak.py",
        synthetic_source / "app/nested/.pytest_cache/leak.py",
        synthetic_source / "app/nested/example.egg-info/leak.py",
    )
    for excluded_file in excluded_files:
        excluded_file.parent.mkdir(parents=True, exist_ok=True)
        excluded_file.write_text("synthetic test sentinel\n")

    wheel = _build_wheel(synthetic_source, tmp_path / "synthetic-wheelhouse")
    with zipfile.ZipFile(wheel) as archive:
        assert "app/wb_ads_cache/__init__.py" not in archive.namelist()
    clean_source = tmp_path / "synthetic-wheelhouse-source"
    assert (clean_source / "app/wb_ads_cache/__init__.py").is_file()
    for excluded_file in excluded_files:
        assert not (clean_source / excluded_file.relative_to(synthetic_source)).exists()


def test_built_wheel_contains_and_imports_runtime_packages(tmp_path: Path) -> None:
    synthetic_source = tmp_path / "synthetic-source"
    _stage_build_source(PROJECT_ROOT, synthetic_source)

    probe = synthetic_source / "app/wheel_probe_nested/deeper"
    probe.mkdir(parents=True)
    (probe.parent / "__init__.py").write_text("", encoding="utf-8")
    (probe / "__init__.py").write_text("", encoding="utf-8")
    (probe / "value.py").write_text(
        "VALUE = 'synthetic-wheel-proof'\n", encoding="utf-8"
    )
    unrelated = synthetic_source / "synthetic_unrelated_package/__init__.py"
    unrelated.parent.mkdir()
    unrelated.write_text("", encoding="utf-8")

    wheel = _build_wheel(synthetic_source, tmp_path / "wheelhouse")
    with zipfile.ZipFile(wheel) as archive:
        installed_files = set(archive.namelist())
        metadata_path = next(
            name for name in installed_files if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
    requirements = [
        Requirement(value) for value in metadata.get_all("Requires-Dist", [])
    ]
    for name, specifier in (("setuptools", ">=69"), ("wheel", "")):
        requirement = next((item for item in requirements if item.name == name), None)
        assert requirement is not None, f"test extra is missing {name}"
        assert str(requirement.specifier) == specifier
        assert requirement.marker is not None
        assert requirement.marker.evaluate({"extra": "test"})
        assert not requirement.marker.evaluate({"extra": ""})
    expected_runtime_files = EXPECTED_RUNTIME_FILES | {
        "app/wheel_probe_nested/deeper/value.py"
    }
    assert expected_runtime_files <= installed_files, (
        "built wheel is missing runtime files: "
        f"{sorted(expected_runtime_files - installed_files)}"
    )

    from setuptools.config.expand import find_packages

    config = tomllib.loads(
        (synthetic_source / "pyproject.toml").read_text(encoding="utf-8")
    )
    discovery_config = config["tool"]["setuptools"]["packages"]["find"]
    discovered = find_packages(
        root_dir=str(synthetic_source), **discovery_config
    )
    assert "app.wheel_probe_nested.deeper" in discovered
    assert "synthetic_unrelated_package" not in discovered
    broad_discovery_config = copy.deepcopy(discovery_config)
    broad_discovery_config["include"] = ["*"]
    broadly_discovered = find_packages(
        root_dir=str(synthetic_source), **broad_discovery_config
    )
    assert "synthetic_unrelated_package" in broadly_discovered

    target = tmp_path / "installed"
    _run(
        "-m",
        "pip",
        "install",
        str(wheel),
        "--target",
        str(target),
        "--no-index",
        "--no-deps",
        "--no-cache-dir",
        "--disable-pip-version-check",
        cwd=tmp_path,
    )

    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    import_probe = """
import importlib
import json
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
origins = {}
for module_name in json.loads(sys.argv[2]):
    module = importlib.import_module(module_name)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(target):
        raise AssertionError(f"{module_name} loaded outside install target: {origin}")
    origins[module_name] = str(origin)
for module_name, expected_value in json.loads(sys.argv[3]).items():
    actual_value = getattr(importlib.import_module(module_name), "VALUE")
    if actual_value != expected_value:
        raise AssertionError(
            f"{module_name}.VALUE was {actual_value!r}, expected {expected_value!r}"
        )
print(json.dumps(origins, sort_keys=True))
"""
    representative_imports = (
        *REPRESENTATIVE_IMPORTS,
        "app.wheel_probe_nested.deeper.value",
    )
    probe = _run(
        "-I",
        "-c",
        import_probe,
        str(target),
        json.dumps(representative_imports),
        json.dumps(
            {"app.wheel_probe_nested.deeper.value": "synthetic-wheel-proof"}
        ),
        cwd=outside_checkout,
    )
    origins = json.loads(probe.stdout)
    assert set(origins) == set(representative_imports)
    print(f"wheel={wheel}")
    print(json.dumps(origins, indent=2, sort_keys=True))
