import json
import os
import shutil
import subprocess

import pytest

from app.config import get_settings
from ops.release_gate import ROOT, safe_environment


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_compose_processes_agree_on_cash_flow_availability(monkeypatch, enabled):
    environment = safe_environment()
    if enabled is not None:
        environment["VELLA_1C_ENABLED"] = enabled
    result = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT / "docker-compose.yml"),
         "config", "--format", "json"],
        env=environment, capture_output=True, text=True, check=True, timeout=20,
    )
    services = json.loads(result.stdout)["services"]
    actual = {}
    for name in ("api", "worker", "canonical-shadow-worker", "beat"):
        with monkeypatch.context() as patch:
            patch.setattr(os, "environ", services[name]["environment"])
            actual[name] = get_settings().one_c_enabled
    assert actual == dict.fromkeys(actual, enabled == "true")
