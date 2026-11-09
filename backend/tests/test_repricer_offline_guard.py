import subprocess
import sys


def test_offline_fence_blocks_dns_tcp_udp_and_runtime_writes_before_collection():
    script = """
import socket
import sqlite3
from types import SimpleNamespace
from tests.repricer_offline_plugin import pytest_sessionstart, pytest_sessionfinish
session = SimpleNamespace(config=SimpleNamespace(), exitstatus=0)
pytest_sessionstart(session)
operations = [
    lambda: socket.getaddrinfo('synthetic.invalid', 443),
    lambda: socket.socket().connect(('192.0.2.1', 443)),
    lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(b'synthetic', ('192.0.2.1', 9)),
    lambda: open('vella_repricer_runtime_state.json', 'w'),
    lambda: open('.env', 'r'),
    lambda: sqlite3.connect('synthetic-persistent.db'),
]
for operation in operations:
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise AssertionError('offline fence did not reject I/O')
with sqlite3.connect(':memory:') as db:
    assert db.execute('select 1').fetchone() == (1,)
pytest_sessionfinish(session, 0)
assert session.exitstatus == 1
assert len(session.config._repricer_offline_violations) == len(operations)
print('offline fences verified')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline fences verified"
