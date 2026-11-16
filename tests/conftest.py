"""Shared pytest fixtures.

Starts the mock SOAP service in a background thread once per test session,
so integration tests make real HTTP calls without a separate terminal.
"""

import socket
import threading
import time

import pytest
import requests
import uvicorn

from mock_server.soap_server import app


def _free_port() -> int:
    """Ask the OS for an unused port (binding to port 0 lets the OS choose)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def endpoint():
    """Run the mock SOAP service for the whole session and yield its /soap URL."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    # daemon=True means the thread can't keep pytest alive if shutdown fails.
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Poll the health endpoint for up to ~5 seconds until the server answers.
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if requests.get(f"{base}/health", timeout=0.5).ok:
                break
        except requests.ConnectionError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Mock server did not start")

    yield f"{base}/soap"

    # Teardown: ask uvicorn to stop and wait for the thread to finish.
    server.should_exit = True
    thread.join(timeout=5)
