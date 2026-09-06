# Vendored Graph_viewer layout code

Source: https://github.com/RingoMao/Graph_viewer
Pinned commit: `362025db24b1d37223c3c44ccf02a55eb2756a42`
Branch: `codex/graph-viewer-timeout-errors`

`layout_engine/` contains byte-identical data-only modules. `filtering.py`
extracts the regular filter and its two helpers from `app/index.py`, changes
its display ceiling to 100, and removes genome expansion. Selection weights
and deterministic ordering are unchanged. `PROVENANCE.json` records hashes.

No upstream query handler or database connection code is imported or copied.
The surrounding facade owns canonical evidence conversion, presentation scale,
initial positions, process deadlines, display budgets and fallback behavior.
The facade's `pankgraph-regular-2` policy uses one deterministic positioning
iteration, checkpointed routing batches, a separate routing budget, and a hard
process deadline. Already optimized positions survive expensive routing; edges
that do not finish use explicitly marked conventional fallback curves. Source
primitives remain byte-identical, and no database/query module is included.
The original repository benchmarks only through 25 nodes; local benchmarks
explicitly report 25/50/100-node results and optimized/fallback status.
