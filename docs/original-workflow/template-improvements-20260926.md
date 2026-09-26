# Original workflow: template improvements, 2026-09-26

The opt-in competing workflow has been removed from runtime code: candidate
coordination, structural assistance, preview replacement, confirmation hooks,
configuration flag and feature-specific tests. The original execution path is
restored. Historical benchmark reports remain; their paired live runner now
fails immediately and directs reproduction to commit d274133. Offline scoring
still works. No service was deployed or restarted.

## Changes after restoring the original workflow

- Annotation template selection no longer silently restricts ordinary GO/pathway
  requests to ten edges. Full retrieval remains subject to existing hard limits;
  explicit ranking/limit requests and bounded paths keep their existing handling.
- Complete FUNCTION_ANNOTATION templates cover heterogeneous pathway targets
  without requiring a single target label.
- A direct node template supports a verified single node population, without
  adding a sample/disease join that the question did not request. Identity
  resolution, immutable request authorization and current inventory proofs remain
  required. Ambiguous owners, unbounded scans and donor-age shortcuts fall back.
- Canonical query-pattern guidance clarifies annotation completeness, GWAS edge
  statistics, donor-property ownership, same-partner annotation paths and
  independent assay sets. The existing legacy snapshot mirrors that canonical
  library. No new KG authority or concrete entity IDs were added to templates.

## Evidence and limits

Read-only replay used the original saved plans from workflow57 run1, changing
only annotation selection through the updated template code. Queries ran against
PanKgraph_08_04 through the protected service environment; no model was called.
Core membership was compared by node ID and relationship type/start/end ID
against tests_vnext/fixtures/acceptance/workflow57.json.

| Question | Nodes | Edges | Missing core nodes/edges | Truncated | Read time |
|---|---:|---:|---|---|---:|
| Q40 CTLA4 GO annotations | 23 | 22 | 0 / 0 | No | 0.053 s |
| Q41 PTPN22 GO annotations | 58 | 57 | 0 / 0 | No | 0.005 s |

These are template retrieval checks, not fresh full-agent success or latency
scores. Added model cost: $0. More annotations can increase formatter context;
no claim of lower total query cost is made.

Focused regression suite: 268 passed, covering templates, runtime binding
proofs, annotation selection, bounded paths, schema packs, original runtime and
historical benchmark scoring.

Broader diagnostic suite: 2777 passed, 44 skipped, 231 subtests passed, 26 failed.
One failure was the template-version assertion and was updated, then passed in
the focused suite. Nineteen lifecycle/grounding/persistence failures were
reproduced on unchanged d274133 in the same isolated environment (169 other
baseline tests passed). Six other failures involved absent git/history or
uncopied deployment support files in the isolated test copy. Therefore the full
suite is not reported green. Unrelated baseline behavior was not changed.

## Remaining failure groups

- Q38/Q39 annotation scopes: templates now support complete retrieval, but an
  invalid or missing planner task still prevents them from being reached.
- Q42 gene overview and Q51/Q53/Q54 donor populations: direct node templates and
  ownership guidance help valid prepared tasks; end-to-end recovery is unproven.
- Q35/Q37 interaction plus annotation: pattern guidance preserves partner roles;
  wrong task bindings or failed planning still need upstream repair.
- Q50/Q56 assay comparisons: independent-set guidance added; planning success
  remains to be retested.
- Q07/Q09 statistics interpretation and planning; Q26/Q31 missing cell subtype
  resolution; Q19 activity-to-OCR linkage: not established fixed by this patch.
- Q12 coloc lead nodes must be materialized from the actual coloc provenance,
  rather than inferred from independent QTL/GWAS memberships.
- Q17 undefined OCR scope and Q57 unresolved nPAP meaning need clarification,
  rather than guessing a scope or data-source mapping.
- Q11 ranking and Q18/Q33 numerical summary errors occurred after useful
  retrieval. Query templates alone cannot ensure correct final prose.

## Suggested structure changes — not implemented

1. M04/M05 should produce one explicit task contract: focus and partner roles,
   verified entities, predicate owners, evidence scope and required joins.
   Repair that contract when diagnostics identify a wrong role or missing filter.
2. M06 should compile supported templates first, using the GPU writer only for
   unsupported or invalid structures. Keep one bounded repair allowance rather
   than racing and comparing independently interpreted scopes.
3. M08/M09 should calculate rankings, counts, comparisons and set operations from
   full backend records. Send these authoritative facts plus evidence references
   to the formatter; do not ask it to infer global statistics from sampled rows.
4. Treat required membership, optional annotations and retrieval completeness
   separately. Preserve useful independent branches while identifying precisely
   which requested part is still unanswered.

Priority: task-contract repair, then deterministic ranking/count summaries.
Neither requires reinstating competing candidates.
