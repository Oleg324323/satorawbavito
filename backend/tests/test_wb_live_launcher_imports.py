"""Fresh service interpreters must import through the launcher's actual clean environment."""
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["app.main", "app.infra.celery_app"])
def test_clean_launcher_environment_imports_service_without_pytest_paths(module):
    backend = Path(__file__).resolve().parents[1]
    launcher = runpy.run_path(str(backend / "ops/wb_live_local.py"))
    environment = launcher["clean_env"]()
    # No test/conftest imports or inherited PYTHONPATH enter the child. Network
    # connects are forbidden as an extra assertion beyond the enclosing sandbox.
    script = "\n".join([
        "import importlib, socket, sys",
        "assert sys.version_info[:2] == (3, 11)",
        "def forbidden(*args, **kwargs): raise AssertionError('Unexpected service connection during import')",
        "socket.socket.connect = forbidden",
        "importlib.import_module(sys.argv[1])",
    ])
    result = subprocess.run([sys.executable, "-c", script, module], cwd=backend,
        env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
