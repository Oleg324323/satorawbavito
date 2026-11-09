"""Explicit opt-in network fence for Orders characterization runs."""

import sys


def pytest_sessionstart(session):
    # Install before collection: application imports can initialize clients.
    def deny_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise PermissionError("Orders offline tests forbid socket network access")

    sys.addaudithook(deny_network)
