# Common caveats — version 2

Write for a biologist who does not know the database schema. Choose zero to two relevant caveats below; do not force a caveat when it adds no useful information. Adapt placeholders only from verified metadata. Never hide an actual retrieval failure, unresolved constraint or material scientific limitation just because it is absent from this list.

## data_currency
Use when the age of a source matters to the question and its date is recorded.
"This result uses data from {recorded source date} and may not include later updates. The PanKgraph team is preparing the next release."
Do not use July 18 for every dataset, infer a date/year from a release identifier, or conflate source generation, retrieval and graph release dates. If no source date is available, omit the date. The next-release statement is project-maintainer guidance, not a measured result.

## local_cell_type
Use for a verified study-specific subtype that extends a standard cell-type identifier, such as CL_0002079_MUC5B.
"MUC5B+ ductal cells are a more detailed study label within the ductal-cell category. This subtype does not yet have its own public ontology term; the PanKgraph team is working with ontology standards teams to improve its representation."
Use the appropriate human-readable subtype. Do not infer local status solely from an underscore: standard CL and UBERON IDs also contain underscores. This is a terminology limitation, not evidence that the measured cell population is invalid.

## source_access
Use when discussing access to raw data or when access restrictions/file availability matter.
"Some raw data may require permission from the original data team. Check the source's access instructions."
If restrictions are explicitly recorded, identify them. If file availability has not been verified, say "Sample records are available here; access to the underlying files still needs to be checked." Do not claim that every source is restricted or that a listed assay guarantees a downloadable file.

## enrichment_not_marker
Use when marker status is asked about or could otherwise be confused with enrichment, unless an actual marker annotation has also been retrieved.
"Enrichment means higher expression relative to the other cell types in the source analysis. It does not by itself establish an annotated marker-gene relationship."
When a marker annotation exists, state that separate source-attributed evidence instead of suggesting it is missing.

## Confidence and plain language
State validated findings directly and confidently within their recorded condition and comparison. Do not weaken them because the response shows only a few records.
One-versus-rest is one cell type versus the remaining cell types in the source analysis. A query or model excerpt returning only ductal and MUC5B+ ductal records does not change that original statistical comparison into a comparison of those two populations. It is not automatically every cell type in the pancreas or genome-wide exclusivity. rank_in_cell_type ranks genes within a cell type, not cell types for a gene.
Keep engineering details (context_sampled, context stubs, collection omissions, node/edge counts, internal field names) out of scientific caveats. Do not derive browser display counts from a model excerpt. A real retrieval truncation still needs a brief plain-language limitation.
Prefer "36 pancreatic lymph-node samples include an RNA measurement from a multiome assay [G1]" over "36 Sample_node records with data_modality=snMultiomics are linked via HAS_SAMPLE edges." Retain the recorded assay label in a table when useful, and retain exact identifiers in evidence/source details rather than crowding the opening sentence.

## Verified database coverage
Use the per-check evidence_coverage supplied before model excerpting. A completed search across all matching cell types can return only ductal and MUC5B+ ductal entries because those are the recorded matches. State: “PanKgraph records CFTR enrichment in these ductal populations under the recorded condition; no additional matching enrichment records were found in this release.” Adapt the gene, category and scope only from verified evidence. Do not describe the statistical comparison as restricted to the returned entries. For a query restricted to a named cell type, only claim absence within that checked scope. Unknown historical coverage or failed/truncated retrieval does not permit an exhaustive-absence claim.

A complete zero-match search is a useful database answer, not an execution failure: “PanKgraph has no matching colocalization record for this gene, signal and disease in the checked release.” This does not establish that the biological relationship cannot occur. Never use missing records to erase measured enrichment, to infer exclusive expression, or to infer an absent marker annotation unless the marker category was separately checked.

## Signal linkage for colocalization
Recorded colocalization is primary evidence. Use coloc_linkage and its supporting references to identify exact GWAS/QTL signal matches. Shared gene or disease membership alone does not establish a match. Distinguish a requested variant recorded as a non-lead credible-set member from a lead variant of another signal. An empty separate QTL/GWAS lookup does not remove a recorded colocalization finding. Report unsupported linkage as not verified, not as proof that colocalization is absent. Repeated metadata for the same colocalization records is not independent evidence.
