"""One structured plan, one streamed evidence answer, a shared budget gateway."""
import asyncio
import json
import time
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import anthropic
from .answer_router import AnswerSkillRouter
from .budget import Budget
from .evidence_context import compact_evidence, scientific_excerpt
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
     'value':{'type':'string'}},'required':['property','operator','value','entity_type']}},
    'complete':{'type':'boolean'}},'required':['id','question','title','rationale','relation_types','depends_on','constraints','complete']}},
  'literature':{'type':'boolean'},'clarification':{'type':['string','null']}},
 'required':['interpreted_question','steps','literature','clarification']}

PLAN_SYSTEM = '''Plan a read-only PanKgraph scientific query. Produce one concise plan with at most twelve independent graph checks. Most questions need one complete natural-language step, not decomposition. For a standalone question, preserve the original wording verbatim as the step question whenever possible. Never expand direct effector prioritization into extra variant, GO, pathway, regulatory or physical-interaction investigation unless explicitly requested. Never infer extra evidence categories or scientific goals. Preserve scope strictly. Combine cleanup and follow-up interpretation here. Resolve pronouns only using provided session history. Record disease/gene/tissue/cohort/property constraints explicitly. Preserve user-supplied identifiers exactly. Use PanKgraph labels Gene, disease, anatomical_structure, variants, donor, GO_term, reactome and relation types in the provided question; do not invent IDs. T1D is type 1 diabetes, MONDO_0005147. IDs use property id; gene symbols use name. Put unknown IDs in the natural-language question rather than inventing them. Constraint values are scalar strings; for IN encode a JSON array as the string. Use the release schema notes below; do not guess property names. If an entity has an explicit identifier, constrain id only, retaining its human name in the question; do not add a redundant name predicate. Do not invent ontology IDs for non-diabetic or antibody-positive cohorts. Context in a requested measurement column is not necessarily a row predicate. Every step question must include all its scientific constraints so it can be sent independently to a Cypher writer. Dependencies refer to earlier step IDs and pass their returned stable entity IDs, never broaden a failed dependency. complete=true for all/every/full/complete requests, false for explicitly limited representative examples. For unspecified sets prefer complete=true. If the user asks for more than twelve independent investigations or lacks a necessary entity, set clarification and no steps. Never perform retrieval or answer the question while planning.'''

# Schema-only reference: accepted PanKgraph 08_04 property export, not held-out answers.
PLAN_SYSTEM += '''
Plan review contract:
- The application will resolve entities against the configured graph and retrieve a bounded initial evidence preview before asking the user to confirm. Do not claim any retrieval succeeded in this planning call.
- Preserve a clean standalone biological question. Do not append machine-oriented parenthetical lists of gene names, cell names, ontology IDs or relationship labels; put these in structured constraints and relation_types. Retain identifiers only when the user supplied them.
- For identity constraints set entity_type to the real graph node label (for example Gene for CFTR and anatomical_structure for ductal cells). For a measurement or relationship-property predicate use null. Never label a gene symbol as a GO term or treat a cell name as a disease.
- Record required exact relationship types in relation_types: enrichment uses GENE_ENRICHED_IN, detection uses GENE_DETECTED_IN, curated marker annotation uses MARKER_GENE_OF. These evidence types are distinct. For a broader investigation record only confidently supported schema types, otherwise ask for clarification.
- The application may add one clearly labeled related-evidence check for a resolved simple gene/cell question. Keep the primary planner focused on the user's request; do not add speculative pathways, causes or unbounded neighborhood searches.
Release schema notes:
- Gene, disease, anatomical_structure, GO_term, reactome: id/name identify entities. Gene symbols use name. anatomical_structure has no tissue_id property; cell names identify cell types.
- Include EVERY named entity in constraints: a question naming both a gene and a cell type requires both, not just the gene. Verified common-cell names in this release are alpha cell (CL_0000171), beta cell (CL_0000169), delta cell (CL_0000173), ductal cell (CL_0002079), and endothelial cell (CL_0000115). Use these exact name values for unambiguous common-cell requests; if the user explicitly supplies the matching ID, use that ID without a redundant name constraint. Do not broaden modified subclasses, negate exclusions, or collapse multiple cell types into one. Preserve the original question instead of adding investigations.
- GENE_ENRICHED_IN: padj (adjusted p), pvalue, condition, rank_in_cell_type, log2_fold_change. T1D_DEG_IN instead uses adjusted_p_value. Never interchange these. A named cell's canonical name is not necessarily its short synonym; do not fabricate equality values.
- GENE_DETECTED_IN: condition, median_donor_cpm, expression_call.
- GENE_ACTIVITY_SCORE_IN: ocr_gene_activity_score_mean, type_1_diabetes_ocr_gene_activity_score_mean, type_2_diabetes_ocr_gene_activity_score_mean, non_diabetic_ocr_gene_activity_score_mean, aab_pos_ocr_gene_activity_score_mean (and corresponding median columns). Cohort-specific activity is encoded in columns, not condition_id predicates.
- PART_OF_QTL_SIGNAL: tissue_id, tissue_name, nominal_p, pip (not a cell node tissue_id). Preserve explicit tissue identifiers from the question.
- donor: id, age, bmi, gender, t1d_stage, diabetes_type, derived_diabetes_status, family_history_of_diabetes. No donor.condition property.
- HAS_DONOR / HAS_SAMPLE link disease, donor and Sample_node; data_modality also links samples. Scope to the user's requested cohort using actual schema, never assume disease nodes replace cohort attributes.
- When a scientific filter cannot be mapped confidently to a real property/value, request clarification instead of inventing a hard constraint or silently omitting it.
'''

from .graph_contract import planner_notes, RELATIONS, LABELS, independent_measurement_steps
PLAN_SCHEMA['properties']['steps']['items']['properties']['relation_types']['items']['enum'] = list(RELATIONS)
PLAN_SCHEMA['properties']['steps']['items']['properties']['evidence_combination'] = {'type': 'string', 'enum': ['independent', 'cooccurrence']}
PLAN_SCHEMA['properties']['steps']['items']['required'].append('evidence_combination')
PLAN_SYSTEM += "\nRetrieve independent detection, enrichment and marker measurements in separate steps. Cooccurrence is only for a user explicitly requesting entities that satisfy multiple measurements together. For specificity/exclusivity, inspect other cell types too; a restriction to the named cell cannot establish exclusivity."
PLAN_SYSTEM += planner_notes() + "\nFor revision_context in history, apply its instruction to its parent plan and original question. Preserve unrelated constraints, return a standalone revised question, keep graph scope unchanged for literature-only instructions. A short revision is not a new question lacking an entity."

# Keep the public plan shape, but stop generating duplicate display fields.
from .planning_fastpath import expand_compact_plan
_step_schema = PLAN_SCHEMA['properties']['steps']['items']
for _field in ('title', 'rationale'):
    _step_schema['properties'].pop(_field)
    _step_schema['required'].remove(_field)
PLAN_SCHEMA['properties'].pop('literature')
PLAN_SCHEMA['required'].remove('literature')
PLAN_SYSTEM += "\nCompact output contract (takes precedence over display instructions): do not generate title, rationale or literature fields. For exactly one step, use an empty step question to reuse interpreted_question verbatim. Use biological language in interpreted_question and step questions; place schema relationship names only in relation_types. For multiple steps, use concise standalone step questions retaining every scientific modifier; never shorten or drop constraints, identifiers, exclusions, completeness or dependencies. Literature is always enabled by the application. Display titles and explanatory boilerplate are supplied deterministically."

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
Full-record fact contract (takes precedence over example-driven wording): each answer_facts ledger was computed before selecting example records. Use its per-assay counts, source/method/throughput distributions, formal GO code names and signal roles. Keep the opening conclusion as accurate as the tables. Colocalization can involve different lead variants; when same_complete_lead_set=false, never say the GWAS and QTL share a lead. Membership does not establish lead status: use Recorded variant as the column heading unless lead_role explicitly establishes lead/nonlead. Never add an unrecorded source qualifier such as GTEx-style. Do not describe all interactions as one method or throughput class unless the full distribution verifies it. Omit unsolicited assay generalizations and speculative technical explanations such as ambient RNA, dropout, aggregation artifacts or doublets unless the user requests hypotheses or source records explicitly report them. Select only relevant supported common caveats; a caveat section is optional, never an invitation to invent uncertainty. Computed donor/sample distributions are available even if individual examples are omitted. Do not say a complete search is limited or source data unavailable because only selected records are shown to you. For an aggregate question, report aggregates and omit individual donor examples unless asked.'''

from .answer_facts import DIGEST as ANSWER_FACTS_DIGEST
STYLE_VERSION = hashlib.sha256((SYNTHESIS_SYSTEM+'\n'+ANSWER_CONTRACT+'\n'+ANSWER_FACTS_DIGEST+'\ngrounded-synthesis-v3').encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PreparedAnswer:
    body: str
    system: list
    profile: dict
    generation: dict = field(default_factory=dict)


def plan_structure_issue(plan):
    if not isinstance(plan,dict) or not isinstance(plan.get('steps'),list):
        return 'malformed_plan'
    if any(not isinstance(s,dict) or not isinstance(s.get('depends_on'),list) or not s.get('id') for s in plan['steps']):
        return 'malformed_step'
    if len(plan['steps']) > 12:
        return 'plan_too_large'
    from .plan_recovery import GENERIC
    if not plan.get('steps') and (not plan.get('clarification') or str(plan.get('clarification')).strip().lower() in GENERIC):
        return 'empty_executable_plan'
    seen = set()
    for step in plan['steps']:
        if not step['id'] or step['id'] in seen or any(dependency not in seen for dependency in step['depends_on']):
            return 'invalid_plan_dependencies'
        seen.add(step['id'])
    return None

class ClaudeGateway:
    def __init__(self,settings):
        self.settings=settings
        self.budget=Budget(settings.state_dir/'budget.sqlite3',settings.budget_usd)
        self.client=anthropic.AsyncAnthropic(api_key=settings.anthropic_key or 'not-configured',max_retries=0,timeout=self.settings.plan_timeout)
        self.last_success=None
        self.answer_router=AnswerSkillRouter()
        from .planning_contract import VerifiedCache
        self.plan_cache=VerifiedCache()
    def _options(self):
        return {'thinking':{'type':'disabled'}} if self.settings.model=='claude-sonnet-5' else {}
    def _reserve(self,purpose,system,body,max_tokens):
        # UTF-8 bytes are a conservative input-token upper bound; include tool JSON/framing.
        bound=len((system+json.dumps(body,ensure_ascii=False)).encode())+12000
        return self.budget.reserve(self.settings.model,purpose,bound,max_tokens)
    async def _create(self,rid,**kwargs):
        try:
            return await self.client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            # Definitive pre-generation rejections consumed no inference tokens.
            if exc.status_code in (400,401,403,404,413,422,429):
                self.budget.settle(rid,{})
            raise
    async def plan(self,question,history, _repair=False, grounding=None):
        if not self.settings.anthropic_key: raise RuntimeError('claude_key_not_configured')
        from .semantic_registry import planner_guidance
        from .investigations import generic_profile_gene, expand_registered_profile
        profile_gene=generic_profile_gene(question) if not history else None
        user=json.dumps({'question':question,'history':history[-6:],'terminology_guidance':planner_guidance(question)},ensure_ascii=False)
        system_text=PLAN_SYSTEM
        schema=PLAN_SCHEMA
        from .planning_contract import SYSTEM as GROUNDED_SYSTEM, VERSION as PLANNING_VERSION, DIGEST as PLANNING_DIGEST
        from .planning_scope import scope_issue, DIGEST as PLANNING_SCOPE_DIGEST
        from .planning_compile import compile_property_owners, DIGEST as COMPILER_DIGEST
        from .planning_requirements import requirements_issue, compile_requested_scope, DIGEST as REQUIREMENTS_DIGEST
        from .pattern_planning import compile_signal_plan, DIGEST as PATTERN_PLAN_DIGEST
        from .schema_drafting import compile_schema_draft, DIGEST as SCHEMA_DRAFT_DIGEST
        def compile_scopes(proposal):
            proposal, issue = compile_property_owners(proposal, grounding, question=question)
            if issue is None:
                proposal, issue = compile_requested_scope(question, grounding, proposal)
            return proposal, issue
        cache_key = None
        if grounding and grounding.get('status') == 'ready':
            from .preplanning_grounding import grounding_guidance
            user=json.dumps({'question':question,'history':history[-6:],'grounding':grounding_guidance(grounding)},ensure_ascii=False)
            system_text=GROUNDED_SYSTEM
            cache_key=self.plan_cache.key(question,history[-6:],grounding_guidance(grounding),PLANNING_VERSION,PLANNING_DIGEST,PLANNING_SCOPE_DIGEST,COMPILER_DIGEST,REQUIREMENTS_DIGEST,PATTERN_PLAN_DIGEST,SCHEMA_DRAFT_DIGEST,schema,self.settings.model)
            if not _repair and getattr(self.settings,'plan_cache_enabled',True):
                cached=self.plan_cache.get(cache_key)
                if cached is not None:
                    cache_issue=plan_structure_issue(cached)
                    if cache_issue is None:
                        cached, cache_issue=compile_scopes(cached)
                    cache_issue=cache_issue or scope_issue(question,grounding,cached) or requirements_issue(question,grounding,cached,history)
                    if cache_issue is None:
                        provider_event('planning_cache', {'hit':True,'key':cache_key,'version':PLANNING_VERSION})
                        return cached
                    provider_event('planning_cache_rejected', {'key':cache_key,'category':cache_issue})
            if not _repair:
                matched = compile_signal_plan(question, grounding, history) or compile_schema_draft(question, grounding, history)
                if matched is not None:
                    matched, pattern_issue = compile_scopes(matched)
                    pattern_issue = (pattern_issue or plan_structure_issue(matched)
                                     or scope_issue(question, grounding, matched)
                                     or requirements_issue(question, grounding, matched, history))
                    provider_event('planning_pattern', {'matched': True, 'valid': pattern_issue is None,
                                   'category': pattern_issue, 'route': matched.get('planning_route')})
                    if pattern_issue is None:
                        matched = expand_compact_plan(matched)
                        matched['retrieval_policy'] = 'partial_independent_v1'
                        if cache_key:
                            self.plan_cache.put(cache_key, matched)
                        return matched
        provider_event('planning_cache', {'hit':False,'key':cache_key,'version':PLANNING_VERSION})
        if profile_gene:
            system_text='Interpret this exact request for a comprehensive gene profile. Record the supplied gene symbol unchanged. The application expands its versioned twelve-category profile after this call and verifies the gene against the graph; do not invent filters, resolve its existence, or generate checks.'
            schema={'type':'object','additionalProperties':False,'properties':{'gene_name':{'type':'string'}},'required':['gene_name']}
        from .investigations import required_categories
        # Twelve complete checks need more structured output than a one-step lookup.
        # Keep the existing wall-clock deadline and persistent reservation cap.
        output_limit=200 if profile_gene else 2400 if _repair or len(required_categories(question))==12 else 1600
        rid=self._reserve('plan',system_text,user,output_limit)
        reply=await self._create(rid,model=self.settings.model,max_tokens=output_limit,
          system=[{'type':'text','text':system_text,'cache_control':{'type':'ephemeral'}}],
          messages=[{'role':'user','content':user}],
          tools=[{'name':'record_plan','description':'Record the proposed plan for user review','input_schema':schema,'strict':True}],
          tool_choice={'type':'tool','name':'record_plan'},**self._options())
        self.budget.settle(rid,reply.usage.model_dump())
        provider_event('planning_response', {'stop_reason':getattr(reply,'stop_reason',None),'repair':_repair})
        for block in reply.content:
            if block.type=='tool_use' and block.name=='record_plan':
                plan=block.input
                provider_event('planning_proposal', {'plan':plan,'repair':_repair,'grounding_version':(grounding or {}).get('version')})
                if profile_gene:
                    if plan.get('gene_name') != profile_gene: raise ValueError('profile_scope_mismatch')
                    plan=expand_registered_profile(question,profile_gene)
                else:
                    from .planning_output import recover_misplaced_steps
                    plan, output_recovery = recover_misplaced_steps(plan, schema)
                    if output_recovery:
                        provider_event('planning_output_recovery', output_recovery)
                self.last_success=time.time()
                issue = plan_structure_issue(plan)
                if issue is None and grounding and grounding.get('status') == 'ready':
                    plan, issue = compile_scopes(plan)
                    provider_event('planning_constraint_compilation', {'valid':issue is None, 'category':issue,
                        'changes':[{'step_id':s.get('id'),'bindings':s['constraint_compilation']}
                                   for s in plan.get('steps',[]) if s.get('constraint_compilation')]})
                    issue = issue or scope_issue(question, grounding, plan) or requirements_issue(question, grounding, plan, history)
                provider_event('planning_output_validation', {'valid': issue is None, 'category': issue})
                if issue and issue != 'plan_too_large' and not _repair:
                    return await self.plan(question, history + [{'role':'system','content':'Repair the invalid planning output: '+issue+'. Preserve the complete original scope. Concrete genes need executable checks, not an empty plan.'}], _repair=True, grounding=grounding)
                if issue:
                    if issue == 'plan_too_large':
                        return {**plan,'steps':[],'proposal_issue':issue,'clarification':'This investigation needs more than twelve graph checks. Please narrow its scope.'}
                    from .plan_recovery import mark_failure
                    return mark_failure({**plan,'proposal_issue':issue})
                try:
                    plan=expand_compact_plan(plan)
                    plan['steps']=[repair_step_constraints(step) for step in plan['steps']]
                    plan=independent_measurement_steps(plan)
                    from .investigations import category_issue
                    issue=category_issue(question, plan)
                    if issue: raise ValueError(issue)
                    if grounding and grounding.get('status') == 'ready':
                        plan['retrieval_policy']='partial_independent_v1'
                    if cache_key and plan.get('steps') and not plan.get('clarification'):
                        self.plan_cache.put(cache_key,plan)
                    return plan
                except (ValueError, KeyError, TypeError) as exc:
                    if not _repair:
                        return await self.plan(question, history + [{'role':'system','content':'The last structured plan failed '+str(exc)+'. Supply valid independent checks preserving every requested category and the original scope.'}], _repair=True, grounding=grounding)
                    raise ValueError('planning_repair_exhausted')
        if not _repair:
            return await self.plan(question, history, _repair=True, grounding=grounding)
        raise ValueError('missing_structured_plan')
    async def repair_cypher(self, step, question, failures, candidate):
        """One grounded, budgeted fallback; the caller must revalidate and EXPLAIN."""
        from .release_schema import REGISTRY
        from .graph_contract import RELATIONS
        kinds=step.get('relation_types',[])
        schema={'type':'object','additionalProperties':False,'properties':{'cypher':{'type':'string'}},'required':['cypher']}
        system='Repair a read-only PanKgraph Cypher query. Preserve every requested entity, filter, dependency and completeness requirement. Use only the supplied schema. Never relax filters to find data. Return actual node and relationship objects with all properties. No writes, procedures, LIMIT, list slices or invented labels/properties. Return an empty cypher string if the exact scope cannot be represented safely.'
        body=json.dumps({'question':question,'step':step,'failed_candidate':candidate,'validation_failures':failures,
            'schema':{k:REGISTRY['relations'].get(k) for k in kinds},'guidance':{k:RELATIONS.get(k) for k in kinds}},ensure_ascii=False)
        rid=self._reserve('cypher_repair',system,body,1800)
        reply=await self._create(rid,model=self.settings.model,max_tokens=1800,
            system=[{'type':'text','text':system}],messages=[{'role':'user','content':body}],
            tools=[{'name':'repair_query','description':'Record one repaired read-only query','input_schema':schema,'strict':True}],
            tool_choice={'type':'tool','name':'repair_query'},**self._options())
        self.budget.settle(rid,reply.usage.model_dump())
        for block in reply.content:
            if block.type=='tool_use' and block.name=='repair_query':
                value=block.input.get('cypher')
                return [value] if isinstance(value,str) and value.strip() else []
        return []

    async def review_grounded_plan(self, question, plan, preview):
        """A bounded verification after script-compiled queries; no plan writing."""
        from .plan_verification import SYSTEM, SCHEMA, VERSION, review_input
        body = json.dumps(review_input(question, plan, preview), ensure_ascii=False, default=str)
        rid = self._reserve('plan_verification', SYSTEM, body, 400)
        reply = await self._create(rid, model=self.settings.model, max_tokens=400,
            system=[{'type': 'text', 'text': SYSTEM}], messages=[{'role': 'user', 'content': body}],
            tools=[{'name': 'verify_plan', 'description': 'Verify the executed scope', 'input_schema': SCHEMA, 'strict': True}],
            tool_choice={'type': 'tool', 'name': 'verify_plan'}, **self._options())
        self.budget.settle(rid, reply.usage.model_dump())
        for block in reply.content:
            if block.type == 'tool_use' and block.name == 'verify_plan':
                value = block.input
                if (isinstance(value, dict) and isinstance(value.get('approved'), bool)
                        and isinstance(value.get('issues'), list) and reply.stop_reason != 'max_tokens'):
                    # Contradictory approval is not a pass.
                    return {**value, 'approved': value['approved'] and not value['issues'], 'version': VERSION}
        raise ValueError('invalid_plan_verification')

    def prepare_answer(self,question,evidence):
        # Inspect full bounded evidence before sampling; this does not call a model.
        routed=self.answer_router.select(evidence)
        compact=compact_evidence(evidence)
        profile={**routed.profile,'style_version':STYLE_VERSION,'evidence_coverage_version':COVERAGE_VERSION}
        profile['context_sampled']=any(item.get('context_sampled',False) for item in compact)
        profile['model_context']={'sampled':profile['context_sampled'], 'steps':[
            {'evidence_id':item.get('evidence_id'), 'selected':item.get('context_counts',{}),
             'omitted':item.get('context_dropped',{})} for item in compact],
            'scope':'synthesis_input_only', 'display_counts_known':False}
        import re
        requested_details = bool(re.search(r'\b(?:donor|sample)[- ]?(?:IDs?|identifiers?|details?)\b|\bwhich\s+(?:donors?|samples?)\b|\b(?:list|identify|find)\b[^.?!]{0,160}\b(?:donors?|samples?)\b', question, re.I))
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
        body=json.dumps({'question':question,'evidence':excerpt,
            'verified_search_scope': SCOPE_NOTE if broad_cell_search(evidence) else 'Use each step\'s evidence_coverage for verified query scope and source comparisons. Unknown historical coverage is not a new verification. Never infer that a search or source comparison was limited merely because few records are returned. A recorded one-versus-rest comparison retains its source-analysis comparator population regardless of query scope.'},ensure_ascii=False,default=str)
        if len(body.encode())>100000: raise ValueError('evidence_context_too_large')
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
        from .evidence_status import outcome_message
        message = outcome_message(evidence)
        if message:
            yield message
            return
        if not self.settings.anthropic_key: raise RuntimeError('claude_key_not_configured')
        prepared=prepared or self.prepare_answer(question,evidence)
        body=prepared.body
        output_limit=prepared.profile.get('answer_budget',{}).get('max_output_tokens',1600)
        rid=self._reserve('synthesis','\n'.join(block['text'] for block in prepared.system),body,output_limit)
        scope_filter = ScopeTextFilter(evidence)
        try:
            async with self.client.messages.stream(model=self.settings.model,max_tokens=output_limit,
                system=prepared.system,
                messages=[{'role':'user','content':body}],**self._options()) as stream:
                async for text in stream.text_stream:
                    visible = scope_filter.feed(text)
                    if visible: yield visible
                final=await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            if exc.status_code in (400,401,403,404,413,422,429):
                self.budget.settle(rid,{})
            raise
        self.budget.settle(rid,final.usage.model_dump()); self.last_success=time.time()
        prepared.generation.update(stop_reason=final.stop_reason,
                                   truncated=final.stop_reason=='max_tokens',
                                   max_output_tokens=output_limit)
        provider_event('answer_generation', dict(prepared.generation))
        tail = scope_filter.feed('', final=True)
        if tail: yield tail
        provider_event('answer_scope_validation', {'scope': 'known_cell_search_contradictions_only', 'corrections': scope_filter.corrections})
        if final.stop_reason=='max_tokens': yield '\n\n[Answer reached its output limit.]'
    async def probe(self):
        if not self.settings.anthropic_key: return {'state':'unavailable','error_category':'not_configured','model':self.settings.model}
        try:
            response=await asyncio.wait_for(self.client.models.retrieve(self.settings.model),10)
            return {'state':'healthy','model':response.id,'auth_ok':True,'inference_verified':self.last_success is not None,'last_inference_success':self.last_success}
        except Exception as exc:
            category={400:'invalid_request',401:'authentication',402:'billing',403:'authorization',404:'model_unavailable',429:'rate_limited'}.get(getattr(exc,'status_code',None),'timeout' if isinstance(exc,(asyncio.TimeoutError,anthropic.APITimeoutError)) else 'connection' if isinstance(exc,anthropic.APIConnectionError) else 'dependency_unavailable')
            return {'state':'unavailable','model':self.settings.model,'auth_ok':False,'error_category':category}
    async def close(self): await self.client.close()
