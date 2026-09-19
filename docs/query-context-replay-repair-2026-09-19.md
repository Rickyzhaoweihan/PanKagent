# Query context and historical replay repairs

This candidate starts from the active dev agent commit `3b050fb56714`.
It is an application-local implementation. It does not change the pinned
KG standard, shared databases, Cypher service, or production agent.

## Oversized answer input

The shared `ClaudeGateway.prepare_answer` boundary is used by the vNext
agent and conventional results synthesis. Existing normal-size evidence
preparation is preserved. When the normal compact evidence exceeds 75,000
UTF-8 bytes, only node `id`, `type`, `description`, and `source` fields enter
the answer view. The fully serialized question/evidence payload has a separate
100,000-byte check and uses the same fallback if its envelope exceeds that cap.

The fallback preserves step references, requested check categories and retrieval status. It selects node
examples deterministically across steps and types, bounds description/source
text, and reports omitted node/edge/row counts in the application profile.
Stable IDs and types are never shortened: an individually oversized identity
record is omitted whole. Source is restricted to the recorded source name,
version and URL fields. No relationship measurements, rows, derived answer
facts or other node properties are exposed in this mode.
Source URLs and versions are preserved whole or omitted, including list members;
non-text descriptions are omitted. Full-evidence answer rewrites are disabled
for this limited view so they cannot reintroduce withheld measurements.

The versioned application answer bundle instructs the formatter to tell the
user the query is too broad for a detailed answer and suggest a more specific
query. This exception overrides the normal no-suggested-search rule only for
the limited view. Interpretation guidance about omitted measurements is
suppressed; node descriptions cannot justify associations or quantitative
claims. Original query evidence and graph/display/download data are unchanged.

This is a limited-view fallback, not a full context-specific KG standard.
It does not raise retrieval limits or turn an invalid query into valid evidence.
Retrieval truncation and model-context omission remain distinct.
Validated retrieval truncation produces a specific narrowing notice while the
confirmation gate remains closed. Independent service errors retain their own
recovery category and retry behavior, even when another check was truncated.

## QTL lists and genomic regions

The verified dev graph/query stack is Neo4j and Cypher. Planner constraints now
accept native string lists for `IN`/`NOT IN`, with one lossless normalizer shared
by compilation, validation and deterministic query templates. Legacy JSON lists
are accepted. A bare comma string is split only against a complete, owner-bound
recorded vocabulary; unresolved values require repair or clarification.
Relationship tissue filters must remain on their recorded relationship owner.
An ownerless semantic `cell_type` constraint can be bound to an anatomical
endpoint ID only when the raw request uniquely grounds a verified cell, all
selected release paths give it the same endpoint role, and the alias is not a
real stored field on those paths. Explicit owners, raw-property requests,
tissues and ambiguous identities do not receive this repair. This covers a
field-ownership failure observed during a fresh browser region regression.

Explicit genomic intervals are bound to a complete public Gene coordinate
aggregate for the verified graph release. Compilation retains chromosome,
assembly, both endpoints and overlap/containment semantics on the same Gene
variable. A unique fully covered release assembly can supply a disclosed
default; mismatched builds, unknown coordinate conventions, incomplete bounds
and ambiguous gene roles fail closed. No coordinate liftover is performed.
Region templates are validated against the same scope contract, including each
UNION branch. Compiler source identity participates in the plan cache key.

Named locus labels are not an exhaustive gene set. A locus-only collection
request must obtain explicit chromosome and interval bounds. Contextual words
such as output IDs, Mb units, top-N ranking and the preposition "for" are not
grounded as gene symbols merely because similarly named genes exist.

## Regression scope

Synthetic tests cover ordinary measurement forwarding, oversized mixed evidence,
non-ASCII byte accounting, the final JSON envelope, immutable evidence,
whole-ID omission, rare node types, failed/truncated step disclosure and pinned
prompt hashes. Saved historical evidence is replayed privately and is not
committed to this repository. ssGSEA execution is excluded from this repair's
acceptance scope as requested; no ssGSEA tool is added.

Validation includes synthetic unit/integration tests, private saved-evidence
replay, and bounded real Neo4j read comparisons against independent parameterized
queries. These checks do not establish final model wording or browser acceptance.

Live deployment and post-change inference acceptance are recorded separately
after they occur; this implementation report does not claim either is complete.
