# Dev planning identity admission repair

Live dev acceptance exposed two planning failures before execution. The planner
selected the release-supported `Gene.hgnc_symbol=ADCY3`, but identity admission
only recognizes canonical ID/name fields. A subsequent explicit-ID request
mistook the lowercase quantifier `all` for the gene BCR's recorded alias `ALL`.
Both requests failed safely; neither is evidence of zero biological matches.

The public metadata inventory now retains the actual `hgnc_symbol` field on Gene
records and candidates. Only positive equality/set predicates whose values
uniquely match that field on fully resolved requested Gene candidates compile to
the corresponding verified IDs. Both catalog completeness and primary-symbol
uniqueness across the entire catalog are required; a single selected display-name
candidate cannot hide another gene with the same primary symbol. Historic aliases, display names alone, missing
field provenance, negative predicates, ambiguous identities and explicit raw
`hgnc_symbol` requests remain unchanged. The existing scope guards still require
all requested anchors and every independent evidence category. Compilation keeps
the original predicate and canonical binding in the audit.

The existing generic-word filter now includes `all`, `any` and `every`. Typed gene
requests and explicit uppercase symbols retain recorded aliases. This adds no
gene-specific workaround and changes no scientific relationship or result data.

Deployment must warm the new `grounding-inventory-5` metadata inventory before
admission checks; the old inventory cannot prove primary-symbol field values.
`preplanning-grounding-6` and `preplanning-property-owners-v5`, together with source
digests, invalidate prior planning cache identities. No provider budget, stored
answer or shared database needs to be reset or rewritten.

The graph contract now fingerprints the grounding inventory, grounder, planning
compiler and scope guard implementations. Existing preview reuse identities
therefore change even when a saved plan's fields happen to remain identical.
Historical results remain readable with the existing rerun advisory; reading
does not change stored results or invoke providers. Confirming an obsolete
preview is rejected by the existing revalidation gate. Deployment must preserve
the state databases and budget ledger; no history migration or deletion is needed.

Targeted regression tests are in `tests_vnext/test_dev_planning_identity.py`.
They cover the two observed request shapes, source-field provenance, wrong and
ambiguous identities, negative/set operators, raw fields, same-type interaction
endpoints, independent QTL scope, and intentionally requested word-like aliases.
