import subprocess
import sys


def test_offline_plugin_blocks_dns_tcp_and_udp_before_io():
    script = """
import socket
from tests.orders_offline_plugin import pytest_sessionstart
pytest_sessionstart(None)
operations = [
    lambda: socket.getaddrinfo('synthetic.invalid', 443),
    lambda: socket.socket().connect(('192.0.2.1', 443)),
    lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(b'synthetic', ('192.0.2.1', 9)),
]
for operation in operations:
    try:
        operation()
    except PermissionError as error:
        assert str(error) == 'Orders offline tests forbid socket network access'
    else:
        raise AssertionError('Network fence did not reject operation')
print('blocked DNS, TCP, UDP')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked DNS, TCP, UDP"
