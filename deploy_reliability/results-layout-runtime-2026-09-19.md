# Results layout runtime repair — 2026-09-19

The dev acceptance query produced `deterministic_grid` / `worker_failed` for a
small graph. The frozen `runtime-7c8335dd` environment omitted PyGraphviz, and the
results service PATH contained no `dot`. The small-graph adapter deliberately
reported fallback rather than claiming optimized geometry.

## Candidate provenance

An inactive owned runtime was prepared at
`/var/local/serviceuser/projects/pankgraph-demo/runtime-layout-20260919-candidate`.
Its `venv` is a full file copy of `runtime-7c8335dd/venv`; all 6,304 original
files and links were compared with their originals and remained unchanged.
Python remains the original Anaconda CPython 3.13.12 and the existing base
interpreter. No current runtime or protected service configuration was edited.

Only PyGraphviz 2.0.1, the Graphviz 14.1.2 `dot` executable, and their native
library closure were added from the preserved owned
`.local/state/pankgraph-results/pygraphviz-test-env` environment. These are
83 copied files totaling 48,416,843 bytes, each bound to its source SHA-256.
`dot -c` generated a registry from the copied plugins in the candidate.
The older `small-layout-runtime` was not reused wholesale: it uses a different
Python patch version and exposes unrelated user/site packages.

The replay archive `runtime-layout.tar.gz` has SHA-256
`dad2f421484eea43c72421b878ff57e648a78594125ecfa787032b979ac509ee`.
Its full `runtime-manifest.json` has SHA-256
`8fb9aead76f9752e930d88ae448912aa813b47e9d9c0ac2ca957698a4221c99b`.
The manifest records 6,384 regular files, interpreter links and 41 Python
packages; the sole additional Python distribution is PyGraphviz 2.0.1.
The copy provenance manifest has SHA-256
`1de354e0f557790eec631bcd95b58e049faa8b3883594f544a705b67dcccd4e0`.
Those private, complete manifests and validation reports remain beside the
runtime archive. As with the existing runtime, replay requires the same Linux
architecture and verified base Python installation.

## Activation contract

Use the verified candidate `venv` as the new results release's `.venv` target.
The results manager prepends that release's `.venv/bin` to its child PATH.
The agent manager retains its existing PATH, and no shared environment is
modified. A release-style `.venv` symlink was exercised: `dot` and every
PyGraphviz native dependency resolved inside the new candidate, without a
dependency on the prior layout environment.

After activation, request a new small-graph presentation and require
`engine=pygraphviz`, `status=optimized`, and no fallback reason. Existing saved
fallback presentations remain unchanged; a successful readiness response alone
does not verify layout. Keep the previous runtime/release for an owned rollback.

## Validation before activation

- Exact baseline file/link comparison: 6,304 checked, zero differences.
- Frozen runtime verification: 6,384 files and 41 packages verified.
- Auth/results and all layout/list/small-graph suites: 71 tests passed under
  the new Linux runtime, including real workers, deterministic geometry,
  parallel records, worker cancellation and deadline handling.
- A separate actual worker through a release-style symlink preserved a
  synthetic three-node/four-edge multigraph and returned optimized PyGraphviz
  geometry without fallback.
- Manager/frozen-runtime regression tests: 15 tests and 13 subtests passed.

These checks use synthetic graphs and mocked service dependencies. They invoke
no inference and create no application jobs. Live API and rendered-browser
acceptance remain required after the coordinated results activation.
