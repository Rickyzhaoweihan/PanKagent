#!/usr/bin/env python3
"""Manage only the serviceuser-owned, isolated results process on port 8795."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import time

APP_ENTRY = "pankgraph_results.app:create_app"
PORT = 8795
DEFAULT_APP = Path("/var/local/serviceuser/projects/pankgraph-results/current")
DEFAULT_VNEXT_ENV = Path("/var/local/serviceuser/.config/pankagent-vnext/runtime.env")
DEFAULT_RESULTS_ENV = Path("/var/local/serviceuser/.config/pankgraph-results/runtime.env")


def read_protected_env(path: Path, prefix: str | None = None) -> dict[str, str]:
    """Read literal dotenv assignments without shell execution or expansion."""
    if path.is_symlink():
        raise ValueError("Protected environment file must not be a symlink")
    stat = path.stat()
    if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise ValueError("Protected environment file must be owned by serviceuser and mode 0600")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError("Invalid protected environment assignment")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or (prefix and not key.startswith(prefix)):
            raise ValueError("Unexpected variable in results environment file")
        parts = shlex.split(raw, comments=True)
        if len(parts) > 1:
            raise ValueError("Environment values containing spaces must be quoted")
        values[key] = parts[0] if parts else ""
    return values


def command(app_dir: Path, python: Path | None = None) -> list[str]:
    return [str(python or app_dir / ".venv/bin/python"), "-m", "uvicorn", APP_ENTRY,
            "--factory", "--host", "127.0.0.1", "--port", str(PORT), "--workers", "1",
            "--no-access-log", "--timeout-graceful-shutdown", "2", "--no-proxy-headers"]


def process_start(pid: int) -> str:
    text = Path("/proc", str(pid), "stat").read_text()
    return text.rsplit(") ", 1)[1].split()[19]


def owned(record: dict | None, app_dir: Path) -> bool:
    try:
        if not record or record.get("port") != PORT or record.get("entry") != APP_ENTRY:
            return False
        pid = int(record["pid"])
        if pid < 2 or record["uid"] != os.geteuid() or process_start(pid) != record["start"]:
            return False
        proc = Path("/proc", str(pid))
        if proc.stat().st_uid != os.geteuid() or (proc / "cwd").resolve() != app_dir.resolve():
            return False
        args = (proc / "cmdline").read_bytes().decode().split("\0")
        return (APP_ENTRY in args and "uvicorn" in args and
                args[args.index("--port") + 1] == str(PORT) and
                args[args.index("--host") + 1] == "127.0.0.1")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def check_port():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", PORT))
        except OSError as exc:
            raise ValueError("Port 8795 is occupied; no existing process changed") from exc


def write_pid(path: Path, record: dict):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pid-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "foreground"])
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_APP)
    parser.add_argument("--vnext-env-file", type=Path, default=DEFAULT_VNEXT_ENV)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_RESULTS_ENV)
    parser.add_argument("--python", type=Path)
    args = parser.parse_args(argv)
    if pwd.getpwuid(os.geteuid()).pw_name != "serviceuser":
        raise ValueError("Run this tool as serviceuser (sudo -n -u serviceuser), never as root")
    os.umask(0o077)
    shared = read_protected_env(args.vnext_env_file)
    results = read_protected_env(args.env_file, "PANK_RESULTS_")
    env = {**os.environ, **shared, **results}
    if int(env.get("PANK_RESULTS_PORT", PORT)) != PORT:
        raise ValueError("This deployment is reserved for isolated results port 8795")
    if env.get("PANK_RESULTS_PUBLIC_PATH", "/pankgraph-vnext").rstrip("/") != "/pankgraph-vnext":
        raise ValueError("This deployment is reserved for the /pankgraph-vnext prefix")
    state = Path(results.get("PANK_RESULTS_STATE_DIR", "/var/local/serviceuser/.local/state/pankgraph-results"))
    if not state.is_absolute() or state.resolve() == Path(shared.get("PANK_VNEXT_STATE_DIR", ".")).resolve():
        raise ValueError("Results state must be an absolute directory separate from vNext state")
    if not shared.get("PANK_VNEXT_STATE_DIR"):
        raise ValueError("Existing protected vNext state is required for the shared budget ledger")
    env["PANK_RESULTS_STATE_DIR"] = str(state)
    env["PANK_RESULTS_PORT"] = str(PORT)
    if not env.get("PANK_RESULTS_PASSWORD_HASH"):
        raise ValueError("Configure the protected application password hash before starting")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise ValueError("Results state must be serviceuser-owned and private (0700)")
    pidfile = state / "results.pid.json"
    with (state / "manage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            record = json.loads(pidfile.read_text()) if pidfile.exists() else None
        except (OSError, ValueError):
            raise ValueError("Invalid results PID record; no process changed")
        running = owned(record, args.app_dir)
        if args.action == "status":
            print(json.dumps({"running": running, "pid": record.get("pid") if record else None, "port": PORT}))
            return
        if args.action == "stop":
            if record and not running:
                raise ValueError("PID ownership does not match this results service; refusing to signal it")
            if running:
                os.kill(record["pid"], signal.SIGTERM)
                for _ in range(80):
                    if not owned(record, args.app_dir):
                        break
                    time.sleep(.1)
                else:
                    raise ValueError("Results service has not exited; no forced kill attempted")
            pidfile.unlink(missing_ok=True)
            print("Only the isolated results process was stopped.")
            return
        if running:
            print("Isolated results service is already running.")
            return
        # A stale PID record is replaced only after verifying the port is free.
        check_port()
        if not args.app_dir.is_dir() or not (args.app_dir / "pankgraph_results/app.py").is_file():
            raise ValueError("Results application release is missing")
        frontend = Path(env.get("PANK_RESULTS_FRONTEND_DIR", ""))
        if not frontend.is_absolute() or not (frontend / "index.html").is_file():
            raise ValueError("A dedicated built results frontend artifact is required")
        launch = command(args.app_dir, args.python)
        if args.action == "foreground":
            os.chdir(args.app_dir)
            os.execve(launch[0], launch, env)
        with (state / "results.log").open("ab") as log:
            child = subprocess.Popen(launch, cwd=args.app_dir, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(.5)
        if child.poll() is not None:
            raise ValueError("Results service failed to start; inspect its private results log")
        record = {"pid": child.pid, "start": process_start(child.pid), "uid": os.geteuid(),
                  "entry": APP_ENTRY, "port": PORT}
        write_pid(pidfile, record)
        print(json.dumps({"started": True, "pid": child.pid, "port": PORT}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        # Never include environment values, command arguments or provider errors.
        if isinstance(exc, OSError):
            raise SystemExit("Results deployment filesystem/process operation failed; inspect configuration locally.")
        raise SystemExit(str(exc))
