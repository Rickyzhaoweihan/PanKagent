"""Service-private configuration; no legacy application imports or defaults."""
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str = field(repr=False)
    postgres_read_dsn: str = field(repr=False)
    api_token: str = field(repr=False)
    snapshot_id: str
    neo4j_database: str = "pankgraph0919"
    postgres_read_user: str = "pankgraph0919_reader"
    port: int = 18919
    query_timeout: int = 10
    max_rows: int = 200
    max_bytes: int = 2_000_000
    allow_sensitive_records: bool = False

    def __post_init__(self):
        uri = urlsplit(self.neo4j_uri)
        if (uri.scheme not in {"bolt", "bolt+s", "bolt+ssc"}
                or uri.hostname not in {"127.0.0.1", "::1"}
                or uri.username or uri.password or not uri.port):
            raise ValueError("Neo4j requires a credential-free loopback Bolt URI")
        if self.neo4j_database != "pankgraph0919":
            raise ValueError("Only the pankgraph0919 Neo4j database is allowed")
        try:
            pg = conninfo_to_dict(self.postgres_read_dsn)
        except Exception:
            raise ValueError("Invalid private PostgreSQL configuration") from None
        if (pg.get("dbname") != "pankgraph0919"
                or self.postgres_read_user != "pankgraph0919_reader"
                or pg.get("user") != self.postgres_read_user
                or pg.get("host") not in {"127.0.0.1", "::1"}
                or not pg.get("port") or pg.get("hostaddr") or pg.get("service")):
            raise ValueError("PostgreSQL requires the isolated database and reader role on loopback")
        if not self.neo4j_user or not self.neo4j_password:
            raise ValueError("Neo4j credentials are required")
        if len(self.api_token) < 32:
            raise ValueError("API token must contain at least 32 characters")
        if not self.snapshot_id or len(self.snapshot_id) > 200:
            raise ValueError("An immutable snapshot ID is required")
        if self.port in {8794, 8795, 8796} or not 1024 <= self.port <= 65535:
            raise ValueError("An independent unprivileged API port is required")
        if not 1 <= self.query_timeout <= 30 or not 1 <= self.max_rows <= 200:
            raise ValueError("Invalid query limits")
        if not 1024 <= self.max_bytes <= 2_000_000:
            raise ValueError("Invalid response byte limit")

    @classmethod
    def from_env(cls):
        required = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "POSTGRES_READ_DSN", "API_TOKEN", "SNAPSHOT_ID")
        values = {name.lower(): os.environ.get("PANK0919_" + name, "") for name in required}
        for name in ("NEO4J_DATABASE", "POSTGRES_READ_USER"):
            if "PANK0919_" + name in os.environ:
                values[name.lower()] = os.environ["PANK0919_" + name]
        for name in ("PORT", "QUERY_TIMEOUT", "MAX_ROWS", "MAX_BYTES"):
            if "PANK0919_" + name in os.environ:
                values[name.lower()] = int(os.environ["PANK0919_" + name])
        privacy = os.environ.get("PANK0919_ALLOW_SENSITIVE_RECORDS", "false").lower()
        if privacy not in {"true", "false"}:
            raise ValueError("ALLOW_SENSITIVE_RECORDS must be true or false")
        values["allow_sensitive_records"] = privacy == "true"
        return cls(**values)
