# PanKagent 57-question comparison

**Keep the original workflow as the default for now.** The competing-candidate workflow gives an earlier first preview, but settles later, costs more, and covers fewer required answers in this run. Neither reaches the requested 80% coverage target.

Both arms used **Claude Sonnet 5** on backend `c364ecd`: the original has competing candidates off; the new arm has them on. GPT-6 Sol reviewed the answers anonymously; this is a workflow comparison, not a Claude-versus-GPT production-model benchmark. All 114 attempts and 57 paired reviews completed. No application code, frontend or dev deployment changed.

| Measure | Original | Competing candidates |
|---|---:|---:|
| Any answer text produced (not correctness) | 45/57 | 41/57 |
| Required verified core / correct empty result | 37/57 (64.91%) | 33/57 (57.89%) |
| Frozen raw core score, before disclosed refinement | 32/57 | 30/57 |
| Median first preview, same 39 successful pairs | 12.28 s | 10.41 s |
| Median settled preview, same 39 pairs | 12.28 s | 15.93 s |
| Median answer completion, same 39 pairs | 20.88 s | 24.71 s |
| Mean API cost / attempted question, including failures | $0.0792 | $0.0878 |
| Planning / attempted question | $0.0465 | $0.0485 |
| Synthesis / attempted question | $0.0318 | $0.0273 |
| Repair and assistance / attempted question | $0.0009 | $0.0119 |
| Database retrieval calls | 78 | 130 |
| GPU writer requests | 103 | 168 |
| Failed preview confirmations | 0 | 3 |

The new arm costs 10.8% more overall. Its recorded decisions include 72 equivalent candidates and one conflict, with no accepted partial-to-complete or missing-evidence replacement in this run. Its lower synthesis subtotal partly reflects fewer answers; it is not a quality or efficiency win. Preview timings are backend events and exclude browser rendering. “Successful pairs” means both reached confirmation-eligible settled previews, not that both answers were scientifically correct.

## Why coverage is below 80%

Eighty percent requires **46/57**. The original needs nine more core-complete results; the new arm needs thirteen. Most failures have verified KG reference records, so they are not simply missing-data questions. Q17 (undefined set of 50 nearby OCRs) and Q57 (unresolved nPAP source) remain reference-scope issues and stay in the denominator.

1. **The new selector can reject useful extra annotations.** Q49 was independently replayed without model calls: both queries return the same sample. The template includes anatomy annotations (4 nodes/3 edges); GPU output has only donor/sample membership (2 nodes/1 edge). `membership()` hashes the entire graph and `Selection.offer()` marks different complete hashes as a conflict. This invalidates a usable result even though requested sample membership agrees. It directly confirms the concern that relevant extras should not be treated as wrong answers.
2. **Shared planning and scope validation fail on supported questions.** Examples: Q07 GWAS statistics, Q37–Q38 partner/pathway plans, Q42 INS introduction, Q50 dual-assay sample counts, Q51 stage-3 donors, Q53–Q54 control donors, Q56 cohort comparison. Q51 emits invalid relationship-property ownership and then rejects donor-stage filters. Generating additional candidates cannot repair a task that never reaches valid execution.
3. **Annotation retrieval is capped too aggressively.** Q39–Q41 use a ten-relationship overview and miss defining GO terms. Q35 incorrectly binds the annotation gene to HLA-DRA rather than its partners, then the required intersection fails. Extra exploration is allowed by this benchmark; these failures are missing vital evidence, not extra-node penalties.
4. **Summary generation loses or misstates correct full evidence.** Q11 retrieves 1,608 GWAS records but both answers give the wrong top ten and deny the two stored P=0 values. Q18 retrieves correct seven-cell activity values but both claim ND medians are higher in every cell type. Q33 retrieves 969 DEG records but the original says all P values are zero; the new version says all genes are downregulated (596 are positive and 373 negative). Q34 mixes counts from different branches and fails to explain the principal antigen-presentation partners.
5. **Some failures are incomplete scope or lifecycle handling.** Q26 and Q31 omit the MUC5B+ ductal subtype. Q19 misses independently verified CFTR/OCR overlap. The new arm fails confirmation on Q14, Q35 and Q55. Q12 provides correct coloc lead IDs in prose but omits required lead nodes/edges from graph output.

Core coverage is therefore an evidence-retrieval measure, **not an answer-quality pass rate**. Even the 37/33 covered cases contain material prose errors listed above. The blinded reviewer is advisory; its mistaken objections to node provenance `data_version` versus queried graph release were rejected. Q35's failed intersection was not relabeled a biological negative.

## Reference and review changes

The 57 questions and their order were preserved. References contain 1,012 distinct nodes and 1,013 stored edge identities, independently checked in PanKgraph_08_04 with no model calls. This verifies existence, not every scientific inference. Core contains vital evidence; extra remains optional. The verified union of nodes/edges did not change during rescoring.

The frozen paid run used fixture commit `9605eae`. Equal offline refinements moved optional lead-membership context to extra for Q13–Q16, and optional cell/disease context for the broad gene introductions Q42/Q44/Q45. Only Q16/Q44/Q45 change the raw score for either arm: original 32→35; new 30→33. Q49's conflicted retained payload is excluded from verified coverage (new 33→32). Reviewed correct empty answers add Q52/Q55 for original and Q52 for new, giving **37 versus 33**. Frozen artifacts and scores remain intact. These are post-run review refinements and must not be presented as a preregistered accuracy benchmark.

Q01/Q02 also expose an interpretation-contract mismatch: indexed QTL edges are treated as lead representatives by the project, while the runtime asks for explicit lead flags. Their returned variants are present, but the wording needs alignment. This test does not change that contract or add S3 non-lead data.

## Cost and reproducibility

Claude ledger: **$9.517674 / $10**, zero pending reservations. GPT review ledger: **$1.483354 / $10**, zero pending reservations. Costs use the checked-in adapter's $2/M uncached input, $10/M output, 0.1× cache-read and 1.25× cache-write accounting; these are configured estimates from returned usage, not a provider invoice. GPT review costs are separate from production query costs. Database/GPU compute is counted as work, not included in API dollars.

One attempt per question per arm; existing internal bounded retries retained. Same model, limits and graph release; isolated sessions and query caches, warm inventory, alternating arm order, literature disabled equally. Independently sampled plans and one run per case limit causal conclusions. The evidence supports retaining the original default for this suite; it does not show that competing candidates cannot work after fixes.

Before another paid comparison, the highest-value changes would be scope-aware candidate equivalence (membership versus optional annotation coverage), property/role binding, full annotation retrieval, and deterministic ranking/aggregates supplied to synthesis. **None of these application changes were made here.**

## Files

- Reviewed question fixture: `tests_vnext/fixtures/acceptance/workflow57.json`
- Paired run: `scripts/acceptance/workflow57.py`
- Advisory reviewer: `scripts/acceptance/workflow57_judge.py`
- Local HTML builder: `scripts/acceptance/workflow57_report.py`
- Aggregate outcomes and per-question explanations accompany this report. Full raw answers, audit events and token usage are retained in the private/local run artifact, not included in the source report.

## Per-question findings

| Question | Original core/empty | New core/empty | Finding |
|---|---|---|---|
| Q01 | yes | yes | All three CFTR QTL representatives retrieved. Both answers refuse lead terminology because runtime requires explicit source flags; this conflicts with the project import convention. Candidate also initially calls distinct signals the same signal. |
| Q02 | yes | yes | Both CENPO variants retrieved. Shared lead convention mismatch. Candidate conflates distinct full credible-set IDs and introduces unsupported GTEx provenance. |
| Q03 | yes | yes | No material issue identified in paired evidence review. |
| Q04 | yes | yes | No material issue identified in paired evidence review. |
| Q05 | yes | yes | No material issue identified in paired evidence review. |
| Q06 | yes | yes | No material issue identified in paired evidence review. |
| Q07 | no | no | Both fail planning despite verified GWAS data. Grounding matches statistical P/PIP text to gene aliases; this is a suspected contributor, not a proven sole cause. |
| Q08 | yes | yes | No material issue identified in paired evidence review. |
| Q09 | no | yes | Original fails planning; candidate answers correctly. Plans are sampled independently, so this alone does not prove a benefit from candidate selection. |
| Q10 | yes | yes | Candidate incorrectly describes 1.74e-7 as below 5e-8, contradicting its correct nonsignificance conclusion. Original reports this correctly. |
| Q11 | yes | yes | Both retrieve 1,608 GWAS records but generate the wrong top ten. True first entries include rs3842753 and rs689 (stored P=0) and rs2476601 (5.908e-208). Both incorrectly say no zero P values. Ranking must be computed on full records before prose. |
| Q12 | no | no | Both give the correct coloc-associated GWAS/QTL lead IDs and PP.H4 values in prose, but omit required lead-variant nodes and membership edges from the graph evidence. Prose usefulness and graph completeness differ. |
| Q13 | yes | yes | Candidate conflates three distinct full QTL signal identifiers into two credible sets. Main coloc evidence is present. |
| Q14 | yes | no | Original produces a coloc answer but conflates distinct full signal IDs. Candidate fails confirmation with preview_revalidation_required and an unresolved coloc scope. |
| Q15 | yes | yes | No material issue identified in paired evidence review. |
| Q16 | yes | yes | Both answer signal/tissue intent. Missing optional GWAS lead membership caused frozen-core failures; equal offline rescore fixes this rubric issue without changing outputs. |
| Q17 | no | no | Question has no genomic radius or rule defining which 50 peaks. Both planning and reference scope remain unresolved; a failed query is not evidence that peaks do not exist. |
| Q18 | yes | yes | Both retrieve all seven cell types and correct numeric rows, but falsely say ND medians exceed T1D across all types. Ductal/acinar are reversed and leukocytes tie. |
| Q19 | no | no | 85 same-assembly ductal OCRs overlapping CFTR were independently verified. Original returns activity but misses the overlap evidence; candidate fails dependency-role planning. This is a supported query the workflow fails to execute. |
| Q20 | yes | yes | No material issue identified in paired evidence review. |
| Q21 | yes | yes | No material issue identified in paired evidence review. |
| Q22 | yes | yes | No material issue identified in paired evidence review. |
| Q23 | yes | yes | No material issue identified in paired evidence review. |
| Q24 | yes | yes | No material issue identified in paired evidence review. |
| Q25 | yes | yes | No material issue identified in paired evidence review. |
| Q26 | no | no | Both resolve ABCC7 to CFTR but omit the required MUC5B+ ductal subtype. Candidate additionally denies rank_in_cell_type even though the returned edge contains rank 1. |
| Q27 | yes | yes | Both correctly retrieve beta-cell INS detection. Candidate opening overgeneralizes this to all evidence classes although marker evidence also includes pancreatic endocrine cells. |
| Q28 | yes | yes | No material issue identified in paired evidence review. |
| Q29 | yes | yes | No material issue identified in paired evidence review. |
| Q30 | yes | yes | No material issue identified in paired evidence review. |
| Q31 | no | no | Both omit the MUC5B+ ductal detection record and therefore do not complete the requested compartment comparison. |
| Q32 | yes | yes | No material issue identified in paired evidence review. |
| Q33 | yes | yes | Both retrieve 969 beta-cell DEG records. Original falsely says all p-values and adjusted p-values are zero. Candidate falsely says all are downregulated: 596 log2 fold changes are positive and 373 negative. |
| Q34 | yes | yes | Both retrieve mandatory HLA antigen-presentation graph evidence but summarize the narrower partner-annotation branch as the whole interaction search (143 vs 202 physical records) and fail to explain the key shared HLA partners. Original also incorrectly generalizes assay types. |
| Q35 | no | no | The annotation branch acquires an inappropriate HLA-DRA gene restriction when it should select partner genes. It then fails annotation scope validation; intersection cannot complete. Candidate additionally fails preview confirmation. Known matching partners exist. |
| Q36 | yes | no | Candidate exhausts planning proposals with missing_requested_scope:Gene and empty_executable_plan. Original retrieves required evidence. |
| Q37 | no | no | Both fail to prepare the combined shared-partner and annotation plan despite verified reference memberships. |
| Q38 | no | no | Both exhaust planning proposals (unclassified diagnostic) before reading ADCY3 KEGG/Reactome annotations. Reference nodes and edges exist. |
| Q39 | no | no | Original fails plan validation. Candidate covers examples from all three GO domains but returns only ten annotation relationships, omitting canonical Wnt signaling and the beta-catenin-TCF7L2 complex required in core. |
| Q40 | no | no | Both return ten annotation relationships, missing three defining negative-regulation T-cell processes. Candidate falsely calls its result complete. |
| Q41 | no | no | Both return ten annotation relationships and omit T-cell receptor regulation, protein dephosphorylation and cytoplasm core annotations. They correctly warn retrieval is partial. |
| Q42 | no | no | Both fail to prepare an executable INS overview plan. A separate, more explicit INS-role question is supported; this is request interpretation, not a missing INS node. |
| Q43 | yes | yes | Both retrieve all requested evidence categories. Original opening incorrectly calls insulin the hormone destroyed in T1D, rather than distinguishing beta-cell destruction and loss of insulin production; candidate avoids this wording. |
| Q44 | yes | yes | Both provide CFTR identity and chloride-channel function. Ductal enrichment is optional for this broad definition question and was moved to extra during equal offline rescoring. |
| Q45 | yes | yes | Both provide a supported PLEKHM1 introduction. Specific T1D and beta-cell DEG context is optional here, so these edges were moved to extra during equal offline rescoring. |
| Q46 | yes | no | Original answers TP53 identity. Candidate fails three proposals with missing_requested_scope:Gene. |
| Q47 | yes | yes | No material issue identified in paired evidence review. |
| Q48 | yes | yes | No material issue identified in paired evidence review. |
| Q49 | yes | no | False conflict independently reproduced with two read-only replays: both candidates return sample 3421820. Template additionally returns anatomy nodes (4 nodes/3 edges versus 2/1). Whole-graph membership comparison rejects useful extra evidence even though requested sample membership agrees. Original answers correctly. |
| Q50 | no | no | Both fail plan preparation for separate scRNA-seq and snMultiomics sample counts. |
| Q51 | no | no | Both fail query validation despite a 44-donor reference. Original diagnostics show invalid HAS_DONOR.id / donor_stage ownership and later rejected t1d_stage filters; candidate has no verified candidate. |
| Q52 | yes | yes | Both execute complete empty requested-filter queries and correctly report no matching records. Accepted empty result, not a vacuous core score. |
| Q53 | no | no | Both fail plan preparation despite the 96-donor reference. |
| Q54 | no | no | Both fail plan preparation despite three matching reference donors. |
| Q55 | yes | no | Original produces the verified empty answer; candidate fails preview confirmation. |
| Q56 | no | no | Both fail planning for the ND-versus-T1D sample comparison. |
| Q57 | no | no | The source name nPAP remains unresolved. Neither silently correcting the name nor a record search for it proves a verified cohort total. |
