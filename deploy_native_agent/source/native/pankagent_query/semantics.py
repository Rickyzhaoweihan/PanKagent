"""Release-owned semantics; no dataset identifiers or interpretation rules in code."""
import copy
import hashlib
import json
from pathlib import Path


def augment(pack, semantics):
    out = copy.deepcopy(pack)
    out['semantics_version'] = semantics['version']
    out['semantics_sha256'] = hashlib.sha256(json.dumps(semantics, sort_keys=True).encode()).hexdigest()
    out['scope_families'] = copy.deepcopy(semantics.get('scope_families', []))
    out['interpretation'] = copy.deepcopy(semantics['interpretation'])
    out['semantics_provenance'] = copy.deepcopy(semantics['provenance'])
    out['identity_fields'] = {k: v for k, v in semantics['identity_fields'].items() if k in out['nodes']}
    for kind, spec in out['relations'].items():
        spec.update(copy.deepcopy(semantics['relation_semantics'].get(kind, {})))
    return out


def interpretation(pack, labels=(), relations=(), evidence=None):
    """Select schema-owned rules using schema labels and observed fields only."""
    data = pack.get('interpretation', {})
    found = {'nodes': set(labels), 'edges': set(relations)}
    props, values = set(), set()
    def scan(value):
        if isinstance(value, dict):
            props.update(value)
            for v in value.values(): scan(v)
        elif isinstance(value, list):
            for v in value: scan(v)
        elif isinstance(value, str): values.add(value)
    scan(evidence or {})
    for kind in found:
        for canonical, aliases in data.get('aliases', {}).get(kind, {}).items():
            if {str(v).casefold() for v in found[kind]} & {str(v).casefold() for v in [canonical]+aliases}:
                found[kind].add(canonical)
    selected = []
    for rule in data.get('rules', []):
        match = rule['match']
        ok = True
        for kind in found:
            any_of, all_of = set(match.get(kind+'_any', [])), set(match.get(kind+'_all', []))
            if any_of and not any_of & found[kind] or not all_of <= found[kind]: ok = False
        if len(found['edges'] & set(match.get('edges_any', []))) < match.get('min_edge_types', 0): ok = False
        if 'properties_any' in match or 'values_any' in match:
            ok = ok and bool(props & set(match.get('properties_any', [])) or values & set(match.get('values_any', [])))
        if ok: selected.append(copy.deepcopy(rule))
    return {'version': pack.get('semantics_version'), 'rules': selected}


def build(schema, semantics, output):
    source = json.loads(Path(schema).read_text())
    result = augment(source, json.loads(Path(semantics).read_text()))
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--schema', required=True); p.add_argument('--semantics', required=True); p.add_argument('--output', required=True)
    a = p.parse_args(); build(a.schema, a.semantics, a.output)
