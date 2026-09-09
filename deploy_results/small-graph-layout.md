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
8795 has not been restarted or modified for this change.
