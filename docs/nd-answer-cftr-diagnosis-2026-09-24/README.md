# ND answer fix and CFTR QTL diagnosis

## ND schema and observed failure

The database dictionary already contains the donor property `diabetes_type`, with
all four observed values and frequencies. `Control Without Diabetes` is the positive
ND category; it must not be inferred from not-T1D or `derived_diabetes_status`.
The `cohort_clinical_fields` semantic module states this interpretation explicitly.

Links on Ringo:
- [Database property and examples](../../pankagent_vnext/agent_schemas/packs/pankgraph/database_schema.json#L5605-L5658)
- [ND interpretation](../../pankagent_vnext/agent_schemas/packs/pankgraph/semantic_interpretation.json#L138-L147)
- [Donor record export status](../../pankagent_vnext/agent_schemas/packs/pankgraph/database_schema.json#L6010-L6018)

Full donor record prototypes are currently absent: the dictionary still labels
record export/prototypes `protected` from the earlier export policy. This is a
legacy configuration label, not a new claim that the public PanKgraph data is
sensitive. Property-level aggregate examples are present. Schema export policy
was not changed for this answer-runtime fix.

The saved ND run had already returned every independently checked sample ID.
It started a correct answer but the 40-second graph-and-answer deadline cancelled
writing mid-sentence. A second defect left 865 saved characters versus 860
characters in durable delta events: answer persistence and event insertion were
separate operations and cancellation could fall between them.

## Focused fix

- Writing gets a separate `ANSWER_TIMEOUT` (default 60 seconds, maximum 120).
  The existing retrieval deadlines remain; the outer run deadline is their sum.
- The stored answer prefix and corresponding delta are committed in one SQLite
  transaction, retaining ownership fencing and terminal-state checks.
- Synthesis failures append a replayable warning to the durable prefix and retain
  their `writing_answer` origin (`E10.TIMEOUT`), including at the top level.
- Empty-answer and outer-deadline fallback text also receives a durable delta.
- No formatter prompt, output-generation method, model, query filter, database
  schema, CFTR behavior, or retry budget was changed.

Validation: 103 focused tests passed. The existing unrelated results-service
`test_results_lease_fences_old_payload_and_preserves_completed_components` failure
was reproduced on the initial run and excluded from the final focused selection;
it was not modified or counted as a pass. New fault-injection coverage verifies
transaction rollback, terminal-state fencing, independent answer deadlines, and
exact timeout-stream reconstruction.

One isolated live replay of “How many samples are available from HPAP ND donors?”:

| Check | Result |
|---|---|
| Reference / returned distinct sample IDs | 5,480 / 5,480; exact membership match |
| Retrieval | Complete; donor and sample steps both complete |
| Written answer | 1,359 characters; normal `end_turn`, not truncated |
| Delta reconstruction | Exact match with saved answer |
| Citation IDs | Valid |
| Synthesis error | None |
| Plan/preview / end-to-end | 16.097 / 81.932 seconds |
| Model calls / settled cost | 2 / $0.0957165 |

The harness overall status is `partial` because literature is deliberately
unavailable in this graph-only test. This is not a graph-answer failure. The
answer reports 95 donors with connected samples; the separate donor-only count
is 96. Donors without samples must not be dropped from donor-only counts.

The live replay tested the substantive deadline/transaction fix; the later
empty-answer and outer-error fallback additions passed offline tests. Full
correctness/performance release gates have not been rerun. This candidate is not
deployed and a longer permitted deadline is not a demonstrated latency improvement.

Cumulative validation ledger after this replay: $6.730489 settled, $0.213457
reserved from the earlier timed-out call, $3.056053 remaining from the same $10
allowance. No allowance or reservation was reset.

## CFTR: exact failure, no code changes

Question: “For CFTR, does the T1D GWAS signal colocalize with a pancreas splicing QTL?”

Saved run: `6b94769b-53f4-4cdc-9d7c-d27c05a74474`.
The coloc and GWAS branches completed. The `qtl` branch did not generate or execute
Cypher: `queries=[]`, `generator_attempts=[]`, `retry_eligible=false`.

The planner's tissue constraint was:

```json
{
  "owner_kind": "relationship",
  "owner_role": "part_of_qtl_signal",
  "relationship_type": "PART_OF_QTL_SIGNAL",
  "entity_type": null,
  "property": "tissue_id",
  "operator": "=",
  "value": "UBERON_0001264"
}
```

The preparation record recognized this relationship-property ownership and
retained a verified pancreas identity. But `request_filter_bindings` contained
only the CFTR gene binding, not the tissue binding. The exact originating reason:

```text
missing_request_authorization:1:node.tissue_id
```

Subsequent task validation: `semantic_scope_unresolved`.
Task category: `filter_scope_unavailable`.
Public diagnostic: `E00.UNCLASSIFIED` (too generic; not a Neo4j query failure).

The `node` in the technical reason is a misleading fallback label used when
`entity_type` is null. The actual proposed constraint above is a relationship
constraint, not a requested node property.

Location: M04 preparation tools, before M06 Cypher generation / M08 execution.
The relevant implementation is
[`semantic_registry.py`, `_verified_qtl_schema_derivation` and request binding](../../pankagent_vnext/semantic_registry.py#L2382-L2440),
then [`query_templates.py`, `_request_authorization_binding`](../../pankagent_vnext/query_templates.py#L138-L160).
The specialized tissue authorization expects `schema_bindings` evidence from a
particular tissue-conversion path; the saved direct relationship-property
constraint lacks that evidence. A canonical resolved tissue ID and correct
property ownership alone do not currently satisfy that code path.

Proposed review target (not implemented): carry the verified pancreas identity
through the direct `PART_OF_QTL_SIGNAL.tissue_id` binding path, preserve its
requested tissue scope, and authorize it equivalently to the existing tissue
conversion path. Then retrieve QTL membership and match its recorded credible-set,
source and tissue to the coloc signal annotations. Do not remove the tissue
condition or equate all CFTR QTL records with the coloc signal.

Full run/evidence artifacts remain in service-owned operations storage. Only this
aggregate diagnosis and the summary report are committed. No CFTR fix or deployment
is included in this change.
