import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "local_project_startup", Path(__file__).parents[1] / "ops/run_local_project.py"
)
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


def test_cpu_incompatible_redis_is_not_used(monkeypatch):
    monkeypatch.setattr(startup.Path, "is_file", lambda _: True)
    monkeypatch.setattr(startup.shutil, "which", lambda _: "/intel/redis-server")
    def version(command, **kwargs):
        if command[0] != "/opt/homebrew/bin/redis-server":
            raise OSError(86, "Bad CPU type in executable")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(startup.subprocess, "run", version)
    assert startup.redis_executable() == "/opt/homebrew/bin/redis-server"


def test_occupied_port_does_not_take_over_existing_service():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="occupied"):
            startup.require_free_ports((port,))
        assert listener.getsockname()[1] == port


def test_local_readiness_ignores_broken_system_proxy(monkeypatch):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    with HTTPServer(("127.0.0.1", 0), Health) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert startup.http_ready(f"http://127.0.0.1:{server.server_port}/health")
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_readiness_cannot_succeed_with_a_dead_dependency():
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    process.wait(timeout=5)
    with pytest.raises(RuntimeError, match="report-worker exited"):
        startup.wait_ready(lambda: True, [("report-worker", process)], timeout=1)


def test_failed_readiness_times_out_without_reporting_success():
    with pytest.raises(RuntimeError, match="did not become ready"):
        startup.wait_ready(lambda: False, [], timeout=0.01)


def test_shutdown_only_stops_owned_children():
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        startup.stop_children([("owned", owned)])
        assert owned.poll() is not None
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
