"""Linux-only deployment configuration for the dedicated Satorna VM.

Reuse the reviewed native launcher's flags. This module does not install, start
services, read keys or modify a database merely by being imported.
"""

import importlib.util
import re
from pathlib import Path

ROLES = {"api": "wb_live_api", "worker": "wb_live_worker", "beat": "wb_live_dispatch"}
STATE = Path("/var/lib/satorna-wb-live")


def service_unit(kind, release):
    """Render only owned, loopback-only service units; never start anything."""
    if type(kind) is not str or kind not in ROLES:
        raise ValueError("WB_LOCAL_ROLE_INVALID")
    if type(release) is not str or not re.fullmatch(r"/opt/satorna-releases/[a-f0-9]{7,40}/backend", release):
        raise ValueError("WB_LOCAL_RELEASE_INVALID")
    python = "/opt/satorna-migration-venv/bin/python"
    commands = {
        "api": f"{python} -m uvicorn app.main:app --host 127.0.0.1 --port 58000 --no-access-log",
        "worker": f"{python} -m celery -A app.infra.celery_app:celery_app worker --queues=vella.wb-live --concurrency=1 --pool=solo --without-gossip --without-mingle --without-heartbeat",
        "beat": f"{python} -m celery -A app.infra.celery_app:celery_app beat --schedule /var/lib/satorna-wb-beat/schedule --pidfile /var/lib/satorna-wb-beat/beat.pid",
    }
    return "\n".join([
        "[Unit]", f"Description=Satorna isolated local {kind}",
        "After=postgresql.service redis-server.service", "Requires=redis-server.service",
        "[Service]", "Type=simple", f"User={ROLES[kind]}", f"Group={ROLES[kind]}",
        f"WorkingDirectory={release}", f"EnvironmentFile=/etc/satorna-wb-live/{kind}.env",
        f"ExecStart={commands[kind]}", f"StateDirectory=satorna-wb-{kind}",
        "StateDirectoryMode=0700", "UMask=0077", "NoNewPrivileges=true",
        "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
        "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectControlGroups=true",
        "RestrictSUIDSGID=true", "CapabilityBoundingSet=", "LimitCORE=0",
        "Restart=on-failure", "RestartSec=5", "StandardOutput=null", "StandardError=null",
        "[Install]", "WantedBy=multi-user.target", "",
    ])


def service_environment(kind, auth_secret):
    if type(kind) is not str or kind not in ROLES:
        raise ValueError("WB_LOCAL_ROLE_INVALID")
    if type(auth_secret) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", auth_secret):
        raise ValueError("WB_LOCAL_AUTH_INVALID")
    path = Path(__file__).with_name("wb_live_local.py")
    spec = importlib.util.spec_from_file_location("satorna_native_environment", path)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    # No Redis password is used: Linux filesystem permissions restrict its
    # Unix socket to the three dedicated service users. Never enable Redis TCP.
    env = native.environment({"auth_secret": auth_secret, "redis_secret": ""}, kind=kind)
    env.update({
        "PATH": "/opt/satorna-migration-venv/bin:/usr/bin:/bin",
        "HOME": "/var/lib/satorna-wb-" + kind,
        "PYTHONDONTWRITEBYTECODE": "1",
        "VELLA_DATABASE_URL": f"postgresql+psycopg://{ROLES[kind]}@/satorna_wb_live",
        "VELLA_REDIS_URL": "unix:///run/redis/redis-server.sock?db=0",
        "VELLA_CELERY_BROKER_URL": "redis+socket:///run/redis/redis-server.sock?virtual_host=0",
        "VELLA_CELERY_RESULT_BACKEND": "redis+socket:///run/redis/redis-server.sock?virtual_host=0",
    })
    if kind != "beat":
        env["VELLA_MARKETPLACE_CREDENTIAL_KEYRING_DIR"] = str(STATE / "keyring")
    return env
