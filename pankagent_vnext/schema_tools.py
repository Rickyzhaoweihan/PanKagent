"""Bounded schema/recorded-value tools sharing the active immutable pack."""
import re
from .agent_schemas import active_pack

SCHEMA_TOOL={'name':'inspect_schema','description':'Inspect exact graph/property definitions, examples, query patterns and expert interpretation. Examples are not exhaustive unless marked.','input_schema':{'type':'object','additionalProperties':False,'properties':{'references':{'type':'array','items':{'type':'string'}}},'required':['references']}}
VALUES_TOOL={'name':'resolve_property_values','description':'Find recorded categorical values on the specified node or relationship property. Never substitutes a node display name for a sample code.','input_schema':{'type':'object','additionalProperties':False,'properties':{'reference':{'type':'string'},'text':{'type':'string'}},'required':['reference','text']}}


def inspect_schema(references):
    if (not isinstance(references,list) or not 1<=len(references)<=6
            or any(not isinstance(ref,str) for ref in references)):
        return {'status':'invalid_request'}
    pack=active_pack();items=[]
    for ref in references:
        try:
            if not isinstance(ref, str): raise ValueError('invalid_reference')
            if ref == 'interpretation.index':
                items.append({'reference': ref, 'rules': [
                    {k:r[k] for k in ('id','database_refs','applicability')}
                    for r in pack.module('semantic_interpretation')['bim']['rules']]})
                continue
            if ref.startswith('interpretation.'):
                rule = next((r for r in pack.module('semantic_interpretation')['bim']['rules']
                             if r['id'] == ref[len('interpretation.'):]), None)
                if rule is None: raise ValueError('unknown_rule')
                items.append({'reference': ref, 'definition': rule})
                continue
            parts = ref.split('.')
            if parts[0] not in {'nodes','relationships'} or len(parts) not in {2,4}:
                raise ValueError('use_type_or_property_reference')
            value=pack.resolve_ref(ref)
            if isinstance(value,dict):value={k:v for k,v in value.items() if k!='observations'} | ({'observations':value['observations']} if 'observations' in value and 'properties' not in value else {})
            if 'properties' in value:
                value['properties'] = {name:{k:v for k,v in prop.items() if k not in {'observations','interpretation_refs'}}
                                       for name,prop in value['properties'].items()}
                value['detail_lookup'] = ref + '.properties.<property>'
            if '.properties.' in ref and value.get('export')=='protected':value.pop('observations',None)
            items.append({'reference':ref,'definition':value})
        except (ValueError,AttributeError):items.append({'reference':ref,'status':'unknown_reference'})
    sem=pack.module('semantic_interpretation');types={'.'.join(r.split('.')[:2]) for r in references}
    rules=[r for r in sem['bim']['rules'] if (types | set(references)).intersection(r['database_refs'])
           and r['applicability'] in {'matched_data_type','recorded_stage_explanation_only'}]
    patterns=[r for r in pack.module('query_patterns')['library']['rules'] if any('relationships.'+rel in types for rel in r['relations'])]
    return {'status':'complete','schema_sha256':pack.digest,'items':items,'interpretation_rules':rules,'query_patterns':patterns,'repair_guidance':pack.module('validation')['repair_guidance']}


async def resolve_property_values(graph, reference, text):
    if not isinstance(reference,str):return {'status':'invalid_request'}
    pack=active_pack();parts=reference.split('.')
    if len(parts)!=4 or parts[0] not in {'nodes','relationships'} or parts[2]!='properties' or not isinstance(text,str) or len(text)>128:return {'status':'invalid_request'}
    try:spec=pack.resolve_ref(reference)
    except ValueError:return {'status':'unknown_reference'}
    if spec['export']=='protected':return {'status':'protected','reference':reference}
    owner,prop=parts[1],parts[3]
    match=f'MATCH (n:`{owner}`)' if parts[0]=='nodes' else f'MATCH ()-[n:`{owner}`]->()'
    # Neo4j cannot stringify a list property. Match its elements while returning
    # the stored list itself, keeping its ownership and representation explicit.
    predicate=('CASE WHEN valueType(n[$property]) STARTS WITH "LIST" '
               'THEN any(x IN n[$property] WHERE toLower(toString(x)) CONTAINS toLower($text)) '
               'ELSE toLower(toString(n[$property])) CONTAINS toLower($text) END')
    rows=await graph._small_query(match+' WHERE n[$property] IS NOT NULL AND ($text = "" OR '+predicate+') RETURN n[$property] AS value,count(*) AS frequency ORDER BY frequency DESC,value LIMIT 11',{'property':prop,'text':text})
    return {'status':'complete','reference':reference,'schema_sha256':pack.digest,'graph_release':graph.settings.graph_version,'values':rows[:10],'values_complete':len(rows)<=10,'search_text':text,'definition':{k:v for k,v in spec.items() if k!='observations'}}


def question_guidance(question, grounding=None):
    pack=active_pack();db=pack.module('database_schema');refs=set()
    for group in ['nodes','relationships']:
        for name,spec in db[group].items():
            pattern=spec['query_terms']
            if isinstance(pattern,str) and pattern and re.search(pattern,question,re.I):refs.add(group+'.'+name)
    for mention in (grounding or {}).get('mentions',[]):
        for candidate in mention.get('candidates',[]):
            kind=candidate.get('entity_type')
            if kind in db['nodes']:refs.add('nodes.'+kind)
    sem=pack.module('semantic_interpretation')
    rules=[{'reference':'interpretation.'+r['id'],'applies_to':r['database_refs'],'applicability':r['applicability']} for r in sem['bim']['rules'] if refs.intersection(r['database_refs'])]
    return {'schema_sha256':pack.digest,'available_references':sorted(refs),'expert_interpretation':rules,
            'interpretation_catalog': 'All retained BIM rules can be inspected by interpretation.<rule id>. Historical, functional-feature and combination rules apply only to the specified representation. Do not infer a filter from an interpretation.',
            'instruction':'Use inspect_schema for exact fields and examples. Use resolve_property_values for recorded codes. A tissue display name is not a Sample_node tissue code. Interpretation describes evidence; it never adds unrequested diagnosis or assay predicates.'}
