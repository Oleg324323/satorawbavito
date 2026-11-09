"""Run the local stack; real WB repricing requires explicit --wb-live."""
from pathlib import Path
import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
WORKER = "satorna-local-reports@localhost"


def redis_executable():
    candidates = [str(ROOT / ".local-preview/tools/redis-stable/src/redis-server"),
                  shutil.which("redis-server"), "/opt/homebrew/bin/redis-server"]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("A Redis binary compatible with this CPU is required; see README.md")


def require_free_ports(ports):
    for port in ports:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Port {port} is occupied; stop the existing local stack first") from None


def wait_ready(check, processes, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited; see .local-preview/logs/{name}.log")
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("Local service did not become ready; see .local-preview/logs")


def http_ready(url):
    # macOS can advertise a system proxy even for localhost; readiness is local.
    with build_opener(ProxyHandler({})).open(url, timeout=1) as response:
        return response.status == 200


def stop_children(processes):
    # Only process groups created by this launcher, never other Python/Node apps.
    for _, process in reversed(processes):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for _, process in reversed(processes):
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    from local_preview_storage import require_existing_preview_database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wb-live", action="store_true", help="Enable real WB repricing for explicitly assigned products")
    args = parser.parse_args()
    os.umask(0o077)
    require_existing_preview_database(ROOT / ".local-preview/preview.sqlite")
    require_free_ports((59999, 58017, 5177))
    redis_binary, node = redis_executable(), shutil.which("node")
    if not node:
        raise RuntimeError("Node.js must be installed")
    python = ROOT / ".venv/bin/python"
    vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
    if not python.exists() or not vite.exists():
        raise RuntimeError("Install the existing backend/frontend dependencies first")
    from redis import Redis
    from celery import Celery

    state = ROOT / ".local-preview"
    logs, redis_dir = state / "logs", state / "redis"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    redis_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    processes, handles = [], []
    env = os.environ.copy()
    # Explicit local preview configuration, not inherited production endpoints.
    env.update(VITE_API_BASE_URL="", VITE_API_PROXY_TARGET="http://127.0.0.1:58017",
               NODE_OPTIONS="--max-old-space-size=1024")

    def start(name, command, cwd):
        log = (logs / f"{name}.log").open("ab")
        handles.append(log)
        processes.append((name, subprocess.Popen(command, cwd=cwd, env=env,
                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)))

    def interrupted(*_args):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    redis = Redis(host="127.0.0.1", port=59999, socket_timeout=1, socket_connect_timeout=1)
    control = Celery("local-readiness", broker="redis://127.0.0.1:59999/0")
    try:
        start("redis", [redis_binary, "--bind", "127.0.0.1", "--port", "59999",
                        "--protected-mode", "yes", "--save", "", "--appendonly", "yes",
                        "--dir", str(redis_dir)], ROOT)
        wait_ready(redis.ping, processes)
        preview = [str(python), "ops/current_cost_local_preview.py", "--wb-readonly", "--avito-readonly"]
        start("report-worker", [*preview, "--report-worker"], ROOT / "backend")
        wait_ready(lambda: control.control.ping(destination=[WORKER], timeout=1), processes)
        if args.wb_live:
            preview = [str(python), "ops/current_cost_local_preview.py", "--wb-live", "--avito-readonly"]
            start("repricer-worker", [*preview, "--repricer-worker"], ROOT / "backend")
            wait_ready(lambda: control.control.ping(destination=["satorna-local-wb-repricer@localhost"], timeout=1), processes)
        start("backend", [*preview, "--report-queue"], ROOT / "backend")
        wait_ready(lambda: http_ready("http://127.0.0.1:58017/health"), processes)
        if args.wb_live:
            start("repricer-beat", [*preview, "--repricer-beat"], ROOT / "backend")
        start("frontend", [node, str(vite), "--host", "127.0.0.1", "--port", "5177", "--strictPort"], ROOT / "frontend")
        wait_ready(lambda: http_ready("http://127.0.0.1:5177/"), processes)
        mode = ", real WB repricing enabled" if args.wb_live else ", marketplaces read-only"
        print(f"Satorna ready: http://127.0.0.1:5177/ (API, Redis, report worker verified{mode})", flush=True)
        while True:
            wait_ready(redis.ping, processes, timeout=3)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_children(processes)
        redis.close()
        control.close()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
