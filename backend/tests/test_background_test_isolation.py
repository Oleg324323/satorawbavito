from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from ops.release_gate import safe_environment


BACKEND_ROOT = Path(__file__).resolve().parents[1]
LEGACY_TEST = (
    "tests/test_wb_repricer_bff.py::"
    "test_manual_cold_full_sync_enqueues_onboarding_task"
)
SAFE_MARKER = (
    "INNER_STATUS=0 COLLECTED=1 FAILED=0 ERRORS=0 SENTINEL_REACHED=0"
)


def test_legacy_onboarding_case_fences_actual_background_entrypoint() -> None:
    child_program = textwrap.dedent(
        f"""
        import io
        import pytest
        from contextlib import redirect_stderr, redirect_stdout

        from app import repricer_tasks

        sentinel_reached = False

        class RunSummary:
            collected = 0
            failed = 0
            errors = 0

            def pytest_collection_finish(self, session):
                self.collected = len(session.items)

            def pytest_runtest_logreport(self, report):
                if report.when == "call" and report.failed:
                    self.failed += 1
                elif report.failed:
                    self.errors += 1

        def safe_sentinel(*_args, **_kwargs):
            global sentinel_reached
            sentinel_reached = True

        repricer_tasks.run_wb_onboarding_for_org = safe_sentinel
        summary = RunSummary()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = pytest.main(
                ["-q", "--tb=no", "--disable-warnings", {LEGACY_TEST!r}],
                plugins=[summary],
            )
        print(
            f"INNER_STATUS={{int(status)}} COLLECTED={{summary.collected}} "
            f"FAILED={{summary.failed}} ERRORS={{summary.errors}} "
            f"SENTINEL_REACHED={{int(sentinel_reached)}}"
        )
        """
    )
    environment = safe_environment()
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    environment["PYTHONNOUSERSITE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", child_program],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{SAFE_MARKER}\n"
    assert completed.stderr == ""
