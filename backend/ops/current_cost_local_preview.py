"""Local UI/API preview; marketplace writes require explicit --wb-live.

Run from backend with the project's Python. Uses the existing development
schema bootstrap, not production migrations. Register a local account in UI.
Default denies all outbound traffic. --avito-readonly permits Avito OAuth and
explicit reads using credentials saved by the user, retaining preview.sqlite.
Use run_local_project.py for the complete stack including the report queue.
"""
from pathlib import Path
import os
import secrets
import socket
import sys
from local_preview_storage import configure_preview_sqlite_storage, require_existing_preview_database

wb_live = "--wb-live" in sys.argv
live_scope = None
if wb_live and any(flag in sys.argv for flag in ("--wb-readonly", "--wb-readonly-data", "--report-worker")):
    raise RuntimeError("WB live mode cannot run a read-only worker or data import")
if any(flag in sys.argv for flag in ("--repricer-worker", "--repricer-beat")) and not wb_live:
    raise RuntimeError("The WB repricer worker and scheduler require --wb-live")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
STATE = ROOT / ".local-preview"
STATE.mkdir(mode=0o700, exist_ok=True)
database_name = "wb-readonly.sqlite" if "--wb-readonly-data" in sys.argv else "preview.sqlite"
if database_name == "preview.sqlite":
    require_existing_preview_database(STATE / database_name, initialize="--init-local-database" in sys.argv)
for name in list(os.environ):
    if name.startswith(("VELLA_", "WB_", "AVITO_", "CELERY_")):
        del os.environ[name]
os.environ.update(
    VELLA_ENV="local", VELLA_DATABASE_URL=f"sqlite+pysqlite:///{STATE / database_name}",
    VELLA_AUTH_SECRET=secrets.token_urlsafe(48), VELLA_AUTH_COOKIE_SECURE="false",
    VELLA_CABINET_REQUIRE_PERSISTENT_STORAGE="true",
    VELLA_AUTH_COOKIE_SAMESITE="lax", VELLA_REPRICER_SCHEDULER_ENABLED=str(wb_live).lower(),
    VELLA_WB_API_MODE="real",
    VELLA_AVITO_REPRICER_WORKER_ENABLED="false", VELLA_REAL_PRICE_APPLY_ENABLED=str(wb_live).lower(),
    VELLA_REPRICER_LOCAL_PRICE_APPLY_ENABLED="false",
    VELLA_REDIS_URL="redis://127.0.0.1:59999/0",
    VELLA_CELERY_BROKER_URL="redis://127.0.0.1:59999/0",
    VELLA_CELERY_RESULT_BACKEND="redis://127.0.0.1:59999/1",
)
# Listening and accepting localhost HTTP do not use connect(). Provider requests
# remain allowlisted at the final HTTP transport boundary, including live mode.
def deny_connect(*args, **kwargs):
    raise OSError("External actions disabled in isolated local preview")
if "--wb-readonly" in sys.argv or wb_live:
    from load_local_wb_readonly import install_readonly_network_guard, allowed_request as wb_read_allowed
    from local_avito_readonly import allowed_request as avito_read_allowed
    if wb_live:
        from local_wb_live import allowed_request as wb_live_allowed
        wb_read_allowed = lambda request: wb_live_allowed(request, scope=live_scope)
    install_readonly_network_guard(socket.socket.connect, socket.socket.connect_ex,
        request_allowed=lambda request: wb_read_allowed(request) or ("--avito-readonly" in sys.argv and avito_read_allowed(request)),
        local_broker_port=59999 if any(flag in sys.argv for flag in (
            "--report-queue", "--report-worker", "--repricer-worker", "--repricer-beat")) else None)
elif "--avito-readonly" in sys.argv:
    from load_local_wb_readonly import install_readonly_network_guard
    from local_avito_readonly import allowed_request
    install_readonly_network_guard(socket.socket.connect, socket.socket.connect_ex,
                                   request_allowed=allowed_request)
elif "--wb-readonly-data" in sys.argv:
    from load_local_wb_readonly import install_readonly_network_guard
    install_readonly_network_guard(socket.socket.connect, socket.socket.connect_ex)
else:
    socket.socket.connect = deny_connect
    socket.socket.connect_ex = deny_connect

from app.main import create_app
from app.infra.db import create_all_for_local_dev, get_engine
from app.infra.models import Base
from datetime import timezone
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator
import uvicorn

class PreviewUtcDateTime(TypeDecorator):
    """SQLite drops timezone metadata; PostgreSQL production does not."""
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value

for table in Base.metadata.tables.values():
    for column in table.columns:
        if isinstance(column.type, DateTime) and column.type.timezone:
            column.type = PreviewUtcDateTime(timezone=True)

configure_preview_sqlite_storage(STATE / database_name, get_engine())
app = create_app()
create_all_for_local_dev()
from local_wb_browser_prices import install_local_browser_price_storage
install_local_browser_price_storage()
if wb_live:
    from local_wb_live import resolve_live_scope
    live_scope = resolve_live_scope()
if any(flag in sys.argv for flag in ("--report-queue", "--report-worker", "--repricer-worker", "--repricer-beat")):
    if "--wb-readonly" not in sys.argv and not wb_live:
        raise RuntimeError("Local queues require a WB network guard")
    from app.infra.celery_app import celery_app
    from local_wb_live import configure_local_celery, REPRICER_QUEUE
    configure_local_celery(celery_app, live_scope=live_scope)
if __name__ == "__main__":
    if "--report-worker" in sys.argv:
        celery_app.worker_main(["worker", "--hostname=satorna-local-reports@localhost", "--pool=solo", "--concurrency=1", "--queues=satorna.local.readonly-reports", "--without-gossip", "--without-mingle", "--without-heartbeat", "--loglevel=WARNING"])
    elif "--repricer-worker" in sys.argv:
        celery_app.worker_main(["worker", "--hostname=satorna-local-wb-repricer@localhost", "--pool=solo", "--concurrency=1", f"--queues={REPRICER_QUEUE}", "--without-gossip", "--without-mingle", "--without-heartbeat", "--loglevel=WARNING"])
    elif "--repricer-beat" in sys.argv:
        celery_app.start(["beat", "--schedule", str(STATE / "repricer-beat-schedule"), "--pidfile", str(STATE / "repricer-beat.pid"), "--loglevel=WARNING"])
    else:
        uvicorn.run(app, host="127.0.0.1", port=58017, log_level="warning")
