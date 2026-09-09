# Small regular graphs

Layout identity `pankgraph-regular-5` selects PyGraphviz `dot` for 1–9
unique displayed nodes. Empty graphs remain empty. Graphs with 10 or more
displayed nodes retain relationship-list or optimized layout selection.

The Graphviz multigraph uses `strict=False`; parallel evidence records remain
separate. Only node positions are returned. The existing frontend supplies
native curved edges and visible labels, without custom router ports/waypoints.
Layout runs inside the existing bounded subprocess; dependency or layout
failure is reported as a fallback, never as successful PyGraphviz execution.
Previous custom coordinates are not reused for small graphs.

The results runtime needs both the `pygraphviz` Python package and Graphviz's
`dot` executable on PATH. Installing the Python requirement alone may require
Graphviz development headers and libraries. Do not modify a shared runtime:
prepare an owned results environment and verify dependencies before activation.

Validation on 2026-09-09: 22 tests passed in an isolated jieliu3 environment,
including the ADCY3 three-node/four-edge multigraph, deterministic nonoverlapping
node positions, 1/9/10-node routing boundaries, and existing larger-graph/list
regressions. This does not constitute browser visual acceptance. Live service
8795 was subsequently activated at the user's explicit deployment request.

## Deployment

The two layout modules from `ff98066` are active on isolated results 8795.
The actual deployed worker returned `engine=pygraphviz`, three nodes and four
edges for the synthetic coloc triangle. Both 8794 and 8795 readiness checks and
five Mac page/asset/health checks passed. No inference was invoked.

Dedicated results Python:
`/var/local/serviceuser/.local/state/pankgraph-results/small-layout-runtime/bin/python`

For future owned-manager restarts, pass this path with `--python` and prepend
`/var/local/serviceuser/.local/state/pankgraph-results/pygraphviz-test-env/bin`
to PATH so the worker can find `dot`. The agent Python environment was unchanged.

Protected deployment/rollback artifacts are under
`/var/local/serviceuser/.local/state/pankgraph-results/small-layout-deploy/`.
The first startup exposed an old results-local copy of agent settings rejecting
ceilings above $10. Its validation maximum was aligned with the current agent's
$30 supported maximum; this does not change the configured budget ceiling or
ledger. The original file is `results-config.before.py`. The confirmed-dead PID
record was archived before restarting. This startup failure is retained, not
counted as successful activation. Only 8795 restarted; 8794 remained running.
