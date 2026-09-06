"""Offline deployment guard tests; never launch, signal or query a service."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("results_manager", Path(__file__).with_name("manage.py"))
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


def test_literal_protected_environment_never_executes_or_expands(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text("PANK_RESULTS_PASSWORD_HASH='pbkdf2_sha256$250000$test$synthetic'\n"
                    "PANK_RESULTS_BASIC_USER='$(touch /must-not-execute)'\n")
    path.chmod(0o600)
    values = manager.read_protected_env(path, "PANK_RESULTS_")
    assert values["PANK_RESULTS_PASSWORD_HASH"].endswith("$test$synthetic")
    assert values["PANK_RESULTS_BASIC_USER"] == "$(touch /must-not-execute)"
    path.write_text("PANK_VNEXT_STATE_DIR=/another-ledger\n")
    with pytest.raises(ValueError, match="Unexpected variable"):
        manager.read_protected_env(path, "PANK_RESULTS_")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        manager.read_protected_env(path)


def test_launch_command_cannot_target_legacy_agent_or_other_port(tmp_path):
    command = manager.command(tmp_path)
    assert command[command.index("--port") + 1] == "8795"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert "pankgraph_results.app:create_app" in command
    assert "pankagent_vnext.app:create_app" not in command
    assert "--no-proxy-headers" in command and "--no-access-log" in command


def test_foreign_or_reused_pid_is_never_owned(tmp_path, monkeypatch):
    base = {"pid": 321, "uid": os.geteuid(), "start": "123", "entry": manager.APP_ENTRY, "port": manager.PORT}
    assert not manager.owned({**base, "entry": "pankagent_vnext.app:create_app"}, tmp_path)
    assert not manager.owned({**base, "port": 8794}, tmp_path)
    monkeypatch.setattr(manager, "process_start", lambda pid: "999")
    assert not manager.owned(base, tmp_path)


def test_port_guard_refuses_occupied_listener_without_process_actions(monkeypatch):
    class Occupied:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def bind(self, address):
            assert address == ("127.0.0.1", 8795)
            raise OSError("already in use")
    monkeypatch.setattr(manager.socket, "socket", lambda: Occupied())
    with pytest.raises(ValueError, match="no existing process changed"):
        manager.check_port()


def test_seed_manifest_is_small_exact_key_allowlist():
    from pankgraph_results.resource_registry import source_for
    manifest = json.loads(Path(__file__).with_name("seed-manifest-v1.json").read_text())
    assert len(manifest) == 4
    for item in manifest:
        source = source_for(item["source"])
        assert source is not None and source.verified and source.kind == "QTL"
        assert source.object_key(item["credible_set"]).endswith(".txt")


def test_nginx_route_preserves_prefix_and_operator_boundary():
    text = Path(__file__).with_name("nginx-prefix.conf.example").read_text()
    assert "location ^~ /pankgraph-vnext/" in text
    assert "proxy_pass http://127.0.0.1:8795;" in text
    assert "proxy_pass http://127.0.0.1:8795/;" not in text
    assert "X-Forwarded-For $proxy_add_x_forwarded_for" in text
    assert "proxy_buffering off;" in text
    assert "auth_basic_user_file" not in text
