# Model-owned query decisions (2026-11-06)

The `model-owned-v2` contract replaces the scope-policy guards introduced on
2026-11-05. The model owns interpretation, candidate choice, search revision,
evidence selection, and answer assessment. Tools provide schema, execution,
identity/provenance and retrieval facts. Schema checks remain technical checks,
not scientific validation.

Removed: mandatory all-candidate IN filters, lexical source-family routing,
Cartesian source coverage, earlier-query relaxation bans, qualifying-ID relevance
rules, prefix exclusion, forced second finish, and permanent diagnostic exclusion.
Schema source descriptions remain descriptive, not mandatory search recipes.

`query.purpose` records the initial working purpose. `select_results` accepts a
new purpose and optional rationale without rewriting the executed query. Replace
selection works in enrichment too; old graph records are not silently pinned.
The exact execution cache is independent of working purpose. `finish.status` is
model-assessed and separate from `retrieval_status` and retrieval checks.

`inspect_result` accepts only graph kind, query ID, offset and limit <=30.
`inspect_evidence` accepts exact owned object/table arguments; without a ref it
lists the query's owned descriptors. Illegal fields receive concrete feedback.
Schema index documents case-sensitive literal string operators. No automatic
filter rewriting, identity merging or graph mapping is added.

Only confirmed identity-field conflicts are rejected by identity validation.
Multiple candidates, source-inventory provenance and question keywords do not
constitute a technical rejection. Directed endpoint validation, scalar types,
read-only transport and exact evidence ownership remain enforced.

Deployment configuration supports `agent_max_model_calls` (default 48) and
`agent_deadline_s` (default 300). Exhaustion is an operational interruption with
retained checkpoints, never a model-declared scientific failure or empty result.
Explicit cancellation remains available. Stage events reflect actual tools.

Outgoing context compacts old tool outputs and, only when necessary, removes
whole old exchanges from the model input with an explicit recovery notice. Full
audit messages and evidence remain intact. `list_results` exposes retained query
handles, exact patterns, pagination and selections; graph/evidence readers recover
records. Original questions and complete recent tool-call/result pairs remain.
This is a context resource policy, not scientific selection or a finish decision.

New formatter requests receive the original question, selected graph, selection
rationales, model assessment, independent retrieval checks and versioned schema
interpretation. The formatter is instructed to correct assessment/evidence
conflicts. Historical schema-agent-v1 uses its schema formatter path; older
saved results retain the legacy adapter. Cross-version execution context is
rejected while historical GETs remain readable.

## Validation

Run `python -m unittest discover -s tests_schema -q` from `source/native`.
42 focused tests cover renamed types, model candidate choices and selection
revision, technical identity conflicts, independent branches, source alternatives,
reader contracts, source rules, same-partner compilation, context identity,
cache reuse, resource interruption and separate answer/retrieval status.
The unchanged legacy suite has 36 passes and its previously documented failure
`test_declared_unfulfilled_condition_cannot_be_complete`.

Real-model acceptance artifacts stay private under
`/db/pankagent-vnext-private/operations/model-owned-20261106/`.
Early candidate failures are retained, including an overlong planner summary
with invented pathway names; it is not scientific acceptance. Generic concise
fact-grounded finish instructions replaced that behavior, without entity-specific
prompt cases or semantic rejection scripts. The independent formatter corrected
the preserved bad summary against actual graph records.

A controlled, renamed-schema recovery fixture supplies two recorded initial
queries: a positive membership and a case-sensitive empty substring search.
Subsequent actions are from the real model. It selects the prior diagnostic as
requested evidence with a rationale and answers from the actual membership.
These initial controlled actions are explicitly marked in the trace.

The paired code/schema/config rollback never restores session or budget state.
Only the owned dev 18864 process is eligible for restart; production, 8794/8795,
graph data and frontend design are outside this change.

## Recorded candidate acceptance and limits

Final candidate schema: `a1fe01f7ce500ae76f855ed7472755ccedd9a3930acd4b6dda929cced7eee1b5`.
Prompt: `268ca4c40c403fc330d8da2908f111bcce061f93d42851088ac154cddc2900ce`.
Query-core bundle: `b8b6534805777cb9987877172aa94aa9e8447cfc5a761edc5d171b7889238271`.
Deployed source commit: `5e74054d557295ec86d5f988b555815b8e9b36f9`.

- Renamed-schema recovery: real model chose a revised search after the controlled
  case-sensitive miss, returned the actual membership, and selected the earlier
  diagnostic as context with a rationale. No tool errors. The first two actions
  are fixture setup; three subsequent model calls are real inference.
- Real connected positive: prepare used six model calls (one correctly recovered
  misspelled `selection` argument); enrichment used two, without errors. Final
  graph has three nodes and two edges joined at the same TUBB8 identity. Independent
  formatting preserved BioGRID record 3893708 and Reactome R-HSA-2132295.
- Real HLA-DRA question: both phases and formatting completed. Prepare used 26
  calls / four technical errors; enrichment six calls / two technical errors.
  The final answer retained the actual MHC class II antigen presentation pathway,
  corrected edge-count/entity-count confusion and did not substitute annotation
  categories for interaction partners. The model omitted interaction searches
  from its final selection and did not retain T1D-specific evidence; GO pagination
  remained incomplete. The formatter disclosed these limits. This is **not**
  acceptance of exhaustive scientific coverage or error-free model planning.
- A prior long-history candidate failed its context envelope in enrichment.
  Replaying that exact saved context with the final compactor reduced 393906
  bytes to 87357 without deleting audit messages or queries. A separate unit
  case verifies whole-exchange windowing and valid tool-call/result pairing.
- Earlier saved 0804 results contained KEGG hsa_04612/hsa_04940 and physical
  interaction records. Live 1104 direct read confirmed HLA-DRA membership in
  Reactome R-HSA-2132295 across eight stored gene identities and returned no
  physical/genetic incident records. These are release-specific observations;
  this change does not repair or reinterpret graph-build omissions.

Deployment observed an idle owned dev service, compared source hashes, backed up
all replaced files and paired config, and restarted only port 18864. Three saved
historical run files retained identical SHA256 hashes. Public historical reads
continued returning their prior completed/partial states. Paired rollback is
`/db/pankagent-vnext-private/operations/model-owned-20261106/deployment/rollback/restore.py`.

## Public dev readback

- [Original HLA-DRA question](https://dev.pankgraph.org/result-new2?provider=vnext&run_id=simple-qwen-bbd3db26372b4f4e82bb1088ae3514f4&session_id=0ea19807-0e96-44c2-88b7-cf3b93361587): **partial**, with actual Reactome antigen-presentation memberships and explicit partner/T1D coverage limits. Prepare: 92.201 s, 10 model calls, two recovered technical errors. Enrichment: 30.397 s, two model calls, no errors. Final formatter and result publication completed.
- [Connected positive](https://dev.pankgraph.org/result-new2?provider=vnext&run_id=simple-qwen-52ad8e91a5a347c6aa86748b4efa1dfb&session_id=784e9109-4343-4fed-9cad-c69fd3c29c3f): **completed**, three nodes / two edges, shared TUBB8 identity preserved. Prepare: four model calls, no errors. Enrichment: four model calls, no errors.
- Live prepare/enrichment request files exactly match their respective general system instructions. Neither contains the audited biological type/source/entity literals. This checks actual outbound messages, not just source text.
- Public terminal events were read back for both runs. Source hashes match the deployment manifest. Historical saved-run hashes remain unchanged after live testing.

This delivery changes agent/tool responsibilities and technical recovery. It does
not establish that every model-planned search is exhaustive or that all model
intermediate statements are correct. The observed public HLA-DRA run remains
partial; this limitation is explicit rather than forced into a success claim.
