# Incremental PanKagent improvement plan

Draft for review, 2026-09-26. No runtime, schema, model, frontend or deployment changes are included in this document.

## 1. Starting point and intended result

Improve the original workflow through small, independently testable changes to interpretation, task preparation, query compilation, candidate acceptance and evidence preparation. Use the existing entity-resolution design as the pattern: the LLM interprets intent and proposes a structure; Python verifies facts and executes it against a pinned schema pack.

The code baseline is **2994f1783e536265a3272378179a09cb2527f92a**. It removes the competing workflow and adds the initial annotation/direct-node template improvements. Those changes are saved on `codex/workflow57-review`, not deployed. Keep model/provider choices fixed while measuring these improvements. Mixed Terra/Claude/GPT roles remain a separate proposal.

The current benchmark contains **57 standalone questions**. The historical original obtained **37/57 verified core-or-correct-empty outcomes** and produced an answer for 45/57. Neither measure means 37 scientifically correct complete answers: Q11, Q18, Q33 and Q34 expose significant interpretation or numerical errors despite core coverage. The target is **at least 46/57 usable, supported answers**, with retrieval and answer quality reported separately. Achieving it is a target, not a forecast.

The latest template patch retrieved all required core nodes/edges for Q40 and Q41 in direct database replay, without truncation. This is not a new end-to-end score. Q17 and Q57 currently need scope clarification; retain them in the denominator and track their clarification behavior separately.

## 2. Use the draw.io boundaries accurately

Reference: `pankagent-tab01.drawio`, page **01 I-O and decisions**, in the PanKgraph `docs/pankagent-vnext/handoffs/pankagent-model-api-handoff/` package. It includes the chain/parallel/mixed panels and the shared query unit. The snapshot is historical; use code 2994f17 for current behavior.

The separate `pankagent-proposed-model-roles.drawio` still describes the removed competing experiment and proposed model split. Do not treat those labels as an active implementation or reinstate that design.

| Diagram boundary | Responsibility in this plan | Typical test cases | Schemas |
|---|---|---|---|
| M00 | One immutable database knowledge pack; generated views only | All cases | 1–4 |
| M01/M02 | Preserve original question and explicit edits | Standalone 57; revision tests remain separate | 1,3 |
| M03/D01 | Verified identities and recorded-value candidates; advisory lookup | Q05,Q07,Q26,Q31,Q57 | 1,3,4 |
| M04/D02/M04R | Interpret intent; produce and repair an executable task without inventing scope | Q07,Q09,Q38,Q39,Q42,Q50–Q56 | 1–4 |
| D3/M06/D6 | Schedule independent branches; bind children to verified parent roles | Q19,Q35,Q37,Q50,Q56 | 2,4 |
| D7/M06a | Compile supported structural patterns or reuse a matching verified cache | Q12,Q19,Q35,Q37–Q42,Q51,Q53,Q54 | 1,2 |
| M06b | Generate bounded Cypher for unsupported shapes using the same task contract | Unsupported paths and paraphrase controls | 1–3 |
| M07/D8 | Validate syntax, schema, authorized predicates, role bindings and feasibility | Every generated/template query | 1,4 |
| M08 | Read Neo4j; verify result shape, witnesses and completeness | Q12,Q19,Q35,Q49,Q52,Q55 | 1,4 |
| D9 | Repair the affected layer within the shared allowance | Invalid ownership, missing predicate, invalid result | 4 |
| M09/D5 | Deterministic joins, set operations, counts and comparisons | Q11,Q18,Q33,Q35,Q37,Q49–Q56 | 1–4 |
| D4/M10 | Publish a coherent usable preview, then freeze it on confirmation | All cases; partial/empty/cancellation tests | 3,4 |
| M11/M12; M13 | Prepare authoritative answer facts; preserve independent viewer evidence | Q11–Q14,Q18,Q27,Q33,Q34,Q43 | 1,3,4 |

Do not introduce a new M05 or renumber M04 as the entity resolver. In this source chart M03 is deterministic entity lookup and M04 is the planning supervisor. During a later diagram update, retain Error and Schema annotations, use “LLM API” for model-driven blocks, and keep any model description adjacent in dark purple. Preserve the original tabs 02–04. This draft leaves the diagrams unchanged.

## 3. Four schemas are the only editable database knowledge sources

Canonical directory: `pankagent_vnext/agent_schemas/packs/pankgraph/`.

| Schema | Owns | Examples needed by this plan |
|---|---|---|
| **1 — database_schema.json** | Exact labels, directed relationship signatures, property ownership/types/units, identifiers, recorded synonyms, source/coordinate metadata and release observations | GWAS P/PIP owners; donor stage/source owners; GO domains; full signal identifiers; OCR assembly/coordinate fields |
| **2 — query_patterns.json** | Structural retrieval patterns, typed roles, return projections, dependency/combination recipes and query-route policy | Direct node population; gene→partner→term; shared partners plus annotations; same-sample tissue; separate assay sets; coloc lead hydration; interval overlap |
| **3 — semantic_interpretation.json** | Database-specific meanings and intent-to-evidence mappings; scoped terminology, assay capabilities, interpretation rules and temporary comments | Lead-signal convention; stage versus diagnosis; marker versus expression; RNA-capable assays; recorded zero P; overlap versus regulation |
| **4 — validation.json** | Registered checks, diagnostics, completeness requirements, allowed repairs, result requirements and shared resource limits | Missing role/predicate diagnostics; incomplete exclusion; valid empty; query/result acceptance; repair budgets |

The manifest contains metadata and pins, not a fifth knowledge source. Python supplies generic tested operators and schema adapters. Prompts, GPU context, validation and answer guidance derive from the same pack. Concrete IDs/filter values still come from the current request and verified runtime bindings; they are not fixed into reusable patterns.

Implement this incrementally as each affected module is touched:

- Move relationship/property-specific repair prose out of `graph.py` into schema-owned repair guidance. Python selects a diagnostic/rule ID and supplies runtime values.
- Replace release-specific symbol/role dispatch with schema capabilities and registered operators where those paths are being changed.
- Derive donor, assay, annotation and statistical mappings from the pack rather than creating new Python keyword lists.
- Make `graph_patterns.json` a generated compatibility artifact or remove it after consumer audit; it must not remain an independently maintained copy.
- Make route configuration authoritative: `query_patterns.json` currently exposes GPU participation policy, while `app.py` forces it independently. Remove that disagreement in the routing work package.
- Add schema-reference, derived-view parity and new-hardcoded-knowledge checks. Inventory remaining legacy exceptions with owners; do not hide an unbounded rewrite in a small feature patch.

Concrete migration inventory for the relevant paths:

| Existing declaration | Schema destination and boundary |
|---|---|
| `scientific_projection.py` relationship/measurement field tables | Property roles in schema 1, interpretation in schema 3; Python checks projections generically |
| `query_templates.py` numeric/coordinate/category field lists and `donor_categories.py` declarations | Schema 1 types/roles/release applicability plus schema 4 binding requirements |
| `graph_contract.py` and `composable_planning.py` biological examples and field-specific guidance | Schemas 2/3; keep generic operations and language parsing as tested Python |
| `graph.py` enrichment and clinical repair prose | Schema 4 rules referring to schema 1 owners and schema 3 meanings |
| `annotation_selection.py` exact HLA plan-shape reservation | First preserve and regression-test Q34; then replace the case-shaped reservation with generic structural budgeting, or a clearly sourced bounded schema 4 hint if still needed |
| Other legacy schema/prompt snapshots | Audit consumers; derive or retire them, with parity tests and no second editable authority |

Observed property values are not automatically filters or closed enumerations. Runtime credentials/endpoints stay in protected deployment configuration, never these schemas. Public test references and archived results are evaluation artifacts, not runtime knowledge sources; the agent must never load benchmark gold memberships. No data-privacy gate is added for the public KG.

## 4. Work packages

### WP0 — freeze evidence and make evaluation reusable

Before runtime edits, pin code, pack digest, graph identity, model configuration and the reviewed `workflow57.json`. Keep the current question IDs/core/extra lists stable during comparison. Freeze a list of supported reference cases, verified empty cases (Q52/Q55), unresolved scope cases (Q17/Q57), and the existing offline failures.

The old `workflow57.py` live runner inherits the retired `workflow50.run()` and now deliberately errors. Build a small reusable **single-arm runner** and invoke it for original versus improved-original. Retain existing scoring helpers and historical raw artifacts. Do not reactivate the competing flag or overwrite the earlier 114 runs.

Deliverables: manifest, per-stage failure classification, matched replay harness and a baseline failure registry. Check cancellation, usage accounting and resume/idempotency without paid calls first.

### WP1 — improve M04 task preparation before adding query retries

Reuse `planning_session.py`, `planning_compile.py`, `planning_contract.py`, `schema_tools.py` and existing preparation tools. Do not add another planning agent or a second task language.

1. Classify requested evidence and property words before treating them as entity candidates. P/PIP in a GWAS request should first match the relevant property semantics; an explicitly named gene must still be resolvable. Q07's alias collision is a suspected contributor and needs an isolated test, not an assumed sole cause.
2. Prefer the smallest supported task shape. A gene introduction must permit an identity-only check; a donor total must not require sample existence or a disease join.
3. Check each filter's owner and role against schema 1 and a verified binding. Name/id equivalence never authorizes an extra disease, tissue or diagnosis constraint.
4. Require the plan to retain every requested evidence family. KEGG plus Reactome, three GO domains, two assay categories and two cohorts must not silently collapse into one.
5. Repair only the failed task using structured diagnostics: invalid owner, missing requested family, role mismatch, unsupported operator or unresolved term. Give the supervisor the failing fields and relevant schema fragment, not another undifferentiated “unclassified” retry.
6. Reuse verified sibling tasks. A task edit invalidates its dependent tasks, not independent successes; use existing dependency/provenance mechanisms.

Document a single task contract using existing fields wherever possible: requested population, typed roles, authorized predicates, relation/path structure, dependency role, return fields, completeness and any requested ranking/count operation. Add a versioned field only where the current contract genuinely cannot represent a requirement. The benchmark's core/extra IDs are never part of it.

Acceptance: Q07/Q09,Q38/Q39,Q42,Q50/Q51/Q53/Q54/Q56 yield valid prepared tasks; malformed-owner and missing-scope fixtures fail with actionable diagnostics; Q44/Q46 and existing entity-resolution controls do not regress. Interpret scope conservatively: nPAP is not silently corrected and “around CFTR” does not acquire an invented radius.

### WP2 — expand M06a structural coverage in small families

Use `query_templates.py`, `bounded_paths.py`, typed dependency bindings and schema 2. Each family is a separate reviewable change with direct reference replay.

| Family | Incremental change | Acceptance |
|---|---|---|
| Node/annotation | Complete node and annotation retrieval; all requested annotation families/domains; explicit target constraints remain typed | Q38–Q42,Q51,Q53,Q54; verify the Q40/Q41 patch end to end |
| Partner/path | Bind focus→partner→annotation in one connected path; shared-partner intersection precedes optional partner annotations | Q35,Q37; Q34 remains a regression control |
| Coloc materialization | Read exact full signal/context identifiers from coloc evidence; resolve referenced lead variants and retrieve matching membership annotations | Q12; preserve Q06,Q13–Q16 and do not require optional membership to retain primary coloc |
| Gene activity/overlap | Carry verified gene interval, genome assembly and cell scope into bounded interval retrieval; hydrate exact returned graph entities | Q19; overlap is never represented as a regulatory edge |
| Donor/sample | Direct donor population when samples are irrelevant; same-sample joins where requested; separate cohort/assay sets | Q47–Q56, including correct empty outcomes |

No question-ID switches or biological IDs in executable templates. Schema patterns specify roles and registered operations; Python emits parameterized Cypher. Avoid a general-purpose DSL or arbitrary Python/Cypher expressions loaded from JSON. Concrete identity/context bindings require the existing proofs.

Q26/Q31 need a narrow resolver-to-planner handoff: a requested cell population may correspond to multiple recorded subtype candidates. Encode the reviewed grouping meaning in schema 3 and verify the actual member IDs at runtime. Do not expand every cell term to all neighboring or similarly named cells.

### WP3 — make D7 routing intentional and improve M06b input

The current original path has cache/template routes, but `app.py` forces one eligible task to try the GPU first and `graph.py` honors that override. Treat removing the mandatory attempt as its own experiment after WP2 acceptance.

Proposed route: matching verified cache or supported template → common validation/read checks → bounded GPU fallback when local compilation is unsupported or invalid → existing targeted repair if eligible. A valid empty result succeeds. Do not send it to a model merely to seek a nonempty answer. Template-only tasks should not require a GPU health probe to execute.

For GPU fallback, build compact input from the same approved task: roles, directed types, owned properties, verified parameters, dependency populations and required evidence. Preserve all mandatory constraints when pruning schema context; if the input cannot fit, report that condition or split only at a valid task boundary. Keep independent task concurrency and the existing bounded GPU batch policy initially; there is no template-versus-GPU race and no replacement preview coordinator.

Acceptance: supported templates run with zero GPU generation calls; unsupported shapes still fall back; cache mismatches cannot bypass validation; graph errors are attributed separately from generator health. Measure database and GPU work, since the GPU service may itself execute generated candidates. Do not claim that cancelling an HTTP request stopped server work.

### WP4 — strengthen M07/D8–M08 selection without a new coordinator

Selection means **the first candidate that satisfies this task's evidence contract**, not the first syntactically valid query or the largest result. Extend the existing execution loop in `graph.py` and reuse evidence-status/coverage helpers. In particular, retain `composable_planning.selected_nodes()`, `complete()`, `snapshot()/verify_derivation()`, `evidence_coverage.build_evidence_coverage()` and the existing coloc/projection checks. This work extends their coverage rather than creating a parallel evidence-contract subsystem.

Before execution: check read-only structure, schema, predicate authorization, roles, dependency bindings, complete-scope requirements and EXPLAIN. Deduplicate exact query/typed-parameter/task-scope pairs; do not attempt risky semantic equivalence by textual rewriting.

After execution: check requested population types, required fields, connected path witnesses, expected source/context bindings, cursor exhaustion and truncation. Result checks must derive from the task and schemas, never a benchmark answer or expected row count.

| Outcome | Action |
|---|---|
| Complete usable evidence, including verified empty | Accept; stop remaining local selection work |
| Invalid query or incompatible result shape | Record the diagnostic; try the next already-budgeted alternative or targeted repair |
| Truncated/partial evidence | Preserve it as partial; try an eligible alternative only if it can address the failure within the same limits; never increase scope/caps silently |
| DB timeout/transport failure | Apply existing transient-error/deadline policy; do not reinterpret it as an empty set or rewrite biological scope |
| Extra optional annotation | Keep useful evidence; it is not a membership conflict |
| Different memberships in an explicit diagnostic replay | Compare population predicates, roles and completeness; unresolved disagreement remains uncertainty, not a row-count vote |

There is no reason to execute every alternative after accepting a complete result. Additional comparison belongs in offline diagnostics. Do not revive late result replacement, descendant recomputation from racing winners, or special confirmation versions. Preserve the original preview/confirm lifecycle.

Repair ownership: syntax/property spelling → M06b/query repair; incorrect role/filter/task scope → M04 preparation; missing identity → M03/M04 tool; combination selector → M09 contract. Every escalation shares schema 4's existing bounds: three planning proposals, two lookup batches of six requests, two execution repairs and seven supervisor calls. Do not give each layer a fresh nested allowance.

Acceptance: valid empty wins; duplicate candidates execute once; missing predicates fail before reads; partial cannot support exhaustive counts or exclusion; Q49's optional tissue enrichment does not create a false conflict. Also test exhausted budget, cancellation, stale cache and late callbacks without changing confirmed evidence.

### WP5 — make M09 and M11 supply trustworthy facts to M12

This is a small evidence-preparation improvement, not a formatter redesign or extra reviewer model.

- Compute top-k, minimum/maximum, distinct counts and group comparisons over full verified records, with a named denominator and missing-value policy. Schema 1 names the fields; schema 3 defines their meaning; schema 2 describes the operation; schema 4 checks completeness.
- For Q11, rank distinct variants by minimum valid stored P, break ties by ID, retain zeros and return the annotations of the supporting records. Do not replace zero with a guessed positive value; preserve numeric precision for very small scientific notation.
- For Q18, pass per-cell mean/median differences and ties. For Q33, pass direction counts and p/padj ranges/missingness. For Q34, label counts by evidence branch so a restricted path count cannot be described as the entire interaction population.
- Keep exact signal IDs/context tuples for Q12–Q14; shared prefixes or the same lead SNP do not collapse distinct signals.
- Set operations use the declared population role and typed IDs. Node display deduplication must not erase distinct relationship measurements, path witnesses or provenance. Benchmark edge identity uses type/start/end; runtime evidence still preserves multiple records with the same endpoints.
- The formatter receives an authoritative fact table plus sampled examples and evidence references. It may explain supported facts but not infer full-population rankings/counts from sampled rows. Add deterministic checks for structured numeric fields and completeness claims; do not claim a generic checker can prove all prose true.

Acceptance: exact rank list, direction/tie summaries and branch counts match independent calculations. Q11/Q18/Q33/Q34 require answer review despite core passing. Q27/Q43 preserve distinctions between expression/marker specificity and biological interpretation.

## 5. Divide the implementation into reviewable changes

| Change | Dependency | Can proceed independently | Completion gate |
|---|---|---|---|
| A: WP0 replay/evaluation | Baseline only | In parallel with schema audit | No paid calls; frozen reference and resumable runner verified |
| B: Schema ownership for touched paths | Baseline only | Parallel with A | Pack/view parity and unchanged-query checks |
| C: WP1 task preparation | B's field contracts | Individual task families after contract agreement | Prepared-plan fixtures plus negative controls |
| D: WP2 template families | C for new task shapes | Families can be implemented separately; no shared-file edits concurrently | Complete reference retrieval and binding proof tests |
| E: WP3/WP4 routing and acceptance | A, applicable D families | Acceptance tests may be authored before routing changes | No loss of existing successes; bounded work; empty/partial correctness |
| F: WP5 numerical facts | A, schema field mappings | Parallel with C/D | Full-record numerical truth and formatter-input checks |
| G: End-to-end acceptance | C–F | Review by question family | Quality, cost and latency gates below |

Use small commits and preserve independent rollback points. Keep model changes, frontend work, live deployment, database writes/index builds, S3 non-lead ingestion and enabling the unverified PostgreSQL accelerator outside this plan. The Neo4j path is sufficient for the initial scope.

## 6. Evaluation and release gates

1. **Offline first:** mock model replies and completion order; test roles, field ownership, request authority, schema parity, unsupported shapes, empty/partial states, numeric precision and materialization limits. Compare failures against the baseline registry rather than silently weakening tests.
2. **Database-only replay:** use read-only independent references for every changed family; compare full typed memberships, edge/path witnesses and required annotations. Log graph/pack identity and actual work. A template replay establishes retrieval only.
3. **Fresh end-to-end comparison:** same original/improved models, graph, settings, literature policy, concurrency and warm/cold/cache treatment. Alternate arm order. Run all 57 once, then a predefined repeated control set covering common and formerly failing families. Never select the best repeated answer. Mark cases used for tuning and reserve paraphrases/alternate entities for held-out transfer checks.
4. **Manual supported-answer review:** blind arm labels where practical; judge required facts, numerical correctness, signal/role interpretation and relevance. A model judge may be advisory if separately budgeted, but cannot override verified records or define success by itself.

Report separately:

- Prepared-plan rate and first failing module/diagnostic.
- Verified core node/edge coverage and verified-empty correctness.
- Optional extra coverage; helpful versus distracting exploration, not extra-node count alone.
- Required properties, paths, quantitative claims and supported-answer pass rate.
- Time to executable plan, first usable preview, complete preview, and final answer; p50 and descriptive p95. Matched-success comparisons plus all-attempt latency/failure cost prevent survivor bias.
- Planning/lookup/repair and formatting API usage/cost separately; also all-in attempted cost and cost per usable answer. GPU requests, database attempts/rows/bytes and retries are separate work measures.

Proposed gates: at least **46/57 supported usable answers**; retain the original 37 core/empty successes; no new wrong-scope/false-empty confirmations; fix the named numerical regressions. As an initial performance guardrail, matched-control median preview time and mean API cost should not worsen by more than 10% without a reviewed explanation tied to newly complete evidence. Report tails even if small samples make them unstable. These thresholds are proposed review criteria, not current measurements.

Q17 and Q57 stay unresolved until the user supplies a scope/source definition. Targeted clarification is the correct behavior but is not counted as an answered scientific question. Reference edits require a new fixture version and equal rescoring of both arms. Do not reintroduce simple/complex labels or alter the user's core/extra structure.

Paid testing requires a fresh explicit remaining-budget check. The historical Claude ledger had $9.517674/$10 settled; do not assume another full replay is authorized by the old ceiling or reset that ledger. Reserve worst-case calls before starting a batch and retain settled/pending usage on interruption. This drafting work spends $0 on model API evaluation.

Only after the review gates should deployment be proposed as a separate action. Keep existing API/preview/confirmation shapes and frontend code unchanged.

## 7. Complete question coverage map

| IDs | Topic and treatment |
|---|---|
| Q01–Q06 | QTL/alias/initial coloc controls; preserve lead convention and full signal context |
| Q07–Q11 | GWAS task ownership/statistics; Q07/Q09 preparation repair, Q11 deterministic ranking |
| Q12–Q16 | Coloc provenance and optional membership; Q12 exact lead materialization, Q14 signal identity |
| Q17–Q19 | OCR scope clarification; gene-activity fact tables; verified same-assembly overlap |
| Q20–Q23 | Effector/marker controls; preserve marker-source semantics without requiring user source vocabulary |
| Q24–Q31 | Enrichment/detection controls; Q26/Q31 subtype coverage; Q27 specificity claims |
| Q32–Q33 | DEG controls and full-record numerical summaries |
| Q34–Q37 | Interaction/annotation paths; Q35/Q37 shared-role repairs; Q34 branch accounting; preserve Q36 |
| Q38–Q41 | Complete pathway/GO preparation and retrieval |
| Q42–Q46 | Gene overview preparation and independent evidence; preserve Q43–Q46, correct unsupported wording |
| Q47–Q51 | Donor stage/same-sample/assay sets; preserve Q47–Q49, repair Q50/Q51 |
| Q52–Q57 | Verified empty and donor/cohort comparison; repair Q53/Q54/Q56, preserve Q52/Q55, clarify Q57 |

Start with C/D's annotation, direct-donor and assay task families: they address frequent supported failures with small changes. Then connected partner/coloc/OCR families. Implement factual summaries alongside these, because increasing retrieval coverage alone will leave visibly wrong answers.

## Sources

- Code baseline: 2994f1783e536265a3272378179a09cb2527f92a.
- `docs/original-workflow/template-improvements-20260926.md`.
- `docs/competing-candidates/workflow57-results.md`, `workflow57-review.md`, `workflow57-case-outcomes.json` — historical evidence only.
- `tests_vnext/fixtures/acceptance/workflow57.json`.
- `pankagent_vnext/agent_schemas/README.md` and the four canonical pack modules.
- `pankagent_vnext/app.py`, `graph.py`, `candidate_policy.py`, `planning_compile.py`, `planning_session.py`, `query_templates.py`, `bounded_paths.py`, `composable_planning.py`, `evidence_coverage.py`, `format_input_modes.py`.
- PanKgraph handoff `HANDOFF.md`, `provenance.json`, and `pankagent-tab01.drawio`; proposed model-role diagram treated as historical proposal.
