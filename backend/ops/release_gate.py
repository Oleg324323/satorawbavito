#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "ops" / "legacy-test-failures.txt"
FOCUSED_TESTS = (
    "tests/test_period.py",
    "tests/test_finance_normalization.py",
    "tests/test_finance_service.py",
    "tests/test_finance_shadow_bridge.py",
    "tests/test_finance_v2.py",
    "tests/test_identity_membership_bridge.py",
    "tests/test_period_finance_legacy_bridge.py",
    "tests/test_finance_migration.py",
    "tests/test_runtime_database_role.py",
)
CONTRACT_TESTS = (
    "tests/test_common_contracts.py",
    "tests/test_openapi_generated_models.py",
    "backend_contracts/tests",
)
Step = tuple[str, tuple[str, ...], Path]
Executor = Callable[[tuple[str, ...], Path], int]


class GateFailure(RuntimeError):
    def __init__(self, step: str, exit_code: int = 1) -> None:
        self.step = step
        self.exit_code = exit_code
        super().__init__(f"{step} failed with exit code {exit_code}")


class GateInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.exit_code = 128 + signum


def run_steps(
    steps: tuple[Step, ...], executor: Executor, completed: list[str]
) -> None:
    for name, command, cwd in steps:
        print(f"[RUN] {name}", flush=True)
        exit_code = executor(command, cwd)
        if exit_code:
            raise GateFailure(name, exit_code)
        completed.append(name)
        print(f"[PASS] {name}", flush=True)


def compare_failure_sets(
    expected: set[str], actual: set[str], errors: set[str]
) -> dict[str, object]:
    added = actual - expected
    missing = expected - actual
    return {
        "status": "exact" if not added and not missing and not errors else "changed",
        "expected_count": len(expected),
        "actual_count": len(actual),
        "expected_ids": sorted(expected),
        "actual_ids": sorted(actual),
        "added_ids": sorted(added),
        "missing_ids": sorted(missing),
        "error_ids": sorted(errors),
    }


def junit_failure_ids(path: Path) -> tuple[set[str], set[str]]:
    failures: set[str] = set()
    errors: set[str] = set()
    for case in ElementTree.parse(path).iter("testcase"):
        error = case.find("error")
        failure = case.find("failure")
        if error is None and failure is None:
            continue
        file_name = case.attrib.get("file")
        class_name = case.attrib.get("classname", "")
        test_name = case.attrib.get("name", "unknown")
        if file_name:
            module = file_name.removesuffix(".py").replace("/", ".")
            test_class = class_name.removeprefix(module).lstrip(".")
            node_id = "::".join(
                part for part in (file_name, test_class, test_name) if part
            )
        else:
            node_id = f"{class_name.replace('.', '/')}.py::{test_name}"
        (errors if error is not None else failures).add(node_id)
    return failures, errors


def cleanup_container(
    container: str | None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if container is None:
        return True
    result = run(
        ["docker", "rm", "-f", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode:
        print(f"[RECOVERY] docker rm -f {container}", flush=True)
        return False
    print(f"[CLEANUP] removed container={container}", flush=True)
    return True


def safe_environment() -> dict[str, str]:
    # A denylist misses broker URLs, keyring paths, rollout flags and new secrets.
    # Inherit only process-launch essentials; task services are supplied explicitly.
    inherited_names = {"PATH", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "COMSPEC", "PATHEXT"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in inherited_names
    }
    environment.update(
        {
            "CI": "1",
            "PYTHONUNBUFFERED": "1",
            "LC_ALL": "C",
            "PIP_CONFIG_FILE": os.devnull,
            "PGPASSFILE": os.devnull,
            "PGSERVICEFILE": os.devnull,
            "NETRC": os.devnull,
            "VELLA_DATABASE_URL": (
                "postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable"
            ),
            "VELLA_REDIS_URL": "redis://127.0.0.1:1/0",
            "VELLA_CELERY_BROKER_URL": "redis://127.0.0.1:1/0",
            "VELLA_CELERY_RESULT_BACKEND": "redis://127.0.0.1:1/0",
            # Defense in depth only: clients can bypass proxy environment variables.
            # This is not a substitute for an OS/container network boundary.
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "VELLA_WB_API_MODE": "fake",
            "VELLA_AVITO_API_MODE": "fake",
            "VELLA_REAL_PRICE_APPLY_ENABLED": "false",
            "VELLA_WB_FEEDBACKS_SEND_ENABLED": "false",
            "VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED": "false",
        }
    )
    return environment


def executor(environment: dict[str, str]) -> Executor:
    def execute(command: tuple[str, ...], cwd: Path) -> int:
        return subprocess.run(command, cwd=cwd, env=environment).returncode

    return execute


def frontend_directory() -> Path:
    configured = os.getenv("SATORNA_FRONTEND_DIR")
    candidate = (
        Path(configured).expanduser()
        if configured
        else ROOT.parent / "frontend"
    )
    if (
        not (candidate / "package.json").is_file()
        and (candidate / "frontend" / "package.json").is_file()
    ):
        candidate /= "frontend"
    if not (candidate / "package.json").is_file():
        raise GateFailure("frontend_discovery")
    return candidate.resolve()


def run_frontend(
    python_environment: dict[str, str], temp: Path, completed: list[str]
) -> None:
    frontend = frontend_directory()
    generated = (
        frontend / "src/features/vella-parity/vellaProductionSnapshot.generated.ts"
    )
    existed = generated.exists()
    original = generated.read_bytes() if existed else b""
    mode = generated.stat().st_mode if existed else None
    try:
        run_steps(
            (
                ("frontend_tests", ("npm", "run", "test"), frontend),
                ("frontend_typecheck", ("npm", "run", "typecheck"), frontend),
                (
                    "frontend_production_build",
                    (
                        "npm",
                        "run",
                        "build",
                        "--",
                        "--outDir",
                        str(temp / "frontend-dist"),
                    ),
                    frontend,
                ),
            ),
            executor(python_environment),
            completed,
        )
    finally:
        if existed:
            generated.write_bytes(original)
            os.chmod(generated, mode or 0o644)
        else:
            generated.unlink(missing_ok=True)


def run_full_backend(
    python: str,
    environment: dict[str, str],
    junit: Path,
    completed: list[str],
    baseline: dict[str, object],
) -> None:
    name = "backend_full_pytest"
    print(f"[RUN] {name}", flush=True)
    result = subprocess.run(
        (
            python,
            "-m",
            "pytest",
            "-q",
            "--tb=no",
            "--no-summary",
            "-o",
            "junit_family=legacy",
            f"--junitxml={junit}",
            "tests",
        ),
        cwd=ROOT,
        env=environment,
    )
    try:
        actual, errors = junit_failure_ids(junit)
    except (OSError, ElementTree.ParseError):
        raise GateFailure(name, result.returncode or 1) from None
    expected = {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    comparison = compare_failure_sets(expected, actual, errors)
    baseline.update(comparison)
    print(
        "SATORNA_LEGACY_BASELINE="
        + json.dumps(comparison, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    expected_exit = 1 if expected else 0
    if comparison["status"] != "exact" or result.returncode != expected_exit:
        raise GateFailure(name, result.returncode or 1)
    completed.append(name)
    print(f"[PASS] {name} exact_legacy_failures={len(expected)}", flush=True)


def check_single_head(
    python: str, environment: dict[str, str], completed: list[str]
) -> str:
    name = "alembic_single_head"
    print(f"[RUN] {name}", flush=True)
    result = subprocess.run(
        (python, "-m", "alembic", "heads"),
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
    )
    heads = [line.split()[0] for line in result.stdout.splitlines() if "(head)" in line]
    if result.returncode or len(heads) != 1:
        raise GateFailure(name, result.returncode or 1)
    completed.append(name)
    print(f"[PASS] {name} revision={heads[0]}", flush=True)
    return heads[0]


def start_postgres(
    prefix: str,
    temp: Path,
    resources: dict[str, str],
    completed: list[str],
) -> tuple[str, str]:
    name = "task_postgres_start"
    print(f"[RUN] {name}", flush=True)
    if subprocess.run(
        ("docker", "info"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise GateFailure(name)

    container = f"{prefix}-postgres"
    database = prefix.replace("-", "_")
    user = "satorna_gate"
    password = secrets.token_urlsafe(24)
    env_file = temp / "postgres.env"
    env_file.write_text(
        f"POSTGRES_DB={database}\nPOSTGRES_USER={user}\nPOSTGRES_PASSWORD={password}\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o600)
    signals = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        created = subprocess.run(
            (
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                f"satorna.release-gate={prefix}",
                "--env-file",
                str(env_file),
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ),
            text=True,
            capture_output=True,
        )
        if created.returncode == 0:
            resources.update(container=container, database=database)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    if created.returncode:
        raise GateFailure(name, created.returncode)
    print(f"[RESOURCE] container={container} database={database}", flush=True)
    print(f"[RECOVERY] docker rm -f {container}", flush=True)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            ("docker", "exec", container, "pg_isready", "-U", user, "-d", database),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise GateFailure(name)

    port_result = subprocess.run(
        ("docker", "port", container, "5432/tcp"),
        text=True,
        capture_output=True,
    )
    try:
        port = port_result.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        int(port)
    except (IndexError, ValueError):
        raise GateFailure(name, port_result.returncode or 1) from None
    completed.append(name)
    print(f"[PASS] {name}", flush=True)
    url = f"postgresql+psycopg://{user}:{quote(password, safe='')}@127.0.0.1:{port}/{database}"
    return url, user


def run_migration_roundtrip(
    python: str, environment: dict[str, str], completed: list[str]
) -> None:
    """Exercise the real empty-database chain; never stamp past broken DDL."""
    run_steps(
        (
            ("migration_upgrade_head", (python, "-m", "alembic", "upgrade", "head"), ROOT),
            ("migration_downgrade_one", (python, "-m", "alembic", "downgrade", "-1"), ROOT),
            ("migration_reupgrade_head", (python, "-m", "alembic", "upgrade", "head"), ROOT),
        ),
        executor(environment),
        completed,
    )


def run_gate(
    temp: Path,
    resources: dict[str, str],
    completed: list[str],
    baseline: dict[str, object],
) -> None:
    python = sys.executable
    environment = safe_environment()
    environment["VELLA_DATABASE_URL"] = (
        "postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable"
    )
    environment["VELLA_REPRICER_STATE_FILE"] = str(temp / "runtime-state.json")
    execute = executor(environment)

    run_steps(
        (
            (
                "python_compile",
                (
                    python,
                    "-m",
                    "compileall",
                    "-q",
                    "app",
                    "backend_contracts",
                    "ops/release_gate.py",
                ),
                ROOT,
            ),
            (
                "whitespace_last_commit",
                ("git", "diff", "--check", "HEAD^", "HEAD", "--"),
                ROOT,
            ),
            ("whitespace_worktree", ("git", "diff", "--check"), ROOT),
            ("whitespace_index", ("git", "diff", "--cached", "--check"), ROOT),
            (
                "backend_focused_pytest",
                (python, "-m", "pytest", "-q", *FOCUSED_TESTS),
                ROOT,
            ),
        ),
        execute,
        completed,
    )
    run_full_backend(
        python, environment, temp / "backend-full.xml", completed, baseline
    )
    run_frontend(environment, temp, completed)
    check_single_head(python, environment, completed)

    prefix = f"satorna-gate-{int(time.time())}-{os.getpid()}-{secrets.token_hex(3)}"
    database_url, _database_user = start_postgres(prefix, temp, resources, completed)
    migration_environment = dict(environment, VELLA_DATABASE_URL=database_url)
    run_migration_roundtrip(python, migration_environment, completed)
    run_steps(
        (
            (
                "contract_openapi_pytest",
                (python, "-m", "pytest", "-q", *CONTRACT_TESTS),
                ROOT,
            ),
        ),
        execute,
        completed,
    )


def main() -> int:
    def interrupt(signum: int, _frame: object) -> None:
        raise GateInterrupted(signum)

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    resources: dict[str, str] = {}
    completed: list[str] = []
    baseline: dict[str, object] = {}
    failed_step: str | None = None
    exit_code = 0
    cleanup_ok = True

    with tempfile.TemporaryDirectory(prefix="satorna-release-gate-") as temp_name:
        try:
            run_gate(Path(temp_name), resources, completed, baseline)
        except GateInterrupted as error:
            failed_step = "interrupted"
            exit_code = error.exit_code
            print("[FAIL] interrupted", flush=True)
        except GateFailure as error:
            failed_step = error.step
            exit_code = error.exit_code or 1
            print(f"[FAIL] {error}", flush=True)
        except Exception as error:  # defensive boundary: still clean exact resources
            failed_step = "unexpected_error"
            exit_code = 1
            print(f"[FAIL] unexpected_error type={type(error).__name__}", flush=True)
        finally:
            cleanup_ok = cleanup_container(resources.get("container"))
            if not cleanup_ok and exit_code == 0:
                failed_step = "cleanup"
                exit_code = 1

    resource_summary = []
    if resources:
        resource_summary.append(
            {
                "container": resources["container"],
                "database": resources["database"],
                "state": "removed" if cleanup_ok else "recovery_required",
            }
        )
    summary = {
        "status": "pass" if exit_code == 0 else "fail",
        "exit_code": exit_code,
        "failed_step": failed_step,
        "completed": completed,
        "legacy_baseline": baseline.get("status"),
        "legacy_failures": baseline.get("actual_count"),
        "resources": resource_summary,
    }
    print(
        "SATORNA_GATE_SUMMARY="
        + json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
