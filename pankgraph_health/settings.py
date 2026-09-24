from dataclasses import dataclass, field
from pathlib import Path
import os
import json
import shlex
from ipaddress import ip_address
from urllib.parse import urlsplit


def protected_values(path):
    path = Path(path)
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Configuration must be owner-only and not a symlink')
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix('export ')
        if not line or line.startswith('#'):
            continue
        key, raw = line.split('=', 1)
        parts = shlex.split(raw, comments=True)
        if len(parts) > 1:
            raise ValueError('Configuration values must be quoted')
        values[key.strip()] = parts[0] if parts else ''
    return values


def cypher_endpoint(value, *, optional=False):
    """Accept only bounded, credential-free loopback HTTP base URLs."""
    if optional and value == '':
        return ''
    message = 'Cypher endpoint must be a loopback HTTP base URL with an explicit port'
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(c.isspace() for c in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        loopback = ip_address(parsed.hostname).is_loopback
        valid = (parsed.scheme == 'http' and loopback and parsed.port is not None
                 and 1 <= parsed.port <= 65535 and parsed.username is None
                 and parsed.password is None and parsed.path in ('', '/')
                 and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return value.rstrip('/')


@dataclass
class Settings:
    state_dir: Path
    user: str
    password_hash: str
    agent_token: str = ''
    interval: float = 30
    retention_days: int = 7
    agent_url: str = 'http://127.0.0.1:8794'
    results_url: str = 'http://127.0.0.1:8795'
    functional_url: str = 'https://functional.pankgraph.org/health'
    port: int = 8796
    frontend_headers: dict = field(default_factory=dict, repr=False)
    cypher_url: str = 'http://127.0.0.1:33917'
    cypher_replica_a_url: str = 'http://127.0.0.1:33918'
    cypher_replica_b_url: str = ''
    basic_auth: bool = True

    def __post_init__(self):
        self.cypher_url = cypher_endpoint(self.cypher_url)
        self.cypher_replica_a_url = cypher_endpoint(self.cypher_replica_a_url, optional=True)
        self.cypher_replica_b_url = cypher_endpoint(self.cypher_replica_b_url, optional=True)

    @classmethod
    def load(cls):
        agent = protected_values(os.environ.get('PANK_HEALTH_AGENT_ENV', '/var/local/serviceuser/.config/pankagent-vnext/runtime.env'))
        results = protected_values(os.environ.get('PANK_HEALTH_RESULTS_ENV', '/var/local/serviceuser/.config/pankgraph-results/runtime.env'))
        cfg = cls(Path(os.environ.get('PANK_HEALTH_STATE_DIR', '/var/local/serviceuser/.local/state/pankgraph-health')),
                  '', '',
                  agent.get('PANK_VNEXT_OPERATOR_TOKEN', ''),
                  cypher_url=agent.get('PANK_VNEXT_CYPHER_URL', 'http://127.0.0.1:33917'),
                  cypher_replica_a_url=os.environ.get('PANK_HEALTH_CYPHER_REPLICA_A_URL',
                      agent.get('PANK_HEALTH_CYPHER_REPLICA_A_URL', 'http://127.0.0.1:33918')),
                  cypher_replica_b_url=os.environ.get('PANK_HEALTH_CYPHER_REPLICA_B_URL',
                      agent.get('PANK_HEALTH_CYPHER_REPLICA_B_URL', '')))
        cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = cfg.state_dir.stat()
        if cfg.state_dir.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError('Dashboard state must be owner-only')
        cfg.basic_auth = results.get("PANK_RESULTS_HEALTH_BASIC_AUTH", "true").lower() != "false"
        operator = cfg.state_dir / "operator-auth.json"
        if cfg.basic_auth:
            info = operator.stat()
            if operator.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Operator credentials must be owner-only")
            credentials = json.loads(operator.read_text())
            cfg.user, cfg.password_hash = credentials.get('user', ''), credentials.get('password_hash', '')
            if not cfg.user or not cfg.password_hash or cfg.password_hash == results.get('PANK_RESULTS_PASSWORD_HASH'):
                raise ValueError("Separate operator authentication is required")
        auth = cfg.state_dir / "frontend-auth.json"
        if auth.exists():
            info = auth.stat()
            if auth.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("Frontend probe credentials must be owner-only")
            cfg.frontend_headers = {"Authorization": json.loads(auth.read_text())["authorization"]}
        return cfg
