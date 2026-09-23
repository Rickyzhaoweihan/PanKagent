# Tissue discovery and usable partial evidence

Implemented in 183aa20, b760e0d and fdfd005.

The two saved Stage 1 sample failures were distinct. One misclassified a request
for tissue/cell-type annotations as an unresolved tissue restriction. The other
rejected generated mandatory sample/tissue joins in a donor parent and then
blocked the dependent sample query; neither had usable final sample evidence.

Changes:
- New normalized plans default to the existing partial-independent policy.
  Verified meaningful primary evidence can survive a failed sibling. Normal
  dependency, request-filter, completeness and resource checks remain in force.
- Descriptive tissue questions and demonstratives such as "these samples" do
  not create tissue restrictions. Explicit unknown tissue filters still block.
- Donor-only parents of explicit HAS_SAMPLE children defer generated sample
  conditions to those children while retaining donor cohort predicates.
- A directed donor-to-sample template supports verified typed dependencies
  without requiring anatomy joins. Sample properties carry tissue annotations.
- The domain prompt explains tissue discovery, optional annotation handling,
  and the difference between tissue and cell-type evidence.

Read-only replay of both saved proposals completed donor and sample retrieval.
The final sample IDs matched an independent HPAP + recorded Stage 1 + assay
reference query: four indexed sample records, one scRNA-seq and three
snMultiomics, with retained original tissue annotations. This does not establish
file availability or justify relabeling multiomics as standalone scRNA-seq.

Private replay, reference, deployment and backup evidence:
`/db/pankagent-vnext-private/operations/discovery-gate/`.
Only dev agent 8794 is activated. Formatting Agent output logic is unchanged.
No paid model calls were made for this change. A new browser-rendered final
answer was not replayed; backend retrieval, count membership and annotation
retention were verified against the live graph.
