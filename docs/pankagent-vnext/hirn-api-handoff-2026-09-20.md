# HIRN literature API — separate team handoff

Prepared September 21 from the September 20 PanKagent audit. These are observed
failures and requested upstream improvements, not changes already made to HIRN.
This document contains no donor records, credentials or raw service logs.

## Ownership boundary

PanKagent will repair its literature-only routing, inappropriate graph-success
gate, same-session forwarding, error categorization and correlation metadata.
HIRN owns search/refinement, candidate selection, literature synthesis, evidence
relevance, publication/model metadata, and provider usage visibility. A failed
PanKagent graph plan is not evidence that the HIRN API itself is unavailable.

## Relevance reproduction

Exact question in fresh sessions:

> Is PLEKHM1 differentially expressed in any cell type in T1D?

| Occurrence | Agent run | Observed selected literature |
| --- | --- | --- |
| 75 / jsonl-000085 | 9f008493-1a0f-4c36-96d1-a2ab5560c1b4 | Alternative attempt r2-2 discusses PTPN22/IFIH1/TYK2 autoimmune GWAS overlap; the earlier r1-3 alternative reported no evidence. |
| 91 / jsonl-000112 | d78d1ce7-fa03-4c95-bdaa-0c907e4ee58b | Main-mechanism answer discusses KRAB-ZFP/NFKB1 immune-cell background without support for the requested PLEKHM1 question. |

Saved agent question, interpreted plan and results retain the correct gene.
The outbound HIRN payload/refined questions were not retained, so the precise
routing-versus-retrieval/refinement cause is unresolved. This is a relevance
finding, not a claim of fabricated PMIDs. Preserve gene, assay and question scope
through retries/selection; prefer an explicit no-evidence result to an unrelated
fallback. General background must be labeled and must not replace the answer.

## Model, assay and publication scope

Exact authored question:

> What does the available HIRN literature say about beta-cell stress and antigen
> presentation in T1D? Separate human-islet evidence from animal or cell-line
> experiments, link the supporting sources, and distinguish direct observations
> from proposed mechanisms.

The direct HIRN request completed in 36.218 seconds with six retrieval attempts,
two selected perspectives and 27 provider requests (usage estimate $0.11384853).
Its answer did not consistently distinguish human islets from ECN90 cells or
identify a cited preprint. See [human-islet study PMID40684438](https://pubmed.ncbi.nlm.nih.gov/40684438/)
and [ECN90 preprint PMID37745505](https://pubmed.ncbi.nlm.nih.gov/37745505/).
The integrated agent run `75cc62b2-1a4c-4030-ad17-be7d2109fed1` failed before
literature retrieval; that part belongs to PanKagent.

Occurrence 77 (`a05b64d1-1481-4940-947f-b7f0cda21d0b`) used the same PLEKHM1
question. Its autophagy discussion blurred human fixed-tissue observations with
mouse flux experiments in [PMID33515072](https://pubmed.ncbi.nlm.nih.gov/33515072/).
Preserve species, primary tissue versus cell line, assay, intervention and
observational-versus-mechanistic scope in each selected perspective.

Occurrence 15 (`e0720880-8ee2-42dd-8a35-c81698f16090`), “Find T1D effector genes,”
also exposed claim-level citation and publication-version gaps. Link each
substantive claim to its supporting source, label preprints and published
updates, and do not count versions of the same study as independent replication.
Relevant version records: [PMID37886586](https://pubmed.ncbi.nlm.nih.gov/37886586/)
and [published article PMC12477748](https://pmc.ncbi.nlm.nih.gov/articles/PMC12477748/).

Follow-up acceptance question, after a successful literature answer:

> Now restrict that literature summary to evidence from human pancreatic tissue
> or primary human islets. Which earlier claims still have support, and which
> depended on animal or cell-line models? Retain the reference links.

## Error and accounting visibility

Eight pending mini-reranker reservations correlated uniquely with owned-process
timeouts 7.998–7.999 seconds after dispatch. Source behavior cancels reranking
after eight seconds and continues with fusion order. Missing usage responses
left a combined **$0.10979610 upper bound**, retained in the audit ledger. This
is temporal/source correlation, not per-provider-request proof or invoice
reconciliation. No reservation was released or marked free.

Requested upstream additions, negotiated without breaking existing consumers:

- Correlate client request IDs with search/refinement/selection attempts and
  provider request IDs; expose safe diagnostics without raw prompts or secrets.
- Provide typed timeout, rate-limit, budget, unavailable and invalid-response
  errors. Distinguish optional rerank fallback from successful reranking.
- Report actual usage when available, and explicit unknown/cancelled state when
  it is not. Never equate missing usage with zero cost.
- Attach model-system, assay and publication-status metadata to claim/source
  links. Retain a truthful no-evidence outcome when scope-matched support fails.

## Acceptance and evidence access

Replay the exact questions above with their required predecessor turns. Require
scope-matched selected answers or honest no-evidence states, source-supported
model/assay restrictions, linked claims, publication-version clarity, and
traceable typed failures/usage. Graph retrieval success is not literature
acceptance. PanKagent adapter tests should additionally cover compatibility with
older HIRN responses that lack the proposed metadata.

The private audit directory `Research/Reports/pankagent-function-test-2026-09-20/`
contains hash-pinned evidence: `literature-relevance-ordinal75.private.json`,
`direct-hirn-new08-epoch1/review.private.json`, current historical manual reviews,
and `reservation-reconciliation-final-20260920.private.json`. Request a scoped,
sanitized reproduction bundle from the project owner; do not publish raw logs.
This document has not been sent to the HIRN team.
