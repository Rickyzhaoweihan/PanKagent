"""Service-owned settings. Never import the legacy executable config module."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def env(name, default=''):
    return os.environ.get('PANK_VNEXT_'+name, default)


@dataclass
class Settings:
    host: str = field(default_factory=lambda: env('HOST','127.0.0.1'))
    port: int = field(default_factory=lambda: int(env('PORT','8794')))
    state_dir: Path = field(default_factory=lambda: Path(env('STATE_DIR','var/vnext')))
    model: str = field(default_factory=lambda: env('MODEL','claude-sonnet-5'))
    anthropic_key: str = field(default_factory=lambda: os.environ.get('ANTHROPIC_API_KEY',''))
    budget_usd: float = field(default_factory=lambda: float(env('BUDGET_USD','10')))
    operator_token: str = field(default_factory=lambda: env('OPERATOR_TOKEN'))
    max_concurrent: int = field(default_factory=lambda: int(env('MAX_CONCURRENT','2')))
    max_queue: int = field(default_factory=lambda: int(env('MAX_QUEUE','8')))
    heartbeat_seconds: float = 2.0
    plan_timeout: float = 30.0
    preview_timeout: float = field(default_factory=lambda: float(env('PREVIEW_TIMEOUT','45')))
    preview_ttl_seconds: float = field(default_factory=lambda: float(env('PREVIEW_TTL_SECONDS','300')))
    run_timeout: float = 40.0
    provider_status_url: str = "https://status.claude.com/api/v2/summary.json"
    literature_api_version: str = field(default_factory=lambda: env('LITERATURE_API_VERSION','hirn-agent-v1'))
    health_interval: float = 30.0
    claude_health_interval: float = 300.0
    literature_timeout: float = 60.0
    literature_url: str = field(default_factory=lambda: env('LITERATURE_URL','http://127.0.0.1:8102'))
    corpus_version: str = field(default_factory=lambda: env('CORPUS_VERSION','hirn-mixed-current'))
    source_policy: str = field(default_factory=lambda: env('SOURCE_POLICY','mixed'))
    cypher_url: str = field(default_factory=lambda: env('CYPHER_URL','http://127.0.0.1:23917'))
    cypher_token: str = field(default_factory=lambda: os.environ.get('CYPHER_API_TOKEN',''))
    neo4j_uri: str = field(default_factory=lambda: env('NEO4J_URI','bolt://127.0.0.1:12687'))
    neo4j_user: str = field(default_factory=lambda: env('NEO4J_USER'))
    neo4j_password: str = field(default_factory=lambda: env('NEO4J_PASSWORD'))
    neo4j_database: str = field(default_factory=lambda: env('NEO4J_DATABASE','pankgraph'))
    graph_version: str = field(default_factory=lambda: env('GRAPH_VERSION','PanKgraph_08_04'))
    graph_identity_file: str = field(default_factory=lambda: env('GRAPH_IDENTITY_FILE','var/vnext/graph-identity.json'))
    graph_timeout: float = 10.0
    cypher_timeout: float = 15.0
    max_nodes: int = 2000
    max_edges: int = 5000
    max_bytes: int = 2_000_000

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        if self.host not in ('127.0.0.1','::1'):
            raise ValueError('vNext development service must bind to loopback')
        if self.model not in ('claude-sonnet-5','claude-haiku-4-5-20251001'):
            raise ValueError('model must have an explicitly configured price')
        if not 0 < self.budget_usd <= 10 or not 1 <= self.max_concurrent <= 4 or not 1 <= self.max_queue <= 32:
            raise ValueError('invalid development budget or queue limits')
        if not 0 < self.preview_timeout <= 120 or not 0 <= self.preview_ttl_seconds <= 3600:
            raise ValueError('invalid preview deadline or reuse window')
