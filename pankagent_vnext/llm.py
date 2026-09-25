"""Claude-led planning and unchanged streamed evidence synthesis."""
from copy import deepcopy
from .agent_schemas import module as schema_module
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import anthropic
from .answer_router import AnswerSkillRouter
from .budget import Budget
from .openai_provider import OpenAIClient, ProviderStatusError
from .evidence_context import MAX_BYTES, NODE_ONLY_MODE, compact_evidence, node_only_evidence, scientific_excerpt
from .constraint_values import VALUE_SCHEMA
from .plan_constraints import repair_step_constraints
from .scope_guard import ScopeTextFilter, broad_cell_search, NOTE as SCOPE_NOTE
from .evidence_coverage import VERSION as COVERAGE_VERSION
from .audit import provider_event

PLAN_SCHEMA = {
 'type':'object','additionalProperties':False,
 'properties':{
  'interpreted_question':{'type':'string'},
  'steps':{'type':'array','items':{
   'type':'object','additionalProperties':False,'properties':{
    'id':{'type':'string'},'question':{'type':'string'},
    'title':{'type':'string'},'rationale':{'type':'string'},
    'relation_types':{'type':'array','items':{'type':'string'}},
    'depends_on':{'type':'array','items':{'type':'string'}},
    'constraints':{'type':'array','items':{'type':'object','additionalProperties':False,'properties':{
     'property':{'type':'string'},'operator':{'type':'string','enum':['=','!=','<>','IN','CONTAINS','STARTS WITH','ENDS WITH','>','>=','<','<=']},
     'entity_type':{'type':['string','null']},
     'owner_role':{'type':'string'},
     'value':VALUE_SCHEMA},'required':['property','operator','value','entity_type']}},
    'complete':{'type':'boolean'}},'required':['id','question','title','rationale','relation_types','depends_on','constraints','complete']}},
  'literature':{'type':'boolean'},'clarification':{'type':['string','null']}},
 'required':['interpreted_question','steps','literature','clarification']}

PLAN_SYSTEM = schema_module('semantics_modalities')['planning_instructions']['fallback']

# Schema-only reference: accepted PanKgraph 08_04 property export, not held-out answers.
PLAN_SYSTEM += '''
Plan review contract:
- The application will resolve entities against the configured graph and retrieve a bounded initial evidence preview before asking the user to confirm. Do not claim any retrieval succeeded in this planning call.
- Preserve a clean standalone biological question. Do not append machine-oriented parenthetical lists of gene names, cell names, ontology IDs or relationship labels; put these in structured constraints and relation_types. Retain identifiers only when the user supplied them.
- For identity constraints set entity_type to the real graph node label. For a measurement or relationship-property predicate use null. Never label a gene symbol as a GO term or treat a cell name as a disease.
- Record required exact relationship types in relation_types: enrichment uses GENE_ENRICHED_IN, detection uses GENE_DETECTED_IN, curated marker annotation uses MARKER_GENE_OF. These evidence types are distinct. For a broader investigation record only confidently supported schema types, otherwise ask for clarification.
- The application may add one clearly labeled related-evidence check for a resolved simple gene/cell question. Keep the primary planner focused on the user's request; do not add speculative pathways, causes or unbounded neighborhood searches.
Release schema notes:
- Gene, disease, anatomical_structure, GO_term, reactome: id/name identify entities. Gene symbols use name. anatomical_structure has no tissue_id property; cell names identify cell types.
- Include EVERY named entity in constraints: a question naming both a gene and a cell type requires both, not just the gene. Preserve the user's names for runtime entity resolution; if the user explicitly supplies an ID, use that ID without a redundant name constraint. Do not broaden modified subclasses, negate exclusions, or collapse multiple cell types into one. Preserve the original question instead of adding investigations.
- GENE_ENRICHED_IN: padj (adjusted p), pvalue, condition, rank_in_cell_type, log2_fold_change. T1D_DEG_IN instead uses adjusted_p_value. Never interchange these. A named cell's canonical name is not necessarily its short synonym; do not fabricate equality values.
- GENE_DETECTED_IN: condition, median_donor_cpm, expression_call.
- GENE_ACTIVITY_SCORE_IN: ocr_gene_activity_score_mean, type_1_diabetes_ocr_gene_activity_score_mean, type_2_diabetes_ocr_gene_activity_score_mean, non_diabetic_ocr_gene_activity_score_mean, aab_pos_ocr_gene_activity_score_mean (and corresponding median columns). Cohort-specific activity is encoded in columns, not condition_id predicates.
- PART_OF_QTL_SIGNAL: tissue_id, tissue_name, nominal_p, pip (not a cell node tissue_id). Preserve explicit tissue identifiers from the question.
- donor: id, age, bmi, gender, t1d_stage, diabetes_type, derived_diabetes_status, family_history_of_diabetes. No donor.condition property.
- HAS_DONOR / HAS_SAMPLE link disease, donor and Sample_node; data_modality also links samples. Scope to the user's requested cohort using actual schema, never assume disease nodes replace cohort attributes.
- When a scientific filter cannot be mapped confidently to a real property/value, request clarification instead of inventing a hard constraint or silently omitting it.
'''

from .graph_contract import planner_notes, RELATIONS, LABELS, independent_measurement_steps
from .bounded_paths import PATH_SPEC_SCHEMA
PLAN_SCHEMA['properties']['steps']['items']['properties']['relation_types']['items']['enum'] = list(RELATIONS)
PLAN_SCHEMA['properties']['steps']['items']['properties']['evidence_combination'] = {'type': 'string', 'enum': ['independent', 'cooccurrence']}
PLAN_SCHEMA['properties']['steps']['items']['properties']['path_spec'] = PATH_SPEC_SCHEMA
PLAN_SCHEMA['properties']['steps']['items']['required'].append('evidence_combination')
PLAN_SYSTEM += "\nRetrieve independent detection, enrichment and marker measurements in separate steps. Cooccurrence is only for a user explicitly requesting entities that satisfy multiple measurements together. For specificity/exclusivity, inspect other cell types too; a restriction to the named cell cannot establish exclusivity."
PLAN_SYSTEM += "\nBounded paths: when the user explicitly asks for an ordered chain, path_spec may describe one connected fixed path of two to four node roles. List nodes in traversal order and one edge between each consecutive pair. Every path constraint must set owner_role to the exact node or edge role. Use direction=either only for physical or genetic interactions. Every bounded-path-v1 step uses evidence_combination=cooccurrence. Never use variable-length, branching, cyclic or disconnected paths."
PLAN_SYSTEM += planner_notes() + "\nFor revision_context in history, apply its instruction to its parent plan and original question. Preserve unrelated constraints, return a standalone revised question, keep graph scope unchanged for literature-only instructions. A short revision is not a new question lacking an entity."

from .planning_prompt_catalog import GUIDANCE as DOMAIN_PLANNING_GUIDANCE
PLAN_SYSTEM += DOMAIN_PLANNING_GUIDANCE

# Keep the public plan shape, but stop generating duplicate display fields.
from .planning_fastpath import expand_compact_plan
_step_schema = PLAN_SCHEMA['properties']['steps']['items']
for _field in ('title', 'rationale'):
    _step_schema['properties'].pop(_field)
    _step_schema['required'].remove(_field)
PLAN_SCHEMA['properties'].pop('literature')
PLAN_SCHEMA['required'].remove('literature')
PLAN_SYSTEM += "\nCompact output contract (takes precedence over display instructions): do not generate title, rationale or literature fields. For exactly one step, use an empty step question to reuse interpreted_question verbatim. Use biological language in interpreted_question and step questions; place schema relationship names only in relation_types. For multiple steps, use concise standalone step questions retaining every scientific modifier; never shorten or drop constraints, identifiers, exclusions, completeness or dependencies. Literature is always enabled by the application. Display titles and explanatory boilerplate are supplied deterministically."

from .composable_planning import extend_schema, GUIDANCE as COMPOSABLE_GUIDANCE
extend_schema(PLAN_SCHEMA)
PLAN_SYSTEM += COMPOSABLE_GUIDANCE

SYNTHESIS_SYSTEM = (Path(__file__).parent / 'prompts' / 'answer_style.md').read_text()
ANSWER_CONTRACT = '''Final presentation contract: return the answer summary only, without follow-up questions or suggested searches. For a simple lookup use a direct sentence, one small table with at most five columns if useful, a source line and a brief evidence caveat only when relevant (aim for 80–160 words total). Preserve IDs and units exactly. Include only returned entities and supported observations. Answer the primary question first; a context step supplies a brief additional observation, not a replacement answer. Distinguish detection from enrichment and exclusive expression. Do not compare measurements across conditions, cohorts or sources as if matched. rank_in_cell_type ranks genes within one cell type; it does not rank cell types for a gene. Never infer the strongest cell type from that rank or from a query restricted to one cell type. Earlier interpretation templates do not require every listed field or extra sections. Never infer unreturned records from generation settings or invent incompleteness when explicit status is complete and sampling/truncation are false.'''
ANSWER_CONTRACT += "\nVerified glossary: IBA means Inferred from Biological aspect of Ancestor; ISS means Inferred from Sequence or structural Similarity; IEA means Inferred from Electronic Annotation. PIP is model-based fine-mapping probability, not effect size. Colocalization supports a shared association signal, not proof of mechanism. Clinical stage descriptions are recorded metadata, not verified ADA/JDRF definitions. Never describe model-context compaction as browser display omission; actual display counts are unavailable at synthesis. Keep internal compaction diagnostics out of the answer. Missing categories mean a partial profile."
PLAN_SYSTEM += "\nGrouped investigations: the application groups up to twelve independent checks into three readable groups. For a gene overview, produce a separate check for each requested evidence type; do not reject a concrete multi-category question merely because it needs over three checks. For generic comprehensive gene profiles cover detection, enrichment, marker, T1D differential expression, effector support, QTL, coloc, GO, pathway annotation, recorded pathway enrichment, physical and genetic interactions. Do not add mandatory GWAS/QTL joins to a request for coloc statistics or signal identities: these are already properties of SIGNAL_COLOC_WITH. QTL is variants -> Gene with tissue properties on the relationship; GWAS is variants -> disease; coloc is Gene -> disease. Pathway names must be resolved across KEGG/Reactome rather than guessed. Missing ranking contrast in fGSEA must be disclosed."
_GLOSSARY = json.loads((Path(__file__).parent/'answer_skills/bim/evidence_glossary.json').read_text())
ANSWER_CONTRACT += '\n'+_GLOSSARY['version']+': '+json.dumps(_GLOSSARY['terms'])
ANSWER_CONTRACT += '\nFor multi-category answers, give a short direct conclusion and at most three compact groups. Cover every checked category with one or two sentences, reporting empty or blocked categories distinctly; avoid exhaustive partner, cell or leading-edge lists. Aim for 450 words or fewer so all caveats fit. Do not infer statistical independence from multiple records, sources, QTL types or different lead variants. For coloc, retain signal identities and avoid assigning trait 1/2 meanings to H1/H2 unless the dataset ordering is explicitly recorded. For fGSEA, say explicitly when the ranked contrast is unavailable; log2err describes p-value estimation uncertainty, never uncertainty of NES/ES. For gene measurements, state the recorded condition (for example ND) beside the result and briefly explain log-CPM, percent expressing or log2 fold change when used. For cohorts, lead with total unique donors and samples, explain the threshold and file-availability limit, and avoid internal field names or unsupported graph-display counts.'
ANSWER_CONTRACT += '\nCounting and interpretation rules: evidence_totals distinguishes relationship records, unique endpoints and node labels; never count the focal gene as a cell type or interaction partner. Never infer a per-donor distribution (most, typical, range, or exactly one each) from aggregate donor/sample totals or selected examples. Say only that some donors have multiple samples when samples exceed donors; omit unverified distribution claims. Selected example rows are not statistically representative. Do not invent ranges or ellipsis rows for unlisted donors. Multiple GO or effector annotations are separate records, not independent evidence. TAS means Traceable Author Statement, not direct experimental evidence. A generic PART_OF_QTL_SIGNAL edge does not establish eQTL subtype, expression units, or which molecular phenotype increases; use molecular trait unless a recorded subtype identifies it. When the fGSEA contrast is missing, explicitly state that the ranking contrast was not provided and biological direction cannot be assigned. Do not describe presymptomatic recorded stage and a no-clinical-diabetes field as conflicting merely because their labels differ. Do not print donor_summary, context-stub, or other internal field names; say retrieved cohort totals or selected examples. For grouped profiles, combine categories under exactly three sections: expression, genetic evidence, and function/interactions; at most four illustrative table rows in the entire profile.'
_COMMON_CAVEATS = (Path(__file__).parent/'answer_skills/bim/common_caveats.md').read_text()
ANSWER_CONTRACT += '\n' + _COMMON_CAVEATS
ANSWER_CONTRACT += '\nCoverage contract: evidence_coverage.query_scope records the validated query scope before any excerpting. When complete_for_requested_scope is true, all matching records for the stated entities, evidence categories and filters were checked in that graph release; zero matches means no matching PanKgraph record in that scope. Say this directly. When cell_type_scope is all_matching, do not invent a comparison restricted to the returned cell types or suggest other types were not queried. A complete search may return only two cell types because those are the recorded matches. The source one-versus-rest analysis still compared each target type with the remaining profiled cell types. No recorded evidence does not establish biological impossibility. Unknown or incomplete coverage never supports exhaustive absence; source-analysis scope, query scope, model examples and graph display are four separate concepts.'

ANSWER_CONTRACT += '\nColocalization linkage: use coloc_linkage records and their supporting_references to distinguish recorded colocalization from exact verified signal membership. A common gene or disease alone is not a shared-signal match. A requested variant can be a non-lead member of a GWAS credible set while also serving as a lead of a different molecular QTL signal; preserve those roles and signal identities. Empty or failed separately indexed GWAS/QTL checks never erase primary recorded colocalization or make it biologically absent. linked_record_count counts records with the recorded exact match rules; do not infer a match for unmatched records or claim repeated linkage summaries are independent evidence.'
ANSWER_CONTRACT += '''
Full-record fact contract (takes precedence over example-driven wording): each answer_facts ledger was computed before selecting example records. Use its per-assay counts, source/method/throughput distributions, formal GO code names and signal roles. Keep the opening conclusion as accurate as the tables. Colocalization can involve different lead variants. same_complete_lead_set=false means the complete lead sets differ, not that they cannot overlap. Report a shared recorded lead only from shared_recorded_lead_variant_ids; an empty list establishes no shared recorded lead, a nonempty list identifies the exact overlap, and null leaves overlap unknown. Membership does not establish lead status: use Recorded variant as the column heading unless lead_role explicitly establishes lead/nonlead. Never add an unrecorded source qualifier such as GTEx-style. Do not describe all interactions as one method or throughput class unless the full distribution verifies it. Omit unsolicited assay generalizations and speculative technical explanations such as ambient RNA, dropout, aggregation artifacts or doublets unless the user requests hypotheses or source records explicitly report them. Select only relevant supported common caveats; a caveat section is optional, never an invitation to invent uncertainty. Computed donor/sample distributions are available even if individual examples are omitted. Do not say a complete search is limited or source data unavailable because only selected records are shown to you. For an aggregate question, report aggregates and omit individual donor examples unless asked.'''

from .answer_facts import DIGEST as ANSWER_FACTS_DIGEST
OVERSIZED_RESULT_CONTRACT = (Path(__file__).parent/'answer_skills/bim/oversized_results.md').read_text()
INDEPENDENT_RESULT_CONTRACT = (Path(__file__).parent/'answer_skills/bim/independent_results.md').read_text()
ANSWER_CONTRACT += '\n' + INDEPENDENT_RESULT_CONTRACT
SYNTHESIS_CONTRACT_VERSION = 'llm-format-agent-v1-streamed'
STYLE_VERSION = hashlib.sha256((SYNTHESIS_SYSTEM+'\n'+ANSWER_CONTRACT+'\n'+ANSWER_FACTS_DIGEST+'\n'+OVERSIZED_RESULT_CONTRACT+'\n'+INDEPENDENT_RESULT_CONTRACT+'\n'+SYNTHESIS_CONTRACT_VERSION).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PreparedAnswer:
    body: str
    system: list
    profile: dict
    generation: dict = field(default_factory=dict)
    facts: list = field(default_factory=list)


def plan_structure_issue(plan):
    from .plan_recovery import contains_tool_markup
    if contains_tool_markup(plan):
        return 'malformed_plan'
    if not isinstance(plan,dict) or not isinstance(plan.get('steps'),list):
        return 'malformed_plan'
    if any(not isinstance(s,dict) or not isinstance(s.get('depends_on'),list) or not s.get('id') for s in plan['steps']):
        return 'malformed_step'
    if len(plan['steps']) > 12:
        return 'plan_too_large'
    from .plan_recovery import GENERIC
    if not plan.get('steps') and (not plan.get('clarification') or str(plan.get('clarification')).strip().lower() in GENERIC):
        return 'empty_executable_plan'
    if (not plan.get('clarification') and re.search(r'\b(?:connected|ordered|five.node|six.node|seven.node)\s+(?:\w+\s+)?(?:chains?|paths?)\b', str(plan.get('interpreted_question', '')), re.I)
            and not any(s.get('path_spec') for s in plan['steps'])):
        return 'connected_chain_requires_path_fragments_and_final_join'
    from .composable_planning import normalize
    try:
        plan = normalize(plan)
    except (ValueError, KeyError, TypeError) as exc:
        return str(exc)
    seen = set()
    for step in plan['steps']:
        if not step['id'] or step['id'] in seen or any(dependency not in seen for dependency in step['depends_on']):
            return 'invalid_plan_dependencies'
        from .bounded_paths import plan_issue as bounded_path_issue
        issue = bounded_path_issue(step)
        if issue:
            return 'unsupported_bounded_path_spec:' + issue
        seen.add(step['id'])
    return None

class ClaudeGateway:
    def __init__(self,settings):
        self.settings=settings
        self.budget=Budget(Path(getattr(settings, 'budget_dir', '') or settings.state_dir)/'budget.sqlite3',settings.budget_usd)
        self.provider = 'openai' if getattr(settings, 'model', '') == 'gpt-6-sol' else 'anthropic'
        if self.provider == 'openai':
            self.client = OpenAIClient(settings.openai_key or 'not-configured', max(settings.plan_timeout, settings.answer_timeout), settings.reasoning_effort)
        else:
            self.client=anthropic.AsyncAnthropic(api_key=settings.anthropic_key or 'not-configured',max_retries=0,timeout=self.settings.plan_timeout)
        self.last_success=None
        self.answer_router=AnswerSkillRouter()
        from .planning_contract import VerifiedCache
        self.plan_cache=VerifiedCache()
    @property
    def api_key(self):
        return (getattr(self.settings, 'openai_key', '') if getattr(self.settings, 'model', '') == 'gpt-6-sol'
                else self.settings.anthropic_key)
    def _options(self):
        return {'thinking':{'type':'disabled'}} if self.settings.model=='claude-sonnet-5' else {}
    async def _reserve(self,purpose,system,body,max_tokens):
        # UTF-8 bytes are a conservative input-token upper bound; include tool JSON/framing.
        bound=len((system+json.dumps(body,ensure_ascii=False)).encode())+12000
        return await self.budget.areserve(self.settings.model,purpose,bound,max_tokens)
    async def _create(self,rid,**kwargs):
        try:
            return await self.client.messages.create(**kwargs)
        except (anthropic.APIStatusError, ProviderStatusError) as exc:
            # Definitive pre-generation rejections consumed no inference tokens.
            if exc.status_code in (400,401,403,404,413,422,429):
                await self.budget.asettle(rid,{})
            raise
    async def plan(self,question,history, _repair=False, grounding=None, resolver=None, preparer=None):
        from .request_context import current, AUTHORITY
        if not self.api_key: raise RuntimeError('model_key_not_configured')
        from .semantic_registry import planner_guidance
        from .investigations import generic_profile_gene, expand_registered_profile
        profile_gene=generic_profile_gene(question) if not history else None
        user=json.dumps({'question':question,'history':history[-6:],'terminology_guidance':planner_guidance(question)},ensure_ascii=False)
        system_text=PLAN_SYSTEM
        schema=PLAN_SCHEMA
        chain_mode = bool(re.search(r'\b(?:connected|ordered|five.node|six.node|seven.node)\s+(?:\w+\s+)?(?:chains?|paths?)\b', question, re.I)) and not re.search(r'\b(?:independent|parallel)\s+(?:checks?|branches|queries|evidence)\b', question, re.I)
        if chain_mode:
            from .chain_drafting import schema as chain_schema
            schema = chain_schema(PLAN_SCHEMA['properties']['steps']['items'])
        from .planning_contract import SYSTEM as GROUNDED_SYSTEM, VERSION as PLANNING_VERSION, DIGEST as PLANNING_DIGEST
        from .planning_scope import scope_issue, DIGEST as PLANNING_SCOPE_DIGEST
        from .planning_compile import compile_property_owners, DIGEST as COMPILER_DIGEST
        from .planning_requirements import requirements_issue, compile_requested_scope, DIGEST as REQUIREMENTS_DIGEST
        from .genomic_scope import compile_genomic_scope, DIGEST as GENOMIC_SCOPE_DIGEST
        from .pattern_planning import compile_signal_plan, DIGEST as PATTERN_PLAN_DIGEST
        from .schema_drafting import compile_schema_draft, DIGEST as SCHEMA_DRAFT_DIGEST
        from .bounded_paths import (compile_hla_path_plan, is_hla_path_request,
                                    DIGEST as BOUNDED_PATH_DIGEST)
        from .independent_checks import DIGEST as INDEPENDENT_CHECKS_DIGEST
        from .coloc_tissue_scope import compile_scope as compile_coloc_tissue, DIGEST as COLOC_TISSUE_DIGEST
        from .cohort_plan_scope import compile_scope as compile_cohort_scope, DIGEST as COHORT_SCOPE_DIGEST
        def compile_scopes(proposal):
            from .preparation_tools import prepare_scope
            return prepare_scope(question, grounding, proposal)
        from .planning_session import initial_assessment
        from .task_preparation import bind_unique_requested_identities, independent_subset, PreparationIssue
        initial_proofs = [deepcopy(c['selection_proof']) for m in (grounding or {}).get('mentions', [])
                          for c in m.get('candidates', []) if c.get('selection_proof')]
        scope_question = (grounding or {}).get('session_scope_question') or question
        local_draft = None
        if grounding and grounding.get('status') == 'ready':
            from .preplanning_grounding import grounding_guidance
            system_text = GROUNDED_SYSTEM
            if not chain_mode:
                try:
                    local_draft = (compile_signal_plan(question, grounding, history)
                                   or compile_hla_path_plan(question, grounding, history)
                                   or compile_schema_draft(question, grounding, history))
                except (ValueError, KeyError, TypeError):
                    local_draft = None  # An advisory draft cannot block Claude.

            public_grounding = grounding_guidance(grounding)
        else:
            public_grounding = grounding or {'status': 'unavailable', 'diagnostic': 'E01'}
        if profile_gene:
            local_draft = expand_registered_profile(question, profile_gene)
        from .schema_tools import question_guidance
        user = json.dumps({'question': question, 'history': history[-6:],
            'request_context': current(question), 'request_authority': AUTHORITY,
            'schema_guidance': question_guidance(question, grounding),
            'verified_facts': {'grounding': public_grounding,
                               'session_population': (grounding or {}).get('session_population')},
            'advisory_suggestions': {'preliminary_assessment': initial_assessment(grounding),
                'terminology_advisory': (grounding or {}).get('terminology_advisory'),
                'terminology_guidance': planner_guidance(question), 'local_draft': local_draft}}, ensure_ascii=False)
        # Even exact/local/profile cases need Claude's final semantic decision.
        if (grounding or {}).get('session_population'):
            system_text += ('\nThe referenced population is verified backend evidence. Plan only the NEW connected queries '
                'from that population. Do not invent individual IDs or repeat its donor/cohort lookup. The backend '
                'will insert the verified parent and typed input binding. Use depends_on=[] for new root tasks; '
                'if explicitly referencing the previous population use only session_population as its dependency name. '
                'Preserve all newly requested assay/tissue filters. '
                'An unspecified tissue is an annotation to return, not a required tissue filter.')
        if profile_gene:
            profile_gene = None
        from .investigations import required_categories
        # Twelve complete checks need more structured output than a one-step lookup.
        # Keep the existing wall-clock deadline and persistent reservation cap.
        output_limit=200 if profile_gene else 4000 if re.search(r'\b(?:chain|chains|path|paths|follow)\b', question, re.I) else 2400 if _repair or len(required_categories(question))==12 else 1600
        if chain_mode:
            from .chain_drafting import GUIDANCE as CHAIN_GUIDANCE
            system_text += '\n' + CHAIN_GUIDANCE
        def finalize(proposal, chosen):
            nonlocal grounding
            if chosen:
                grounding = deepcopy(grounding or {})
                grounding['status'] = 'ready'
                grounding.setdefault('identity', {})['graph_release'] = chosen[0]['graph_release']
                for proof in chosen:
                    prior_candidate = next((deepcopy(c) for m in grounding.get('mentions', [])
                        for c in m.get('candidates', []) if c.get('id') == proof['id']
                        and c.get('entity_type') == proof['entity_type']), {})
                    grounding['mentions'] = [m for m in grounding.get('mentions', [])
                        if str(m.get('requested', '')).casefold() != proof['mention'].casefold()]
                    grounding['mentions'].append({'requested': proof['mention'], 'state': 'resolved',
                        'candidates': [{**prior_candidate, **proof, 'labels': [proof['entity_type']], 'match_kind': proof['match_method']}]})
            plan = proposal
            if chain_mode:
                from .chain_drafting import expand
                plan = expand(plan, question)
            else:
                from .planning_output import recover_misplaced_steps
                plan, output_recovery = recover_misplaced_steps(plan, schema)
            from .composable_planning import normalize
            from .session_inputs import attach
            plan = attach(plan, (grounding or {}).get('session_population'))
            plan = normalize(plan)
            plan = bind_unique_requested_identities(scope_question, grounding, plan)
            used = {(item['entity_type'], item['id']) for step in plan.get('steps', []) for item in step.get('preparation_trace', [])}
            plan['entity_selection_proofs'] = [p for p in initial_proofs if (p['entity_type'], p['id']) in used]
            issue = plan_structure_issue(plan)
            if issue is None and grounding and grounding.get('status') == 'ready' and not plan.get('clarification'):
                plan, issue = compile_scopes(plan)
                issue = issue or scope_issue(scope_question, grounding, plan) or requirements_issue(question, grounding, plan, history)
            if issue:
                partial = independent_subset(plan, issue)
                if partial:
                    partial, partial_issue = compile_scopes(partial)
                    partial_issue = partial_issue or scope_issue(scope_question, grounding, partial) or requirements_issue(question, grounding, partial, history)
                    if partial_issue:
                        partial = None
                    else:
                        partial = expand_compact_plan(partial)
                        partial['steps'] = [repair_step_constraints(s) for s in partial['steps']]
                raise PreparationIssue(issue, partial)
            plan = expand_compact_plan(plan)
            plan['steps'] = [repair_step_constraints(step) for step in plan['steps']]
            plan = independent_measurement_steps(plan)
            from .investigations import category_issue
            issue = category_issue(question, plan)
            if issue: raise ValueError(issue)
            plan['retrieval_policy'] = 'partial_independent_v1'
            self.last_success = time.time()
            return plan
        from .planning_session import run
        return await run(self, question, user, system_text, schema, output_limit, finalize,
                         resolver=resolver, preparer=preparer, initial_proofs=initial_proofs)

    async def interpret_revision(self, question, instruction, parent_plan):
        from .request_context import current, AUTHORITY
        from .revision_interpreter import SCHEMA, SYSTEM
        from .planning_output import matches_schema
        body = json.dumps({'current_question': question, 'revision_instruction': instruction,
            'request_context': current(question), 'request_authority': AUTHORITY,
            'plan_mode': parent_plan.get('execution_mode'),
            'checks': [{'question': s.get('question'), 'depends_on': s.get('depends_on'),
                        'path_spec': s.get('path_spec')} for s in parent_plan.get('steps', [])]}, ensure_ascii=False)
        rid = await self._reserve('revision_interpretation', SYSTEM, body, 1000)
        reply = await self._create(rid, model=self.settings.model, max_tokens=1000,
            system=[{'type': 'text', 'text': SYSTEM}], messages=[{'role': 'user', 'content': body}],
            tools=[{'name': 'interpret_revision', 'description': 'Produce one complete revised question',
                    'input_schema': SCHEMA, 'strict': True}],
            tool_choice={'type': 'tool', 'name': 'interpret_revision'}, **self._options())
        await self.budget.asettle(rid, reply.usage.model_dump())
        for block in reply.content:
            if block.type == 'tool_use' and block.name == 'interpret_revision' and matches_schema(block.input, SCHEMA):
                result = block.input
                if reply.stop_reason != 'max_tokens' and result['new_question'].strip() and len(result['new_question']) <= 6000:
                    return result
        raise ValueError('invalid_revision_interpretation')

    async def assist_query_structure(self, payload):
        from .query_assistance import SCHEMA, SYSTEM
        from .planning_output import matches_schema
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode()) > 100000:
            return {'action':'no_change','step_json':'','reason':'diagnostic_context_limit'}
        rid = await self._reserve('query_structure_assistance', SYSTEM, body, 2400)
        reply = await self._create(rid, model=self.settings.model, max_tokens=2400,
            system=[{'type':'text','text':SYSTEM}], messages=[{'role':'user','content':body}],
            tools=[{'name':'propose_structure','description':'Propose a validated structural repair',
                    'input_schema':SCHEMA,'strict':True}],
            tool_choice={'type':'tool','name':'propose_structure'}, **self._options())
        await self.budget.asettle(rid, reply.usage.model_dump())
        for block in reply.content:
            if block.type == 'tool_use' and block.name == 'propose_structure' and matches_schema(block.input, SCHEMA):
                return block.input
        return {'action':'no_change','step_json':'','reason':'invalid_assistance_response'}

    async def repair_cypher(self, step, question, failures, candidate):
        """One grounded, budgeted fallback; the caller must revalidate and EXPLAIN."""
        from .release_schema import REGISTRY
        from .graph_contract import RELATIONS
        kinds=step.get('relation_types',[])
        schema={'type':'object','additionalProperties':False,'properties':{'cypher':{'type':'string'}},'required':['cypher']}
        system='Repair a read-only PanKgraph Cypher query. Preserve every requested entity, filter, dependency and completeness requirement. Use only the supplied schema. Never relax filters to find data. Return actual node and relationship objects with all properties. No writes, procedures, LIMIT, list slices or invented labels/properties. Return an empty cypher string if the exact scope cannot be represented safely.'
        from .request_context import current, AUTHORITY
        body=json.dumps({'question':question,'request_context':current(stored=step.get('request_context')), 'request_authority':AUTHORITY,
            'step':step,'failed_candidate':candidate,'validation_failures':failures,
            'schema':{k:REGISTRY['relations'].get(k) for k in kinds},'guidance':{k:RELATIONS.get(k) for k in kinds}},ensure_ascii=False)
        rid=await self._reserve('cypher_repair',system,body,1800)
        reply=await self._create(rid,model=self.settings.model,max_tokens=1800,
            system=[{'type':'text','text':system}],messages=[{'role':'user','content':body}],
            tools=[{'name':'repair_query','description':'Record one repaired read-only query','input_schema':schema,'strict':True}],
            tool_choice={'type':'tool','name':'repair_query'},**self._options())
        await self.budget.asettle(rid,reply.usage.model_dump())
        for block in reply.content:
            if block.type=='tool_use' and block.name=='repair_query':
                value=block.input.get('cypher')
                return [value] if isinstance(value,str) and value.strip() else []
        return []

    async def review_grounded_plan(self, question, plan, preview):
        """A bounded verification after script-compiled queries; no plan writing."""
        from .plan_verification import SYSTEM, SCHEMA, VERSION, review_input
        from .request_context import current, AUTHORITY
        review = review_input(question, plan, preview)
        review['effective_question'] = review.pop('original_question')
        body = json.dumps({**review,
            'request_context':current(question, plan.get('request_context')), 'request_authority':AUTHORITY}, ensure_ascii=False, default=str)
        rid = await self._reserve('plan_verification', SYSTEM, body, 400)
        reply = await self._create(rid, model=self.settings.model, max_tokens=400,
            system=[{'type': 'text', 'text': SYSTEM}], messages=[{'role': 'user', 'content': body}],
            tools=[{'name': 'verify_plan', 'description': 'Verify the executed scope', 'input_schema': SCHEMA, 'strict': True}],
            tool_choice={'type': 'tool', 'name': 'verify_plan'}, **self._options())
        await self.budget.asettle(rid, reply.usage.model_dump())
        for block in reply.content:
            if block.type == 'tool_use' and block.name == 'verify_plan':
                value = block.input
                if (isinstance(value, dict) and isinstance(value.get('approved'), bool)
                        and isinstance(value.get('issues'), list) and reply.stop_reason != 'max_tokens'):
                    # Contradictory approval is not a pass.
                    return {**value, 'approved': value['approved'] and not value['issues'], 'version': VERSION}
        raise ValueError('invalid_plan_verification')

    def prepare_answer(self,question,evidence):
        from .request_context import current, AUTHORITY
        from .output_scope import aggregate_only, project
        # Route on schema and preserve public record evidence. Context-size
        # compaction is independent of the project's public-data authorization.
        routed=self.answer_router.select(evidence)
        if aggregate_only(question):
            # Compute full donor/sample facts before context-size compaction.
            from .answer_facts import build_answer_facts
            evidence = {key: {**step, 'answer_facts': build_answer_facts(step)}
                        for key, step in evidence.items()}
            evidence = {key: project(step) for key, step in evidence.items()}
        # Compact evidence for model context size; no cohort privacy suppression.
        compact=compact_evidence(evidence)
        profile={**routed.profile,'style_version':STYLE_VERSION,'evidence_coverage_version':COVERAGE_VERSION}
        profile['context_sampled']=any(item.get('context_sampled',False) for item in compact)
        profile['model_context']={'sampled':profile['context_sampled'], 'steps':[
            {'evidence_id':item.get('evidence_id'), 'selected':item.get('context_counts',{}),
             'omitted':item.get('context_dropped',{})} for item in compact],
            'scope':'synthesis_input_only', 'display_counts_known':False}
        import re
        requested_details = bool(re.search(r'\b(?:donor|sample)[- ]?(?:IDs?|identifiers?|details?)\b|\bwhich\s+(?:donors?|samples?)\b|\b(?:list|identify|find)\b[^.?!]{0,160}\b(?:donors?|samples?)\b', question, re.I))
        aggregate_question = bool(re.search(r'\bhow many\b|\bcount(?:s|ed|ing)?\b|\bnumber of\b|\btotal(?:s)?\b', question, re.I))
        # Functional measurement interpretation needs its actual sample values.
        # A count-only request still uses aggregate facts, even if source records
        # incidentally contain functional or clinical fields.
        requested_details = requested_details or (bool(profile.get('functional_features')) and not aggregate_question)
        excerpt=scientific_excerpt(compact, include_donor_details=requested_details)
        profile['model_context']['individual_donor_details_requested'] = requested_details
        if not re.search(r'\brank(?:s|ed|ing)?\b', question, re.I):
            # Gene rank within a cell is irrelevant to a cross-cell effect-size
            # comparison. Full evidence/hover/download records remain intact.
            def relevant(value):
                if isinstance(value,dict):
                    return {k:relevant(v) for k,v in value.items() if k != 'rank_in_cell_type'}
                if isinstance(value,list):return [relevant(v) for v in value]
                return value
            excerpt=relevant(excerpt)
            profile['model_context']['omitted_unrequested_fields']=['rank_in_cell_type']
        def answer_body(items):
            limited = bool(items) and all(item.get('answer_evidence_scope',{}).get('mode') == NODE_ONLY_MODE and not item.get('identity_path_records') for item in items)
            scope = ('Oversized query: node identities, descriptions and provenance only; no relationship or measurement conclusions are supported by this view.'
                if limited else SCOPE_NOTE if broad_cell_search(evidence) else 'Use each step\'s evidence_coverage for verified query scope and source comparisons. Unknown historical coverage is not a new verification. Never infer that a search or source comparison was limited merely because few records are returned. A recorded one-versus-rest comparison retains its source-analysis comparator population regardless of query scope.')
            from .format_input_modes import input_structure
            return json.dumps({'question':question,'evidence':items,'verified_search_scope':scope,
                'request_context':current(question), 'request_authority':AUTHORITY,
                'input_structure':input_structure(evidence),
                'interpretation_warnings': list(dict.fromkeys(w for step in evidence.values()
                    for w in (step.get('requested_scope') or {}).get('interpretation_warnings', [])))},ensure_ascii=False,default=str)
        body=answer_body(excerpt)
        if len(body.encode()) > MAX_BYTES:
            # Include the final question, JSON spacing and scope notes in the
            # size decision, not only the intermediate compact evidence.
            compact=compact_evidence(evidence, max_bytes=max(1000, MAX_BYTES-len(answer_body([]).encode())-20000))
            excerpt=scientific_excerpt(compact, include_donor_details=requested_details)
            body=answer_body(excerpt)
        if len(body.encode()) > MAX_BYTES:
            raise ValueError('answer_request_envelope_too_large')
        node_only=bool(compact) and all(item.get('context_compaction') == NODE_ONLY_MODE and not item.get('identity_path_records') for item in compact)
        profile['context_sampled']=any(item.get('context_sampled',False) for item in compact)
        profile['model_context'].update(sampled=profile['context_sampled'],
            mode=NODE_ONLY_MODE if node_only else 'standard',
            query_too_broad=node_only,
            steps=[{'evidence_id':item.get('evidence_id'), 'selected':item.get('context_counts',{}),
                    'omitted':item.get('context_dropped',{}), 'mode':item.get('context_compaction')} for item in compact])
        profile['answer_contract_version'] = SYNTHESIS_CONTRACT_VERSION
        profile['synthesis_mode'] = 'llm_formatter'
        if node_only:
            profile['model_context']['exposed_node_fields']=['id','type','description','source']
            profile['model_context']['measurement_guidance_suppressed']=True
            profile['answer_budget']={'max_output_tokens':900,
                                     'primary_checks':sum(step.get('purpose') != 'context' for step in evidence.values())}
            return PreparedAnswer(body,[{'type':'text','text':SYNTHESIS_SYSTEM,'cache_control':{'type':'ephemeral'}},
                {'type':'text','text':ANSWER_CONTRACT},
                {'type':'text','text':OVERSIZED_RESULT_CONTRACT}],profile)
        system=[{'type':'text','text':SYNTHESIS_SYSTEM,'cache_control':{'type':'ephemeral'}}]
        if routed.guidance:
            system.append({'type':'text','text':'Matched interpretation guidance (apply under the evidence and presentation rules above):\n'+routed.guidance,
                           'cache_control':{'type':'ephemeral'}})
        system.append({'type':'text','text':ANSWER_CONTRACT})
        if any(step.get('donor_summary',{}).get('unique_donors') for step in evidence.values()):
            system.append({'type':'text','text':'For this nonempty donor cohort, start with the number of donors matching the approved scope, including documented assay capabilities. Explain the exact assay-label distinction afterward. Do not open with No donors when the approved capability search returned matching donors. Preserve all counts, provenance and file-availability caveats.'})
        primary_count=sum(step.get('purpose') != 'context' for step in evidence.values())
        profile['answer_budget']={'max_output_tokens':2400 if primary_count >= 3 else 1600,
                                 'primary_checks':primary_count}
        if primary_count >= 3:
            system.append({'type':'text','text':'For this multi-check answer, cover every requested category concisely under at most three short headings. Use at most FOUR illustrative table rows in the ENTIRE answer, never a row for every returned cell or partner. Prefer one or two sentences per category with its source reference; aim for 350–450 words. Use authoritative full-result evidence_totals for counts. Never infer a total unique-partner count from visible example rows or from only start/end endpoint counts. Omit a count if its correct denominator is unavailable. Leave exhaustive records to the existing graph and downloads.'})
        if any(edge.get('type') == 'PART_OF_QTL_SIGNAL' for step in evidence.values() for edge in step.get('edges', [])):
            system.append({'type':'text','text':'QTL terminology for these records: a source name such as GTEx or INSPIRE alone is not a molecular-phenotype definition. Use molecular QTL unless an explicit recorded class or supplied verified subtype mapping identifies expression, splicing, or exon QTL. A generic slope does not establish an expression-unit effect. Do not label a generic QTL as eQTL in the opening sentence and then disclaim the subtype later.'})
        return PreparedAnswer(body,system,profile)

    async def synthesize(self,question,evidence,*,prepared=None):
        if not self.api_key:
            raise RuntimeError('model_key_not_configured')
        prepared = prepared or self.prepare_answer(question, evidence)
        body = prepared.body
        output_limit = prepared.profile.get('answer_budget', {}).get('max_output_tokens', 1600)
        rid = await self._reserve('synthesis', '\n'.join(block['text'] for block in prepared.system), body, output_limit)
        # The node-only view deliberately withholds relationship/comparison
        # evidence. Never reintroduce it through a full-evidence text rewrite.
        node_only = prepared.profile.get('model_context', {}).get('mode') == NODE_ONLY_MODE
        identity_ids = {item.get('evidence_id') for item in prepared.profile.get('model_context', {}).get('steps', [])
                        if item.get('mode') == NODE_ONLY_MODE}
        filter_evidence = {key: value for key, value in evidence.items()
                           if value.get('evidence_id') not in identity_ids}
        scope_filter = None if node_only else ScopeTextFilter(filter_evidence)
        try:
            async with self.client.messages.stream(model=self.settings.model, max_tokens=output_limit,
                    system=prepared.system, messages=[{'role':'user','content':body}],
                    **self._options()) as stream:
                async for text in stream.text_stream:
                    visible = text if scope_filter is None else scope_filter.feed(text)
                    if visible:
                        yield visible
                final = await stream.get_final_message()
        except (anthropic.APIStatusError, ProviderStatusError) as exc:
            if exc.status_code in (400,401,403,404,413,422,429):
                await self.budget.asettle(rid,{})
            raise
        await self.budget.asettle(rid, final.usage.model_dump())
        self.last_success = time.time()
        prepared.generation.update(stop_reason=final.stop_reason, truncated=final.stop_reason=='max_tokens',
                                   max_output_tokens=output_limit,
                                   synthesis_mode='llm_formatter', streamed=True,
                                   answer_contract_version=SYNTHESIS_CONTRACT_VERSION)
        provider_event('answer_generation', dict(prepared.generation))
        tail = '' if scope_filter is None else scope_filter.feed('', final=True)
        if tail:
            yield tail
        provider_event('answer_scope_validation', {
            'scope': 'skipped_for_node_identity_only' if node_only else 'known_cell_search_contradictions_only',
            'corrections': [] if scope_filter is None else scope_filter.corrections})
        if final.stop_reason == 'max_tokens':
            yield '\n\n[Answer reached its output limit.]'

    async def probe(self):
        if not self.api_key: return {'state':'unavailable','error_category':'not_configured','model':self.settings.model}
        try:
            response=await asyncio.wait_for(self.client.models.retrieve(self.settings.model),10)
            return {'state':'healthy','model':response.id,'auth_ok':True,'inference_verified':self.last_success is not None,'last_inference_success':self.last_success}
        except Exception as exc:
            category={400:'invalid_request',401:'authentication',402:'billing',403:'authorization',404:'model_unavailable',429:'rate_limited'}.get(getattr(exc,'status_code',None),'timeout' if isinstance(exc,(asyncio.TimeoutError,anthropic.APITimeoutError)) else 'connection' if isinstance(exc,anthropic.APIConnectionError) else 'dependency_unavailable')
            return {'state':'unavailable','model':self.settings.model,'auth_ok':False,'error_category':category}
    async def close(self):
        await self.client.close()
        await self.budget.aclose()
