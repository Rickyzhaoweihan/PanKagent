"""Versioned agent facts. No models, database access, or executable schema code."""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parent
MODULES = ('graph_storage', 'identity', 'semantics_modalities', 'query_patterns', 'validation_repair')
COMPILERS = ('cohort_scope', 'property_owners', 'independent_measurements', 'coloc_tissue',
             'requested_scope', 'genomic_scope', 'dependency_bindings')


class SchemaPack:
    """Snapshot loaded once; callers receive copies, never shared mutable facts."""
    def __init__(self, directory):
        directory = Path(directory).resolve()
        self._data = {'manifest': json.loads((directory / 'manifest.json').read_text())}
        manifest = self._data['manifest']
        if manifest.get('schema') != {'id': 'kg-agent.pack', 'version': '1.0.0'}:
            raise ValueError('unsupported_agent_schema_contract')
        if set(manifest.get('modules', {})) != set(MODULES):
            raise ValueError('invalid_agent_schema_modules')
        for name, filename in manifest['modules'].items():
            path = (directory / filename).resolve()
            if path.parent != directory or path.suffix != '.json':
                raise ValueError('invalid_agent_schema_path')
            self._data[name] = json.loads(path.read_text())
        self._validate()
        self.digest = hashlib.sha256(json.dumps(self._data, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=False).encode()).hexdigest()

    def _validate(self):
        data = self._data
        for name in MODULES:
            if data[name].get('schema', {}).get('version') != '1.0.0':
                raise ValueError('unsupported_agent_schema_module:' + name)
        # JSON Schema documents are available to editors/scripts; enforce their
        # top-level field/type contract without adding a runtime dependency.
        types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
        for name, value in data.items():
            contract = json.loads((ROOT / 'contracts' / (name + '.schema.json')).read_text())
            if set(value) != set(contract['required']):
                raise ValueError('invalid_agent_schema_fields:' + name)
            for key, spec in contract['properties'].items():
                if 'const' in spec and value[key] != spec['const']:
                    raise ValueError('invalid_agent_schema_value:' + name + ':' + key)
                if 'type' in spec and type(value[key]) is not types[spec['type']]:
                    raise ValueError('invalid_agent_schema_type:' + name + ':' + key)
        registry = data['graph_storage']['registry']
        labels = set(registry['nodes'])
        symbols = list(labels) + list(registry['relations'])
        symbols += [p for props in registry['nodes'].values() for p in props]
        symbols += [p for spec in registry['relations'].values() for p in spec['properties']]
        identity = data['identity']
        symbols += [identity['id_field'], identity['synonym_field'], *identity['name_fields']]
        if any(not isinstance(s, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', s) for s in symbols):
            raise ValueError('invalid_agent_schema_identifier')
        for relation, spec in registry['relations'].items():
            for path in spec['paths']:
                if not set(path['source'] + path['target']) <= labels:
                    raise ValueError('invalid_agent_schema_endpoints:' + relation)
        if not set(data['identity']['public_labels'] + data['identity'].get('exact_only_labels', [])) <= labels:
            raise ValueError('invalid_agent_schema_identity_labels')
        for reference in identity['session_references']:
            if (set(reference) != {'entity_type', 'mention_pattern', 'query_relations'}
                    or reference['entity_type'] not in labels
                    or not set(reference['query_relations']) <= set(registry['relations'])):
                raise ValueError('invalid_agent_schema_session_reference')
            re.compile(reference['mention_pattern'])
        library = data['query_patterns']['library']
        if library['graph_release'] != registry['release']:
            raise ValueError('agent_schema_release_mismatch')
        for rule in library['rules']:
            if not set(rule['relations']) <= set(registry['relations']):
                raise ValueError('invalid_agent_schema_pattern:' + rule['id'])
            for source, relation, target in rule['required_paths']:
                if not any(source in p['source'] and target in p['target']
                           for p in registry['relations'].get(relation, {}).get('paths', [])):
                    raise ValueError('invalid_agent_schema_pattern_path:' + rule['id'])
        if data['validation_repair']['scope_compilers'] != list(COMPILERS):
            raise ValueError('invalid_agent_schema_compiler_pipeline')
        materialization = data['validation_repair'].get('backend_materialization', {})
        for key, ceiling in {'max_nodes': 12000, 'max_edges': 20000, 'max_bytes': 12000000}.items():
            if type(materialization.get(key)) is not int or not 0 < materialization[key] <= ceiling:
                raise ValueError('invalid_backend_materialization_limit:' + key)
        limits = data['validation_repair']['limits']
        ceilings = {'lookup_batches': 2, 'requests_per_batch': 6, 'candidates_per_request': 10,
                    'planning_proposals': 3, 'execution_repairs': 2, 'total_claude_calls': 7}
        if set(limits) != set(ceilings) or any(type(limits[k]) is not int or not 0 < limits[k] <= v
                                             for k, v in ceilings.items()):
            raise ValueError('invalid_agent_schema_limits')

    def module(self, name):
        return deepcopy(self._data[name])

    def identity(self):
        manifest = self._data['manifest']
        return {'id': manifest['pack']['id'], 'version': manifest['pack']['version'],
                'sha256': self.digest, 'graph_release': self._data['graph_storage']['registry']['release'],
                'standard': deepcopy(manifest['standard'])}


@lru_cache(maxsize=1)
def active_pack():
    # Startup-only selection. Never supplied by a user question or hot reloaded.
    return SchemaPack(os.environ.get('PANK_VNEXT_SCHEMA_PACK', ROOT / 'packs' / 'pankgraph'))


def module(name):
    return active_pack().module(name)


def run_context(settings):
    root = ROOT.parent
    names = ('llm.py', 'planning_session.py', 'planning_contract.py', 'graph_contract.py')
    return {'application_sha256': hashlib.sha256(b''.join(
                p.relative_to(root).as_posix().encode() + p.read_bytes()
                for p in sorted(root.rglob('*.py')))).hexdigest(),
            'agent_schema': active_pack().identity(),
            'model': getattr(settings, 'model', None),
            'prompt_sha256': hashlib.sha256(b''.join((root / n).read_bytes() for n in names)).hexdigest(),
            'database': {'backend': 'neo4j', 'release': getattr(settings, 'graph_version', None),
                         'name': getattr(settings, 'neo4j_database', None)}}
