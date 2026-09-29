# New-agent retrieval-first revision — 2026-09-29

Implemented locally on the existing `codex/workflow57-review` branch, based on `ce40cae`. This is the existing new agent, not another variant. Original dev, frontend and deployed services were not changed.

## Behavior and boundaries

- M03/M04 distinguish schema property words and instruction/domain words from entity aliases. Closed cohort drafts use complete current vocabulary, then normal scope and binding validation. Unknown modifiers and donor anti-existence requests fall back to the general planner.
- M04 retains the existing typed scalar normalization: unwrap a singleton only when the owner/property is verified scalar; do not silently pick from multiple values.
- M06 uses complete node/relationship records. New plans mark ranking/interpretation as formatting work. Cypher ranking, top-k limits and unrequested statistical cutoffs are rejected. Explicit membership predicates and cohort exclusions remain mandatory.
- The formatter receives the original request, recorded properties and existing full-record numeric/count facts. Context sampling cannot establish an exhaustive ranking. Existing materialization caps remain, so very large retrievals may be partial rather than complete.
- The closed clinical path covers stage-only donors, recorded control classifications, exact assays and independent cohorts. Labels and thresholds come from schema/runtime inventory; no benchmark IDs or expected answer IDs are used by runtime code.
- Q57 uses the current clarification/revision interface; frontend code is unchanged.
- New database-specific mappings, terminology, statistical-field meanings and request patterns live in the existing four-module schema pack. Pack revision 2.2.0 is a schema revision, not a third service. The three schema contract files describe permitted structure.

## Targeted questions

“Addressed” below describes implementation and local verification, not a measured live-answer pass.

| Case | Question | Change / limit |
|---|---|---|
| Q11 | Summarize the 10 distinct variants with the smallest recorded T1D GWAS association P values. Rank each variant by its minimum valid P value, break equal-P-value ties by variant ID, and return its recorded GWAS annotations, including PIP, alleles and credible set. Report stored zero P values as recorded zeros. | Property-role grounding and complete retrieval; ranking remains in formatter preparation using full-record decimal facts. No Cypher top-k. |
| Q12 | For ADCY3 and T1D, show the recorded colocalization signals, both GWAS and QTL lead variants, and PP.H4. Are the lead variants identical? | Requested PP.H4 and lead annotations are schema-owned properties. Existing independent GWAS/QTL lead bindings remain separate; no same-lead inference. |
| Q14 | Show PLEKHM1 colocalization with type 1 diabetes and identify the linked GWAS and molecular QTL signals without assuming the lead SNPs are the same. | Same retrieval/interpretation split as Q12; preserve both signal roles and their recorded links. |
| Q17 | Find 50 OCR peaks around CFTR | Parameterized PostgreSQL gene-coordinate -> peak-overlap provider implemented. “Around” requires interval clarification. Current source remains disabled pending release/assembly/per-table coordinate verification; no live completion claim. |
| Q18 | What gene-activity scores are recorded for INS across pancreatic cell types? Compare the recorded non-diabetic and T1D mean and median scores. | Existing verified scalar-singleton normalization retained and regression tested. Full-record activity comparison facts remain authoritative. |
| Q19 | In ductal cells, what gene-activity scores are recorded for CFTR, and which OCR peaks overlap the CFTR gene body on the same genome assembly? Return all overlapping peaks with coordinates and the recorded activity annotations. Distinguish genomic overlap from evidence that a peak regulates CFTR. | Existing same-assembly complete gene-body overlap template retained. Interpretation must distinguish overlap from regulation. This mixed tissue/activity request stays on its existing Neo4j route. |
| Q26 | Is ABCC7 enriched in ductal cells? Include the canonical gene name and recorded enrichment measurements. | Schema-owned property-role guidance and complete annotation retrieval; existing reviewed ductal population grouping retained. No new model replay establishes that both populations are selected reliably. |
| Q37 | Which genes interact with both CFTR and ADCY3 through recorded physical or genetic interactions, and which GO terms, KEGG pathways and Reactome pathways annotate those shared partners? | Deferred as requested; no special shared-partner workaround added. |
| Q39 | What GO biological-process, molecular-function and cellular-component terms annotate TCF7L2? | Formal GO domain words are interpretation roles rather than incidental entity aliases. Existing category validation retained. |
| Q42 | Tell me about gene INS | Closed, grounded node-only lookup; scope validation now accepts this request without inventing a required relationship. |
| Q45 | What is PLEKHM1? | Node-only identity lookup supports “What is …”; scalar ownership normalization retained. |
| Q50 | Count RNA-capable samples from HPAP stage 1 donors, including scRNA-seq and snMultiomics. Keep the assay labels separate. | Complete RNA-capability retrieval with exact assay labels retained; no sample-count aggregation in Cypher. Runtime vocabulary and existing capability rules authorize expansion. |
| Q51 | In Hpap, how many donors have a recorded T1D stage of 3? | Leading source clause and “stage of 3” parsed; stage alone no longer introduces a disease filter. Direct donor-record template. |
| Q52 | How many HPAP stage 3 donors do NOT have a recorded diabetes type of T1D? | NOT treated as an instruction; recorded diabetes-type exclusion remains a predicate, not a positive disease identity. |
| Q54 | Count HPAP non-diabetic control donors who are recorded as T1D stage 1. | Stage and recorded control classification bound independently; no inferred T1D diagnosis or derived-status substitution. |
| Q55 | How many scRNA-seq samples, excluding multiome assays, come from spleen in HPAP non-diabetic controls? | Exact assay, multiome exclusion, source and tissue stay on one sample witness with recorded control classification. |
| Q56 | Compare exact scRNA-seq sample availability between HPAP ND and T1D donors. Count distinct samples separately for each cohort. | Separate cohort tasks; added typed diagnosis -> donor -> sample template for the T1D branch. No forced tissue join. |
| Q57 | How many T1D stage 2 donors are in nPAP? | Inventory-backed spelling clarification occurs before planning/query calls. Existing user-confirmed correction flow resumes planning, preview, confirmation and answer; mocked lifecycle tested. Actual stage inventory still determines whether stage 2 is supported. |

## PostgreSQL source status

The provider first reads the uniquely identified gene's chromosome/start/end, then executes a parameterized interval-overlap query in the same read-only repeatable-read transaction. Each table's verified coordinate convention is normalized to half-open intervals. It does not write SQL with an LLM or invent graph edges. Returned identities must hydrate against the selected graph release. Record/byte limits, query timeouts and proof checks remain enforced.

Earlier read-only inspection found `ensembl_genes_node` and `ocr_peak_node` with `id`, `chr`, `start`, `end`. The old proposed OCR mapping used nonexistent `start_loc`/`end_loc` PostgreSQL columns; schema 1 now records the observed names. CFTR's PostgreSQL start was 117287120; saved Neo4j evidence reports start_loc 117287119. The one-base difference is **not** enough to establish assembly, release pairing or per-table conventions. `postgresql.enabled` remains false. A private `PANK_VNEXT_POSTGRESQL_DSN` and verified schema mapping are required before enabling this provider. No credential was added to code or reports.

Current provider supports an explicit gene-body overlap. An unspecified “around” request asks for interval clarification; a future flanking implementation must preserve the user's explicit distance. Q17 is therefore not claimed as solved against the live database.

## Validation

648 focused tests and 17 subtests passed locally (15.72 s). Coverage includes schema contracts, grounding, clinical resolution, query templates, scalar compilation, ranking guards, full-record facts, clarification/revision, runtime confirmation, and PostgreSQL mocked boundary/parameter/materialization behavior. Alternate gene/source IDs and rejected unknown modifiers check that the implementation does not depend on benchmark answer IDs.

No paid model calls were made in this continuation: OpenAI $0; Claude $0. There is no new 57-question live score, speed comparison, or measured answer-quality improvement. Previous evaluation results remain unchanged.

The attempted remote integration-test upload was rejected by automatic approval review because uploading agent source to jieliu3 required explicit destination authorization. The upload did not execute. Live database/provider validation and any source push remain pending; no deployment was attempted.
