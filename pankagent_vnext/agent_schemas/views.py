"""Read-only compatibility projections. No independently editable graph facts."""
from copy import deepcopy


def temporary_comment_text(sem, release, relation=None):
    """Render reviewed release-scoped notes without modifying stored graph facts."""
    return '\n'.join('Temporary schema comment — '+c['section']+': '+c['text']
                     for c in sem.get('temporary_comments', [])
                     if c['graph_release'] == release
                     and (relation is None or relation in c['relations']))


def project(data, name):
    db, sem = data['database_schema'], data['semantic_interpretation']
    if name == 'identity':
        return {'schema': {'id': 'kg-agent.identity', 'version': '2.0.0'}, **db['lookup']}
    if name == 'graph_storage':
        registry = {'version': db['registry_version'], 'release': db['release'],
                    'identity_strength': db['identity_strength'],
                    'nodes': {k: list(v['properties']) for k, v in db['nodes'].items()},
                    'relations': {k: {'paths': v['endpoints'], 'properties': list(v['properties'])}
                                  for k, v in db['relationships'].items()},
                    'categories': {k+'.'+p: spec['reviewed_values'] for k, v in db['relationships'].items()
                                   for p, spec in v['properties'].items() if 'reviewed_values' in spec},
                    'aliases': {k: v['aliases'] for k, v in db['nodes'].items() if v['aliases']}}
        return {'schema': {'id': 'kg-agent.graph-storage', 'version': '2.0.0'},
                'registry': registry, 'relationship_guidance': {k: v+'\n'+temporary_comment_text(sem, db['release'], k)
                                              for k,v in sem['relation_guidance'].items()},
                'labels': sem['display_labels'], 'postgresql': db['postgresql']}
    if name == 'semantics_modalities':
        result = deepcopy({k: v for k, v in sem.items() if k not in {'bim','relation_guidance','display_labels'}})
        comments = temporary_comment_text(sem, db['release'])
        if comments:
            result['planning_instructions'] = {k:v+'\n'+comments for k,v in result['planning_instructions'].items()}
        result['terminology']['PROPERTIES'] = {k: list(db['nodes'][k]['properties'])
                                              for k in result['terminology'].pop('property_type_refs')}
        result['anatomy'] = {'ALIASES': {r['id']: [r['name'], r['aliases']]
                            for r in db['nodes']['anatomical_structure']['reviewed_aliases']}}
        result['intent_vocabulary'] = {
            'RELATION_TERMS': {k: v['query_terms'] for k, v in db['relationships'].items() if v['query_terms']},
            'ENTITY_TYPE_TERMS': {k: v['query_terms'] for k, v in db['nodes'].items() if v['query_terms']}}
        result['request_fields'] = {key: {p: v['request_pattern'] for p, v in db['nodes'][label]['properties'].items()
                                         if v['request_pattern']}
                                   for label, key in [('donor','_DONOR_REQUEST_FIELD_PATTERNS'),
                                                       ('Sample_node','_SAMPLE_REQUEST_FIELD_PATTERNS')]}
        return result
    if name == 'validation_repair':
        return data['validation']
    return data[name]
