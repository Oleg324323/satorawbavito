import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import ops.release_gate as release_gate

from ops.release_gate import (
    GateFailure,
    cleanup_container,
    compare_failure_sets,
    junit_failure_ids,
    run_steps,
)


def test_release_gate_happy_path_runs_every_step() -> None:
    completed: list[str] = []
    seen: list[str] = []
    steps = tuple((name, (name,), Path(".")) for name in ("compile", "test", "build"))

    run_steps(steps, lambda command, _cwd: seen.append(command[0]) or 0, completed)

    assert seen == completed == ["compile", "test", "build"]


def test_release_gate_stops_at_first_failure() -> None:
    completed: list[str] = []
    seen: list[str] = []
    steps = tuple((name, (name,), Path(".")) for name in ("compile", "test", "build"))

    with pytest.raises(GateFailure, match="test failed"):
        run_steps(
            steps,
            lambda command, _cwd: (
                seen.append(command[0]) or (1 if command[0] == "test" else 0)
            ),
            completed,
        )

    assert seen == ["compile", "test"]
    assert completed == ["compile"]


def test_release_gate_reports_changed_baseline() -> None:
    comparison = compare_failure_sets({"test_a", "test_b"}, {"test_a", "test_c"}, set())

    assert comparison["status"] == "changed"
    assert comparison["added_ids"] == ["test_c"]
    assert comparison["missing_ids"] == ["test_b"]


def test_release_gate_reads_failure_and_error_ids(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite><testcase classname="tests.test_one" name="test_failure" '
        'file="tests/test_one.py"><failure /></testcase>'
        '<testcase classname="tests.test_two.TestCase" name="test_error" '
        'file="tests/test_two.py"><error /></testcase></testsuite>',
        encoding="utf-8",
    )

    assert junit_failure_ids(junit) == (
        {"tests/test_one.py::test_failure"},
        {"tests/test_two.py::TestCase::test_error"},
    )


def test_release_gate_cleanup_targets_only_created_container() -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        return CompletedProcess(command, 0)

    assert cleanup_container(None, fake_run)
    assert cleanup_container("satorna-gate-123-postgres", fake_run)
    assert calls == [["docker", "rm", "-f", "satorna-gate-123-postgres"]]


def test_release_gate_does_not_inherit_runtime_connections_or_flags(monkeypatch):
    # Synthetic values only; this test must never open these connections.
    inherited = {
        "VELLA_REDIS_URL": "redis://synthetic.invalid/0",
        "VELLA_CELERY_BROKER_URL": "redis://synthetic.invalid/1",
        "VELLA_CELERY_RESULT_BACKEND": "redis://synthetic.invalid/2",
        "VELLA_DATABASE_URL": "postgresql://synthetic.invalid/example",
        "VELLA_MARKETPLACE_CREDENTIALS_ENABLED": "true",
        "VELLA_MARKETPLACE_CREDENTIALS_KEYRING_DIR": "/synthetic/keyring",
        "VELLA_CANONICAL_SHADOW_ENABLED": "true",
        "VITE_CANONICAL_WB_ABC_PNL_ROLLOUT": "synthetic-canary",
        "UNRECOGNIZED_PRIVATE_SETTING": "synthetic-canary",
        "PYTHONPATH": "/synthetic/injected",
        "PYTEST_ADDOPTS": "--synthetic-injected-option",
        "NODE_OPTIONS": "--require=/synthetic/injected",
        "HTTPS_PROXY": "http://synthetic.invalid:80",
        "PIP_CONFIG_FILE": "/synthetic/pip.conf",
        "PGPASSFILE": "/synthetic/pgpass",
        "PGSERVICEFILE": "/synthetic/pgservice",
        "NETRC": "/synthetic/netrc",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)

    environment = release_gate.safe_environment()

    for key, value in inherited.items():
        assert environment.get(key) != value, key
    assert environment["VELLA_WB_API_MODE"] == "fake"
    assert environment["VELLA_AVITO_API_MODE"] == "fake"
    assert environment["VELLA_REAL_PRICE_APPLY_ENABLED"] == "false"


def test_release_gate_preserves_only_required_local_process_environment(monkeypatch):
    monkeypatch.setenv("PATH", "/synthetic/bin")
    monkeypatch.setenv("TMPDIR", "/synthetic/temp")

    environment = release_gate.safe_environment()

    assert environment["PATH"] == "/synthetic/bin"
    assert environment["TMPDIR"] == "/synthetic/temp"
    assert environment["LC_ALL"] == "C"


def test_release_gate_defaults_do_not_target_working_local_services():
    environment = release_gate.safe_environment()

    assert environment["VELLA_REDIS_URL"] == "redis://127.0.0.1:1/0"
    assert environment["VELLA_CELERY_BROKER_URL"] == "redis://127.0.0.1:1/0"
    assert environment["VELLA_CELERY_RESULT_BACKEND"] == "redis://127.0.0.1:1/0"
    assert "@127.0.0.1:1/" in environment["VELLA_DATABASE_URL"]
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:1"
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:1"
    assert environment["ALL_PROXY"] == "http://127.0.0.1:1"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost,::1"
    for name in ("PIP_CONFIG_FILE", "PGPASSFILE", "PGSERVICEFILE", "NETRC"):
        assert environment[name] == os.devnull


def test_release_gate_finds_frontend_in_its_own_worktree(monkeypatch, tmp_path):
    backend = tmp_path / "worktree" / "backend"
    frontend = tmp_path / "worktree" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(release_gate, "ROOT", backend)
    monkeypatch.delenv("SATORNA_FRONTEND_DIR", raising=False)

    assert release_gate.frontend_directory() == frontend.resolve()
