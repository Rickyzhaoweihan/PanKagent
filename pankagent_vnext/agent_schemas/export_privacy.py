"""Export checks return only counts/paths, never protected record values."""
from collections.abc import Mapping


def audit_export(document, protected_ids):
    identifiers = {str(value) for value in protected_ids if value is not None and str(value)}
    matches, violations = set(), set()
    strings_checked = 0

    def walk(value, path):
        nonlocal strings_checked
        if isinstance(value, Mapping):
            for key, child in value.items():
                walk(child, path + '.' + str(key))
        elif isinstance(value, list):
            for child in value:
                walk(child, path + '[]')
        elif isinstance(value, str):
            strings_checked += 1
            if any(identifier in value for identifier in identifiers):
                matches.add(path)

    walk(document, 'root')
    for group in ('nodes', 'relationships'):
        for name, definition in document[group].items():
            path = group + '.' + name
            observation = definition.get('observations', {})
            examples = list(observation.get('prototypes', []))
            for signature in observation.get('prototype_signatures', []):
                examples.extend(signature.get('prototypes', []))
            if group == 'nodes' and definition.get('record_export', 'protected') == 'protected' and examples:
                violations.add(path)
            if group == 'relationships':
                for example in examples:
                    endpoints = example.get('source_types', []) + example.get('target_types', [])
                    if not endpoints or any(document['nodes'].get(label, {}).get('record_export', 'protected') == 'protected'
                                            for label in endpoints):
                        violations.add(path)
            for key, prop in definition['properties'].items():
                if prop['export'] == 'protected' and prop.get('observations') != {'status': 'protected', 'exhaustive': False}:
                    violations.add(path + '.properties.' + key)
    return {'passed': not matches and not violations, 'strings_checked': strings_checked,
            'protected_identifier_count': len(identifiers), 'identifier_matches': len(matches),
            'affected_schema_paths': sorted(matches), 'policy_violations': sorted(violations)}
