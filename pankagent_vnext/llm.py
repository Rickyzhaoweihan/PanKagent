"""One structured plan, one streamed evidence answer, a shared budget gateway."""
import asyncio
import json
import time
from dataclasses import dataclass
import hashlib
from pathlib import Path
import anthropic
from .answer_router import AnswerSkillRouter
from .budget import Budget
from .evidence_context import compact_evidence

PLAN_SCHEMA = {
 'type':'object','additionalProperties':False,
 'properties':{
  'interpreted_question':{'type':'string'},
  'steps':{'type':'array','items':{
   'type':'object','additionalProperties':False,'properties':{
    'id':{'type':'string'},'question':{'type':'string'},
    'depends_on':{'type':'array','items':{'type':'string'}},
    'constraints':{'type':'array','items':{'type':'object','additionalProperties':False,'properties':{
     'property':{'type':'string'},'operator':{'type':'string','enum':['=','IN','CONTAINS','STARTS WITH','ENDS WITH','>','>=','<','<=']},
     'value':{'type':'string'}},'required':['property','operator','value']}},
    'complete':{'type':'boolean'}},'required':['id','question','depends_on','constraints','complete']}},
  'literature':{'type':'boolean'},'clarification':{'type':['string','null']}},
 'required':['interpreted_question','steps','literature','clarification']}

PLAN_SYSTEM = '''Plan a read-only PanKgraph scientific query. Produce one concise plan with at most three graph steps. Most questions need one complete natural-language step, not decomposition. For a standalone question, preserve the original wording verbatim as the step question whenever possible. Never expand direct effector prioritization into extra variant, GO, pathway, regulatory or physical-interaction investigation unless explicitly requested. Never infer extra evidence categories or scientific goals. Preserve scope strictly. Combine cleanup and follow-up interpretation here. Resolve pronouns only using provided session history. Record disease/gene/tissue/cohort/property constraints explicitly. Preserve user-supplied identifiers exactly. Use PanKgraph labels Gene, disease, anatomical_structure, variants, donor, GO_term, reactome and relation types in the provided question; do not invent IDs. T1D is type 1 diabetes, MONDO_0005147. IDs use property id; gene symbols use name. Put unknown IDs in the natural-language question rather than inventing them. Constraint values are scalar strings; for IN encode a JSON array as the string. Use the release schema notes below; do not guess property names. If an entity has an explicit identifier, constrain id only, retaining its human name in the question; do not add a redundant name predicate. Do not invent ontology IDs for non-diabetic or antibody-positive cohorts. Context in a requested measurement column is not necessarily a row predicate. Every step question must include all its scientific constraints so it can be sent independently to a Cypher writer. Dependencies refer to earlier step IDs and pass their returned stable entity IDs, never broaden a failed dependency. complete=true for all/every/full/complete requests, false for explicitly limited representative examples. For unspecified sets prefer complete=true. Set literature=true for scientific mechanism/explanation questions or explicit literature requests; simple entity lookups default false. The literature service will supply context and alternative perspectives. If the user asks for more than three independent investigations or lacks a necessary entity, set clarification and no steps. Never perform retrieval or answer the question while planning.'''

# Schema-only reference: accepted PanKgraph 08_04 property export, not held-out answers.
PLAN_SYSTEM += '''
Release schema notes:
- Gene, disease, anatomical_structure, GO_term, reactome: id/name identify entities. Gene symbols use name. anatomical_structure has no tissue_id property; cell names identify cell types.
- GENE_ENRICHED_IN: padj (adjusted p), pvalue, condition, rank_in_cell_type, log2_fold_change. T1D_DEG_IN instead uses adjusted_p_value. Never interchange these. A named cell's canonical name is not necessarily its short synonym; do not fabricate equality values.
- GENE_DETECTED_IN: condition, median_donor_cpm, expression_call.
- GENE_ACTIVITY_SCORE_IN: ocr_gene_activity_score_mean, type_1_diabetes_ocr_gene_activity_score_mean, type_2_diabetes_ocr_gene_activity_score_mean, non_diabetic_ocr_gene_activity_score_mean, aab_pos_ocr_gene_activity_score_mean (and corresponding median columns). Cohort-specific activity is encoded in columns, not condition_id predicates.
- PART_OF_QTL_SIGNAL: tissue_id, tissue_name, nominal_p, pip (not a cell node tissue_id). Preserve explicit tissue identifiers from the question.
- donor: id, age, bmi, gender, t1d_stage, diabetes_type, derived_diabetes_status, family_history_of_diabetes. No donor.condition property.
- HAS_DONOR / HAS_SAMPLE link disease, donor and Sample_node; data_modality also links samples. Scope to the user's requested cohort using actual schema, never assume disease nodes replace cohort attributes.
- When a scientific filter cannot be mapped confidently to a real property/value, request clarification instead of inventing a hard constraint or silently omitting it.
'''

SYNTHESIS_SYSTEM = (Path(__file__).parent / 'prompts' / 'answer_style.md').read_text()
ANSWER_CONTRACT = '''Final presentation contract: return the answer summary only, without follow-up questions or suggested searches. For a simple lookup use a direct sentence, one small table with at most five columns if useful, a source line and one brief evidence caveat (aim for 80–160 words total). Preserve IDs and units exactly. Include only returned entities and supported observations. Earlier interpretation templates do not require every listed field or extra sections. Never infer unreturned records from generation settings or invent incompleteness when explicit status is complete and sampling/truncation are false.'''
STYLE_VERSION = hashlib.sha256((SYNTHESIS_SYSTEM+'\n'+ANSWER_CONTRACT).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PreparedAnswer:
    body: str
    system: list
    profile: dict

class ClaudeGateway:
    def __init__(self,settings):
        self.settings=settings
        self.budget=Budget(settings.state_dir/'budget.sqlite3',settings.budget_usd)
        self.client=anthropic.AsyncAnthropic(api_key=settings.anthropic_key or 'not-configured',max_retries=0,timeout=18)
        self.last_success=None
        self.answer_router=AnswerSkillRouter()
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
    async def plan(self,question,history):
        if not self.settings.anthropic_key: raise RuntimeError('claude_key_not_configured')
        user=json.dumps({'question':question,'history':history[-6:]},ensure_ascii=False)
        rid=self._reserve('plan',PLAN_SYSTEM,user,1600)
        reply=await self._create(rid,model=self.settings.model,max_tokens=1600,
          system=[{'type':'text','text':PLAN_SYSTEM,'cache_control':{'type':'ephemeral'}}],
          messages=[{'role':'user','content':user}],
          tools=[{'name':'record_plan','description':'Record the proposed plan for user review','input_schema':PLAN_SCHEMA,'strict':True}],
          tool_choice={'type':'tool','name':'record_plan'},**self._options())
        self.budget.settle(rid,reply.usage.model_dump())
        for block in reply.content:
            if block.type=='tool_use' and block.name=='record_plan':
                plan=block.input
                seen=set()
                for step in plan['steps']:
                    if not step['id'] or step['id'] in seen or any(d not in seen for d in step['depends_on']):
                        raise ValueError('invalid_plan_dependencies')
                    seen.add(step['id'])
                if len(plan['steps'])>3: raise ValueError('plan_too_large')
                self.last_success=time.time()
                return plan
        raise ValueError('missing_structured_plan')
    def prepare_answer(self,question,evidence):
        # Inspect full bounded evidence before sampling; this does not call a model.
        routed=self.answer_router.select(evidence)
        compact=compact_evidence(evidence)
        profile={**routed.profile,'style_version':STYLE_VERSION}
        profile['context_sampled']=any(item.get('context_sampled',False) for item in compact)
        body=json.dumps({'question':question,'evidence':compact},ensure_ascii=False,default=str)
        if len(body.encode())>100000: raise ValueError('evidence_context_too_large')
        system=[{'type':'text','text':SYNTHESIS_SYSTEM,'cache_control':{'type':'ephemeral'}}]
        if routed.guidance:
            system.append({'type':'text','text':'Matched interpretation guidance (apply under the evidence and presentation rules above):\n'+routed.guidance,
                           'cache_control':{'type':'ephemeral'}})
        system.append({'type':'text','text':ANSWER_CONTRACT})
        return PreparedAnswer(body,system,profile)

    async def synthesize(self,question,evidence,*,prepared=None):
        if not self.settings.anthropic_key: raise RuntimeError('claude_key_not_configured')
        prepared=prepared or self.prepare_answer(question,evidence)
        body=prepared.body
        rid=self._reserve('synthesis','\n'.join(block['text'] for block in prepared.system),body,1600)
        try:
            async with self.client.messages.stream(model=self.settings.model,max_tokens=1600,
                system=prepared.system,
                messages=[{'role':'user','content':body}],**self._options()) as stream:
                async for text in stream.text_stream: yield text
                final=await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            if exc.status_code in (400,401,403,404,413,422,429):
                self.budget.settle(rid,{})
            raise
        self.budget.settle(rid,final.usage.model_dump()); self.last_success=time.time()
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
