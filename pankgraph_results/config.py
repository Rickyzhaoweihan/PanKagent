"""Results settings; protected vNext settings remain the graph/budget authority."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit


def env(name, default=""):
    return os.environ.get("PANK_RESULTS_" + name, default)


@dataclass
class ResultsSettings:
    state_dir: Path = field(default_factory=lambda: Path(env("STATE_DIR", "var/results")))
    frontend_dir: Path = field(default_factory=lambda: Path(env("FRONTEND_DIR", "../pank_frontend_vnext/build")))
    agent_url: str = field(default_factory=lambda: env("AGENT_URL", "http://127.0.0.1:8794"))
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: int(env("PORT", "8795")))
    public_path: str = field(default_factory=lambda: env("PUBLIC_PATH", "/pankgraph-vnext"))
    max_concurrent: int = 2
    max_queue: int = 8
    display_nodes: int = 100
    layout_timeout: float = 5.0
    resource_timeout: float = 10.0
    resource_max_bytes: int = 50 * 1024 * 1024
    resource_cache_max_bytes: int = 2 * 1024 * 1024 * 1024
    resource_max_rows: int = 50000
    resource_max_objects: int = 4
    resource_ttl_seconds: float = 86400
    dbsnp_command: str = field(default_factory=lambda: env("DBSNP_COMMAND", "dbsnp-query"))
    operator_token: str = field(default_factory=lambda: env("OPERATOR_TOKEN"))
    basic_user: str = field(default_factory=lambda: env("BASIC_USER", "pank-demo"))
    password_hash: str = field(default_factory=lambda: env("PASSWORD_HASH"))
    testing: bool = False

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        self.frontend_dir = Path(self.frontend_dir)
        agent = urlsplit(self.agent_url)
        if agent.scheme != "http" or agent.hostname not in {"127.0.0.1", "::1"} or agent.username or agent.password:
            raise ValueError("agent_url_must_be_loopback")
        if self.host != "127.0.0.1" or not 1 <= self.port <= 65535:
            raise ValueError("invalid_listener")
        if not self.public_path.startswith("/") or ".." in self.public_path:
            raise ValueError("invalid_public_path")
        self.public_path = self.public_path.rstrip("/")
