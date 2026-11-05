# Schema-owned query and formatter semantics

New runs use the `schema-agent-v1` contract in `source/native/pankagent_query`.
Runtime release/manifest authentication remains in the existing query transport.
No graph import, mapping change, inference router or frontend change is included.

## Build and source ownership

`source/native/schema_semantics.json` is the reviewed, versioned semantic overlay.
It retains the deployed interpretation bundle's rules, aliases, clinical/functional
interpretations, provenance and original file hashes. The former formatter's
conditional QTL/donor interpretation text is retained here too. Data-dependent
rules and explicit scope qualifiers live in this file, not core/model prompts.

Build a new immutable schema file with:

```sh
python -m pankagent_query.semantics --schema EXISTING_RELEASE_PACK --semantics schema_semantics.json --output CANDIDATE_PACK
```

Review release identity and the resulting hash; bind the paired config to that
exact pack. The original pack remains available for rollback. Schema tool index
returns endpoints, direction semantics and scope alternatives. Detailed responses
and the final recorded schema-tool handoff carry selected interpretation rules.
New formatter requests use only that handoff and generic presentation instructions.
Legacy saved runs retain their original formatter adapter and remain readable.

## Query contract

New `query` calls require `purpose=requested|diagnostic|context`; pagination uses
`query_id` alone. `select_results` must retain the registered purpose. Diagnostic
queries cannot enter the answer graph. New `finish` calls use prior selections,
not legacy `query_id(s)`. Exact repeated queries reuse their registry entry.
A relaxed same-shape query cannot replace its earlier required conjunction.

Identity candidates include primary identities and matched field/value/namespace.
External accessions cannot be substituted for an observed primary ID. Co-label
fulltext access is allowed only using actual schema label sets and filters the
returned candidates back to the requested label.

Scope families validate declared alternatives at finish, without creating queries.
Explicit source/subtype qualifiers are schema data. Missing coverage requires
additional selected evidence, partial status, or clarification. Joined families require
coverage of their combinations at a shared node, not only marginal coverage. A schema-backed
scope-review response remains within the existing loop; it is not independent
scientific validation. Query validity never establishes semantic correctness.

Saved context binds release, schema hash, prompt version and prompt hash. New
confirmation rejects an older context with HTTP409. Cross-version history retains
only original user questions; old query/answer context must be resolved again.
The formatter receives the original question. Historical GET and formatter
retry are retained. New results expose separate execution, schema, condition,
pagination and truncation checks to the formatter. Interpretation and provenance
are not silently cut to fit; presentation sampling drops whole records with counts.

## Validation

Run `python -m unittest discover -s tests_schema -v` from `source/native`.
The fixtures rename all biological types to test dataset independence and include
alternative relations, separate annotation categories, identity misuse, diagnostics,
context version mismatch, co-label search, and formatter evidence boundaries.
The untouched legacy suite has one documented baseline failure:
`test_declared_unfulfilled_condition_cannot_be_complete`.

Real-model attempts and their failures are retained privately under the deployment
operation. Test results must distinguish actual scope acceptance from nonempty
execution. Live activation requires idle 18864, source hash comparison, private
source/config backup and paired rollback; never restart 8794/8795 or databases.

### 2026-11-05 acceptance

Base Ringo commit: `094a3a8d65d50c65bd4281602e9cb82a3b27a243`.
The isolated branch preserves the retained developer checkout and changes only
agent source, tests, semantic metadata and this handoff.

- 35 schema-contract tests pass. Python compilation and `git diff --check` pass.
- Legacy tests: 36 pass; the unchanged legacy core retains the baseline failure
  named above. Its code is not replaced for historical reads.
- Real 1104 explicit HLA-DRA physical/genetic -> same partner ->
  R-HSA-2132295: both requested branches execute and return empty. Ambiguous
  single-ID calls are rejected; the model corrects them before execution.
- The original broad question retains eight distinct resolved HLA-DRA IDs and
  eight selected branches spanning both interaction types and GO/Reactome/KEGG.
  All branches retain a functional endpoint condition; no diagnostic is selected.
  Coverage is agent-assessed, not independent scientific validation; substring
  searches remain lexical candidate tests rather than ontology closure.
- RPL22L1 -> physical -> TUBB8 -> R-HSA-2132295 returns three nodes and two edges,
  with the same TUBB8 binding. Final candidate run has no recoverable errors.
- A real independent formatter probe renders that connected evidence with the
  schema-owned rules and source distinctions. Existing budget ledger is retained.

Failed intermediate trials (including context overflow and dropped qualifiers)
are retained alongside accepted traces in the private operation, not relabeled
as successes. Deployment and public two-stage acceptance are recorded separately
in `/db/pankagent-vnext-private/operations/schema-agent-20261105/deployment.json`
and `live-*.json`. Rollback restores paired source/configuration, never session,
evidence or budget state. Only the owned dev port 18864 is eligible for restart.
