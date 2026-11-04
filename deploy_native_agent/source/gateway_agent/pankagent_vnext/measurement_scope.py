"""Release-specific evidence granularity; no query execution or scope removal."""
import hashlib
from pathlib import Path

VERSION = 'measurement-scope-v1'
RELEASE = 'PanKgraph_08_04'
CELL_SUMMARIES = {'GENE_DETECTED_IN', 'GENE_ENRICHED_IN', 'T1D_DEG_IN', 'GENE_ACTIVITY_SCORE_IN'}
# These fields do not identify strata in the stored gene-to-cell summaries.
DONOR_STRATA = {'id', 'name', 'age', 'bmi', 'gender', 'sex_at_birth', 't1d_stage',
               'diabetes_type', 'derived_diabetes_status', 'hba1c_percentage',
               'hla_typing', 'hla_status', 'aab_state', 'race',
               'predicted_genetic_ancestry', 'c_peptide_ng_ml', 'diabetes_duration'}
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def measurement_scope_recovery(step, graph_release):
    if graph_release != RELEASE or not set(step.get('relation_types') or []) & CELL_SUMMARIES:
        return None
    constraints = [c for c in step.get('constraints', []) if c.get('entity_type') == 'donor'
                   and str(c.get('property', '')).split('.')[-1] in DONOR_STRATA]
    if not constraints:
        return None
    return {
        'category': 'unsupported_expression_stratification',
        'title': 'This comparison needs donor-level expression data',
        'message': ('The graph stores these gene measurements as cell-type summaries, rather than values linked to each donor. '
                    'Applying your donor filters to those summaries would not produce the requested comparison. '
                    'Your filters have been kept. You can request the recorded cell-type summary, or retain your original question for an analysis using donor-level expression data.'),
        'retryable': False,
        'suggestions': [{
            'label': 'Show the recorded cell-type summary',
            'instruction': ('Change this expression comparison to the existing cell-type summary without donor-specific stratification. '
                            'Keep the requested genes, cell types, tissues and evidence categories; state that this does not answer the original donor-filtered comparison.')
        }, {
            'label': 'Keep donor and expression checks separate',
            'instruction': ('Keep my donor filters in a separate donor/sample lookup, and retrieve the existing expression summary for the requested genes and cell types independently. '
                            'Do not describe that expression summary as measured in the filtered donors. Keep the original donor-specific comparison explicitly unanswered.')
        }],
        'evidence': {'graph_release': RELEASE, 'contract_version': VERSION,
                     'source': 'verified release relationship endpoints and measurement properties',
                     'unavailable_operation': 'join donor metadata to donor-resolved gene measurements',
                     'preserved_donor_filters': constraints},
    }
