"""Grounded spelling suggestions; never authorize a correction automatically."""
import re
from difflib import SequenceMatcher

VERSION = 'term-clarification-v1'
SOURCE_EXPANSIONS = {'HPAP': ('Human Pancreas Analysis Program',)}


def source_role_text(text, vocabulary):
    """A verified source's explicit long name is not an anatomical predicate."""
    sources = set(vocabulary.get('donor_sources', [])) | set(vocabulary.get('sample_sources', []))
    for source, names in SOURCE_EXPANSIONS.items():
        if source not in sources:
            continue
        for name in names:
            text = re.sub(r'\b' + re.escape(source) + r'\s*\(\s*' + re.escape(name) + r'\s*\)', source, text, flags=re.I)
    return text


def candidates(question, vocabulary):
    if not vocabulary.get('inventory_complete'):
        return []
    from .tissue_aliases import PLN_ID, PLN_NAME, PLN_ALIASES
    from .anatomy_resolution import ALIASES
    records = []
    for name in sorted(set(vocabulary.get('donor_sources', [])) | set(vocabulary.get('sample_sources', []))):
        if isinstance(name, str):
            records.append({'role': 'source', 'name': name, 'id': name, 'aliases': [name]})
    for tissue in vocabulary.get('tissues', []):
        name, identifier = tissue.get('name'), tissue.get('id')
        if not isinstance(name, str) or not identifier:
            continue
        aliases = [name]
        configured = ALIASES.get(identifier)
        if configured and configured[0] == name:
            aliases += list(configured[1])
        if identifier == PLN_ID and name == PLN_NAME:
            aliases += list(PLN_ALIASES)
        records.append({'role': 'tissue', 'name': name, 'id': identifier, 'aliases': aliases})
    issues = []
    # Scope only explicit source/tissue positions; never fuzz genes, IDs, or numbers.
    pattern = r'\b(?:in|from|within|cohort|source|tissue)\s+(?:the\s+)?([A-Za-z][A-Za-z-]{2,})\b'
    for match in re.finditer(pattern, question, re.I):
        token = match.group(1)
        if token.lower() in {'donor','donors','sample','samples','stage','all','both','each','human','gene','genes','tissue','cohort'}:
            continue
        # Longer exact tissue names and source names take precedence over spelling.
        tail = question[match.start(1):]
        exact = [r for r in records if any(re.match(re.escape(a)+r'(?!\w)', tail, re.I) for a in r['aliases'])]
        if exact:
            if any(token in r['aliases'] or r['role'] == 'tissue' for r in exact):
                continue
            options = [(r, r['name'], 'case_variant', 1.0) for r in exact if r['name'].casefold() == token.casefold()]
        else:
            options = []
            for r in records:
                if re.search(r'\b(?:gene|protein|variant)\s+(?:in|from|within)\s+(?:the\s+)?$', question[:match.start(1)], re.I):
                    continue
                for alias in r['aliases']:
                    if len(alias.split()) != 1 or abs(len(alias)-len(token)) > 1:
                        continue
                    score = SequenceMatcher(None, alias.casefold(), token.casefold()).ratio()
                    # Three-letter acronyms require a context-compatible alias;
                    # this admits PKN -> PLN, but never declares PKN a stored alias.
                    threshold = 2/3 if len(token) == len(alias) == 3 else .75
                    if score >= threshold:
                        display = 'pancreatic lymph node' if r['id'] == PLN_ID and r['name'] == PLN_NAME else r['name']
                        options.append((r, display, 'spelling_candidate', score))
            if not options and token.isupper() and not re.search(r'\b(?:gene|protein|variant)\b', question, re.I):
                issues.append({'term': token, 'span': list(match.span(1)), 'candidates': []})
                continue
        dedup = {}
        for record, label, basis, score in sorted(options, key=lambda x: -x[3]):
            dedup.setdefault((record['role'],record['id']), {'label': label, 'canonical_name': record['name'],
                'canonical_id': record['id'], 'entity_role': record['role'], 'match_basis': basis})
        if dedup:
            issues.append({'term': token, 'span': list(match.span(1)), 'candidates': list(dedup.values())[:2]})
    return issues


def recovery(question, vocabulary, graph_version):
    issues = candidates(question, vocabulary)
    if not issues:
        return None
    unique = all(len(i['candidates']) == 1 for i in issues)
    suggestions = []
    combinations = [[i['candidates'][0] for i in issues]] if unique else [[c] for c in issues[0]['candidates']] if len(issues) == 1 else []
    for choices in combinations:
        revised = question
        for issue, choice in reversed(list(zip(issues, choices))):
            a,b = issue['span']; revised = revised[:a] + choice['label'] + revised[b:]
        label = 'Use ' + ', '.join(c['label'] for c in choices)
        suggestions.append({'label': label, 'instruction': 'Use this corrected question: ' + revised,
                            'recommended_question': revised, 'matches': choices})
    if unique:
        message = 'Do you mean ' + ' and '.join(i['candidates'][0]['label'] for i in issues) + '?'
    elif len(issues) == 1 and issues[0]['candidates']:
        message = 'Which meaning did you intend for "' + issues[0]['term'] + '"?'
    else:
        message = 'Please clarify ' + ', '.join('"'+i['term']+'"' for i in issues) + '.'
    for issue in issues:
        for c in issue['candidates']:
            if 'proxy' in c['canonical_name']:
                message += ' This release records ' + c['label'] + ' as ' + c['canonical_name'] + '.'
    if suggestions:
        message += '\nSuggested question: ' + suggestions[0]['recommended_question']
    return {'category': 'term_clarification', 'title': 'Check the intended term', 'message': message,
        'retryable': False, 'suggestions': suggestions, 'issues': issues,
        'graph_version': graph_version, 'version': VERSION, 'original_question': question}


def repair_generated_scope(source, resolved):
    """One conservative repair for an independent donor-only planning mistake."""
    from copy import deepcopy
    removable = {
        'A generated sample-tissue filter was not authorized by the user request and was removed.',
        'A generated sample-tissue filter was not requested and was removed.',
    }
    issues = set(resolved.get('semantic_issues') or [])
    request = source.get('semantic_request') or {}
    if (not issues or not issues <= removable or request.get('source') != 'user_request'
            or not request.get('question') or source.get('depends_on') or source.get('path_spec')
            or set(source.get('relation_types') or []) - {'HAS_DONOR'}
            or not re.search(r'\bdonors?\b', request['question'], re.I)):
        return None
    repaired = deepcopy(source)
    repaired['question'] = request['question']
    repaired['constraints'] = [c for c in source.get('constraints', [])
                              if c.get('entity_type') != 'anatomical_structure']
    return repaired
