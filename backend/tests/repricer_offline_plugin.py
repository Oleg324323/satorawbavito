"""Opt-in: pytest -p tests.repricer_offline_plugin ...

Uses the same pre-collection Python audit-hook pattern as T3's Orders fence.
This is a Python test fence, not an OS sandbox or a production runtime setting.
"""

import os
import sys


def pytest_sessionstart(session):
    violations = []
    if session is not None:
        session.config._repricer_offline_violations = violations

    def deny_io(event, args):
        blocked = event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}
        if event == "sqlite3.connect":
            blocked = args[0] != ":memory:"
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            name = os.path.basename(os.fsdecode(args[0]))
            blocked = (name == ".env" or name.startswith(".env.")
                       or name.startswith("vella_repricer_runtime_state.json"))
        if blocked:
            # Event name only: never retain paths, addresses, or request payloads.
            violations.append(event)
            raise PermissionError("Repricer offline tests forbid external I/O")

    sys.addaudithook(deny_io)


def pytest_sessionfinish(session, exitstatus):
    # A caught exception must not let attempted network/storage I/O pass silently.
    if getattr(session.config, "_repricer_offline_violations", []):
        session.exitstatus = 1
