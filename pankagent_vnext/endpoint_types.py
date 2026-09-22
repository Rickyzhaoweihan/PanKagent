"""Conservative type implications from complete paths in one verified release.

This small metadata projection has no graph client, query fallback or planner
import. It never turns a shared generic ontology parent into a specific type.
"""
import hashlib
import json
from pathlib import Path

_RAW = Path(__file__).with_suffix('.json').read_bytes()
REGISTRY = json.loads(_RAW)
DIGEST = hashlib.sha256(_RAW + Path(__file__).read_bytes()).hexdigest()


def inferred_endpoint_types(nodes, paths, graph_release, *, excluded=()):
    """Only fill unlabeled endpoints when every possible type is identical.

    The caller supplies mandatory, directed, single-branch patterns. Explicit
    labels are never changed, and contradictory explicit endpoints grant no
    type implications to their neighbors. Unknown alternatives fail closed.
    """
    if graph_release != REGISTRY['release']:
        return {}
    candidates, blocked = {}, set(excluded)
    generic = set(REGISTRY['non_specific_labels'])
    for source, target, kinds in paths:
        for kind in kinds:
            alternatives = REGISTRY['relations'].get(kind)
            if not alternatives:
                blocked.update((source, target))
                continue
            compatible = [path for path in alternatives
                if (not nodes.get(source) or nodes[source] <= set(path['source']))
                and (not nodes.get(target) or nodes[target] <= set(path['target']))]
            if not compatible:
                blocked.update((source, target))
                continue
            for variable, side in ((source, 'source'), (target, 'target')):
                for path in compatible:
                    types = set(path[side]) - generic
                    if len(types) != 1:
                        blocked.add(variable)
                    else:
                        candidates.setdefault(variable, set()).update(types)
    return {variable: next(iter(types)) for variable, types in candidates.items()
            if variable not in blocked and not nodes.get(variable) and len(types) == 1}
