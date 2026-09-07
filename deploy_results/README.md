# Isolated results deployment

This package serves only the approved result flow at `127.0.0.1:8795` and the
public prefix `/pankgraph-vnext/`. It preserves the existing result-page design.
It does not replace or restart PanKagent on 8794, HIRN, Neo4j, PostgreSQL,
Cypher generation, tunnels, or existing production routes.

The files here are staged tooling. Installing the nginx snippet and reloading
nginx are separate operator actions; no nginx change is made by these scripts.

## Private runtime arrangement

Run management commands as `sudo -n -u serviceuser`, never as root. Recommended
paths (all can be supplied through the CLI or protected results environment):

| Item | Path |
| --- | --- |
| Results release | `/var/local/serviceuser/projects/pankgraph-results/current` |
| Results Python environment | `<release>/.venv/bin/python` |
| Dedicated built frontend | `/var/local/serviceuser/projects/pankgraph-results/frontend` |
| Existing protected vNext environment | `/var/local/serviceuser/.config/pankagent-vnext/runtime.env` |
| Protected results environment | `/var/local/serviceuser/.config/pankgraph-results/runtime.env` |
| Results PID/log/cache/index state | `/var/local/serviceuser/.local/state/pankgraph-results` |

Stage a separate checkout/release and install `requirements-results.txt` into
its Python environment. Copy the approved built frontend artifact into the
dedicated frontend directory. Do not copy credentials into a release or frontend
bundle. Do not package datasets, `.env` files, logs, caches, or SQLite databases
in Git.

The two environment files must be serviceuser-owned and mode 0600; the results
state directory must be mode 0700. `manage.py` reads literal assignments without
shell execution or variable expansion. It loads the existing vNext environment
first, retaining its configured `PANK_VNEXT_STATE_DIR` and shared persistent
Claude budget ledger. The results environment may contain only `PANK_RESULTS_*`
variables, so it cannot override the protected graph identity or shared ledger.
Results state must be a different directory from vNext state.

The protected results file requires these assignments; the password hash is
created privately and is never supplied on a command line or stored in Git:

```dotenv
PANK_RESULTS_STATE_DIR=/var/local/serviceuser/.local/state/pankgraph-results
PANK_RESULTS_FRONTEND_DIR=/var/local/serviceuser/projects/pankgraph-results/frontend
PANK_RESULTS_AGENT_URL=http://127.0.0.1:8794
PANK_RESULTS_PORT=8795
PANK_RESULTS_PUBLIC_PATH=/pankgraph-vnext
PANK_RESULTS_BASIC_USER=pank-demo
# Set PANK_RESULTS_PASSWORD_HASH to the existing application's hash_password()
# output through a private interactive operator workflow. Never use a sample.
# Set PANK_RESULTS_DBSNP_COMMAND to the approved existing read-only dbSNP adapter.
```

`pankgraph_results.auth.hash_password` generates the required PBKDF2 hash.
Single-quote its value in the protected env file to keep dollar separators
literal. The application authenticates HTTP Basic itself; nginx needs no
`htpasswd` file. Public Basic authentication requires the existing HTTPS server.

## Start, check, stop

From the staged release, with paths adjusted if needed:

```bash
sudo -n -u serviceuser python3 deploy_results/manage.py status
sudo -n -u serviceuser python3 deploy_results/manage.py start
sudo -n -u serviceuser python3 deploy_results/manage.py stop
```

`--app-dir`, `--python`, `--vnext-env-file`, and `--env-file` override defaults.
`start.sh` runs the same validated configuration in the foreground for an
operator-owned supervisor. Use one launch method at a time; foreground launches
are supervised by their owner and do not create the manager's PID record.

The manager checks port availability before starting. Its stop operation verifies
the PID, start time, OS owner, exact module/port/host arguments, and working
directory. It signals only its own results process with SIGTERM, waits up to
eight seconds, and refuses a forced kill. A mismatched PID record is reported
for operator inspection; no unrelated process is signaled. Uvicorn receives a
two-second request-drain timeout; application lifespan cleanup can take longer.

Use local `/health/live`, `/health/ready`, `/health/components` and `/metrics`
to inspect cached component states. Nginx must forward `X-Forwarded-For`: this
prevents proxied health requests from being mistaken for trusted loopback
operator requests. Uvicorn proxy-header rewriting is disabled so the application
can make that distinction using the actual connection and header.

## Exact-key resource seed

`seed-manifest-v1.json` names four previously verified public objects, one per
registered QTL source. It is a small seed, not an exhaustive association index.
The T1D/GWAS candidate prefix remains unverified and is deliberately not seeded.

```bash
sudo -n -u serviceuser .venv/bin/python -m pankgraph_results.resources \
  --state-dir /var/local/serviceuser/.local/state/pankgraph-results/resources \
  --seed-manifest deploy_results/seed-manifest-v1.json --max-objects 4
```

The CLI fetches only those exact public keys, validates the seven-column TSV
schema, and stores bounded assets plus a source-scoped SQLite association index
outside Git. It never lists the S3 bucket. It reports sanitized per-object
outcomes; inspect every outcome before relying on coverage. ETag/Last-Modified,
SHA-256, schema, timestamps and source identity accompany cached records.
Missing files, denied access and schema mismatches remain unavailable outcomes.
Resource indexes contain only cached validated sets and cannot establish that a
SNP or gene has no associations outside their stated coverage.

## Nginx staging and rollback

Stage `nginx-prefix.conf.example` inside the existing HTTPS server block without
changing other locations. It forwards only the exact `/pankgraph-vnext/` prefix,
preserves the path, disables response buffering for SSE, and adds security
headers. An authorized nginx operator must validate the full configuration with
`nginx -t`, review the effective route, and reload nginx. These commands are not
run by the results deployment tooling.

After operator activation, verify authentication, static assets, SSE progress,
local downloads, and external health restrictions through the public prefix.
Record existing 8794 and shared-service health before and after smoke checks.
Rollback removes only this new nginx location through the same operator
validation/reload process and stops only the results PID. Retain private result
state and the shared budget ledger; do not delete or replace the old deployment.

## Local access before public-route activation

Use an existing SSH forward to the isolated results port, or start a loopback
forward on a free local port:

```bash
ssh -N -L 127.0.0.1:18796:127.0.0.1:8795 jieliulab3-codex
```

In a separate terminal, run `python3 deploy_results/local_demo_proxy.py
--access-file /path/to/protected/access.txt`. The owner-only file contains the
existing `Username:` and `Password:` fields. Open
`http://127.0.0.1:18795/pankgraph-vnext/`. Credentials stay server-side; the helper
accepts only loopback Host/Origin values and the demo prefix. It does not create
credentials, manage remote services or activate nginx. Stop only the helper and
SSH processes you started. A public-route 404 must not be presented as a working
public deployment.
