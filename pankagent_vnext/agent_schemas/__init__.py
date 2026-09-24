"""Versioned agent facts. No models, database access, or executable schema code."""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
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
        registry = data['graph_storage']['registry']
        labels = set(registry['nodes'])
        for relation, spec in registry['relations'].items():
            for path in spec['paths']:
                if not set(path['source'] + path['target']) <= labels:
                    raise ValueError('invalid_agent_schema_endpoints:' + relation)
        if not set(data['identity']['public_labels']) <= labels:
            raise ValueError('invalid_agent_schema_identity_labels')
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
    # A release packages one pack. No per-request environment changes or hot reload.
    return SchemaPack(ROOT / 'packs' / 'pankgraph')


def module(name):
    return active_pack().module(name)


def run_context(settings):
    root = ROOT.parent
    names = ('llm.py', 'planning_session.py', 'planning_contract.py', 'graph_contract.py')
    return {'agent_schema': active_pack().identity(),
            'model': getattr(settings, 'model', None),
            'prompt_sha256': hashlib.sha256(b''.join((root / n).read_bytes() for n in names)).hexdigest(),
            'database': {'backend': 'neo4j', 'release': getattr(settings, 'graph_version', None),
                         'name': getattr(settings, 'neo4j_database', None)}}
