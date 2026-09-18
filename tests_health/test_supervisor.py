"""Monitor restart guards; never start or signal a real service in these tests."""
import errno
import json
import signal
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pankgraph_health import supervisor


def listener():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(2)
    try:
        server.bind(('127.0.0.1', 0))
    except BaseException:
        server.close()
        raise
    server.listen(1)
    return server


def test_port_guard_refuses_an_active_listener_without_disturbing_it():
    with listener() as server:
        address = server.getsockname()
        with pytest.raises(OSError) as failed:
            supervisor.check_port(address[1])
        assert failed.value.errno == errno.EADDRINUSE
        with socket.create_connection(address, timeout=2) as client:
            accepted, _ = server.accept()
            with accepted:
                client.sendall(b'alive')
                assert accepted.recv(5) == b'alive'


def test_port_guard_accepts_restart_after_server_side_connection_close():
    with listener() as server:
        address = server.getsockname()
        with socket.create_connection(address, timeout=2) as client:
            accepted, _ = server.accept()
            # The server initiates FIN, putting its local endpoint into TIME_WAIT
            # on Linux. This reproduces stopping uvicorn before restarting it.
            accepted.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b''
            client.shutdown(socket.SHUT_WR)
            assert accepted.recv(1) == b''
            accepted.close()
    # Some OSes recycle the endpoint immediately, but a new listener must remain
    # possible on all supported systems. The option-order test below guarantees
    # the regression stays detectable even when this OS does not retain it.
    supervisor.check_port(address[1])
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(address)
        replacement.listen(1)


def test_port_guard_enables_reuse_before_loopback_bind(monkeypatch):
    events = []
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.setsockopt.side_effect = lambda *args: events.append(('option', *args))
    sock.bind.side_effect = lambda address: events.append(('bind', address))
    monkeypatch.setattr(supervisor.socket, 'socket', lambda: sock)
    supervisor.check_port(8796)
    assert events == [('option', socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
                      ('bind', ('127.0.0.1', 8796))]


def invoke_stop(tmp_path, monkeypatch, ownership):
    record = {'pid': 2468, 'uid': 1234, 'start': '123456', 'cwd': '/fixture/backend'}
    path = tmp_path / 'supervisor.pid.json'
    path.write_text(json.dumps(record))
    monkeypatch.setattr(supervisor.Settings, 'load', lambda: SimpleNamespace(state_dir=tmp_path, port=8796))
    monkeypatch.setattr(supervisor.os, 'umask', lambda _: None)
    monkeypatch.setattr(supervisor, 'owned', Mock(side_effect=ownership))
    monkeypatch.setattr(supervisor.time, 'sleep', lambda _: None)
    kill, popen = Mock(), Mock()
    monkeypatch.setattr(supervisor.os, 'kill', kill)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', popen)
    monkeypatch.setattr('sys.argv', ['health-supervisor', 'stop'])
    return record, path, kill, popen


def test_stop_refuses_stale_pid_ownership_without_signaling(tmp_path, monkeypatch):
    record, path, kill, popen = invoke_stop(tmp_path, monkeypatch, [False])
    with pytest.raises(ValueError, match='ownership mismatch; no process signaled'):
        supervisor.main()
    kill.assert_not_called()
    popen.assert_not_called()
    assert json.loads(path.read_text()) == record


def test_stop_signals_only_verified_supervisor_and_removes_record_after_exit(tmp_path, monkeypatch):
    _, path, kill, popen = invoke_stop(tmp_path, monkeypatch, [True, False])
    supervisor.main()
    kill.assert_called_once_with(2468, signal.SIGTERM)
    popen.assert_not_called()
    assert not path.exists()
