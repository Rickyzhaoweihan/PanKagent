# Schema-agent implementation and dev acceptance — 2026-11-05

## Version and deployment boundary

- Base: Ringo `094a3a8d65d50c65bd4281602e9cb82a3b27a243`.
- Isolated branch: `codex/schema-agent-20261105`; retained developer checkout untouched.
- Deployed source: `658665e4cf00803e3f4cbfe47529a29d4d6ffaa5`.
- Release: `PanKgraph_1104` (graph/evidence data unchanged).
- Schema SHA256: `fc13611ce35fc0f8c8450995030d56a5d6e673559676a16cffa55a99a8654edf`.
- Contract versions: `schema-agent-v1`, `schema-semantics-v1`.
- Only the owned dev agent at loopback 18864 was restarted. No frontend design,
  graph data, BioGRID build, production service, or 8794/8795 service was changed.
- Original source/config and each subsequent paired rollback are private under
  `/db/pankagent-vnext-private/operations/schema-agent-20261105*/rollback/`.
  The unsuffixed operation restores the pre-task pair; suffixed operations restore
  the immediately preceding pair. Rollback scripts do not restore run/budget state.
  Restore procedures are retained and inspected; rollback was not exercised live.

## Tests and exact evidence

35 new contract tests pass, including renamed synthetic types, physical/genetic
alternatives, pathway-only retrieval, GO/pathway category separation, joined
combination coverage, same-partner compilation, external ID misuse, ambiguous
external/name candidates, diagnostic exclusion, purpose promotion rejection,
query/resolver reuse, stale seed/history, incomplete conditions and formatter state.
Python compilation and whitespace validation pass.

The untouched legacy suite has 36 passing tests and one pre-existing failure:
`test_declared_unfulfilled_condition_cannot_be_complete` in the legacy core. The
new core's corresponding incomplete-condition test passes.

The user's historical run `simple-qwen-7ac40531cd1d45feb83de36d09275d31` remained
byte-equivalent after canonical JSON serialization via the public API, with hash
`f24db16918e743e26a8b8bc2186c829d8d7edbef808bd4d174f684e5864119b0`.
An isolated deployed-runtime check rejected stale confirmation with HTTP409
without mutating its synthetic run.

Public two-stage acceptance on revision `af47724`:

- [Connected positive](https://dev.pankgraph.org/result-new2?provider=vnext&run_id=simple-qwen-8276f7350da64f62892311f52f7ac1d6&session_id=797b1ae2-f0a2-4190-b1cd-b2bbeb69c826): completed, 3 nodes/2 edges, RPL22L1 — physical interaction — TUBB8 — membership — Reactome R-HSA-2132295. Both links use the same stored TUBB8 ID. Enrichment and independent formatting completed.
- [Explicit HLA-DRA metapath](https://dev.pankgraph.org/result-new2?provider=vnext&run_id=simple-qwen-8fcb4f589bee4d85a99a906fc50a6430&session_id=e7505852-cf5c-4c9e-ad56-b07aaccbfef2): completed with empty evidence, both physical and genetic same-partner/pathway conjunctions. Formatter distinguishes successful execution/schema checks from independent semantic validation and biological absence.

The later category-only schema revision was checked against the saved real
positive, explicit HLA-DRA and broad-question query registries without replaying
retrieval. Existing presentation APIs returned the positive graph with 3 nodes,
2 edges, available layout, and non-applicable optional resources. Its SSE stream
included the completed terminal event. No browser-render acceptance is claimed.

The final `ead8920` private candidate reran the exact original question and selected
six complete requested branches (physical/genetic × GO/Reactome/KEGG), each with
all eight resolved HLA-DRA node IDs and a qualifying endpoint filter. Diagnostics
were excluded. The result was empty. Source-specific lexical filters are recorded
in the trace; these checks do not establish exhaustive biological concept closure.

## Preserved failures and limitations

Intermediate failures are retained, including context overflow, qualifier loss,
arbitrary single-ID attempts, a model that incorrectly ANDed candidate nodes, and
a public run that stopped after an empty prefix and correctly reported partial.
They are not counted as successful acceptance. The final generic planning rule
requires complete conjunctions even after empty diagnostic prefixes. Candidate
IDs are expressed by one node variable with an IN filter, preserving separate rows.

Condition coverage remains explicitly agent-assessed. Schema validation establishes
structural compatibility, not independent scientific correctness. Full schema,
query/formatter traces, hashes and selected evidence are stored privately in the
operation manifests; no tokens, graph dumps, or patient-level data are committed.

## Model context envelope

The full original-question public run on `ead8920` passed planning but exceeded
the model's 65,536-token limit during enrichment. `4b4eb67` retains the complete
conversation, tool arguments and evidence in the run. Only outgoing older tool
result payloads are replaced by explicit compact records containing status,
query handles, hashes and recovery tool calls. The final formatter's schema rules
remain intact. Reviewed scope is retained across same-version phases.

The exact interrupted run's approved seed was tested with the correction: enrichment
completed with six selected requested queries, two model calls and zero recoverable
errors. Outgoing message bytes fell from 216,697 to 77,291 on its first call and
from 232,582 to 85,356 on its second. No graph query was replayed.

## Qualifying-identity provenance

A subsequent public run on `4b4eb67` completed operationally, but claim-level
inspection rejected its KEGG binding: MSIGDB_HS_M47487 is named
`KEGG_MEDICUS_REFERENCE_KEAP1_NRF2_SIGNALING_PATHWAY`, not antigen presentation.
That run is explicitly not a scientific acceptance success.

`92a631e` rejects a qualifying endpoint ID unless it was supplied by the user,
resolved from a concept rather than merely looked up by its ID, or retrieved by
a qualifying name/property filter. Unfiltered source inventories cannot ground
qualifiers. Scope review exposes the actual observed bound entity names alongside
IDs and compares them with the original question. The exact bad binding is now
rejected in a replay of its validation inputs; the valid connected positive remains
accepted. The prompt hash now covers tool definitions and scope-review instructions
as well as both phase instructions. No biological name was added to core logic.

## Completion coverage guard

A public run on `92a631e` still selected only interaction prefixes. This is retained
as a coverage failure, even though its formatter candidly described the limitation.
Final validation now requires literally user-named resolved concepts to remain
bound in requested selections; union coverage permits alternative branches. A
selected one-edge prefix alongside its stricter joined query is rejected. The
exact prefix-only trace is rejected for its unbound user concept; the six-branch
trace passes. Explicit user IDs require identifier boundaries, and context identity
also includes the query-core hash, preventing reuse across validator changes.

## Final observed outcome

[Exact original question](https://dev.pankgraph.org/result-new2?provider=vnext&run_id=simple-qwen-51972bce06f3487aa73add3d1a1ae35a&session_id=bce56f78-da2c-4759-9e3f-990948b9a5ce)
completed planning, retrieval, enrichment and independent formatting on `394c19d`.
Its honest final status is **partial**, with no execution/formatter errors:

- Six selected requested patterns each contain both edges through the same partner,
  preserving all eight HLA-DRA primary IDs; no diagnostic/prefix is selected.
- Physical and genetic alternatives were tested against GO:0019882, the Reactome
  candidate set R-HSA-2132295 / R-HSA-1236975 / R-HSA-983169, and KEGG names
  containing `antigen`. All six queries returned zero rows with complete pagination.
- Observed Reactome names were respectively MHC class II antigen presentation,
  Antigen processing-Cross presentation, and Class I MHC mediated antigen processing
  & presentation. No unrelated KEGG ID was substituted.
- A KEGG antigen-presentation concept could not be bound. This remains incomplete
  semantic coverage, not proof of biological absence or exhaustive pathway coverage.

The final one-line state fix in `658665e` propagates inherited unfulfilled conditions
into `retrieval_checks.condition_coverage=incomplete`. It was checked using the
actual saved handoff without rerunning retrieval or changing its graph; the linked
historical run was not rewritten. All final scope/identity guards were also replayed
on the connected positive, preserving 3 nodes/2 edges. No new model replay was
necessary for this status-only correction.

Final deployed identity:

- Prompt SHA256: `478b0097261a82cb47fefd656d78a3df5bc9c0f707d1cf58f7c600e5d0051547`.
- Query-core SHA256: `13396e013f0f3ec2dd387f59902a815bd943babaf08b7710249899e561d2d2f1`.
- Schema SHA256: `fc13611ce35fc0f8c8450995030d56a5d6e673559676a16cffa55a99a8654edf`.

Final public readback retained the original user run exactly and served the latest
run's partial terminal SSE event. Deployed file hashes match the deployment
manifest. The final manifest, paired rollback and readback are in
`/db/pankagent-vnext-private/operations/schema-agent-20261105-r9/`. All versions'
rollback inventories include every introduced file. Logs/backups remain private;
no state or budget database was restored or reset.
