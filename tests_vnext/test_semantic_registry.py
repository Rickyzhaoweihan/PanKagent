import copy
import json
import time
import pytest
from pankagent_vnext.semantic_registry import resolve, STAGES, meaningful_row, donor_summary
from pankagent_vnext.graph import validate_cypher

VOCAB={'stages':list(STAGES.values()),'sources':['HPAP'],'modalities':['scRNA-seq','scATAC-seq','snMultiomics','CITE-seq Protein'],'modality_links_verified':True}

def prepared(q='Find HPAP stage 3 T1D donors with spleen scRNAseq data'):
    p={'id':'s1','question':q,'constraints':[],'complete':True,'relation_types':['HAS_DONOR'],'depends_on':[]}
    if 'spleen' in q:p['constraints'].append({'property':'name','entity_type':'anatomical_structure','operator':'=','value':'spleen'});p['relation_types'].append('HAS_SAMPLE')
    return resolve(p,VOCAB,'PanKgraph_08_04')


def query(p):
    q="MATCH (c:disease)-[cd:HAS_DONOR]->(d:donor)"
    groups=p['sample_requirements']['modality_groups']
    for i,g in enumerate(groups):q+=f" MATCH (d)-[ds{i}:HAS_SAMPLE]->(s{i}:Sample_node)<-[a{i}:HAS_SAMPLE]-(t{i}:anatomical_structure)"
    conditions=[]
    for c in p['constraints']:
        if c['entity_type'] in ['donor','disease']:
            v='d' if c['entity_type']=='donor' else 'c';conditions.append(f"{v}.{c['property']} {c['operator']} {json.dumps(c['value'])}")
    for i,g in enumerate(groups):
        conditions.extend([f't{i}.name = "spleen"',f's{i}.data_modality '+('IN '+json.dumps(g) if len(g)>1 else '= '+json.dumps(g[0]))])
    return q+' WHERE '+' AND '.join(conditions)+' RETURN d'

@pytest.mark.parametrize('stage',['stage 3','stage III','stage3'])
def test_stage_canonical_values(stage):
    p=prepared('Find HPAP T1D '+stage+' donors')
    assert not p['semantic_issues']
    assert next(c['value'] for c in p['constraints'] if c['property']=='t1d_stage')==STAGES['3']
    assert validate_cypher(query(p),p)==[]


def test_rna_capability_and_exact_assay_stay_distinct():
    broad=prepared();exact=prepared('Find HPAP stage 3 T1D donors with spleen standalone scRNAseq only')
    assert broad['sample_requirements']['modality_groups']==[['scRNA-seq','snMultiomics']]
    assert exact['sample_requirements']['modality_groups']==[['scRNA-seq']]
    assert validate_cypher(query(broad),broad)==[]
    assert validate_cypher(query(exact),exact)==[]
    assert 'CITE-seq Protein' not in str(broad['sample_requirements'])


def test_wrong_owner_and_nonexistent_property_rejected():
    p=prepared();q=query(p)
    assert validate_cypher(q.replace('d.t1d_stage','c.t1d_stage'),p)
    assert any(x.startswith('invalid_node_property') for x in validate_cypher(q+' , s0.anatomical_structure_id',p))
    assert validate_cypher(q.replace('d.data_source = "HPAP"','d.data_source = "OTHER"'),p)
    assert validate_cypher(q.replace('(d)-[ds0','(c)-[ds0'),p)
    assert validate_cypher(q+' LIMIT 10',p)


def test_rna_and_atac_separate_samples_must_share_donor_and_tissue():
    p=prepared('Find HPAP stage 3 T1D donors with spleen RNA and ATAC data')
    assert len(p['sample_requirements']['modality_groups'])==2
    q=query(p);assert validate_cypher(q,p)==[]
    assert validate_cypher(q.replace('(d)-[ds1','(other:donor)-[ds1'),p)
    assert validate_cypher(q.replace('t1.name = "spleen"','t1.name = "blood"'),p)
    joint=prepared('Find HPAP stage 3 T1D donors with spleen paired RNA and ATAC multiome data')
    assert joint['sample_requirements']['modality_groups']==[['snMultiomics']]
    assert validate_cypher(query(joint),joint)==[]


def test_unknown_stage_never_substituted_and_resolution_is_fast():
    assert prepared('Find HPAP stage 13 T1D donors')['semantic_issues']
    start=time.perf_counter()
    for _ in range(100):prepared()
    assert (time.perf_counter()-start)/100<.2


def test_empty_wrappers_and_scalar_zero():
    assert not meaningful_row({'nodes':[],'edges':[]})
    assert meaningful_row({'value':None})
    assert meaningful_row({'count':0})
    assert meaningful_row({'value':False})


def test_summary_deduplicates_without_claiming_downloads():
    d={'id':'HPAP-X','labels':['donor'],'properties':{'data_source':'HPAP','t1d_stage':STAGES['3']}}
    s={'id':'S1','labels':['Sample_node'],'properties':{'data_modality':'snMultiomics'}}
    edge={'type':'HAS_SAMPLE','start_id':'HPAP-X','end_id':'S1'}
    summary=donor_summary({'nodes':[d,d,s,s],'edges':[edge,edge]})
    assert summary['unique_donors']==summary['unique_samples']==1
    assert summary['rows'][0]['file_availability']=='not_verified'
    assert summary['assay_capabilities']['snMultiomics']['components']==['RNA','ATAC']


def test_map_property_wrong_owner_and_modality_node_equivalence():
    p=prepared();q=query(p)
    assert validate_cypher(q.replace('(c:disease)','(c:disease {t1d_stage:"wrong"})'),p)
    q=q.replace(' WHERE ', ' MATCH (m:data_modality)-[:HAS_SAMPLE]->(s0) WHERE ').replace('s0.data_modality IN','m.id IN')
    assert validate_cypher(q,p)==[]
    assert validate_cypher(q.replace('->(s0) WHERE','->(unrelated:Sample_node) WHERE'),p)


def test_runtime_retrieval_does_not_count_empty_collections():
    import asyncio
    from types import SimpleNamespace
    from tests_vnext.test_graph import FakeSession, FakeTransaction
    from pankagent_vnext.graph import GraphAdapter
    async def check():
        adapter=object.__new__(GraphAdapter)
        adapter.settings=SimpleNamespace(graph_timeout=1,max_nodes=20,max_edges=20,max_bytes=10000)
        adapter._session=lambda:FakeSession(FakeTransaction([{'nodes':[],'edges':[]}]))
        result=await adapter._retrieve('RETURN [] AS nodes, [] AS edges',{})
        assert result['status']=='empty' and result['rows']==[]
        adapter._session=lambda:FakeSession(FakeTransaction([{'count':0}]))
        result=await adapter._retrieve('RETURN 0 AS count',{})
        assert result['status']=='complete' and result['rows']==[{'count':0}]
    asyncio.run(check())


def test_old_donor_run_advisory_does_not_rewrite_durable_content():
    from pankagent_vnext.app import public_run
    old={'plan':{'steps':[{'question':'HPAP donors'}],'contract_sha256':'old'},'graph_answer':'old answer','status':'completed'}
    original=copy.deepcopy(old)
    assert 'rerun_advisory' in public_run(old)
    assert old==original


def test_extra_tissue_string_filter_and_sample_join_rejected():
    p=prepared();q=query(p).replace(' RETURN d',' AND s0.anatomical_structure = "spleen" RETURN d')
    assert 'unrequested_identity_filter:anatomical_structure' in validate_cypher(q,p)
    p=prepared('Find HPAP stage 3 T1D donors');q=query(p).replace(' WHERE',' MATCH (d)-[:HAS_SAMPLE]->(s:Sample_node) WHERE')
    assert 'unrequested_sample_join_for_donor_only_lookup' in validate_cypher(q,p)


def test_aliased_owner_keeps_type_and_connectivity():
    p=prepared();q=query(p).replace(' WHERE ',' WITH c, d AS other, s0, t0 WHERE ').replace('d.t1d_stage','other.t1d_stage').replace('d.data_source','other.data_source')
    assert validate_cypher(q,p)==[]


def test_including_multiome_is_not_paired_only_and_exclusion_is_preserved():
    p=prepared('Find HPAP stage 3 T1D donors with spleen RNA data including multiome')
    assert p['sample_requirements']['modality_groups']==[['scRNA-seq','snMultiomics']]
    p=prepared('Find HPAP stage 3 T1D donors with spleen standalone scRNAseq, exclude multiomics')
    assert p['sample_requirements']['modality_groups']==[['scRNA-seq']]
