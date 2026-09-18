"""Deterministic source groups; no fetching or model-generated destinations."""
from urllib.parse import urlparse
import hashlib

SOURCE_ALIASES = {
    'kegg': ('kegg', 'KEGG'), 'ensembl': ('ensembl', 'Ensembl'),
    'dbsnp': ('dbsnp', 'dbSNP'), 'hpap': ('hpap', 'HPAP data portal'),
    'hpap data portal': ('hpap', 'HPAP data portal'),
}


def source_identity(label, url):
    host = (urlparse(url).hostname or '').lower().removeprefix('www.')
    for domain, alias in [('kegg.jp', 'kegg'), ('genome.jp', 'kegg'),
                          ('ensembl.org', 'ensembl'), ('hpap.pmacs.upenn.edu', 'hpap')]:
        if host == domain or host.endswith('.' + domain):
            return SOURCE_ALIASES[alias]
    if host in {'ncbi.nlm.nih.gov'} and urlparse(url).path.startswith('/snp/'):
        return SOURCE_ALIASES['dbsnp']
    if str(label).strip().lower() in SOURCE_ALIASES:
        return SOURCE_ALIASES[str(label).strip().lower()]
    name = str(label or host or 'External source').strip()
    return ('source:' + hashlib.sha256((name.casefold() + '|' + host).encode()).hexdigest()[:16], name)


def _props(item):
    return item.get('properties', item.get('~properties', {})) or {}


def _entity(node):
    node_id = str(node.get('id', node.get('~id', '')))
    labels = node.get('labels', node.get('~labels', []))
    return {'id': node_id, 'name': str(_props(node).get('name') or node_id),
            'types': labels if isinstance(labels, list) else [str(labels)]}


def build_external_link_groups(links, nodes, edges):
    """Keep all entity associations even when the legacy URL list was deduplicated."""
    by_id = {str(n.get('id', n.get('~id', ''))): n for n in nodes}
    contexts = {}
    for kind, items in [('node', nodes), ('edge', edges)]:
        for item in items:
            url = _props(item).get('data_source_url')
            if not isinstance(url, str):
                continue
            entities = [_entity(item)] if kind == 'node' else [
                _entity(by_id[endpoint]) if endpoint in by_id else {'id': endpoint, 'name': endpoint, 'types': []}
                for endpoint in [str(item.get('start_id', item.get('source', item.get('start', item.get('~start', ''))))),
                                 str(item.get('end_id', item.get('target', item.get('end', item.get('~end', '')))))] if endpoint]
            relation = str(item.get('type', item.get('~type', ''))) if kind == 'edge' else ''
            label = (' → '.join(e['name'] for e in entities) + (f' · {relation}' if relation else '')) if kind == 'edge' else entities[0]['name']
            contexts.setdefault(url, []).append((label, entities, {'kind': kind,
                'id': str(item.get('id', item.get('~id', ''))), 'field': 'data_source_url', 'relation': relation}))
    groups = {}
    for source, description, url in links:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                continue
        except (ValueError, TypeError):
            continue
        key, name = source_identity(source, url)
        group = groups.setdefault(key, {'source_key': key, 'display_name': name, 'entries': {}})
        entry = group['entries'].setdefault(url, {'url': url, 'label': '', 'entities': [], 'provenance': []})
        matches = contexts.get(url, [])
        if not matches:
            # These are the same registered identity routes emitted by resources.py.
            from urllib.parse import quote
            for node in nodes:
                entity = _entity(node)
                nid = entity['id']
                if url in ['https://www.ensembl.org/Homo_sapiens/Gene/Summary?g=' + quote(nid),
                           'https://www.ncbi.nlm.nih.gov/snp/' + nid]:
                    matches.append((entity['name'], [entity], {'kind': 'node', 'id': nid, 'field': 'registered_identity_route'}))
        labels = []
        for label, entities, provenance in matches:
            if label and label not in labels:
                labels.append(label)
            for entity in entities:
                if entity not in entry['entities']:
                    entry['entities'].append(entity)
            if provenance not in entry['provenance']:
                entry['provenance'].append(provenance)
        if not labels:
            labels = [str(description or url)]
            entry['provenance'].append({'kind': 'source', 'field': 'external_links'})
        entry['label'] = '; '.join(labels)
    result = []
    for group in groups.values():
        group['entries'] = sorted(group['entries'].values(), key=lambda e: (e['label'].casefold(), e['url']))
        result.append(group)
    return sorted(result, key=lambda g: (g['display_name'].casefold(), g['source_key']))
