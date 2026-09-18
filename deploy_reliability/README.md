# Unified isolated demo releases

Scope: agent `127.0.0.1:8794`, results `127.0.0.1:8795`, and the independent health
collector `127.0.0.1:8796`. Production and shared Cypher, Neo4j, functional and
literature services are outside this deployment tool's control.

## Build and verify

Run the backend tests and frontend tests/schema validator, then build the frontend
from its existing lockfile. Preserve the build log and dependency/runtime inventory.
From the backend checkout:

```sh
python -m deploy_reliability.release build --backend-root . --frontend-build ../pank_frontend_vnext/build --runtime-inventory /path/to/runtime-inventory.json --output /path/to/release.tar.gz
python -m deploy_reliability.release verify /path/to/release.tar.gz --sha256 EXPECTED_ARCHIVE_SHA256
```

Repeat the build to a second archive before publishing; identical inputs must
produce the same digest. The manifest binds every payload byte and records the
source commits **and dirty source hashes**, schemas, frontend assets and observed
runtime. It never represents a dirty checkout as just its base commit. No state,
secrets, node_modules, symlinks or AppleDouble files are packaged.

`freeze_runtime.py freeze runtime.tar.gz`, run under the existing demo Python,
preserves exact installed dependency bytes. Restore this trusted archive into a
new private directory, then run `freeze_runtime.py verify DIRECTORY` under the
restored `venv/bin/python`. This is an offline replay artifact for the same Linux
architecture/base Python installation; it is not a cross-platform container.
The verifier checks dependency files and resolved interpreter hashes. The pinned
`requirements-demo-runtime.lock` documents all observed versions. Use `python -m`
entry points because copied virtualenv scripts may retain their original shebang.

## Private backup and restoration acceptance

Run tools as `serviceuser` using the verified demo Python. Never use the host's
unversioned `python3` (it may be too old). `backup.py snapshot NEW_DIRECTORY`
uses SQLite's online backup API with a deadline. It captures only the active
session, budget, result and resource databases, resource assets, protected runtime
configuration, and health state/credentials when present. Historical research
runs and logs are excluded. All backup files stay private on the trusted host.

`backup.py restore-drill BACKUP --destination NEW_DIRECTORY` verifies every file,
restores into a new directory, runs SQLite integrity checks, and compares schema
and every row's logical hash. It also verifies assets and config hashes. It never
overwrites a destination or promotes a copy into a live state directory.

Online snapshots are individually consistent per database. For a cross-service
release checkpoint, drain the demo queues, stop the three owned services, then use
`snapshot NEW_DIRECTORY --quiescent`. This command refuses while any of the three
demo ports accepts connections. Complete a restoration drill before restarting.

## Activation and rollback

Keep the old agent checkout, results checkout, frontend directory and protected
configuration intact. Upload the verified archive into a new release directory:

```text
/var/local/serviceuser/projects/pankgraph-demo/releases/RELEASE/
  release-manifest.json
  backend/              # exact source; .venv points to the verified frozen runtime
  frontend/             # matching tested build
```

The `.venv` link is an explicitly created deployment link after archive validation;
it is not accepted as a source archive member. Start/stop from `backend/` with:

```sh
.venv/bin/python -m deploy_reliability.manage agent start --release "$PWD"
.venv/bin/python -m deploy_reliability.manage results start --release "$PWD"
.venv/bin/python -m pankgraph_health.supervisor start
```

The manager checks serviceuser identity, PID start time, owner, cwd, module and
exact listener before signaling anything. It always uses one worker. Results
receives its frontend directory from this release without editing protected
configuration. PID records are separate from the previous deployment managers.

Both applications acquire a durable single-active-owner lease before accepting
work or recovering old jobs. Every guarded write checks its fencing epoch in the
same transaction. A live second owner is refused. Clean shutdown releases its
lease; a crash can require up to60 seconds for expiry. Do not override a live
lease or automatically replay an uncertain provider request. This is **not an
active-active scheduler**; keep `workers=1` until distributed claiming, provider
idempotency and host-failure acceptance are implemented.

After startup check both readiness contracts, ownership state, actual HTML and
bundle hashes, saved-result reads, and the authenticated health UI. Health polling
does not execute inference. Test its own supervisor separately and verify its
child PID changes while agent/results PIDs remain unchanged.

To roll back this release, use `manage ... stop` for the new results and agent,
and stop the health supervisor from its release cwd. Restart the retained previous
applications using their existing `deploy_vnext/manage.py` and
`deploy_results/manage.py` with their original paths. Their original frontend and
config remain available. Ownership migrations are additive; compatibility reads
must be checked on restored copies before activation. Prefer rollback with current
state to avoid losing new work or budget records. Never replace the live budget
ledger with an older backup after any new provider activity. Full state restoration
requires a separately reviewed outage/reconciliation procedure.

The same-origin demo route is `/pankgraph-vnext/health-dashboard/`. A separate
loopback tunnel can reach8796 if results8795 is unavailable. No public nginx or
host-reboot service activation is implied by these start commands.
