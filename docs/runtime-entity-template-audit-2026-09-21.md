# Runtime entity and query-template audit — 2026-09-21

## Finding

The ND/healthy failure was not caused by a Cypher template containing a fixed
donor, disease or ontology ID. The bad run did not use a local template. A
semantic resolver treated the negated phrase `not type 1 diabetes` as a positive
T1D mention, added a disease filter, and the resulting two-relation plan fell
back to generated Cypher. The split checks also lost the HPAP source because
source inheritance examined only the generated step wording. Answer context then
hid the retrieved donors' recorded clinical categories, so Stage 3 records could
be mislabeled ND.

The observed total of 10 was therefore not evidence that the cohort was right.
The Stage 3 T1D records and the requested ND/healthy records were different
donor sets whose counts happened to agree.

## Executable template inventory

All executable templates in `pankagent_vnext/query_templates.py` were reviewed.
They describe graph structure only: registered node labels, relationship types,
property slots, operators and parameter names. No gene, variant, disease,
tissue, donor, assay, source, cohort value or node ID is embedded in template
text. Runtime values remain in the separate parameter map.

| Template ID | Structural scope | Required value evidence |
|---|---|---|
| `directed_relation_records` | One registry-approved directed relationship, with all compatible registered endpoint paths retained | immutable-request authorization for every predicate; current entity or category proof where applicable |
| `donor_tissue_same_sample_records` | `donor -HAS_SAMPLE-> sample <-HAS_SAMPLE- anatomy`, forcing donor and tissue predicates onto the same sample | resolved current anatomy plus current donor/sample category proof and immutable-request authorization |
| `verified_gene_region_records` | Gene nodes inside one verified interval | verified coordinate contract plus immutable-request authorization for every interval predicate |
| `verified_variant_dependency_gwas` | Reviewed GWAS relationship restricted by upstream dependency variant IDs | current dependency-node labels plus the base template's request and runtime proofs |

Compiled templates retain value-free `parameter_bindings` metadata: constraint
index, owner, property, operator, proof source, proof kind, graph release and,
when applicable, the runtime inventory digest and immutable-request digest.
Selected evidence also retains the template ID, template digest and schema
digest so a replay can identify the exact structural template and proof snapshot.

## Two independent proof boundaries

A value being present in the graph does not prove that the user requested it,
and a phrase in the request does not prove that it is a current graph value.
Those are separate gates:

1. **Immutable-request authorization.** Every final parameterized constraint
   must have exactly one `request_filter_bindings` record for the same constraint
   index and canonical binding. Its source must be `immutable_user_request`, its
   request SHA-256 must match the trusted original question, and its graph release
   must match the prepared step. Missing, duplicate or stale authorization blocks
   execution.
2. **Current-graph/runtime proof.** Every node `id`/`name` predicate must have a
   unique current-release entity resolution for that exact final constraint.
   Every protected categorical predicate must additionally have exactly one
   `verified_runtime_*` binding tied to the current semantic-inventory digest and
   graph release. This covers all donor categorical fields, donor `t1d_stage`,
   sample `data_source`, and sample `data_modality`.

Ordinary scalar predicates still require immutable-request authorization,
registered owner/property/operator validation, and exact generated-Cypher
equivalence. They are not promoted to entity or category proofs merely because a
planner emitted them. The shared `runtime_binding_errors` execution gate applies
before template, verified-cache and generated-query routes, so falling back to a
model cannot bypass either proof boundary.

## Request interpretation and completeness audit

The resolver now reconstructs filters from the trusted original request after
planning and splitting, rather than treating generated step prose as authority.
Its scope reader is quote-aware and tracks nested parentheses, brackets and
braces. Explicit and implicit property operands are masked before entity and role
extraction. Consequently, text such as a note, description, sample ID, source
URL, HLA value or projected display field cannot silently become a disease,
tissue, assay, source or cohort filter. Connectors inside quoted/property values
do not incorrectly begin a new clinical scope, while a later real clause such as
`and use scRNA-seq` remains visible.

Raw-request completeness checks cover the supported donor and sample property
families rather than only the three diabetes fields. The final prepared scope
must retain each explicit field/operator/value and polarity. Direct donor IDs,
owner-qualified source fields and arbitrary current donor-linked diseases are
also bound from the immutable request. A substring of a longer operand is not
accepted as authorization for the shorter value.

The following cases fail closed instead of broadening the query:

- an unknown or non-unique tissue, assay, donor/sample source, donor identifier,
  donor modifier or donor-linked disease;
- an explicit raw donor/sample predicate that is absent or only partly preserved
  in the final constraints;
- a negative tissue or donor-disease link that the positive-link query contract
  cannot represent;
- ambiguous multi-source, multi-tissue or multi-disease scope that cannot keep
  each value attached to its intended owner/check;
- an assay exclusion that cannot be represented exactly, or a split plan that
  fails to cover every requested positive assay alternative;
- a generated donor, source, tissue, assay, stage or disease filter not traceable
  to the immutable request.

No fuzzy match is substituted for unresolved scope. ND/healthy control wording
is resolved to the unique current `donor.diabetes_type` control category;
recorded `t1d_stage` remains a separate field and never establishes ND, diabetes
type or diagnosis. Dataset-source inventories are owner-separated for donor and
sample values. Disease identities come from the current donor-linked disease
inventory or the normal live entity resolver, never a prompt constant.

## Generated-Cypher predicate guard

The generated route is not trusted merely because the prepared plan was safe.
After tokenization, every comparison against any registered node or relationship
property in `MATCH` maps or `WHERE` must match an exact prepared constraint
alternative with the correct owner, property, operator and value, or a verified
dependency-ID set. This all-property guard also follows simple scalar aliases.
Unsupported expressions, casts, null tests, owner changes and additional
predicates fail closed. The existing required-filter checks independently ensure
that every requested constraint occurs in every `UNION` arm.

This guard is broader than the earlier identity, clinical and measurement
sentinel lists: a model cannot invent a filter on an otherwise ordinary recorded
property and escape validation simply because that property was not clinical.
Every mandatory relationship in generated Cypher must also be present in the
prepared step's relationship contract. The only narrow implicit witness is
`HAS_DONOR` when an explicitly authorized disease constraint must connect its
donor cohort; arbitrary extra joins fail before `EXPLAIN` or retrieval.

## Answer integrity and privacy

The execution ledger computes anonymous full-cohort marginals over all uniquely
retrieved donor nodes for four distinct fields:

- `diabetes_type`
- `derived_diabetes_status`
- `t1d_stage`
- `data_source`

Those full marginals remain private and are evaluated before any outbound
minimization. They are mandatory integrity facts for donor-cohort answers:
contradictory prepared scope, returned classifications outside the requested
cohort, and scalar counts without sufficient donor classification evidence fail
closed. Stage is never substituted for either diabetes classification, and
source remains provenance rather than clinical status. Individual donor IDs are
not included in the marginal summaries.

Only classification fields explicitly requested or used to define the cohort
are copied into synthesis/display context. Privacy is enforced at multiple
boundaries:

- Cypher validation rejects projection of an unrequested protected donor field
  even when it is given an arbitrary alias;
- dynamic donor property lookup, `properties(d)`, and full donor node-map
  projections are rejected when they could expose protected fields;
- recursive output sanitization removes protected classification keys from raw
  rows, donor summaries and answer-block inputs; and
- compact donor nodes omit unrequested classification properties.

The same public-payload sanitizer is used for REST responses, live SSE events
and replayed SSE events, so choosing a different transport cannot expose or
reinterpret a protected classification.

The private ledger is not modified by those output guards, so privacy
minimization cannot weaken the cohort-integrity check.

## Runtime inventory, cache and privacy facts

The public pre-planning entity catalogue is rebuilt from the selected graph and
is release/endpoint/database keyed. Disk and memory snapshots expire after 300
seconds; the remaining in-memory lifetime is derived from the persisted build
time, so loading a nearly expired file does not restart its TTL. A forced rebuild
bypasses the inner semantic cache. Atomic cache writes use mode `0600`.
Identity, content and envelope digests detect wrong-release, stale and partially
edited snapshots; protected file permissions plus live rebuilding, not an
unkeyed digest alone, are the authenticity boundary.

The persisted/model-visible catalogue contains public entity records and
aggregate sample terminology needed for planning, including owner-separated
source values, modalities, valid recorded stage labels and documented assay
capabilities. It deliberately omits the complete donor categorical-value maps
and current donor-linked disease inventory. Those clinical/cohort inventories
remain in the execution-side semantic vocabulary and are used for current-value
proofs without being copied into general model grounding. Neither cache contains
donor/sample rows as entity examples.

## Regression coverage

Static tests scan executable template source and compiled Cypher for embedded
entity and clinical sentinels. Behavioral tests cover the original question;
negation and contrast; quoted, delimited and implicit property operands; direct
IDs; donor/sample property completeness; unknown tissues, assays, sources and
diseases; split assay coverage; wrong-owner and unrequested-filter removal;
changed, ambiguous and stale runtime categories; stale entity/request/inventory
proofs; all-property generated-query injection; projection/map/alias privacy;
cache freshness and clinical-inventory exclusion; full-private versus
request-visible marginals; and answer-level cohort mismatch/scalar-count
rejection.

## Acceptance boundary

This change is source and test validation only. It does not deploy, restart or
otherwise alter a vNext, Results or production service. A separately authorized
deployment must still be followed by a production replay using the exact
original wording. Acceptance evidence must record the resolved constraints,
request and runtime proofs, selected template or generated route, executed
parameters, full private integrity result, request-visible marginals and rendered
answer. The public screenshot remains evidence of the pre-fix deployment until
that replay passes.
