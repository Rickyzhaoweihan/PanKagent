"""Release-scoped dataset tissue bindings; not universal anatomical synonyms."""
import re

PLN_ID = 'UBERON_0015865'
PLN_NAME = 'pancreaticosplenic lymph node (proxy for "pancreatic LN")'
PLN_ALIASES = ('PLN', 'pancreatic lymph node', 'pancreatic lymph nodes', 'pancreatic LN')

def matched_tissues(question, vocabulary):
    tissues = [t for t in vocabulary if isinstance(t.get('name'), str) and
               re.search(r'(?<!\w)' + re.escape(t['name']) + r'(?!\w)', question, re.I)]
    requested = next((alias for alias in PLN_ALIASES if re.search(
        r'(?<!\w)' + re.escape(alias) + r'(?!\w)', question, re.I)), None)
    # Do not silently merge a named sample fraction into the whole tissue.
    fraction = re.search(r'\bPLN[_ -][BHT]\b', question, re.I)
    proxy = next((t for t in vocabulary if t.get('id') == PLN_ID and t.get('name') == PLN_NAME), None)
    if requested and not fraction and proxy and all(t.get('id') != PLN_ID for t in tissues):
        tissues.append({**proxy, 'requested_alias':requested, 'match_kind':'dataset_proxy',
                       'explanation':'PLN uses this release’s pancreaticosplenic lymph-node proxy; recorded sample fractions remain distinct.'})
    return tissues
