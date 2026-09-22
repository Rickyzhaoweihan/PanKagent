# HIRN literature API — separate team handoff

Prepared September 21 from the September 20 PanKagent audit. These are observed
failures and requested upstream improvements, not changes already made to HIRN.
This document contains no donor records, credentials or raw service logs.

## Ownership boundary

PanKagent owns the repairs to literature-only routing, the inappropriate graph-success
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

## September 21 PanKagent integration retest

The isolated backend candidate `d9226832939708511b3773a65185e9a0ff413b9c`
completed the following requests as confirmable **literature-only** plans, with
zero invented graph steps:

| Case | Run ID | Adapter observation |
| --- | --- | --- |
| Beta-cell stress / antigen presentation | `e0f1a487-e661-47f7-b3cc-2f0075b7a20d` | Complete upstream response; fresh-session history hash retained. |
| Viral infection / conflicting evidence | `f0f7b21d-6d12-4bf1-8f19-e39e1db830a4` | Complete upstream response; fresh-session history hash retained. |
| Restrict previous answer to human tissue/islets | `19dac963-dbdb-45a8-ba75-50fb0b204b04` | Complete response; one predecessor turn forwarded and a distinct history hash retained. |

These demonstrate repaired PanKagent routing and session forwarding, not
independent validation of the scientific claims selected by HIRN. The existing
`/stream` body remains compatible; client-side request IDs and question/history
hashes do not prove what happened inside upstream refinement or ranking.

The new test campaign separately retained $0.01373880 with missing usage after
this epoch. It was not released or charged to the prior campaign. A separate
admission interruption occurred when concurrent worst-case output reservations
could not fit the candidate phase cap; that is a test-budget interruption, not
proof of a HIRN retrieval defect. Audit-only dispatch concurrency was subsequently
bounded without changing upstream prompts, retrieval code or provider request
bodies. These runs should not be used as production latency measurements.

### IFIH1 partial-graph independence and remaining citation gap

Exact question:

> What PanKgraph and HIRN evidence connects IFIH1 to T1D and beta-cell antiviral
> responses? Separate genetic association, expression context and experimental
> mechanism, and state which links remain hypotheses.

Candidate `62f6ba3aa32b3c1c40ce414e9cba0161e42e78ef` run
`d56bb2ad-420b-4c68-a318-3ea49be76b81` retained graph evidence and reported a
literature timeout at the original 60-second agent deadline. Audit-only provider
serialization contributed to timing; this does not establish upstream latency
under normal dispatch. With the isolated agent/wrapper deadline extended to
180 seconds, run `cfba5713-646c-4867-bd65-0b3782064354` returned complete
literature while the graph correctly remained partial. Production defaults were
not changed.

In the latter run, selected alternative `r1-3` adds a beta-cell Adar knockout
mouse / Ifih1 disruption claim without an attached claim-level citation. The
only reference listed for that selected perspective is PMID38914291. The client
correctly labels its citation check as reference-linkage only, not verification
of scientific support. The upstream owner should attach the supporting source
to that claim or remove/qualify it. Alternative `r2-2` includes PMID40027743 with
`journal: bioRxiv`; the prose does not identify its preprint status. These are
observed response-metadata/citation gaps, not an independent adjudication of
the underlying experimental findings. Private evidence is the hash-pinned
`ifih1-epoch-2` run snapshot in the new repair campaign.

### Human-primary follow-up remains scientifically out of scope

Run `19dac963-dbdb-45a8-ba75-50fb0b204b04` received the exact human pancreatic
tissue / primary-islet restriction and one predecessor turn. Its selected
`context_mechanism` answer includes EndoC-βH1 CVB4 experiments and stem-cell-derived
beta-cell experiments alongside primary-islet observations, without identifying
those model systems as evidence excluded by the restriction. This is a reproduced
upstream scope/selection defect even though the API response is complete and
its references are linked. Expected: retain supported primary tissue/islet
claims, separately identify earlier claims that depended on excluded models,
and preserve their reference links for that comparison. Evidence:
`authored-epoch-4`, case `new10_hirn_human_followup`, private run snapshot.

In `new08_hirn_beta_stress`, the selected main answer identifies cultured human
islets, but the selected alternative supplies a general mechanistic narrative
without consistently labeling experimental model or observation versus proposed
mechanism. In `new09_hirn_viral_conflict`, the alternative explicitly acknowledges
causal uncertainty and a NOD-mouse context, but its later mechanism claims lack
individual attached citation markers. These are scope and claim-linkage review
findings; complete transport status is not a scientific acceptance result.

### New PLEKHM1 relevance replay

In historical epoch 4 at backend `5bd7f468ff969f36df6002eb6f7a0f3fb9888566`,
run `c51d6c4d-0965-41b1-9c3a-d4a742db8bcb` (`jsonl-000032`, same exact
PLEKHM1 question above) returned no literature evidence in its main perspective,
but the selected alternative discusses general m6A/islet-composition findings
(PMID38409327) without establishing the requested PLEKHM1 link. Other identical
replays, including `113a6f69-d8a1-4ec1-9448-e37560c0593b` and
`d8b864c7-f335-41c4-9006-930a3c745655`, returned no evidence in both perspectives.
This variability remains upstream relevance/selection work. The PanKagent graph
answer consistently retained PLEKHM1 as a Gene and the same beta-cell DEG record;
that separate record does not validate unrelated literature alternatives.

### Three fresh stability repetitions

All three independent repetitions on backend
`2ee5bc9c7de5660e643cc0f58af38753243ac336` returned identical canonical graph
records and preserved the Gene/cell-type roles and recorded DEG values.

| Repeat | Agent run | Selected literature |
| --- | --- | --- |
| 1 | `c3871fba-ba65-4e11-8367-dfd41d0d9251` | Main: no evidence. Alternative: PLEKHM1/GWAS association context (PMID29093700) plus general T1D GWAS background (PMID34012112); this does not answer cell-type differential expression. |
| 2 | `46486993-768c-4686-8976-1f51704fa4ac` | Both perspectives: no evidence. |
| 3 | `01b5b359-5eda-4915-91f7-4aa8fddc5699` | Both perspectives: no evidence. |

Association background must be labeled as such and not selected as support for
an unverified differential-expression explanation. These observations do not
adjudicate the cited articles' biology; they demonstrate variable question scope
in selected responses to identical requests. Private graph-record hashes and
zero-cost reopen observations are in `plekhm1-epoch-1/stability-review.private.json`.
