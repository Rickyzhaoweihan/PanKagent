"""PanKgraph release vocabulary shared by planning and GPU request construction.

Labels/relationships verified by read-only RL metadata on 2026-09-07.
Measurement guidance is release-specific, not a scientific answer template.
"""
import hashlib
import json

VERSION = 'pankgraph-08-04-intent-v3'
RELATIONS = {
    'GENE_ENRICHED_IN': 'Gene -> anatomical_structure; measured enrichment, not exclusive expression. Properties: padj, pvalue, log2_fold_change, rank_in_cell_type, condition.',
    'GENE_DETECTED_IN': 'Gene -> anatomical_structure; detection/expression, not enrichment. Return recorded measurements and condition without inventing significance cutoffs.',
    'MARKER_GENE_OF': 'Gene -> anatomical_structure; marker annotation is distinct from measured enrichment.',
    'T1D_DEG_IN': 'Gene -> anatomical_structure; differential expression in T1D. T1D context is encoded by this relationship, not a disease endpoint or extra disease-id predicate. No GENE_ANNOTATION join exists. adjusted_p_value is distinct from enrichment padj.',
    'EFFECTOR_GENE_OF': 'Gene -> disease; effector prioritization evidence, not differential expression. Preserve the requested disease.',
    'FUNCTION_ANNOTATION': 'Gene -> kegg or reactome; pathway membership is annotation, not pathway activation.',
    'ASSOCIATED_WITH_GO': 'Gene -> GO_term; GO_term.go_domain stores biological_process, molecular_function or cellular_component. Neither namespace nor ontology_namespace exists. Constrain the GO_term node go_domain for a requested ontology domain.',
    'PHYSICAL_INTERACTION': 'Gene interaction evidence; preserve both requested endpoints.',
    'GENETIC_INTERACTION': 'Gene interaction evidence; preserve both requested endpoints.',
    'PART_OF_QTL_SIGNAL': 'Molecular QTL evidence; tissue_id, tissue_name, nominal_p, pip. Molecular association is not disease association.',
    'PART_OF_GWAS_SIGNAL': 'Disease association/credible-set evidence; PIP is candidate support, not effect size.',
    'SIGNAL_COLOC_WITH': 'Colocalization evidence; shared-signal support is not proof of mechanism.',
    'GENE_ACTIVITY_SCORE_IN': 'Gene activity; cohort-specific measurements are columns, not invented condition_id predicates.',
    'HAS_DONOR': 'Disease and donor cohort link; use actual donor diabetes_type/derived_diabetes_status fields.',
    'HAS_SAMPLE': 'Sample linkage; Sample_node is the node label.',
    'OCR_PEAK_IN': 'Chromatin accessibility evidence.',
    'FGSEA_ENRICHED_IN': 'Gene-set enrichment evidence, not direct gene expression.',
    'PART_OF': 'Anatomical containment.', 'HAS_CELL_TYPE': 'Anatomical cell-type linkage.',
    'SUBCLASS_OF': 'Cell-type subclass linkage.', 'HAS_STATE': 'Cell-state linkage.',
    'REPRESENTS_COMPOSITE_LABEL': 'Composite cell-label linkage.',
    'LYMPH_FLOWS_TO': 'Anatomical lymph flow.', 'ADJACENT_TO': 'Anatomical adjacency.',
}
LABELS = ['Gene', 'disease', 'anatomical_structure', 'GO_term', 'reactome', 'kegg',
          'variants', 'donor', 'Sample_node', 'data_modality', 'OCR_peak',
          'regulatory_elements', 'ontology', 'sequence_variant', 'snv', 'deletion',
          'indel', 'insertion', 'provenance']
from .semantic_registry import DIGEST as SEMANTIC_DIGEST
DIGEST = hashlib.sha256(json.dumps({'semantics': SEMANTIC_DIGEST, 'version': VERSION, 'relations': RELATIONS, 'labels': LABELS}, sort_keys=True).encode()).hexdigest()


def planner_notes():
    return '\nVerified release contract '+VERSION+' (no other relationship names):\n' + '\n'.join(k+': '+v for k,v in RELATIONS.items())


def generation_request(step, base_question):
    """Add binding guidance without removing any original biological modifier."""
    relations = step.get('relation_types') or []
    bindings = []
    resolved = {e.get('constraint_index'): e for e in step.get('resolved_entities', []) if e.get('state') == 'resolved'}
    for i, c in enumerate(step.get('constraints', [])):
        e = resolved.get(i)
        if e:
            bindings.append(f"{e['entity_type']} with id {json.dumps(e['id'])} (verified name {json.dumps(e.get('name'))}); these identify the same entity")
        else:
            bindings.append(f"{c.get('entity_type') or 'recorded property'} {c.get('property')} {c.get('operator')} {json.dumps(c.get('value'))}")
    if step.get('semantic_registry'):
        requirements=step.get('sample_requirements',{})
        samples=bool(requirements.get('modality_groups')) or any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[]))
        paths='disease -HAS_DONOR-> donor'
        if samples:paths+='; donor -HAS_SAMPLE-> Sample_node; anatomical_structure -HAS_SAMPLE-> the SAME Sample_node'
        text=base_question+'\nUse these verified bindings (replace shorthand values in the question): '+'; '.join(bindings)+'.'
        text+='\nRequired connected schema paths: '+paths+'.'
        if samples:text+=' Sample_node.data_modality stores the assay. Tissue is matched on the linked anatomical_structure; do not filter the descriptive Sample_node.anatomical_structure string.'
        else:text+=' Donor-only lookup: no sample or assay joins or filters.'
        if requirements.get('separate_bindings'):text+=' For RNA AND ATAC use two Sample_node variables, each linked to the SAME donor and requested tissue and constrained to its corresponding modality group. They may identify the same multiome sample.'
        text+=' Return all matched nodes and connecting relationships with properties, as nodes and edges. No LIMIT, SKIP, list slices, rank, or additional filters.'
        if len(text)>4000:raise ValueError('generation_question_too_long')
        return text
    notes = [RELATIONS[r] for r in relations if r in RELATIONS]
    suffix = '\nRequired relationship types: '+', '.join(relations)+'.' if relations else ''
    if bindings: suffix += '\nRequired entity/property constraints: '+'; '.join(bindings)+'.'
    if notes: suffix += '\n'+'\n'.join(notes)
    suffix += '\nReturn matching nodes and relationships with properties. Do not add unrequested disease, donor, significance or rank filters.'
    if step.get('complete', True): suffix += ' Return all matches without LIMIT, SKIP or list slices.'
    from .semantic_registry import generation_guidance
    result = base_question + suffix + generation_guidance(step)
    # Never silently truncate a scientific constraint to fit the API.
    if len(result) > 4000:
        raise ValueError('generation_question_too_long')
    return result

MEASUREMENTS = {'GENE_ENRICHED_IN', 'GENE_DETECTED_IN', 'MARKER_GENE_OF', 'T1D_DEG_IN'}


def independent_measurement_steps(plan):
    """Prevent an accidental inner join from narrowing independent measurements."""
    from copy import deepcopy
    result = deepcopy(plan)
    expanded = []
    mapping = {}
    for step in result.get('steps', []):
        kinds = step.get('relation_types') or []
        if len(kinds) > 1 and set(kinds) <= MEASUREMENTS and step.get('evidence_combination', 'independent') == 'independent':
            if any(c.get('property') not in ('id', 'name') for c in step.get('constraints', [])):
                result.update(steps=[], clarification='These measurements have different filters. Please separate the measurement checks so each keeps its intended condition and statistical criteria.')
                return result
            ids = []
            for index, kind in enumerate(kinds):
                child = deepcopy(step)
                child['id'] = step['id'] + '_measurement_' + str(index + 1)
                child['relation_types'] = [kind]
                child['question'] = step['question'] + '\nRetrieve only '+kind+' evidence in this step; the other measurement types are checked independently. Do not require the other relationships to exist.'
                child['title'] = step.get('title', 'Check recorded evidence') + ' — ' + {'GENE_ENRICHED_IN':'enrichment', 'GENE_DETECTED_IN':'detection', 'MARKER_GENE_OF':'marker annotation', 'T1D_DEG_IN':'differential expression'}[kind]
                ids.append(child['id']); expanded.append(child)
            mapping[step['id']] = ids
        else:
            expanded.append(step)
    if len(expanded) > 3:
        result.update(steps=[], clarification='This comparison needs more than three independent evidence checks. Please narrow the requested measurements or entities.')
        return result
    for step in expanded:
        step['depends_on'] = [child for parent in step.get('depends_on', []) for child in mapping.get(parent, [parent])]
    result['steps'] = expanded
    return result


def normalize_release_constraints(step):
    """Bind two verified release concepts without changing generated Cypher."""
    from copy import deepcopy
    result = deepcopy(step)
    kinds = set(result.get('relation_types') or [])
    constraints, mappings = [], list(result.get('schema_bindings') or [])
    for constraint in result.get('constraints', []):
        c = dict(constraint)
        if kinds == {'ASSOCIATED_WITH_GO'} and c.get('property') in ('namespace', 'ontology_namespace', 'go_domain') and c.get('entity_type') in (None, 'GO_term'):
            if c.get('property') != 'go_domain' or c.get('entity_type') != 'GO_term':
                mappings.append({'from': deepcopy(c), 'to': 'GO_term.go_domain', 'contract': VERSION})
            c.update(property='go_domain', entity_type='GO_term')
        if kinds == {'T1D_DEG_IN'} and c.get('entity_type') == 'disease' and c.get('operator', '=') == '=' and (c.get('property'), c.get('value')) in (('id', 'MONDO_0005147'), ('name', 'type 1 diabetes')):
            mappings.append({'from': deepcopy(c), 'to': 'required relationship T1D_DEG_IN', 'contract': VERSION})
            continue
        constraints.append(c)
    result['constraints'] = constraints
    if mappings: result['schema_bindings'] = mappings
    return result
