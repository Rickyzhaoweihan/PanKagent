# Independent evidence and bounded annotation overviews

A truncated branch previously prevented confirmation even when independent checks
had verified evidence. Large synthesis input also switched every check to node
identity only. Neither behavior respected an independent multi-category question.

Opted-in partial investigations now retain terminal truncations alongside failed
checks without rerunning them at confirmation. Successful independent checks
remain usable. Missing checks are disclosed; their records are excluded from
synthesis but retained in the audit. Dependency verification, exact population
checks, cancellation, scope validation and the global materialization cap remain.

For ordinary annotation overviews, FUNCTION_ANNOTATION and ASSOCIATED_WITH_GO each
select at most 10 relationships before aggregation, ordered by stable identifiers.
This is an illustrative selection, not a biological ranking or total count. The
original user question controls the exception: explicit all/count/proportion or
selection/ranking requests retain their requested semantics. Other evidence types
retain their existing query contracts. Every bounded template still passes normal
constraint validation and EXPLAIN. A LIMIT applied only after collect is rejected.

Synthesis reduces the largest branch first, retaining relationships where reduced
context fits. Only branches that still cannot fit become node identity only.
Citations keep their original step numbers. Bundle 1.9.3 explains mixed evidence
modes and selected annotations without implying missing biological evidence.

The retrieval materialization cap (currently 2 MB) is distinct from the 75 KB
compact evidence target, 100 KB final answer-input envelope, and paid inference
budget. Raising a limit is not part of this fix. Existing stored answers are not
rewritten.
