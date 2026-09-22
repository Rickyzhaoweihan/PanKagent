"""Verified detection-property ownership; includes projections and ORDER BY.

The property set is from the complete PanKgraph_08_04 relationship inventory.
Node identifiers and properties on other relationship types are unaffected.
"""
import hashlib
from pathlib import Path

RELEASE='PanKgraph_08_04'
VERSION='detection-property-ownership-v1'
PROPERTIES=frozenset({
    'cell_type','condition','data_source','data_version','detection_rule','end_id',
    'expression_call','gene_symbol','max_donor_log_cpm','max_pct_cells_expressing',
    'mean_donor_cpm','mean_donor_log_cpm','mean_pct_cells_expressing',
    'median_donor_cpm','median_donor_log_cpm','median_pct_cells_expressing',
    'min_donor_log_cpm','min_median_log_cpm_threshold','min_median_pct_cells_threshold',
    'source_file','start_id','total_cells',
})
DIGEST=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validation_errors(tokens,step,parameters):
    if step.get('graph_version')!=RELEASE:
        return []
    from .graph import _predicate_owner
    variables=set()
    for index,token in enumerate(tokens[:-3]):
        if token.value!='[' or tokens[index+1].kind not in ('WORD','IDENT') or tokens[index+2].value!=':':
            continue
        end=next((j for j in range(index+3,len(tokens)) if tokens[j].value in ('{',']')),len(tokens))
        kinds={tokens[j+1].value for j in range(index+2,end-1) if tokens[j].value in (':','|')}
        # Alternatives remain structurally checked by the existing validator.
        if 'GENE_DETECTED_IN' in kinds:
            variables.add(tokens[index+1].value)
    for index,token in enumerate(tokens[1:-1],1):
        if (token.kind=='WORD' and token.value.upper()=='AS' and tokens[index-1].value in variables
                and (index<2 or tokens[index-2].value!='.')
                and tokens[index+1].kind in ('WORD','IDENT')):
            variables.add(tokens[index+1].value)
    errors=[]
    for index,token in enumerate(tokens):
        access=index>=2 and tokens[index-1].value=='.'
        mapped=(index>0 and index+1<len(tokens) and tokens[index-1].value in ('{',',') and tokens[index+1].value==':')
        if (token.kind in ('WORD','IDENT') and (access or mapped)
                and _predicate_owner(tokens,index) in variables and token.value not in PROPERTIES):
            errors.append('invalid_relation_property:GENE_DETECTED_IN.'+token.value+':use_recorded_detection_fields')
    return sorted(set(errors))
