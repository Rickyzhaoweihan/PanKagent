# Live validation: deployment blocked

Candidate: `3fcdb935078e4977dc479199712d3275b530d352`, existing `Ringo` branch.
Date: 2026-09-24. Database: `PanKgraph_08_04`. Model: `claude-sonnet-5`.

The user confirmed that PanKgraph content is public and explicitly authorized
Claude validation and dev deployment. Data-use approval is resolved. **The
candidate did not pass correctness acceptance and was not deployed.**

## Execution and outcomes

The frozen 51-case correctness manifest ran through isolated application
plan → preview → confirm → answer sessions, at concurrency two, with real
Claude, GPU Cypher and Neo4j calls. The harness intentionally disables literature;
terminal `partial` by itself is therefore not a graph failure. Database reference
queries compare full typed memberships, rather than answer text or counts alone.

| Group | Result |
|---|---|
| HPAP donor/sample matrix, including PLN | 15/16 complete memberships matched; ND all-samples failed |
| Current landing-page examples | 13/14 produced graph answers; CFTR pancreas splicing-QTL failed |
| Historical variants | 6/21 matched complete reference memberships; 11 planning failures; 4 other outcomes need scope/evidence review |
| Additional “What is T1D?” canary | Failed at M04 identity-proof validation; no final answer |
| Focused offline request-authority tests | 15 passed |
| Streaming | Every nonempty answer in these runs reconstructed exactly from its stored deltas |

`correctness.json` and `disease-canary.json` retain aggregate run metrics.
Nonempty answers are not automatically marked as supported-answer passes.
The frontend cases do not have reference-ID queries in this harness, so their
`membership_match=false` values are not membership failures.

## Concrete release blockers

1. **Disease definition / M04:** “What is T1D?” failed with
   `E03.ENTITY_CHOICE_UNVERIFIED`. After lookup and repair, the model selected
   the correct disease ID, `MONDO_0005147`, but the retained proof check rejected
   its choice for mention `T1D`. The last lookup used `Type 1 Diabetes`; the final
   plan used `T1D`. This is an identity-evidence binding failure, not absent data.
2. **CFTR landing example / preparation:** “For CFTR, does the T1D GWAS signal
   colocalize with a pancreas splicing QTL?” failed with
   `invalid_property_owner:qtl:Gene.tissue_name`. The plan attached the tissue
   property to a gene rather than its recorded relationship owner; bounded
   repair did not produce an executable plan.
3. **HPAP ND all-samples / retrieval and dependencies:** the donor task acquired
   `HAS_SAMPLE` retrieval and optional tissue expansion. Its materialization was
   truncated, and the dependent sample task became `dependency_unavailable`.
   No complete sample count was produced. The independent reference succeeded.
4. **Variant scope checks:** the stage-3 wording “In Hpap, how many donors have
   a recorded T1D stage of 3?” lost source/stage authorization. Local inspection
   confirmed that `dataset_source_owner` returns `None` for that wording and
   the stage parser does not accept `stage of 3`. Uppercase `NOT` and the `PP`
   portion of `PP.H4` were also treated as required gene mentions in separate
   failed cases. These are actual planner/preparation failures.

Saved prior variant outcomes show improvements in the stage-1 paraphrase,
tissue discovery and donor-to-sample follow-up, but the CFTR sQTL variant and
spleen variant succeeded previously and failed in this candidate run. Those
observed regressions remain unresolved; they were not erased by rerunning until
success.

## Answer and harness review notes

- The ABCC7 query resolved to CFTR and returned supported ductal-cell enrichment
  measurements. Its reference query includes all enriched cell types, whereas
  the actual answer selected the canonical ductal node. The 2-versus-1 mismatch
  needs an explicit subtype-scope expectation; it is not an identity failure.
- The CENPO lead-QTL answer initially described two variants as sharing a
  credible set, then acknowledged their different full identifiers and sizes.
  This contradiction requires answer-quality review; do not count the existence
  of prose as acceptance.
- The PLEKHM1 coloc answer retrieved the linked signals, but its closing phrase
  that colocalization is “evaluated at the lead-SNP level” is unsupported. Different
  lead variants do not establish that interpretation.
- ADCY3 pathway and HLA-DRA comparative answers used explicitly labeled bounded
  annotation overviews. Such partial overview retrieval is distinct from a failed
  count or an unexecuted core query.

## Budget and deployment state

The existing cumulative $10 ledger was reused, never reset:

- Before this run: **$2.662702** settled, zero reserved.
- This turn: **$3.598704** settled for 51 cases plus one canary.
- After this run: **$6.261406** settled, **$0** reserved, **$3.738594** remaining.

Matched performance controls were not run because correctness already blocks
release. Latency/cost acceptance remains pending; no performance improvement is
claimed. Full raw runs, queries, evidence, model responses and audit events remain
in the existing service-owned operations directory, outside Git.

No application code, schema, diagram, runtime configuration or serving state was
changed in this validation turn. No deployment manager stop/start was invoked.
The serving dev release remains
`20260924-schema-supervisor-6d7340d/backend`; production and sibling services were
not modified. Dev, results and health live endpoints returned HTTP 200 during
validation. Their health is not a substitute for the failed correctness gate.

Next release work must repair the concrete failures, rerun affected references
and the required acceptance/performance gates within the remaining authorized
ledger, and only then replace dev through the ownership-aware manager.
