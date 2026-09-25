"""Service-owned settings. Never import the legacy executable config module."""
from .agent_schemas import module as schema_module
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
    openai_key: str = field(default_factory=lambda: os.environ.get('OPENAI_API_KEY',''))
    reasoning_effort: str = field(default_factory=lambda: env('REASONING_EFFORT','none'))
    budget_dir: str = field(default_factory=lambda: env('BUDGET_DIR',''))
    anthropic_key: str = field(default_factory=lambda: os.environ.get('ANTHROPIC_API_KEY',''))
    budget_usd: float = field(default_factory=lambda: float(env('BUDGET_USD','10')))
    operator_token: str = field(default_factory=lambda: env('OPERATOR_TOKEN'))
    max_concurrent: int = field(default_factory=lambda: int(env('MAX_CONCURRENT','2')))
    max_queue: int = field(default_factory=lambda: int(env('MAX_QUEUE','8')))
    heartbeat_seconds: float = 2.0
    plan_timeout: float = field(default_factory=lambda: float(env('PLAN_TIMEOUT','45')))
    preview_timeout: float = field(default_factory=lambda: float(env('PREVIEW_TIMEOUT','45')))
    grouped_preview_timeout: float = field(default_factory=lambda: float(env('GROUPED_PREVIEW_TIMEOUT','120')))
    preview_ttl_seconds: float = field(default_factory=lambda: float(env('PREVIEW_TTL_SECONDS','300')))
    run_timeout: float = 40.0
    answer_timeout: float = field(default_factory=lambda: float(env('ANSWER_TIMEOUT', '60')))
    provider_status_url: str = "https://status.claude.com/api/v2/summary.json"
    literature_api_version: str = field(default_factory=lambda: env('LITERATURE_API_VERSION','hirn-agent-v1'))
    health_interval: float = 30.0
    claude_health_interval: float = 300.0
    literature_timeout: float = 60.0
    glkb_url: str = field(default_factory=lambda: env('GLKB_URL', 'http://127.0.0.1:5055'))
    glkb_timeout: float = 330.0
    literature_concurrency: int = 4
    literature_queue: int = 8
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
    cypher_initial_requests: int = field(default_factory=lambda: int(env('CYPHER_INITIAL_REQUESTS', '2')))
    cypher_initial_scope: str = field(default_factory=lambda: env('CYPHER_INITIAL_SCOPE', 'all'))
    cypher_generation_concurrency: int = field(default_factory=lambda: int(env('CYPHER_GENERATION_CONCURRENCY', '4')))
    plan_cache_enabled: bool = field(default_factory=lambda: env('PLAN_CACHE_ENABLED','1') == '1')
    grounded_query_policy: bool = field(default_factory=lambda: env('GROUNDED_QUERY_POLICY','1') == '1')
    max_nodes: int = field(default_factory=lambda: schema_module('validation_repair')['backend_materialization']['max_nodes'])
    max_edges: int = field(default_factory=lambda: schema_module('validation_repair')['backend_materialization']['max_edges'])
    max_bytes: int = field(default_factory=lambda: schema_module('validation_repair')['backend_materialization']['max_bytes'])

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)
        if self.host not in ('127.0.0.1','::1'):
            raise ValueError('vNext development service must bind to loopback')
        if self.model not in ('claude-sonnet-5','claude-haiku-4-5-20251001','gpt-6-sol'):
            raise ValueError('model must have an explicitly configured price')
        if self.reasoning_effort not in ('none','low','medium','high','xhigh','max'):
            raise ValueError('unsupported reasoning effort')
        if self.model == 'gpt-6-sol':
            self.provider_status_url = ''  # Do not report Anthropic status as OpenAI health.
        if not 0 < self.budget_usd <= 30 or not 1 <= self.max_concurrent <= 4 or not 1 <= self.max_queue <= 32:
            raise ValueError('invalid development budget or queue limits')
        if not 0 < self.preview_timeout <= 120 or not 0 < self.grouped_preview_timeout <= 120 or not 0 <= self.preview_ttl_seconds <= 3600:
            raise ValueError('invalid preview deadline or reuse window')
        if not 0 < self.plan_timeout <= 60:
            raise ValueError('invalid planning deadline')
        if not 0 < self.answer_timeout <= 120:
            raise ValueError('invalid answer deadline')
        if self.cypher_initial_requests not in (1, 2, 4):
            raise ValueError('initial Cypher requests must be one, two or four')
        if self.cypher_generation_concurrency not in (1, 2, 4):
            raise ValueError('Cypher generation concurrency must be one, two or four')
        if self.cypher_initial_scope not in ("cohort", "all"):
            raise ValueError("initial Cypher scope must be cohort or all")
