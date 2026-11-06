"""Validate identity field usage against schema and observed resolver candidates."""
from .core import Feedback


def explicit_identity(value, question):
    import re
    return re.search(r"(?<!\w)"+re.escape(str(value))+r"(?!\w)", question) is not None


def validate_bindings(pack, pattern, contexts, question="", require_resolved_qualifiers=False, queries=None):
    candidates = [c for ctx in contexts if ctx.get('tool') == 'resolve_entity'
                  for c in ctx.get('output', {}).get('candidates', [])]
    for node in pattern['nodes']:
        fields = pack.get('identity_fields', {}).get(node['label'], {})
        primaries = [f for f,s in fields.items() if s.get('role') == 'primary']
        compatible = [c for c in candidates if node['label'] in c.get('labels', [])]
        for f in node.get('filters', []):
            if f['property'] not in primaries or f.get('op') not in ('eq', 'in'): continue
            for value in f['value'] if f['op'] == 'in' else [f['value']]:
                if any(c['properties'].get(f['property']) == value for c in compatible): continue
                matches = []
                for c in compatible:
                    for key, v in c['properties'].items():
                        if key == f['property']: continue
                        if value in (v if isinstance(v, list) else [v]):
                            matches.append({'field': key, 'value': value, 'node_identity': c.get('node_identity', {k:c['properties'].get(k) for k in primaries})})
                if matches:
                    raise Feedback('Resolved value belongs to another identity/name field, not the primary ID. Use exact returned node IDs or the original field: '+str(matches))
