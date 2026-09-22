"""Keep recorded colocalization separate from optional molecular/GWAS records.

In this release SIGNAL_COLOC_WITH is Gene -> disease. A missing separately
indexed QTL edge must not eliminate a recorded colocalization result. These
checks reject an unsupported combined retrieval; they never rewrite a query.
"""
import hashlib
import json
from pathlib import Path

VERSION = 'coloc-primary-evidence-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validation_errors(tokens, step, parameters):
    # A relationship need not have a variable. Read type tokens inside actual
    # -[...] patterns, including anonymous edges, before any property map. A
    # name-to-type binding map alone misses -[:TYPE]-> and admits false empties.
    kinds = set()
    for i, token in enumerate(tokens):
        if token.value != '[' or i == 0 or tokens[i - 1].value != '-':
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].value not in {'{', ']'}:
            if (tokens[j].value in {':', '|'} and j + 1 < len(tokens)
                    and tokens[j + 1].kind in {'WORD', 'IDENT'}):
                kinds.add(tokens[j + 1].value)
            j += 1
    if 'SIGNAL_COLOC_WITH' not in kinds:
        return []
    if kinds & {'PART_OF_GWAS_SIGNAL', 'PART_OF_QTL_SIGNAL'}:
        return ['coloc_requires_independent_evidence_checks']
    return []


GUIDANCE = (
    'Recorded colocalization uses (gene:Gene)-[coloc:SIGNAL_COLOC_WITH]->(disease:disease). '
    'Do not attach mandatory or optional PART_OF_GWAS_SIGNAL/PART_OF_QTL_SIGNAL patterns '
    'to this evidence check. Those categories are retrieved independently. '
    'A missing separately indexed QTL edge is not missing colocalization evidence. '
    'Preserve the gene, disease and source filters; use recorded signal identifiers '
    'for linkage, never a shared gene alone.'
)


def compact_generation_request(step, base_question=None):
    """Describe one previously normalized primary check without prompt repetition.

    The original question and normalization provenance stay unchanged on the
    plan. Arbitrary question text cannot enter this shortcut: the current scope
    contract must already have decomposed it into this exact complete check.
    Extra predicates, unresolved names, dependencies or other scopes retain the
    ordinary full generation guidance. This emits instructions, never Cypher.
    """
    from .coloc_scope import DIGEST as scope_digest, RELEASE
    scope = step.get('coloc_scope') or {}
    constraints = step.get('constraints') or []
    if (scope.get('role') != 'primary' or scope.get('digest') != scope_digest
            or step.get('graph_version') != RELEASE
            or step.get('relation_types') != ['SIGNAL_COLOC_WITH']
            or step.get('complete') is not True or step.get('depends_on')
            or step.get('ranking_contract') or step.get('ranking_issue')
            or len(constraints) != 2):
        return None
    bindings = {}
    for index, constraint in enumerate(constraints):
        kind = constraint.get('entity_type')
        if (kind not in {'Gene', 'disease'} or kind in bindings
                or constraint.get('operator') != '=' or constraint.get('property') not in {'id', 'name'}
                or constraint.get('owner_kind', 'node') != 'node' or constraint.get('relationship_type')):
            return None
        identifier = constraint.get('value') if constraint.get('property') == 'id' else None
        resolved = [item for item in step.get('resolved_entities') or []
                    if item.get('constraint_index') == index and item.get('state') == 'resolved'
                    and item.get('entity_type') == kind]
        if resolved:
            ids = {item.get('id') for item in resolved if isinstance(item.get('id'), str)}
            if len(ids) != 1 or identifier is not None and identifier not in ids:
                return None
            identifier = next(iter(ids))
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            return None
        bindings[kind] = identifier
    if set(bindings) != {'Gene', 'disease'}:
        return None
    request = (
        'Find every recorded colocalization relationship for the following exact gene and disease in PanKgraph. '
        'Generate one read-only Cypher query.\n'
        f'Node identity filters: Gene.id = {json.dumps(bindings["Gene"])}; disease.id = {json.dumps(bindings["disease"])}.\n'
        'Directed schema: Gene is the source, SIGNAL_COLOC_WITH is the relationship, disease is the target. '
        'This check consists of that single relationship type.\n'
        'Recorded relationship fields include pp_h4_abf, gwas_signal_id, qtl_signal_id, gwas_lead_vars, '
        'qtl_lead_vars, coloc_dataset, data_source and data_version. Identity id belongs to the nodes.\n'
        'Return all matching full Gene and disease nodes as nodes, and full relationship objects with their '
        'properties as edges. Use collections of the actual nodes and relationships. '
        'All records are required; no LIMIT, SKIP, list slicing, ranking or additional predicates. '
        'An unsorted complete result is appropriate.'
    )
    return request if len(request) <= 1500 else None
