"""Exercise the real port guard without importing the CLI or launching uvicorn."""

import errno
from pathlib import Path
import shlex
import socket
import subprocess
import sys

import pytest


MANAGE = Path(__file__).resolve().parents[1] / "deploy_vnext" / "manage.py"


def attempt_start(tmp_path, port):
    app_dir = tmp_path / "isolated app"
    executable = app_dir / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!/bin/sh\nprintf started > "$PANK_TEST_START_MARKER"\nexit 23\n'
    )
    executable.chmod(0o700)
    marker = tmp_path / "startup-marker"
    state = tmp_path / "private-state"
    config = tmp_path / "runtime.env"
    config.write_text(
        f"PANK_VNEXT_STATE_DIR={shlex.quote(str(state))}\n"
        f"PANK_VNEXT_PORT={port}\n"
        f"PANK_TEST_START_MARKER={shlex.quote(str(marker))}\n"
    )
    config.chmod(0o600)
    result = subprocess.run(
        [sys.executable, str(MANAGE), "start", "--app-dir", str(app_dir),
         "--env-file", str(config)],
        capture_output=True, text=True, timeout=5,
    )
    # The stub exits immediately and therefore never leaves a service or PID behind.
    assert not (state / "service.pid").exists()
    return result, marker


def listener():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(2)
    try:
        server.bind(("127.0.0.1", 0))
    except PermissionError:
        server.close()
        pytest.skip("Loopback socket tests require local network access outside this sandbox")
    server.listen(1)
    return server


def test_port_guard_rejects_active_listener_without_starting_or_disturbing_it(tmp_path):
    with listener() as server:
        address = server.getsockname()
        result, marker = attempt_start(tmp_path, address[1])
        assert result.returncode != 0
        assert "Port already occupied" in result.stderr
        assert not marker.exists()
        # The existing listener remains alive and accepts a new connection.
        with socket.create_connection(address, timeout=2) as client:
            accepted, _ = server.accept()
            with accepted:
                client.sendall(b"ok")
                assert accepted.recv(2) == b"ok"


def test_port_guard_allows_restart_with_recent_connection_in_time_wait(tmp_path):
    with listener() as server:
        address = server.getsockname()
        with socket.create_connection(address, timeout=2) as client:
            accepted, _ = server.accept()
            # Close the server side first so its local port enters TIME_WAIT.
            accepted.close()
            assert client.recv(1) == b""

    # Demonstrate the condition that rejected restarts before SO_REUSEADDR.
    with socket.socket() as ordinary_probe:
        try:
            ordinary_probe.bind(address)
        except OSError as error:
            assert error.errno == errno.EADDRINUSE
        else:
            pytest.skip("This OS did not retain the closed server connection in TIME_WAIT")

    result, marker = attempt_start(tmp_path, address[1])
    assert result.returncode != 0  # Expected: the stub exits rather than serving.
    assert "New service failed at startup" in result.stderr
    assert "Port already occupied" not in result.stderr
    assert marker.read_text() == "started"
