import importlib.util
import json
import os
from pathlib import Path
import socket

import pytest

spec = importlib.util.spec_from_file_location("manager0919", Path(__file__).parents[1] / "deploy_0919" / "manage.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_bootstrap_refuses_existing_directory(tmp_path):
    sentinel = tmp_path / "retained"
    sentinel.write_text("unrelated")
    with pytest.raises(RuntimeError, match="absent"):
        m.bootstrap(tmp_path, Path("/missing"), Path("/missing"))
    assert sentinel.read_text() == "unrelated"


def test_occupied_port_is_not_reused():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="occupied"):
            m.free_port(listener.getsockname()[1])


def test_foreign_pid_is_never_signaled(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "api.json").write_text(json.dumps({"pid": 123, "identity": {"start_ticks": "old"}}))
    monkeypatch.setattr(m, "process_identity", lambda pid: {"start_ticks": "new"})
    monkeypatch.setattr(m.os, "kill", lambda *args: pytest.fail("Foreign process was signaled"))
    with pytest.raises(RuntimeError, match="mismatch"):
        m.stop_api(tmp_path)


def test_foreign_database_pid_is_not_owned(tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    pidfile.write_text("123\n")
    monkeypatch.setattr(m, "process_identity", lambda pid: {"uid": os.getuid(), "cmdline": "/other/postgres"})
    with pytest.raises(RuntimeError, match="owned instance"):
        m.verify_daemon(pidfile, tmp_path)


def test_private_credentials_reject_group_readable_file(tmp_path):
    (tmp_path / "config").mkdir()
    target = tmp_path / "config" / "credentials.json"
    target.write_text("{}")
    target.chmod(0o640)
    with pytest.raises(RuntimeError, match="private"):
        m.credentials(tmp_path)


def test_freeze_retries_staged_but_inactive_configuration(tmp_path, monkeypatch):
    conf = tmp_path / "neo4j" / "conf"
    conf.mkdir(parents=True)
    (conf / "neo4j.conf").write_text("dbms.databases.default_to_read_only=true\n")
    monkeypatch.setattr(m, "owned_root", lambda root: None)
    monkeypatch.setattr(m, "verify_daemon", lambda *args: None)
    states = iter(["read-write", "read-only"])
    monkeypatch.setattr(m, "graph_access", lambda root: next(states))
    calls = []
    monkeypatch.setattr(m, "run", lambda args, **kwargs: calls.append(args))
    m.freeze_graph(tmp_path)
    assert calls[0][-1] == "restart"


def test_api_refuses_writable_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owned_root", lambda root: None)
    monkeypatch.setattr(m, "graph_access", lambda root: "read-write")
    with pytest.raises(RuntimeError, match="read-only"):
        m.start_api(tmp_path, tmp_path / "releases" / "candidate", "snapshot")
