# PanKgraph 0919 isolated deployment

This project overlay owns a new native PostgreSQL cluster, a new Neo4j Community
instance and an independent API. Both logical databases are `pankgraph0919`.
It does not manage the legacy agent/results/health services or nginx.

| Component | Owned loopback port | Runtime |
|---|---:|---|
| PostgreSQL | 15919 | PostgreSQL 18.3 |
| Neo4j Bolt | 17919 | Neo4j Community 5.26.2 / Java 21 |
| Neo4j HTTP | 17419 | Same dedicated Neo4j instance |
| Query API | 18919 | Python 3.13 / FastAPI |

The deployment root is `/db/pankgraph0919`, owned by the service account. Its
`postgres/` and `neo4j/` directories contain only this release's data. Sources,
bundles, credentials and detailed audits stay outside Git in its private
`sources/`, `config/` and `audit/` directories. Released application code is under
`releases/20260919-r1`; Python dependencies live in its own `venv/`.

`config/credentials.json` is mode 0600; the containing directory is private.
The manager generates fresh credentials on the server. It never prints a DSN,
password or bearer token. The query client reads the token privately rather
than placing it in shell arguments. No public route is activated.

## Build and acceptance

Use the separately verified standard wheel pinned by `requirements-0919.txt`.
The profile is `context-aware-source-results` version `0.1.0-draft`; canonical
identity follows `cakg-identity-v1`. It is an additive extension to the GitHub
standard, not a change to the existing PanKgraph release or its pinned submodule.

1. `deploy_0919.manage bootstrap` requires an absent root and unused ports. It
   extracts a clean Neo4j distribution and initializes a fresh PostgreSQL data
   directory. Never point it at another database or an existing runtime root.
2. Acquire the source tables with `pankgraph0919_ingest`; see
   [source handling](0919-sources.md). Keep failures and unknown metadata visible.
3. Build closed source bundles using `pankgraph0919_ingest.parallel_build` with
   at most four processes, an empty output directory and a pinned gene reference.
   A final global index appears only after every worker finishes and hashes agree.
4. `deploy_0919.build --root … --index …` checks every indexed path/hash, then
   performs one atomic PostgreSQL load into an empty schema. Neo4j must also be
   empty. It validates canonical identities, node/edge endpoints, evidence links,
   source accounting and graph materialization. No destructive retry is provided.
5. `manage grant-reader` grants only SELECT on the new schema to
   `pankgraph0919_reader`. `manage freeze-graph` activates and verifies actual
   Neo4j read-only access. A staged configuration file alone does not pass.
6. `manage start-api --release … --snapshot …` refuses a writable graph and an
   occupied API port. It starts only the new application, clears inherited Python
   import paths, and keeps sensitive record detail redacted.
7. Run `deploy_0919.acceptance --root …`. It verifies real database counts,
   authenticated API behavior, source evidence joins, pagination, quarantine,
   read-only enforcement and the recorded legacy listener PIDs. Its sanitized
   result is `audit/api-acceptance.json`; any required failure exits nonzero.

The SQL status `accepted` means technical storage acceptance. Source-result
membership does not mean significance, causality, normalized effect direction,
or full scientific readiness. The coverage report and open curation issues are
part of the release. Staging a raw matrix is not a completed sample/donor join.

## Query and recovery

On the server, run the private client as the runtime owner with the release on
the module search path. The following request can be supplied as JSON on stdin
to `python -m deploy_0919.query --root /db/pankgraph0919 --request -`:

```json
{"cypher":"MATCH (g:BioEntity:Gene {name:$name})-[r:HAS_EXPRESSION_RESULT_IN]->(c:BioEntity:Cell_type) RETURN g,r,c","parameters":{"name":"INS"},"mode":"detail","filters":{"collection_id":"pankbase:03_de_markers"},"limit":5}
```

The [API contract](0919-api.md) covers independent PostgreSQL search and context
filters. Direct Neo4j/PG access also requires the private server configuration.
The Neo4j Community account is confined to this dedicated instance; data-write
denial is enforced by the live read-only database setting in addition to the API
query boundary. PostgreSQL uses a separately restricted reader role.

`manage stop-api` verifies UID, PID start time, command and working directory
before signaling the owned process. `start-api` refuses any live PID or occupied
port, including a foreign process. `start-databases` operates only on the marked
root and checks owned daemon command paths. Existing ports 8794, 8795 and 8796
remain outside these commands. No boot-time service registration is installed;
the operator can restart the owned services with this manager after a host reboot.

On a failed import, retain the source index, audit and database state for
inspection. A PostgreSQL failure rolls back its transaction; a partial Neo4j load
stays unaccepted and is never deleted automatically. The `--graph-only` build
option applies only when PostgreSQL is loaded and Neo4j is still empty. Do not
alter an accepted snapshot behind a running API.
