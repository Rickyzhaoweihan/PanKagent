"""Shared configuration for the GPU tunnel supervisor + load-balancing router.

All settings come from environment variables (loaded from the repo-root ``.env``
via python-dotenv), so the standalone ``tunnel_supervisor.py`` / ``load_balancer.py``
processes read the same config as the main app.

Fleet topology (5 GPUs serving the ``cypher-writer`` vLLM model):

    local port 7000  -> lh0304 (H100)   via SSH -L tunnel
    local port 7001  -> lh0304 (H100)   via SSH -L tunnel
    local port 7002  -> lh0303 (L40s)   via SSH -L tunnel
    local port 7003  -> lh0303 (L40s)   via SSH -L tunnel
    local port 8002  -> this box (L40s) already local, no tunnel

The router listens on :8010 (8002 is the local vLLM) and the app points at it
via VLLM_PORT=8010.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is a declared dep
    load_dotenv = None

# Repo root is the parent of this gpu_router/ directory.
BASE_DIR = Path(__file__).resolve().parent          # .../gpu_router
REPO_ROOT = BASE_DIR.parent                          # .../PanKLLM_implementation
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Load .env from the repo root (idempotent; does not override already-set vars).
if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")

# PID / state files live next to the code.
TUNNEL_PID_FILE = BASE_DIR / "tunnel.pid"
ROUTER_PID_FILE = BASE_DIR / "router.pid"
WATCHDOG_STATE_FILE = BASE_DIR / "watchdog_state.json"
WATCHDOG_LOCK_FILE = BASE_DIR / "watchdog.lock"


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# SSH tunnel settings
# --------------------------------------------------------------------------- #
SSH_USER = _env("SSH_USER", "rickyhan")
LIGHTHOUSE_HOST = _env("LIGHTHOUSE_HOST", "lighthouse.arc-ts.umich.edu")
# SSH_PASSWORD is required for the tunnel; never logged.
SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")
# Duo: the option to send at the "Passcode or option (1-N):" prompt. "1" = push
# to the primary device (you approve on your phone).
DUO_OPTION = _env("DUO_OPTION", "1")

# Forwards as "localport:remotehost:remoteport" entries, comma-separated.
# Only the *tunnelled* (cluster) backends belong here; the local :8002 backend
# needs no tunnel.
_DEFAULT_TUNNELS = (
    "7000:lh0304:7000,"
    "7001:lh0304:7001,"
    "7002:lh0303:7002,"
    "7003:lh0303:7003"
)


@dataclass(frozen=True)
class Forward:
    local_port: int
    remote_host: str
    remote_port: int

    def ssh_flag(self) -> list[str]:
        return ["-L", f"{self.local_port}:{self.remote_host}:{self.remote_port}"]


def parse_tunnels(spec: str | None = None) -> list[Forward]:
    spec = spec if spec is not None else _env("TUNNEL_SPECS", _DEFAULT_TUNNELS)
    out: list[Forward] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        lp, rh, rp = chunk.split(":")
        out.append(Forward(int(lp), rh.strip(), int(rp)))
    return out


TUNNELS: list[Forward] = parse_tunnels()

# ssh hardening for a long-lived, self-healing tunnel.
SSH_OPTIONS = [
    "-N",                                   # no remote command, just forwards
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "TCPKeepAlive=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=30",
    "-o", "NumberOfPasswordPrompts=2",
]

# Tunnel supervisor reconnect backoff (seconds).
TUNNEL_BACKOFF_BASE = _env_int("TUNNEL_BACKOFF_BASE", 5)
TUNNEL_BACKOFF_MAX = _env_int("TUNNEL_BACKOFF_MAX", 60)
# How long to wait after Duo approval for the forwards to come up.
TUNNEL_CONNECT_GRACE = _env_int("TUNNEL_CONNECT_GRACE", 30)


# --------------------------------------------------------------------------- #
# Load-balancing router settings
# --------------------------------------------------------------------------- #
ROUTER_HOST = _env("ROUTER_HOST", "127.0.0.1")
ROUTER_PORT = _env_int("ROUTER_PORT", 8010)

# Backends as "host:port:weight" entries, comma-separated. Weight is a positive
# integer; H100 ports get a larger weight so they take proportionally more load.
_DEFAULT_BACKENDS = (
    "127.0.0.1:7000:2,"   # H100 (lh0304)
    "127.0.0.1:7001:2,"   # H100 (lh0304)
    "127.0.0.1:7002:1,"   # L40s (lh0303)
    "127.0.0.1:7003:1,"   # L40s (lh0303)
    "127.0.0.1:8002:1"    # L40s (local, this box)
)


@dataclass
class Backend:
    host: str
    port: int
    weight: int = 1
    healthy: bool = True            # optimistic until first health sweep
    inflight: int = 0
    consecutive_fails: int = 0
    consecutive_ok: int = 0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"


def parse_backends(spec: str | None = None) -> list[Backend]:
    spec = spec if spec is not None else _env("ROUTER_BACKENDS", _DEFAULT_BACKENDS)
    out: list[Backend] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        host, port = parts[0].strip(), int(parts[1])
        weight = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 1
        out.append(Backend(host=host, port=max(1, port), weight=max(1, weight)))
    return out


# Health-check + proxy tuning.
HEALTH_INTERVAL = _env_int("ROUTER_HEALTH_INTERVAL", 10)     # seconds between sweeps
HEALTH_TIMEOUT = _env_int("ROUTER_HEALTH_TIMEOUT", 5)        # per-probe timeout
HEALTH_FAIL_THRESHOLD = _env_int("ROUTER_HEALTH_FAIL_THRESHOLD", 2)
HEALTH_OK_THRESHOLD = _env_int("ROUTER_HEALTH_OK_THRESHOLD", 1)
PROXY_READ_TIMEOUT = _env_int("ROUTER_PROXY_READ_TIMEOUT", 300)   # cypher gen can be slow
PROXY_CONNECT_TIMEOUT = _env_int("ROUTER_PROXY_CONNECT_TIMEOUT", 10)
PROXY_MAX_RETRIES = _env_int("ROUTER_PROXY_MAX_RETRIES", 4)       # retry across other backends (up to all 5)


def health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/health"
