#!/usr/bin/env python3
"""Dedicated persistent local WB stack. No existing services or credentials used.

Usage: python3 backend/ops/wb_live_local.py start|stop|status
Optional VELLA_WB_LIVE_PYTHON selects an installed Python 3.11 with project deps.
State and newly generated keyring live outside Git, mode 0700/0600.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote

BACKEND = Path(__file__).resolve().parents[1]
STATE = Path.home() / "Library/Application Support/Satorna WB Live"
MARKER = "satorna-wb-live-owned-v1"
PORTS = {"postgres": 55432, "redis": 56379, "api": 58000}

def private_write(path, data):
    if path.is_symlink():
        raise RuntimeError("WB_LOCAL_UNSAFE_STATE")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)

def clean_env():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8", "PYTHONPATH": os.pathsep.join((str(BACKEND), str(BACKEND / "backend_contracts"))), "PGPASSFILE": "/dev/null",
        "PGSERVICEFILE": "/dev/null", "NETRC": "/dev/null"}

def run(args, *, env=None):
    result = subprocess.run(args, cwd=BACKEND, env=env or clean_env(), capture_output=True)
    if result.returncode:
        # Command output can contain driver settings. Never print it.
        raise RuntimeError("WB_LOCAL_COMMAND_FAILED")
    return result.stdout

def binary(name):
    found = shutil.which(name)
    for root in (Path("/opt/homebrew/opt/postgresql@16/bin"), Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        if found:
            break
        if (root / name).is_file():
            found = str(root / name)
    if not found:
        raise RuntimeError("WB_LOCAL_BINARY_MISSING:" + name)
    return found

def python_runtime():
    candidates = [os.environ.get("VELLA_WB_LIVE_PYTHON"), str(BACKEND / ".venv/bin/python"),
        shutil.which("python3.11"), "/tmp/satorna-backend311-20260909/bin/python"]
    probe = "import sys; assert sys.version_info[:2]==(3,11); import uvicorn,celery,psycopg,alembic,cryptography,sqlalchemy"
    for path in candidates:
        if path and Path(path).is_file():
            tested = subprocess.run([path, "-c", probe], env=clean_env(), capture_output=True)
            if tested.returncode == 0:
                return path
    raise RuntimeError("WB_LOCAL_PYTHON311_DEPS_MISSING: set VELLA_WB_LIVE_PYTHON")

def initialize():
    os.umask(0o077)
    if STATE.is_symlink():
        raise RuntimeError("WB_LOCAL_UNSAFE_STATE")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    path = STATE / "stack.json"
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("owner") != MARKER or path.is_symlink():
            raise RuntimeError("WB_LOCAL_FOREIGN_STATE")
        return data
    if any(STATE.iterdir()):
        # launcher.lock is made by main, and is the sole expected initial file.
        if {p.name for p in STATE.iterdir()} != {"launcher.lock"}:
            raise RuntimeError("WB_LOCAL_FOREIGN_STATE")
    for name in ("pg", "socket", "redis", "keyring", "logs"):
        (STATE / name).mkdir(mode=0o700)
    data = {"owner": MARKER, "auth_secret": secrets.token_urlsafe(48), "redis_secret": secrets.token_hex(32),
        "processes": {}}
    private_write(STATE / "keyring/1", secrets.token_bytes(32))
    private_write(path, json.dumps(data))
    return data

def environment(data, *, kind="api"):
    env = clean_env()
    role = "wb_live_dispatch" if kind == "beat" else ("wb_live_worker" if kind == "worker" else "wb_live_api")
    url = f"postgresql+psycopg://{role}@/satorna_wb_live?host={quote(str(STATE / 'socket'), safe='')}&port={PORTS['postgres']}"
    redis_url = f"redis://:{data['redis_secret']}@127.0.0.1:{PORTS['redis']}/0"
    env.update({"VELLA_DATABASE_URL": url, "VELLA_REDIS_URL": redis_url, "VELLA_CELERY_BROKER_URL": redis_url,
        "VELLA_CELERY_RESULT_BACKEND": redis_url, "VELLA_AUTH_SECRET": data["auth_secret"],
        "VELLA_ENVIRONMENT": "local", "VELLA_WB_API_MODE": "real", "VELLA_WB_LIVE_SYNC_ENABLED": "true",
        "VELLA_MARKETPLACE_CREDENTIALS_ENABLED": "true", "VELLA_AUTH_COOKIE_SECURE": "false", "VELLA_AUTH_COOKIE_SAMESITE": "lax",
        "VELLA_CORS_ALLOWED_ORIGINS": "http://127.0.0.1:5173,http://localhost:5173"})
    for name in ("REAL_PRICE_APPLY_ENABLED", "REPRICER_LOCAL_PRICE_APPLY_ENABLED", "REPRICER_SCHEDULER_ENABLED",
        "REPRICER_WB_SYNC_ENABLED", "AVITO_REPRICER_WORKER_ENABLED", "AVITO_REPRICER_PRICE_APPLY_ENABLED",
        "AVITO_RETURNS_SYNC_ENABLED", "WB_FEEDBACKS_SEND_ENABLED", "CANONICAL_SHADOW_COLLECTION_ENABLED",
        "FINANCE_SHADOW_INGEST_ENABLED", "ADVERTISING_SHADOW_INGEST_ENABLED", "REVIEW_SHADOW_ENABLED"):
        env["VELLA_" + name] = "false"
    if kind != "beat":
        env.update({"VELLA_MARKETPLACE_CREDENTIAL_KEYRING_DIR": str(STATE / "keyring"),
            "VELLA_MARKETPLACE_CREDENTIAL_CURRENT_KEY_VERSION": "1", "VELLA_MARKETPLACE_CREDENTIAL_KEY_VERSIONS": "1"})
    return env

def identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None

def alive(record):
    return record and identity(record["pid"]) == record["identity"]

def save(data):
    private_write(STATE / "stack.json", json.dumps(data))

def spawn(data, name, args, env):
    if alive(data["processes"].get(name)):
        return
    # Service logs can expose raw driver diagnostics. Suppress runtime output;
    # the HTTP layer and durable source error codes are the supported diagnostics.
    proc = subprocess.Popen(args, cwd=BACKEND, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(.5)
    if proc.poll() is not None:
        raise RuntimeError("WB_LOCAL_SERVICE_FAILED:" + name)
    data["processes"][name] = {"pid": proc.pid, "identity": identity(proc.pid), "backend": str(BACKEND)}
    save(data)

def provision(python, data):
    pg = STATE / "pg"
    if not (pg / "PG_VERSION").exists():
        run([binary("initdb"), "-D", str(pg), "-U", "wb_live_owner", "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"])
    probe = subprocess.run([binary("pg_ctl"), "-D", str(pg), "status"], capture_output=True)
    if probe.returncode != 0:
        run([binary("pg_ctl"), "-D", str(pg), "-l", str(STATE / "logs/postgres.log"), "-o",
            f"-p {PORTS['postgres']} -h '' -k '{STATE / 'socket'}'", "-w", "start"])
    postmaster = (pg / "postmaster.pid").read_text().splitlines()
    pid = int(postmaster[0])
    command = identity(pid)
    if len(postmaster) < 2 or Path(postmaster[1]) != pg or not command or f"postgres -D {pg}" not in command:
        raise RuntimeError("WB_LOCAL_POSTGRES_OWNERSHIP_FAILED")
    data["processes"]["postgres"] = {"pid": pid, "identity": command, "backend": str(BACKEND)}
    save(data)
    env = clean_env()
    env.update(PGHOST=str(STATE / "socket"), PGPORT=str(PORTS["postgres"]), PGUSER="wb_live_owner", PGDATABASE="postgres")
    names = run([binary("psql"), "-Atc", "SELECT datname FROM pg_database WHERE datname='satorna_wb_live'"], env=env)
    if not names.strip():
        run([binary("createdb"), "satorna_wb_live"], env=env)
    env = environment(data)
    env["VELLA_DATABASE_URL"] = env["VELLA_DATABASE_URL"].replace("wb_live_api@", "wb_live_owner@")
    run([python, "-m", "alembic", "upgrade", "head"], env=env)
    pg_env = clean_env()
    pg_env.update(PGHOST=str(STATE / "socket"), PGPORT=str(PORTS["postgres"]), PGUSER="wb_live_owner", PGDATABASE="satorna_wb_live")
    run([binary("psql"), "-v", "ON_ERROR_STOP=1", "-f", str(BACKEND / "ops/wb-live-local-grants.sql")], env=pg_env)

def start(data):
    python = python_runtime()
    for record in data["processes"].values():
        if alive(record) and record.get("backend") != str(BACKEND):
            raise RuntimeError("WB_LOCAL_OTHER_CHECKOUT_RUNNING")
    for name in ("redis", "api"):
        if not alive(data["processes"].get(name)):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", PORTS[name]))
                except OSError:
                    raise RuntimeError("WB_LOCAL_PORT_OCCUPIED:" + name) from None
    provision(python, data)
    private_write(STATE / "redis/redis.conf", "\n".join([
        "bind 127.0.0.1", f"port {PORTS['redis']}", "protected-mode yes", "appendonly yes", "appendfsync everysec",
        f'dir "{STATE / "redis"}"', f"requirepass {data['redis_secret']}", "loglevel warning"]) + "\n")
    spawn(data, "redis", [binary("redis-server"), str(STATE / "redis/redis.conf")], clean_env())
    spawn(data, "api", [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS["api"]), "--no-access-log"], environment(data))
    spawn(data, "worker", [python, "-m", "celery", "-A", "app.infra.celery_app:celery_app", "worker",
        "--queues=vella.wb-live", "--concurrency=1", "--pool=solo", "--without-gossip", "--without-mingle", "--without-heartbeat"], environment(data, kind="worker"))
    spawn(data, "beat", [python, "-m", "celery", "-A", "app.infra.celery_app:celery_app", "beat",
        "--schedule", str(STATE / "beat-schedule"), "--pidfile", str(STATE / "beat.pid")], environment(data, kind="beat"))
    print("WB local services started; API http://127.0.0.1:58000. WB token must be supplied through the UI.")

def stop(data):
    for name in ("beat", "worker", "api", "redis"):
        record = data["processes"].get(name)
        if alive(record):
            os.kill(record["pid"], signal.SIGTERM)
            for _ in range(100):
                if not alive(record):
                    break
                time.sleep(.1)
            if alive(record):
                raise RuntimeError("WB_LOCAL_STOP_PENDING:" + name)
        data["processes"].pop(name, None)
        save(data)
    if (STATE / "pg/postmaster.pid").exists():
        if not alive(data["processes"].get("postgres")):
            raise RuntimeError("WB_LOCAL_POSTGRES_OWNERSHIP_FAILED")
        run([binary("pg_ctl"), "-D", str(STATE / "pg"), "-m", "fast", "-w", "stop"])
    data["processes"].pop("postgres", None)
    save(data)
    print("Owned services stopped; PostgreSQL, Redis and keyring data retained.")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args()
    if args.command != "start" and not (STATE / "stack.json").exists():
        print("WB local stack is not initialized.")
        return
    if STATE.is_symlink():
        raise RuntimeError("WB_LOCAL_UNSAFE_STATE")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "launcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = initialize()
        if args.command == "start":
            start(data)
        elif args.command == "stop":
            stop(data)
        else:
            print(json.dumps({name: bool(alive(data["processes"].get(name))) for name in ("postgres", "api", "worker", "beat", "redis")}))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("WB_LOCAL_") else "WB_LOCAL_UNAVAILABLE"
        print(safe, file=sys.stderr)
        sys.exit(1)
