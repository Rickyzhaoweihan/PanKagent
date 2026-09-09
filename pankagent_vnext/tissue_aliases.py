"""Release-scoped tissue mentions: longest spans first, then canonical IDs."""
import re

PLN_ID = 'UBERON_0015865'
PLN_NAME = 'pancreaticosplenic lymph node (proxy for "pancreatic LN")'
PLN_ALIASES = ('PLN', 'pancreatic lymph node', 'pancreatic lymph nodes', 'pancreatic LN')

def matched_tissues(question, vocabulary):
    mentions = []
    def add(name, record):
        for match in re.finditer(r'(?<!\w)' + re.escape(name) + r'(?!\w)', question, re.I):
            mentions.append((match.start(), match.end(), record))
    for tissue in vocabulary:
        if isinstance(tissue.get('name'), str):
            add(tissue['name'], tissue)
    # Reuse the same verified alias registry as entity resolution, including islet.
    from .anatomy_resolution import ALIASES
    for tissue in vocabulary:
        configured = ALIASES.get(tissue.get('id'))
        if configured and tissue.get('name') == configured[0] and tissue.get('id') != PLN_ID:
            for alias in configured[1]:
                add(alias, {**tissue, 'requested_alias':alias, 'match_kind':'verified_alias'})
    proxy = next((t for t in vocabulary if t.get('id') == PLN_ID and t.get('name') == PLN_NAME), None)
    if proxy:
        for alias in PLN_ALIASES:
            for match in re.finditer(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', question, re.I):
                if re.match(r'[_ -][BHT]\b', question[match.end():], re.I):
                    continue
                mentions.append((match.start(), match.end(), {**proxy, 'requested_alias': alias,
                    'match_kind': 'dataset_proxy',
                    'explanation': 'PLN uses this release’s pancreaticosplenic lymph-node proxy; recorded sample fractions remain distinct.'}))
    # A generic tissue inside a longer mention is not a second requested tissue.
    # Separate occurrences ("pancreatic lymph node and lymph node") remain distinct.
    maximal = [m for m in mentions if not any(a <= m[0] and b >= m[1] and (a,b) != m[:2] for a,b,_ in mentions)]
    result = {}
    for _, _, record in sorted(maximal, key=lambda m:(m[0], m[1], str(m[2].get('id')))):
        result.setdefault(record['id'], record)
    return list(result.values())
