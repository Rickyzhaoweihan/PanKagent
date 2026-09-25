"""Release-scoped terminology and assay capabilities; never executable Cypher."""
from copy import deepcopy
from .agent_schemas import module as schema_module
from difflib import get_close_matches
import hashlib
import json
from pathlib import Path
import re

VERSION = schema_module('semantics_modalities')['terminology']['VERSION']
RELEASE = schema_module('semantics_modalities')['terminology']['RELEASE']
SOURCE = schema_module('semantics_modalities')['terminology']['SOURCE']
STAGES = schema_module('semantics_modalities')['terminology']['STAGES']
# Property ownership is verified against this release, not inferred from global keys.
PROPERTIES = schema_module('semantics_modalities')['terminology']['PROPERTIES']
ALIASES = schema_module('semantics_modalities')['terminology']['ALIASES']
CAPABILITIES = schema_module('semantics_modalities')['terminology']['CAPABILITIES']
from .donor_categories import DIGEST as DONOR_CATEGORIES_DIGEST
DIGEST = hashlib.sha256(json.dumps([VERSION, RELEASE, PROPERTIES, ALIASES, CAPABILITIES, SOURCE, DONOR_CATEGORIES_DIGEST],sort_keys=True).encode() + Path(__file__).read_bytes() + Path(__file__).with_name('tissue_aliases.py').read_bytes()).hexdigest()


def donor_intent(step):
    typed_donor = any(c.get('entity_type')=='donor' for c in step.get('constraints',[]))
    if typed_donor:
        return True
    relations = set(step.get('relation_types') or [])
    owners = {c.get('entity_type') for c in step.get('constraints', [])}
    if (step.get('relation_types') == [] and owners and None not in owners
            and not owners.intersection({'donor', 'Sample_node', 'data_modality'})
            and not re.search(r'\bdonors?\b|\bcohorts?\b|\bsamples?\b|\bHPAP\b|\bstage\s*[123I]',
                              step.get('question', ''), re.I)):
        # A selected entity-only task stays entity-only even when a sibling
        # in the same original request concerns a donor population.
        return False
    if relations and not relations.intersection({'HAS_DONOR', 'HAS_SAMPLE'}):
        # "Between diabetic and non-diabetic donors" describes the source
        # contrast of a molecular measurement, not a donor-inventory request.
        return False
    request = step.get('semantic_request') or {}
    text = (request.get('question') if request.get('source') == 'user_request'
            and isinstance(request.get('question'), str) else step.get('question', ''))
    clinical = (control_cohort_polarity(text) if isinstance(text, str)
                else {'positive': False, 'negative': False})
    return bool(re.search(r'\bdonors?\b|\bHPAP\b', text, re.I)
                or clinical['positive'] or clinical['negative']
                # Disease identity alone is not a cohort request.
                or (bool(_disease_mentions(text)) and ('HAS_SAMPLE' in relations
                    or bool(re.search(r'\bsamples?\b', text, re.I))))
                or re.search(r'\b(?:recorded\s+)?stage\s*(?:[123]|I{1,3})\b', text, re.I)
                or any(c.get('entity_type') == 'donor'
                       for c in step.get('constraints', [])))


def semantic_intent(step):
    """Sample-only lookups need the same verified assay vocabulary as cohorts."""
    if donor_intent(step):
        return True
    relations = set(step.get('relation_types') or [])
    if relations:
        return ('HAS_SAMPLE' in relations and (relations <= {'HAS_SAMPLE', 'HAS_DONOR'}
                or any(c.get('entity_type') in {'Sample_node', 'data_modality'} for c in step.get('constraints', []))
                or bool(re.search(r'\bsamples?\b', step.get('question', ''), re.I))))
    return (any(c.get('entity_type') in {'Sample_node', 'data_modality'} for c in step.get('constraints', []))
            or bool(re.search(r'\bsamples?\b', step.get('question', ''), re.I)))


def dataset_source_owner(question, value, occurrence=None):
    """Read an explicit source role; caller verifies the recorded source value.

    Cohort-qualified sample searches use the donor's source. Explicit sample
    provider/data_source wording stays sample-owned. Conflicting roles or a
    source-name gene alias cannot authorize a change of owner.
    """
    if not isinstance(question, str) or not isinstance(value, str) or not value:
        return None
    all_matches = list(re.finditer(r'(?<![\w_])' + re.escape(value) + r'(?![\w_]|[-:.]\d)', question, re.I))
    if occurrence is not None:
        start = occurrence.start() if hasattr(occurrence, 'start') else int(occurrence)
        all_matches = [match for match in all_matches if match.start() == start]
    owners = set()
    for match in all_matches:
        before, after = question[max(0, match.start()-140):match.start()], question[match.end():match.end()+100]
        if re.search(r'\bgene\s*[:=]?\s*$', before, re.I):
            continue
        sample = bool(re.search(r'(?:\bSample_node\s*\.\s*data_source|\b(?:sample|assay)[- ](?:data[- ]?)?(?:source|provider))(?:\s+(?:field|label))?\s*(?:is|=|:|of|from)?\s*[\"\']?$', before, re.I)
                      or re.search(r'\bsamples?\b[^.!?;]{0,60}\bdata[- ]source\b(?:\s+(?:field|label))?\s*(?:is|=|:|of|from)?\s*[\"\']?$', before, re.I)
                      or re.match(r'[\"\']?\s+as\s+(?:the\s+)?(?:sample|assay)[- ](?:data[- ]?)?(?:source|provider)\b', after, re.I))
        sample = sample or bool(re.match(r'[\"\']?\s+(?:for|of)\s+samples?\b', after, re.I)
                                and re.search(r'\bdata[- ]source\b[^.!?;]{0,30}$', before, re.I))
        donor = bool(re.search(r'(?:\bdonor\s*\.\s*data_source|\b(?:donor|cohort)[- ](?:data[- ]?)?source)\s*(?:is|=|:|of|from)?\s*[\"\']?$', before, re.I)
                     or re.match(r'[\"\']?\s+(?:donors?|cohort)\b', after, re.I))
        if not sample and not donor:
            donor = bool(
                re.search(r'\b(?:donors?|cohort)\s+(?:(?:are\s+)?available\s+)?(?:in|from|provided\s+by|sourced\s+from)\s+(?:the\s+)?$', before, re.I)
                or re.search(r'\b(?:donors?|cohort)\s+(?:excluding|exclude|without|except)\b[^.!?;]{0,70}$', before, re.I)
                or re.match(r'\s*(?:-(?:only|derived))?\s+'
                         r'(?:[A-Za-z0-9_:/()+.-]+\s+){0,8}'
                         r'(?:donors?|cohort|samples?)\b', after, re.I)
                or re.search(r'\bsamples?\s+(?:from|provided\s+by|sourced\s+from|'
                             r'excluding|exclude|without|except)\s*$', before, re.I))
        # Ordinary 'metadata' denotes information, not a request to select
        # the dataset whose recorded source happens to be named Metadata.
        if value.casefold() == 'metadata' and not (sample or donor):
            continue
        if sample:
            owners.add('Sample_node')
        if donor:
            owners.add('donor')
    return owners.pop() if len(owners) == 1 else None


def _source_scope_candidate(question, occurrence):
    before = question[max(0, occurrence.start() - 100):occurrence.start()]
    after = question[occurrence.end():occurrence.end() + 80]
    return bool(
        re.search(r'\b(?:donors?|samples?|cohort)\s+(?:from|provided\s+by|sourced\s+from)\s+(?:the\s+)?$', before, re.I)
        or re.search(r'\b(?:donors?|cohort)\s+(?:excluding|exclude|without|except)\b[^.!?;]{0,70}$', before, re.I)
        or re.search(r'\b(?:data[- ]?source|source|provider)\s*(?:is|=|:|of|from)?\s*$', before, re.I)
        or re.match(r'\s*(?:-(?:only|derived))?\s+(?:[A-Za-z0-9_:/()+.-]+\s+){0,8}(?:donors?|samples?|cohort)\b', after, re.I)
        or re.search(r'\bsamples?\s+(?:from|provided\s+by|sourced\s+from|excluding|exclude|without|except)\s*$', before, re.I)
        or re.match(r'\s*(?:or|versus|vs\.?)\b', after, re.I))


def _scope_clause_boundary(text, start):
    """End one typed value/projection atom without swallowing a later filter.

    A conjunction inside a bare value is not automatically a boundary.  It is
    a boundary only when the following wording starts another property or a
    recognizable filter role.  This preserves ``thyroid and lupus`` for the
    raw-field completeness check while retaining ``and have T1D`` as scope.
    """
    quoted = [False] * len(text)
    quote = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            quoted[index] = quote is not None
            escaped = False
            continue
        if char == '\\' and quote is not None:
            quoted[index] = True
            escaped = True
            continue
        apostrophe = (char == "'" and index > 0 and index + 1 < len(text)
                      and text[index - 1].isalnum() and text[index + 1].isalnum())
        if char in {'"', "'", '`'} and not apostrophe:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            quoted[index] = True
            continue
        quoted[index] = quote is not None
    protected = list(quoted)
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    for index, char in enumerate(text):
        if quoted[index]:
            protected[index] = True
            continue
        if char in '([{':
            stack.append(char)
            protected[index] = True
        elif char in pairs:
            protected[index] = True
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif stack:
            protected[index] = True
    end = next((index for index in range(start, len(text))
                if text[index] in '.!?;\n' and not protected[index]
                and not (text[index] == '.' and index > 0 and index + 1 < len(text)
                         and text[index - 1].isdigit() and text[index + 1].isdigit())),
               len(text))
    field_starts = [pattern for prop, pattern in _DONOR_REQUEST_FIELD_PATTERNS.items()
                    if prop != 't1d_stage']
    field_starts.extend(_SAMPLE_REQUEST_FIELD_PATTERNS.values())
    next_role = (
        r'(?:' + '|'.join(field_starts) + r')'
        r'|\b(?:from|at|restricted\s+to|using?|uses?|have|has|are|is|'
        r'recorded|classified|label(?:ed|led)|categorized)\b'
        r'|\b(?:tissues?|anatom(?:y|ical)|assays?|modalit(?:y|ies))\b'
        r'|\b(?:[A-Za-z0-9_:+./-]+\s+){0,6}'
        r'(?:samples?|donors?|cohort|tissues?|assays?|modalit(?:y|ies)|anatom(?:y|ical))\b'
    )
    connector = re.compile(
        r'\s+(?:as\s+well\s+as|along(?:side|\s+with)|together\s+with|'
        r'in\s+addition\s+to|while(?:\s+also)?|and|or|but|plus|&)\s+|\s*,\s*', re.I)
    for match in connector.finditer(text, start, end):
        if any(protected[match.start():match.end()]):
            continue
        if re.match(r'\s*(?:(?:also|then)\s+)?'
                    r'(?:(?:whose|having)\s+|with\s+(?:(?:a|an|the)\s+)?|'
                    r'(?:a|an|the)\s+)?(?:' + next_role + r')',
                    text[match.end():], re.I):
            return match.start()
    return end


def scope_incidental_spans(text):
    """Return equal-offset spans whose literals are values, not query scope."""
    if not isinstance(text, str):
        return []
    spans = []
    predicate = re.compile(
        r'(?:!=|<>|>=|<=|=|>|<|\bcontains?|\bmentions?|\bincludes?|'
        r'\bequals?|\bis\b|\bwas\b|\bwere\b|\bstarts?\s+with\b|'
        r'\bends?\s+with\b)', re.I)
    nonscope_fields = [(prop, pattern) for prop, pattern in _DONOR_REQUEST_FIELD_PATTERNS.items()
                       if prop not in {'data_source', 'diabetes_type',
                                       'derived_diabetes_status', 't1d_stage'}]
    nonscope_fields.extend((prop, _SAMPLE_REQUEST_FIELD_PATTERNS[prop])
                           for prop in ('id', 'note', 'contact', 'data_version'))
    for prop, pattern in nonscope_fields:
        for field in re.finditer(pattern, text, re.I):
            end = _scope_clause_boundary(text, field.end())
            prefix = text[max(0, field.start() - 80):field.start()]
            suffix = text[field.end():end]
            projection_only = (re.search(
                r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                prefix, re.I) and not re.search(
                r'\b(?:where|with|whose|having|filter(?:ed)?)\b', prefix, re.I))
            implicit_value = bool(re.search(r'[A-Za-z0-9]', suffix))
            if projection_only and implicit_value:
                spans.append((field.start(), end, 'projection_literal'))
            elif predicate.search(text, field.end(), end) or implicit_value:
                spans.append((field.start(), end, 'typed_property_value'))
    projection = re.compile(
        r'\b(?:return|report|display|list|output)\b[^.!?;\n]{0,120}?'
        r'\b(?:literal|label|phrase|whether|notes?|contact|output)\b', re.I)
    for match in projection.finditer(text):
        spans.append((match.start(), _scope_clause_boundary(text, match.end()),
                      'projection_literal'))
    merged = []
    for start, end, kind in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end),
                          merged[-1][2])
        else:
            merged.append((start, end, kind))
    return merged


def scope_intent_text(text):
    """Mask typed value/projection atoms while preserving character offsets."""
    if not isinstance(text, str):
        return ''
    text = re.sub(r'\bregardless\s+of\s+(?:their\s+)?(?:recorded\s+)?(?:T1D\s+)?stage(?:s)?\b', lambda m: ' ' * len(m.group()), text, flags=re.I)
    chars = list(text)
    for start, end, _ in scope_incidental_spans(text):
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def identity_authorization_text(text):
    """Mask property operands before authorizing an entity-name filter.

    A literal can name a real graph entity while serving only as the value of
    a different predicate (for example ``Gene.description contains lupus``).
    Existence plus a same-spelling occurrence must never turn that operand into
    an additional disease, gene, tissue, or pathway filter.  This stricter mask
    is intentionally used only for the final identity-authorization fallback;
    explicitly typed constraints remain eligible through
    :func:`_raw_constraint_authorized`.
    """
    if not isinstance(text, str):
        return ''
    spans = list(scope_incidental_spans(text))
    try:
        from .release_schema import REGISTRY
        properties = {str(prop) for values in REGISTRY.get('nodes', {}).values()
                      for prop in values}
        properties.update(str(prop)
                          for spec in REGISTRY.get('relations', {}).values()
                          for prop in spec.get('properties', []))
    except (ImportError, AttributeError, TypeError):
        properties = set()
    # Common user-facing field names may not be physical release properties,
    # but still establish an unmistakable property-value role.
    properties.update({'condition', 'description', 'label', 'phenotype_text'})
    forms = sorted({re.escape(prop).replace('_', r'[ _-]+')
                    for prop in properties if prop}, key=len, reverse=True)
    if not forms:
        return scope_intent_text(text)
    field_pattern = r'(?:' + '|'.join(forms) + r')'
    predicate = (r'(?:!=|<>|>=|<=|=|>|<|\bcontains?|\bmentions?|\bincludes?|'
                 r'\bequals?|\bis|\bwas|\bwere|\bstarts?\s+with|'
                 r'\bends?\s+with)')
    field = re.compile(
        r'(?:(?P<owner>\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)|'
        r'(?P<intro>\b(?:whose|having|with)\s+(?:(?:a|an|the)\s+)?))?'
        r'(?P<field>\b' + field_pattern + r'\b)', re.I)
    for match in field.finditer(text):
        end = _scope_clause_boundary(text, match.end())
        suffix = text[match.end():end]
        explicit_predicate = re.match(r'\s*' + predicate, suffix, re.I)
        passive_scope = re.match(
            r'\s*(?:is|was|were)\s+(?:recorded|available|reported|shown|listed|returned)\s+(?:for|in|from|on)\b',
            suffix, re.I)
        implicit_value = (match.group('intro') is not None
                          and bool(re.search(r'[A-Za-z0-9]', suffix)))
        if passive_scope:
            continue
        if match.group('owner') is not None or explicit_predicate or implicit_value:
            # A qualified field without an operand (for example "return
            # Gene.description") is a projection, not a value-bearing span.
            if explicit_predicate or implicit_value:
                spans.append((match.start(), end, 'property_operand'))
    merged = []
    for start, end, kind in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2])
        else:
            merged.append((start, end, kind))
    chars = list(text)
    for start, end, _ in merged:
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def _unresolved_tissue_role(text, vocabulary, matched):
    """Detect an explicit tissue slot that has no unique live-graph match."""
    # Mask only descriptive question phrases, never an explicit named filter
    # elsewhere in the request (e.g. "from spleen; what tissue ...?").
    text = re.sub(r'\b(?:what|which)\s+(?:tissues?|cell[ -]?types?)'
                  r'(?:\s*/\s*(?:tissues?|cell[ -]?types?))?'
                  r'\s+(?:are|is|do|does)\b', ' ', text, flags=re.I)
    if re.search(r'\b(?:tissue|anatom(?:y|ical)(?:[ _-]+structure)?)\b'
                 r'\s*(?:is|=|:|of|from)?\s*[A-Za-z0-9]', text, re.I):
        return not matched
    known = {str(record.get('name') or '').casefold() for record in vocabulary.get('tissues') or []
             if isinstance(record, dict)}
    known.update(str(record.get('id') or '').casefold() for record in vocabulary.get('tissues') or []
                 if isinstance(record, dict))
    # A reviewed alias suppresses the generic unknown-modifier warning only
    # after that exact request has already resolved to one current-release
    # tissue. Merely existing in the alias table is never enough to drop an
    # unmatched tissue restriction.
    matched_aliases = set()
    try:
        from .anatomy_resolution import ALIASES as anatomy_aliases
    except ImportError:
        anatomy_aliases = {}
    for record in matched or []:
        for value in (record.get('requested_alias'), record.get('name'), record.get('id')):
            if isinstance(value, str) and value:
                matched_aliases.add(value.casefold())
        configured = anatomy_aliases.get(record.get('id'))
        if configured and configured[0] == record.get('name'):
            matched_aliases.update(str(value).casefold() for value in configured[1])
            matched_aliases.update(
                str(value).casefold() + 's' for value in configured[1]
                if isinstance(value, str) and len(value) >= 3
                and not value.casefold().endswith('s'))
    sources = {str(value).casefold() for value in vocabulary.get('sources') or []
               if isinstance(value, str)}
    for occurrence in re.finditer(
            r'\b(?:samples?|specimens?)\s+(?:from|collected\s+from)\s+'
            r'(?:an?\s+)?(?:tissue\s+)?'
            r'([A-Za-z0-9_:+./-]+)(?:\s+tissue)?\b', text, re.I):
        value = occurrence.group(1).casefold()
        tail = text[occurrence.end():occurrence.end() + 20]
        if value not in sources and not re.match(r'\s+donors?\b', tail, re.I):
            return not matched
    stop = {'these', 'those', 'the', 'such', 'find', 'show', 'count', 'matching', 'available', 'all', 'any', 'many',
            'donor', 'donors', 'and', 'or', 'their', 'nd', 'hpap', 'control', 'healthy', 't1d', 't2d', 'rna', 'atac',
            'seq', 'multiome', 'multiomics', 'assay'}
    for occurrence in re.finditer(r'\b([A-Za-z][A-Za-z0-9_-]*)\s+(?:samples?|specimens?)\b', text, re.I):
        modifier = occurrence.group(1).casefold()
        if (modifier in stop or modifier in known or modifier in matched_aliases
                or modifier == 'sequencing'
                or re.search(r'(?:seq|omics)$', modifier)):
            continue
        if any(match.start() <= occurrence.start() < match.end()
               for value in vocabulary.get('modalities') or []
               if isinstance(value, str)
               for match in re.finditer(re.escape(value) + r'\s+samples?\b', text, re.I)):
            continue
        return True
    return False


def _unresolved_assay_role(text, has_intent):
    if has_intent:
        return False
    return bool(re.search(
        r'\b(?:assay|data[ _-]+modality|modality)\b\s*(?:is|=|:|of)?\s*'
        r'[A-Za-z0-9]|\bsamples?\s+(?:using|with|assayed\s+by)\s+[A-Za-z0-9_+.-]+'
        r'(?:\s+sequencing)?\b|\b(?:using|use|with)\s+[A-Za-z0-9_+.-]*(?:seq|omics)\b|'
        r'\b[A-Za-z0-9_+-]+(?:\s+sequencing|[-_]?seq|omics)\s+samples?\b', text, re.I))


def _unresolved_source_role(text, vocabulary):
    recorded = {value.casefold() for value in vocabulary.get('sources') or []
                if isinstance(value, str)}
    # ``samples from X`` is shared English grammar for a tissue and a dataset.
    # A value recorded in either live inventory is resolved, not an unknown
    # source.  Trailing sentence punctuation is not part of the candidate.
    tissues = {str(record.get(key)).casefold()
               for record in vocabulary.get('tissues') or []
               if isinstance(record, dict)
               for key in ('id', 'name') if isinstance(record.get(key), str)}
    modalities = {value.casefold() for value in vocabulary.get('modalities') or []
                  if isinstance(value, str)}
    resolved_roles = recorded | tissues | modalities
    patterns = (
        r'\b(?:donors?|samples?|cohort)\s+(?:from|provided\s+by|sourced\s+from)\s+'
        r'(?:the\s+)?([A-Za-z0-9_:+./-]+)',
        r'\b(?:sample|donor|cohort)(?:[ _-]+data)?[ _-]+(?:source|provider)\s*'
        r'(?:is|=|:|of|from)?\s*([A-Za-z0-9_:+./-]+)',
    )
    return any(match.group(1).rstrip('.,:;/').casefold() not in resolved_roles
               for pattern in patterns for match in re.finditer(pattern, text, re.I))


def _unresolved_donor_modifier(text, vocabulary):
    """Flag an unexplained leading cohort label instead of dropping it."""
    recognized = set()
    for value in vocabulary.get('sources') or []:
        if isinstance(value, str):
            recognized.update(re.findall(r'[a-z0-9]+', value.casefold()))
    for record in vocabulary.get('donor_diseases') or []:
        if not isinstance(record, dict):
            continue
        values = [record.get('id'), record.get('name')]
        synonyms = record.get('synonyms')
        values.extend(synonyms if isinstance(synonyms, list) else [])
        for value in values:
            if isinstance(value, str):
                recognized.update(re.findall(r'[a-z0-9]+', value.casefold()))
    stop = recognized | {
        'find', 'show', 'list', 'count', 'group', 'grouped', 'report', 'breakdown',
        'matching', 'available', 'all', 'any', 'among',
        'many', 'the', 'these', 'those', 'hpap', 'healthy', 'control', 'controls',
        'nd', 't1d', 't2d', 'non', 'diabetic', 'diabetes', 'type', 'stage', 'recorded',
        'sample', 'samples', 'from', 'with', 'whose', 'having', 'female', 'male',
    }
    for match in re.finditer(r'\b([A-Za-z][A-Za-z0-9_-]*)\s+donors?\b', text, re.I):
        modifier = match.group(1).casefold()
        if modifier not in stop and not modifier.isdigit():
            return True
    return False


def _generic_donor_disease_mentions(text, vocabulary):
    """Resolve explicitly role-qualified donor diseases from the live inventory."""
    result = []
    for record in vocabulary.get('donor_diseases') or []:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str):
            continue
        values = [record.get('id'), record.get('name')]
        synonyms = record.get('synonyms')
        values.extend(synonyms if isinstance(synonyms, list) else [])
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            for match in re.finditer(r'(?<!\w)' + re.escape(value) + r'(?!\w)', text, re.I):
                before = text[max(0, match.start() - 45):match.start()]
                after = text[match.end():match.end() + 30]
                role = (re.match(r'\s+(?:donors?|cohort)\b', after, re.I)
                        or re.search(r'\b(?:donors?|cohort)\s+(?:with|having|without|'
                                     r'excluding|exclude|except|other\s+than|negative\s+for|'
                                     r'with\s+no|not\s+diagnosed\s+with|diagnosed\s+with)\s*$',
                                     before, re.I)
                        or re.search(r'\b(?:donors?|cohort)\s+(?:suffering\s+from|'
                                     r'with\s+(?:a\s+)?diagnosis\s+of|diagnosed\s+as)\s*$',
                                     before, re.I)
                        or re.search(r'\b(?:not\s+)?diagnosed\s+with\s*$', before, re.I))
                if role:
                    result.append({'record': record, 'requested': value,
                                   'negated': _negated_at(text, match.start(), match.end())})
    unique = {}
    for item in result:
        unique[(item['record']['id'], item['negated'])] = item
    return list(unique.values())


def _trusted_request(step):
    request = step.get('semantic_request') or {}
    if request.get('source') == 'user_request' and isinstance(request.get('question'), str):
        return request['question'], True
    return str(step.get('question') or ''), False


def _negated_at(text, start, end=None):
    """Return whether the immediately surrounding clause excludes a mention."""
    before = text[max(0, start - 70):start]
    # Contrast conjunctions start a new polarity scope. In particular, the
    # negation in "not T1D but T2D" and "not StudyA but HPAP" must not leak
    # onto the positive alternative.
    before = re.split(r'[.!?;,()]|\b(?:but|however|whereas|yet)\b', before,
                      flags=re.I)[-1]
    # "do not exclude X" is positive, unlike "do not include X".
    if re.search(r'\bdo\s+not\s+(?:exclude|omit|remove)\s*$', before, re.I):
        return False
    if re.search(r'\bnot\s+(?:only|just)\s*$', before, re.I):
        return False
    prefix_negative = bool(re.search(
        r'(?:\b(?:not|no|never|neither|nor|non|without|excluding|exclude|except|omit|omitting|remove|removing|lacking)\b'
        r'|\b(?:other\s+than|free\s+of|negative\s+for)\b|\bdo\s+not\s+(?:include|use|select)\b)[^.!?;,()]{0,35}$',
        before, re.I))
    if prefix_negative:
        return True
    if end is None:
        token = re.match(r'[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*', text[start:])
        end = start + token.end() if token else start
    after = text[end:min(len(text), end + 70)]
    return bool(re.match(
        r'\s*(?:[-–—]\s*)?(?:(?:(?:is|are|was|were|being)\s+)?'
        r'(?:excluded|omitted|removed|not\s+included)\b'
        r'|(?:donors?|samples?|records?|cohort)\s+(?:(?:is|are|was|were)\s+)?'
        r'(?:excluded|omitted|removed|not\s+included)\b)',
        after, re.I))


def _disease_suffix_negative(text, end):
    after = text[end:min(len(text), end + 45)]
    return bool(re.match(
        r'(?:[-–—]\s*(?:free|negative)\b|\s+negative\s+donors?\b)',
        after, re.I))


def _disease_mentions(text):
    result = []
    pattern = (r'\bT(?P<short>[12])D(?:M)?\b|'
               r'\btype\s*(?P<prefix>[12]|II|I)\s*diabetes(?:\s+mellitus)?\b|'
               r'\bdiabetes(?:\s+mellitus)?\s*type\s*(?P<suffix>[12]|II|I)\b')
    for match in re.finditer(pattern, text, re.I):
        raw = (match.group('short') or match.group('prefix') or match.group('suffix')).casefold()
        kind = {'i': '1', 'ii': '2'}.get(raw, raw)
        result.append({'kind': kind, 'text': match.group(0),
                       'start': match.start(), 'end': match.end(),
                       'negated': (_negated_at(text, match.start(), match.end())
                                   or _disease_suffix_negative(text, match.end()))})
    return result


def control_cohort_polarity(text):
    """Return explicit positive/negative control-cohort intent.

    A bare adjective such as ``healthy spleen`` is deliberately not a donor
    classification. Every accepted form either names a control cohort directly
    or says that a donor/cohort is recorded or classified as one. Outer
    exclusions are evaluated at the start of the complete phrase so that the
    semantic ``non``/``without`` inside a control label stays positive.
    """
    direct_patterns = (
        r'\bND\s*/\s*healthy(?:\s+(?:controls?|donors?|cohort))?',
        r'\bND\b\s*(?:\(\s*non[- ]diabet(?:ic|es?)\s*\)|\s+(?:controls?|donors?|cohort)\b)',
        r'\bnon[- ]diabet(?:ic|es?)\s+(?:controls?|donors?|cohort)\b',
        r'\bhealthy\s+(?:controls?|donors?|cohort)\b',
        r'\bcontrols?\s+without\s+diabetes\b',
        r'\b(?:donors?|cohort)\s+without\s+diabetes\b',
    )
    if re.search(r'\b(?:donors?|cohort|samples?)\b', text, re.I):
        direct_patterns += (r'\bND\b',)
    mentions = []
    for pattern in direct_patterns:
        for match in re.finditer(pattern, text, re.I):
            mentions.append((match.start(), match.end(),
                             _negated_at(text, match.start(), match.end())))
    # For classification wording, polarity belongs to the classification verb,
    # not the earlier donor noun. This keeps "donors not recorded as healthy"
    # from becoming a positive control filter while retaining the positive
    # meaning of the inner label "non-diabetic".
    classified = (r'\b(?:donors?|cohort)\b[^.!?;]{0,35}?\b'
                  r'(?P<classifier>recorded|classified|label(?:ed|led)|categorized|identified)'
                  r'\s+as\s+(?:ND|healthy|non[- ]diabet(?:ic|es?)|controls?\s+without\s+diabetes)\b')
    for match in re.finditer(classified, text, re.I):
        mentions.append((match.start(), match.end(),
                         _negated_at(text, match.start('classifier'), match.end())))
    # Overlapping aliases (for example ND/healthy donors) describe one phrase.
    unique = {(start, end, negative) for start, end, negative in mentions}
    return {'positive': any(not negative for _, _, negative in unique),
            'negative': any(negative for _, _, negative in unique)}


def _control_cohort_intent(text):
    return control_cohort_polarity(text)['positive']


def _category_values(vocabulary, field):
    values = (vocabulary.get('donor_categorical_values') or {}).get(field)
    return values if (vocabulary.get('donor_categories_complete') is True
                      and isinstance(values, list)
                      and all(isinstance(value, str) for value in values)) else None


def _control_category(vocabulary):
    values = _category_values(vocabulary, 'diabetes_type')
    if values is None:
        return None
    candidates = []
    for value in values:
        normalized = re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()
        if (normalized in {'nd', 'healthy', 'healthy control', 'non diabetic'}
                or normalized == 'control without diabetes'
                or normalized == 'without diabetes'):
            candidates.append(value)
    return candidates[0] if len(candidates) == 1 else None


def _disease_category(vocabulary, kind):
    candidates = []
    pattern = (r'\b(?:t1d(?:m)?|type (?:1|i) diabetes|diabetes mellitus type (?:1|i)|diabetes type (?:1|i))\b'
               if kind == '1' else
               r'\b(?:t2d(?:m)?|type (?:2|ii) diabetes|diabetes mellitus type (?:2|ii)|diabetes type (?:2|ii))\b')
    for record in vocabulary.get('donor_diseases') or []:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str):
            continue
        raw = [record.get('name'), record.get('id')]
        synonyms = record.get('synonyms')
        if isinstance(synonyms, str):
            try:
                parsed = json.loads(synonyms)
            except (TypeError, ValueError):
                parsed = re.split(r'[|;]', synonyms)
            synonyms = parsed if isinstance(parsed, list) else [synonyms]
        raw.extend(synonyms if isinstance(synonyms, list) else [])
        text = ' | '.join(str(value) for value in raw if isinstance(value, str))
        if re.search(pattern, re.sub(r'[_-]+', ' ', text), re.I):
            candidates.append(record)
    return candidates[0] if len(candidates) == 1 else None


def _runtime_match(binding, requested, kind, vocabulary, release):
    return {'requested': requested, 'canonical_binding': deepcopy(binding),
            'match_kind': 'verified_runtime_' + kind, 'registry_version': VERSION,
            'source': 'current complete graph categorical inventory',
            'graph_release': release, 'inventory_sha256': vocabulary.get('inventory_sha256')}



def diagnosis_filter_intent(text):
    # Explanation and exclusion clauses do not request another cohort filter.
    text = re.sub(r'\b(?:distinguish|differentiate|explain|compare)\b[^.!?;]*', '', text, flags=re.I)
    text = re.sub(r"\b(?:without|do not|don't|no)\b[^.!?;]*\b(?:diagnos\w*|clinical diabetes|diabetes_type)\b[^.!?;]*", '', text, flags=re.I)
    return bool(re.search(r'diagnos|clinical diabetes|disease (?:link|category)|diabetes_type', text, re.I))


def t1d_stage_field_intent(text):
    """Recognize stage as a requested field without inventing a diagnosis."""
    if not isinstance(text, str):
        return False
    return bool(
        re.search(r'\bstage\s*(?:[-:]|is\b|equals?\b)?\s*(?:\d+|I{1,3})\b',
                  text, re.I)
        or re.search(r'\bT1D\s+stages?\b', text, re.I)
        or re.search(r'\b(?:by|across|per)\s+(?:T1D\s+)?stages?\b', text, re.I)
        or re.search(r'\b(?:stage|stages)\s+(?:distribution|values?|breakdown|grouping)\b',
                     text, re.I)
        or re.search(r'\b(?:distribution|values?|breakdown|grouping)\s+(?:of|for|by)\s+'
                     r'(?:T1D\s+)?stages?\b', text, re.I)
        or re.search(r'\b(?:any|all|each)\b[^.!?;]{0,30}\b(?:T1D\s+)?stages?\b',
                     text, re.I))


def _disease_mention_is_stage_label(text, mention):
    start, end = mention.get('start', 0), mention.get('end', 0)
    return bool(
        re.match(r'\s+stages?\b', text[end:], re.I)
        or re.search(r'\bstage\s*(?:[-:]\s*)?(?:\d+|I{1,3})\s*(?:of\s+)?$',
                     text[max(0, start - 45):start], re.I))

def _diagnosis_request_text(step, vocabulary):
    request = step.get('semantic_request') or {}
    trusted = request.get('source') == 'user_request' and isinstance(request.get('question'), str)
    text = request['question'] if trusted else step.get('question', '')
    # A stored stage label is a category description, not a second request for
    # diagnosis. Flexible punctuation covers the display '(...)' expansion.
    for value in sorted((v for v in vocabulary.get('stages', []) if isinstance(v, str)), key=len, reverse=True):
        words = re.findall(r'[A-Za-z0-9]+', value)
        if words:
            pattern = r'(?<!\w)' + r'[\s:(),;_-]*'.join(re.escape(word) for word in words) + r'(?!\w)'
            text = re.sub(pattern, ' recorded stage ', text, flags=re.I)
    instruction = request.get('revision_instruction') if trusted else None
    if isinstance(instruction, str) and instruction.strip():
        clinical = r'(?:diagnos\w*|clinical[- ](?:disease|diabetes)|diabetes_type|disease\s+(?:filter|category|classification|link))'
        if re.search(clinical, instruction, re.I):
            remove = bool(re.search(r'\b(?:remove|drop|omit|clear)\b.{0,60}' + clinical, instruction, re.I))
            add = bool(re.search(r'\b(?:add|require|include)\b.{0,60}' + clinical, instruction, re.I))
            if remove and not add:
                return '', 'revision_remove_clinical_filter'
            if add and not remove and not re.search(r'\b(?:not|without|except|excluding)\b', instruction, re.I):
                return instruction, 'revision_add_clinical_filter'
            # A revision may mention clinical scope while changing something
            # else. Preserve explicit keep wording; do not guess other changes.
            if not re.search(r'\b(?:keep|retain|preserve|keeping|retaining|preserving)\b.{0,70}' + clinical, instruction, re.I):
                return text, 'ambiguous_clinical_revision'
    return text, 'original_user_request' if trusted else 'question_without_recorded_stage_descriptions'


def planner_guidance(question):
    if not re.search(r'\bdonors?\b|\bHPAP\b|multiom|scRNA|ATAC', question,re.I): return ''
    return ('\nDonor/sample terminology: stage labels and assay names are resolved by the application against current graph values. '
            'Keep requested stages in ordinary biological wording. donor.t1d_stage belongs only to donor. A recorded T1D stage is not an additional diagnosed-diabetes category restriction; add the latter only when explicitly requested. '
            'Sample_node.data_modality is the assay; tissue uses anatomical_structure -HAS_SAMPLE-> Sample_node. '
            'RNA can be a documented component of multiome; retain exact-only or paired requirements. '
            'For a donor-cohort lookup, connect each sample directly to its donor. A tissue-only sample lookup does not require a donor join; do not add samples to a donor-only lookup.')


def _assay_values(constraint):
    operator = str(constraint.get('operator', '=')).upper()
    raw = constraint.get('value')
    if operator in {'IN', 'NOT IN'}:
        try:
            values = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        return values if isinstance(values, list) and values and all(isinstance(v, str) for v in values) else None
    return [raw] if isinstance(raw, str) else None


def _canonical_assay(value, available):
    alias = ALIASES.get(re.sub(r'[^a-z0-9]', '', str(value).lower()), value)
    matches = [v for v in available if isinstance(v, str) and v.casefold() == str(alias).casefold()]
    return matches[0] if len(matches) == 1 else value


def _mentioned_assays(question, available):
    words = [v.casefold() for v in re.findall(r'[A-Za-z0-9]+', question)]
    aliases = {re.sub(r'[^a-z0-9]', '', value.casefold()): value for value in available if isinstance(value, str)}
    aliases.update({key: value for key, value in ALIASES.items() if value in available})
    values, start = [], 0
    while start < len(words):
        matches = [(end, aliases[''.join(words[start:end])])
                   for end in range(start + 1, min(len(words), start + 7) + 1)
                   if ''.join(words[start:end]) in aliases]
        if matches:
            end, value = max(matches)
            if value not in values:
                values.append(value)
            start = end
        else:
            start += 1
    return values


def _explicit_modality_label(question, available):
    """Recognize a recorded field value, not a general RNA capability request."""
    for match in re.finditer(r'\bdata[ _]modality\b', question, re.I):
        suffix = re.match(r'\s*(?:(?:=|is|of|:)\s*)?[\"\x27]?([A-Za-z0-9].*)', question[match.end():])
        if not suffix:
            continue
        words = re.findall(r'[A-Za-z0-9]+', suffix.group(1))[:7]
        for end in range(1, len(words) + 1):
            if _canonical_assay(' '.join(words[:end]), available) in available:
                return True
    return False


def _without_negated_assays(question, available, *, return_values=False):
    """Mask directly negated verified assay names for capability detection only.

    The original wording and typed constraints remain unchanged. Missing or
    ambiguous modality names are not silently assigned a positive capability.
    """
    tokens = list(re.finditer(r'[A-Za-z0-9]+', question))
    words = [token.group().casefold() for token in tokens]
    aliases = {re.sub(r'[^a-z0-9]', '', value.lower()): value for value in available if isinstance(value, str)}
    aliases.update(ALIASES)
    spans = []
    for start in range(len(tokens)):
        candidates = [end for end in range(start + 1, min(len(tokens), start + 7) + 1)
                      if ''.join(words[start:end]) in aliases]
        if not candidates:
            continue
        end = max(candidates)
        excluded = _negated_at(question, tokens[start].start(), tokens[end - 1].end())
        excluded = excluded or bool(re.search(
            r'\bdata[ _]modality\s*(?:!=|<>|is\s+not)\s*[\"\x27]?$',
            question[:tokens[start].start()], re.I))
        if excluded:
            spans.append((tokens[start].start(), tokens[end - 1].end(), aliases[''.join(words[start:end])]))
    if return_values:
        return list(dict.fromkeys(value for _, _, value in spans if value in available))
    chars = list(question)
    for start, end, _ in spans:
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)


def _assay_intent_values(text, available):
    """Return runtime modality values authorized by one wording span.

    This is an authorization envelope, not the final grouping compiler.  It
    mirrors the existing exact-label/capability distinction so generated step
    prose may narrow a raw request but cannot introduce a different assay.
    """
    positive_text = _without_negated_assays(text, available)
    negative = set(_without_negated_assays(text, available, return_values=True))
    named = set(_mentioned_assays(positive_text, available))
    rna = bool(re.search(r'(?<![a-z])(?:sc|sn)?RNA[\s-]*(?:seq)?|transcriptom',
                         positive_text, re.I))
    atac = bool(re.search(r'ATAC|chromatin accessibility', positive_text, re.I))
    paired = bool(re.search(r'multiom|paired|joint', positive_text, re.I))
    exact = bool(re.search(
        r'\bstandalone\b|\bexact(?:ly)?\b.{0,20}(?:RNA|ATAC)|'
        r'(?:RNA[\s-]*seq|ATAC[\s-]*seq)\s+only\b|'
        r'\bonly\s+(?:sc|sn)?(?:RNA|ATAC)|exclude\s+multiom', text, re.I))
    label_lookup = bool(re.search(
        r'\b(?:samples?|records?)\s+(?:explicitly\s+)?label(?:ed|led)\b|'
        r'\b(?:assay|modality)\s+(?:label|value)\s*(?:=|is|of|:)\s*', text, re.I)
        or _explicit_modality_label(positive_text, available))
    capability_inclusion = bool(re.search(
        r'\b(?:include|including|also|or)\b[^.!?;\n]{0,120}'
        r'\b(?:(?:sn)?multiom\w*|(?:RNA|ATAC)\s+components?)', positive_text, re.I))
    if named and (label_lookup or not paired) and not capability_inclusion:
        exact = True
    positive = set(named)
    if paired and not exact:
        positive.add('snMultiomics')
    if not exact:
        if rna:
            positive.update({'scRNA-seq', 'snMultiomics'} & set(available))
        if atac:
            positive.update({'scATAC-seq', 'snMultiomics'} & set(available))
    return positive, negative, bool(positive or negative or rna or atac or paired)


_DONOR_REQUEST_FIELD_PATTERNS = schema_module('semantics_modalities')['request_fields']['_DONOR_REQUEST_FIELD_PATTERNS']


_SAMPLE_REQUEST_FIELD_PATTERNS = schema_module('semantics_modalities')['request_fields']['_SAMPLE_REQUEST_FIELD_PATTERNS']


def _constraint_values(constraint):
    raw = constraint.get('value')
    if str(constraint.get('operator', '=')).upper() in {'IN', 'NOT IN'}:
        try:
            raw = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        return raw if isinstance(raw, list) else []
    return [raw]


def _value_mentions(text, value):
    if isinstance(value, bool):
        literal = 'true' if value else 'false'
    elif isinstance(value, (str, int, float)):
        literal = str(value)
    else:
        return []
    return list(re.finditer(r'(?<!\w)' + re.escape(literal) + r'(?!\w)', text, re.I))


def _canonical_owner_name(value):
    aliases = {
        'sample': 'Sample_node', 'samples': 'Sample_node',
        'sample_node': 'Sample_node', 'gene': 'Gene', 'genes': 'Gene',
        'variant': 'variants', 'variants': 'variants',
        'donor': 'donor', 'donors': 'donor',
        'disease': 'disease', 'diseases': 'disease',
        'anatomy': 'anatomical_structure', 'tissue': 'anatomical_structure',
        'anatomical_structure': 'anatomical_structure',
    }
    return aliases.get(str(value).strip().casefold(), str(value).strip())


def _qualified_field_owner(text, field):
    """Return a directly qualified ``Owner.property``/``owner property`` role."""
    prefix = text[max(0, field.start() - 45):field.start()]
    match = re.search(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\.\s*|\s+)$', prefix, re.I)
    if not match:
        return None
    candidate = _canonical_owner_name(match.group(1))
    try:
        from .release_schema import REGISTRY as schema
        known = set(schema.get('nodes', {})) | set(schema.get('relations', {}))
    except (ImportError, AttributeError, TypeError):
        known = set(PROPERTIES)
    return candidate if candidate in known else None


def _request_field_mentions(owner, prop, text):
    """Return explicit mentions of one property in the immutable request."""
    if prop in {'id', 'name'} and owner:
        field_pattern = r'\b(?:id|identifier)\b' if prop == 'id' else r'\bname\b'
        qualified = [match for match in re.finditer(field_pattern, text, re.I)
                     if _qualified_field_owner(text, match) == owner]
        if qualified:
            return qualified
    if owner == 'Sample_node' and prop in _SAMPLE_REQUEST_FIELD_PATTERNS:
        matches = list(re.finditer(_SAMPLE_REQUEST_FIELD_PATTERNS[prop], text, re.I))
        if matches:
            return [match for match in matches
                    if _qualified_field_owner(text, match) in {None, owner}]
    if prop in {'id', 'name'} and owner:
        aliases = {
            'Sample_node': r'\bsamples?\b',
            'anatomical_structure': r'\b(?:anatom(?:y|ical)|tissues?|organs?)\b',
            'disease': r'\b(?:diseases?|diagnos(?:is|es))\b',
            'Gene': r'\bgenes?\b',
            'variants': r'\bvariants?\b',
        }
        matches = list(re.finditer(aliases.get(owner, r'(?!)'), text, re.I))
        if matches:
            return matches
    if owner == 'donor':
        if prop == 'id' and re.search(r'\b(?:donor|HPAP[-_][A-Za-z0-9]+)\b', text, re.I):
            return list(re.finditer(r'\b(?:donor|HPAP[-_][A-Za-z0-9]+)\b', text, re.I))
        pattern = _DONOR_REQUEST_FIELD_PATTERNS.get(prop)
        if pattern:
            return [match for match in re.finditer(pattern, text, re.I)
                    if _qualified_field_owner(text, match) in {None, owner}]
    phrase = re.sub(r'[_-]+', r'[ _-]+', re.escape(str(prop)))
    matches = (list(re.finditer(r'(?<!\w)' + phrase + r'(?!\w)', text, re.I))
               if prop else [])
    # A qualified owner is authoritative. ``Gene.description`` must never
    # authorize a disease.description filter merely because both endpoints
    # expose a property with the same spelling.
    return [match for match in matches
            if _qualified_field_owner(text, match) in {None, owner}]


def _property_owner_is_unambiguous(owner, prop, text, relations=()):
    """Require a node-property owner when the release has several candidates."""
    if not owner or not prop:
        return True
    if ((owner == 'donor' and prop in _DONOR_REQUEST_FIELD_PATTERNS)
            or (owner == 'Sample_node' and prop in _SAMPLE_REQUEST_FIELD_PATTERNS)):
        # These user-facing field grammars are owner-specific. Explicit
        # ``OtherOwner.field`` occurrences were already removed above.
        return True
    try:
        from .release_schema import REGISTRY as schema
    except (ImportError, AttributeError, TypeError):
        return True
    possible = {kind for kind, fields in schema.get('nodes', {}).items()
                if prop in fields}
    relation_specs = [schema.get('relations', {}).get(kind, {})
                      for kind in (relations or [])]
    endpoint_types = {kind for spec in relation_specs
                      for path in spec.get('paths', [])
                      for side in ('source', 'target') for kind in path.get(side, [])}
    if endpoint_types:
        possible &= endpoint_types
    if len(possible) <= 1:
        return owner in possible if possible else True
    fields = _request_field_mentions(owner, prop, text)
    return bool(fields) and all(_qualified_field_owner(text, field) == owner
                                for field in fields)


def _request_field_named(owner, prop, text):
    return bool(_request_field_mentions(owner, prop, text))


def _other_request_field_mentions(owner, prop, text):
    """Locate competing fields that must not lend their values/operators."""
    matches = []
    if owner == 'donor':
        for other_prop, pattern in _DONOR_REQUEST_FIELD_PATTERNS.items():
            if other_prop != prop:
                matches.extend(re.finditer(pattern, text, re.I))
    if owner == 'Sample_node':
        for other_prop, pattern in _SAMPLE_REQUEST_FIELD_PATTERNS.items():
            if other_prop != prop:
                matches.extend(re.finditer(pattern, text, re.I))
    for other_prop in PROPERTIES.get(owner, []):
        if other_prop == prop or owner == 'donor' and other_prop in _DONOR_REQUEST_FIELD_PATTERNS:
            continue
        phrase = re.sub(r'[_-]+', r'[ _-]+', re.escape(str(other_prop)))
        matches.extend(re.finditer(r'(?<!\w)' + phrase + r'(?!\w)', text, re.I))
    # A catalog-like word inside this field's literal is not a neighboring
    # field.  For example, ``cause of death contains Stage 3`` must not let
    # ``Stage 3`` truncate the cause_of_death atom.
    current_fields = _request_field_mentions(owner, prop, text)
    current_value_spans = [(start, end) for start, end, kind in scope_incidental_spans(text)
                           if kind == 'typed_property_value'
                           and any(start <= field.start() < end
                                   for field in current_fields)]
    return [match for match in matches if not any(
        start < match.start() and match.end() <= end
        for start, end in current_value_spans)]


def _request_atom_bounds(owner, prop, text, field):
    """Bound one field's local clause, excluding neighboring field atoms."""
    delimiters = [index for index, char in enumerate(text) if char in '.!?;\n'
                  and not (char == '.' and index > 0 and index + 1 < len(text)
                           and text[index - 1].isdigit() and text[index + 1].isdigit())]
    sentence_start = max((index for index in delimiters if index < field.start()),
                         default=-1) + 1
    sentence_ends = [index for index in delimiters if index >= field.end()]
    sentence_end = min((end for end in sentence_ends if end >= 0), default=len(text))
    peer_pattern = (_DONOR_REQUEST_FIELD_PATTERNS.get(prop) if owner == 'donor'
                    else _SAMPLE_REQUEST_FIELD_PATTERNS.get(prop)
                    if owner == 'Sample_node' else None)
    peer_fields = (list(re.finditer(peer_pattern, text, re.I)) if peer_pattern
                   else _request_field_mentions(owner, prop, text))
    same = [match for match in peer_fields
            if (match.start(), match.end()) != (field.start(), field.end())]
    others = sorted([*_other_request_field_mentions(owner, prop, text), *same],
                    key=lambda match: match.start())
    before = [match for match in others
              if sentence_start <= match.start() and match.end() <= field.start()]
    after = [match for match in others
             if field.end() <= match.start() < sentence_end]
    connectors = r'\b(?:and|or|but|with|whose|having|plus)\b|,'
    if before:
        nearest = before[-1]
        between = text[nearest.end():field.start()]
        separators = list(re.finditer(connectors, between, re.I))
        sentence_start = (nearest.end() + separators[-1].end()
                          if separators else nearest.end())
    if after:
        nearest = after[0]
        between = text[field.end():nearest.start()]
        separator = re.search(connectors, between, re.I)
        sentence_end = (field.end() + separator.start()
                        if separator else nearest.start())
    return sentence_start, sentence_end


def _request_field_operand(text, field):
    """Parse one complete local operand; never authorize a substring of it."""
    end = _scope_clause_boundary(text, field.end())
    tail = text[field.end():end]
    operators = [
        ('STARTS WITH', r'^\s*starts?\s+with\b'),
        ('ENDS WITH', r'^\s*ends?\s+with\b'),
        ('CONTAINS', r'^\s*(?:contains?|mentions?|includes?)\b'),
        ('!=', r'^\s*(?:!=|<>)'), ('>=', r'^\s*>='), ('<=', r'^\s*<='),
        ('>', r'^\s*>'), ('<', r'^\s*<'),
        ('=', r'^\s*(?:=|:|equals?\b|is\b|was\b|were\b)'),
    ]
    operator, remainder = '=', tail
    for candidate, pattern in operators:
        match = re.match(pattern, tail, re.I)
        if match:
            operator, remainder = candidate, tail[match.end():]
            break
    value = remainder.strip()
    # Common classification grammar is still one equality operand.
    value = re.sub(r'^(?:recorded|classified|label(?:ed|led)|categorized)\s+as\s+',
                   '', value, flags=re.I)
    if len(value) >= 2:
        pairs = {'"': '"', "'": "'", '`': '`', '(': ')', '[': ']', '{': '}'}
        close = pairs.get(value[0])
        if close and value.endswith(close):
            value = value[1:-1].strip()
    return operator, value


def _explicit_field_constraint(owner, prop, text, field):
    start, end = _request_atom_bounds(owner, prop, text, field)
    tail = text[field.end():end]
    if not re.match(
            r'\s*(?:!=|<>|>=|<=|=|>|<|:|contains?|mentions?|includes?|'
            r'equals?|is\b|was\b|were\b|starts?\s+with\b|ends?\s+with\b)',
            tail, re.I):
        return None
    operator, value = _request_field_operand(text[:end], field)
    value = value.strip().rstrip('.,;!?').strip()
    if not value:
        return None
    return {'entity_type': owner, 'property': prop,
            'operator': operator, 'value': value}


def _numeric_atom_predicates(prop, atom):
    """Parse each numeric comparator with its own adjacent value."""
    found = []
    def add(operator, value):
        candidate = {'entity_type': 'donor', 'property': prop,
                     'operator': operator, 'value': value}
        if not any(_same_constraint(candidate, existing) for existing in found):
            found.append(candidate)
    ranges = list(re.finditer(
        r'(?<![\w.])(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*'
        r'(\d+(?:\.\d+)?)(?![\w.])', atom, re.I))
    for match in ranges:
        add('>=', match.group(1)); add('<=', match.group(2))
    for match in re.finditer(r'(>=|<=|>|<|=)\s*(\d+(?:\.\d+)?)(?![\w.])', atom):
        add(match.group(1), match.group(2))
    leading = (
        ('<=', r'at\s+most|(?:no|not)\s+more\s+than|not\s+older\s+than|maximum'),
        ('>=', r'at\s+least|not\s+less\s+than|minimum'),
        ('>', r'above|over|greater\s+than|more\s+than|older\s+than'),
        ('<', r'below|under|less\s+than|younger\s+than'),
    )
    for operator, phrase in leading:
        for match in re.finditer(r'\b(?:' + phrase + r')\b\s*(\d+(?:\.\d+)?)(?![\w.])',
                                 atom, re.I):
            add(operator, match.group(1))
    trailing = (
        ('>=', r'or\s+older|and\s+above'), ('<=', r'or\s+younger|and\s+below'),
    )
    for operator, phrase in trailing:
        for match in re.finditer(r'(?<![\w.])(\d+(?:\.\d+)?)\s+\b(?:' + phrase + r')\b',
                                 atom, re.I):
            add(operator, match.group(1))
    return found


def _request_operator_matches(constraint, text):
    operator = str(constraint.get('operator', '=')).upper()
    values = _constraint_values(constraint)
    if not values or any(not _value_mentions(text, value) for value in values):
        return False
    if operator in {'=', 'IN'}:
        return all(any(not _negated_at(text, match.start(), match.end())
                       for match in _value_mentions(text, value)) for value in values)
    if operator in {'!=', '<>', 'NOT IN'}:
        return all(any(_negated_at(text, match.start(), match.end()) for match in _value_mentions(text, value))
                   for value in values)
    value_pattern = '|'.join(re.escape(str(value)) for value in values)
    for symbol_match in re.finditer(
            r'(>=|<=|>|<)\s*(?:' + value_pattern + r')(?=\W|$)', text):
        if symbol_match.group(1) == operator and not _negated_at(text, symbol_match.start()):
            return True
    # A written range authorizes its lower and upper bounds; the resolver still
    # preserves each exact typed operator/value in the audit proof.
    if operator in {'>=', '<='}:
        ranges = list(re.finditer(
            r'\b\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\b', text, re.I))
        if any(not _negated_at(text, match.start()) for match in ranges):
            return True
    phrases = {
        '>': r'\b(?:above|over|greater\s+than|more\s+than|older\s+than)\b',
        '>=': r'\b(?:at\s+least|not\s+less\s+than|minimum|or\s+older|and\s+above)\b',
        '<': r'\b(?:below|under|less\s+than|younger\s+than)\b',
        '<=': r'\b(?:at\s+most|(?:no|not)\s+more\s+than|not\s+older\s+than|maximum|or\s+younger|and\s+below)\b',
        'CONTAINS': r'\b(?:contains?|including|with)\b',
        'STARTS WITH': r'\bstarts?\s+with\b',
        'ENDS WITH': r'\bends?\s+with\b',
    }
    return any(not _negated_at(text, match.start())
               for match in re.finditer(phrases.get(operator, r'(?!)'), text, re.I))


def _membership_values_locally_qualify_field(constraint, field, value_mentions,
                                              text, start, end):
    """Reject projection prose and cross-phrase value borrowing for equality."""
    operator = str(constraint.get('operator', '=')).upper()
    if operator not in {'=', '!=', '<>', 'IN', 'NOT IN'}:
        return True
    local_values = [[match for match in mentions
                     if start <= match.start() and match.end() <= end]
                    for mentions in value_mentions]
    if any(not mentions for mentions in local_values):
        return False
    prefix = text[max(start, field.start() - 45):field.start()]
    if (re.search(r'\b(?:report|return|display|list|breakdown|group|stratify)\b', prefix, re.I)
            and not re.search(r'\b(?:with|where|whose|having|filter(?:ed)?)\b', prefix, re.I)):
        return False
    chosen = [mentions[0] for mentions in local_values]
    if operator in {'IN', 'NOT IN'}:
        first = min(chosen, key=lambda match: match.start())
        return (field.end() <= first.start()
                and first.start() - field.end() <= 50
                and not re.search(r'\b(?:report|return|display|breakdown|group|stratify|'
                                  r'data\s+available|values?|distribution|samples?|records?|'
                                  r'count|number|total)\b',
                                  text[field.end():first.start()], re.I))
    value = chosen[0]
    if field.end() <= value.start():
        gap = text[field.end():value.start()]
        return (len(gap) <= 50
                and not re.search(r'\b(?:and|or|but|report|return|display|list|breakdown|group|stratify|'
                                  r'data\s+available|values?|distribution|samples?|donors?|records?|'
                                  r'count|number|total)\b',
                                  gap, re.I))
    if value.end() <= field.start():
        gap = text[value.end():field.start()]
        reviewed_category_gap = bool(re.fullmatch(
            r'\s+donors?\s+(?:by|with)\s+(?:recorded\s+)?', gap, re.I))
        return (len(gap) <= 35
                and (reviewed_category_gap or not re.search(
                    r'\b(?:and|or|but|with|whose|report|return|display|list|breakdown|group|stratify|'
                    r'data\s+available|values?|distribution|samples?|donors?|records?|'
                    r'count|number|total)\b', gap, re.I)))
    return False


def _raw_constraint_authorized(constraint, text, relations=()):
    owner, prop = constraint.get('entity_type'), constraint.get('property')
    if (owner == 'donor' and prop == 'id'
            and isinstance(constraint.get('value'), str)
            and re.fullmatch(r'HPAP[-:]\d+', constraint['value'], re.I)
            and _value_mentions(scope_intent_text(text), constraint['value'])):
        return str(constraint.get('operator', '=')).upper() == '='
    if (constraint.get('owner_kind') in {None, 'node'}
            and not constraint.get('relationship_type')
            and not _property_owner_is_unambiguous(owner, prop, text, relations)):
        return False
    fields = _request_field_mentions(owner, prop, text)
    values = _constraint_values(constraint)
    value_mentions = [_value_mentions(text, value) for value in values]
    if not fields or not values or any(not mentions for mentions in value_mentions):
        return False
    for field in fields:
        start, end = _request_atom_bounds(owner, prop, text, field)
        atom = text[start:end]
        if end - start > 180:
            continue
        if (str(constraint.get('operator', '=')).upper() not in {'IN', 'NOT IN'}
                and re.search(r'\b(?:or|versus|vs\.?)\b', atom, re.I)):
            # A single scalar predicate cannot silently select one branch of a
            # same-field alternative. A reviewed split or exact list is needed.
            continue
        # Every value must occur in this same field-local atom. This prevents
        # e.g. ``age 30 and BMI 25`` from authorizing age=25 or BMI=30, and it
        # keeps an age range from lending its comparator to recorded HbA1c.
        operator = str(constraint.get('operator', '=')).upper().replace('<>', '!=')
        if prop in _NUMERIC_DONOR_REQUEST_FIELDS:
            parsed_numeric = _numeric_atom_predicates(prop, atom)
            if not any(_same_constraint(constraint, candidate)
                       for candidate in parsed_numeric):
                continue
        exact_string_operator = operator in {
            '=', '!=', 'CONTAINS', 'STARTS WITH', 'ENDS WITH'}
        if exact_string_operator and prop not in _NUMERIC_DONOR_REQUEST_FIELDS:
            parsed_operator, parsed_value = _request_field_operand(text, field)
            parsed_operator = parsed_operator.upper().replace('<>', '!=')
            expected = str(values[0]).strip() if len(values) == 1 else ''
            if (not expected or parsed_operator != operator
                    or parsed_value.casefold() != expected.casefold()):
                continue
        if (all(any(start <= match.start() and match.end() <= end for match in mentions)
                for mentions in value_mentions)
                and _membership_values_locally_qualify_field(
                    constraint, field, value_mentions, text, start, end)
                and _request_operator_matches(constraint, atom)):
            return True
    return False


_NUMERIC_DONOR_REQUEST_FIELDS = {
    'age', 'bmi', 'hba1c_percentage', 'c_peptide_ng_ml',
    'diabetes_duration', 'hospital_stay_hours',
}


def _same_constraint(left, right):
    left_op = str(left.get('operator', '=')).upper().replace('<>', '!=')
    right_op = str(right.get('operator', '=')).upper().replace('<>', '!=')
    return (left.get('entity_type') == right.get('entity_type')
            and left.get('property') == right.get('property')
            and left_op == right_op
            and str(left.get('value')).casefold() == str(right.get('value')).casefold())


def _required_raw_donor_filters(text, vocabulary):
    """Extract only reviewed, explicit donor predicates for completeness checks."""
    required, ambiguous, unparsed = [], set(), set()
    for match in re.finditer(r'\bHPAP[-:]\d+\b', scope_intent_text(text), re.I):
        required.append({'entity_type': 'donor', 'property': 'id',
                         'operator': '=', 'value': match.group(0)})
    categories = vocabulary.get('donor_categorical_values') or {}
    special = {'data_source', 'diabetes_type', 'derived_diabetes_status', 't1d_stage'}
    for prop, pattern in _DONOR_REQUEST_FIELD_PATTERNS.items():
        if prop in special:
            continue
        field_matches = list(re.finditer(pattern, text, re.I))
        if prop == 'data_version':
            sample_spans = [(match.start(), match.end()) for match in re.finditer(
                _SAMPLE_REQUEST_FIELD_PATTERNS['data_version'], text, re.I)]
            field_matches = [match for match in field_matches if not any(
                start <= match.start() and match.end() <= end
                for start, end in sample_spans)]
        if not field_matches:
            continue
        atoms = []
        for field in field_matches:
            start, end = _request_atom_bounds('donor', prop, text, field)
            atom = text[start:end]
            if atom not in atoms:
                atoms.append(atom)
        if prop in categories:
            mentioned = []
            for value in categories.get(prop) or []:
                for operator in ('=', '!='):
                    candidate = {'entity_type': 'donor', 'property': prop,
                                 'operator': operator, 'value': value}
                    if _raw_constraint_authorized(candidate, text):
                        required.append(candidate)
                        mentioned.append(value)
            def categorical_predicate(atom):
                field = next((match for match in field_matches
                              if match.group(0) in atom), None)
                if re.search(
                        r'(?:!=|<>|=|\b(?:is|was|were|equals?|contains?)\b)',
                        atom, re.I):
                    return True
                if field:
                    prefix = text[max(0, field.start() - 80):field.start()]
                    roles = list(re.finditer(
                        r'\b(?:with|whose|having|recorded|classified|labeled|labelled)\b',
                        prefix, re.I))
                    if roles and not re.search(
                            r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                            prefix[roles[-1].end():], re.I):
                        return True
                    suffix = text[field.end():_scope_clause_boundary(text, field.end())]
                    if (re.search(r'[A-Za-z0-9]', suffix)
                            and not re.match(
                                r'\s*(?:distribution|values?|data\s+available|breakdown|groups?)\b',
                                suffix, re.I)
                            and not re.search(
                                r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                                prefix, re.I)):
                        return True
                return False
            predicate_atoms = [atom for atom in atoms if categorical_predicate(atom)]
            if predicate_atoms and not mentioned:
                unparsed.add(prop)
            if any(re.search(r'\b(?:or|versus|vs\.?)\b', atom, re.I)
                   for atom in predicate_atoms):
                ambiguous.add(prop)
            continue
        if prop == 'id':
            for atom in atoms:
                match = re.search(
                    r'\b(?:donor|record)\s*(?:id|identifier)\b\s*(?:=|is|:)?\s*'
                    r'([A-Za-z0-9][A-Za-z0-9_.:-]*)', atom, re.I)
                if match:
                    required.append({'entity_type': 'donor', 'property': 'id',
                                     'operator': '=', 'value': match.group(1)})
            continue
        if prop in _NUMERIC_DONOR_REQUEST_FIELDS:
            for atom in atoms:
                parsed = _numeric_atom_predicates(prop, atom)
                required.extend(parsed)
                numbers = re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', atom)
                if not parsed and numbers:
                    candidate = {'entity_type': 'donor', 'property': prop,
                                 'operator': '=', 'value': numbers[0]}
                    if _raw_constraint_authorized(candidate, text):
                        required.append(candidate)
                if (not parsed and re.search(r'\b(?:or|versus|vs\.?)\b', atom, re.I)
                        and len(numbers) > 1):
                    ambiguous.add(prop)
            continue
        if prop == 'other_disease_records':
            explicit = [_explicit_field_constraint('donor', prop, text, field)
                        for field in field_matches]
            required.extend(candidate for candidate in explicit if candidate)
            if not any(explicit):
                unparsed.add(prop)
            continue
        explicit = [_explicit_field_constraint('donor', prop, text, field)
                    for field in field_matches]
        explicit = [candidate for candidate in explicit if candidate]
        if explicit:
            required.extend(explicit)
            continue
        predicate_role = False
        for field in field_matches:
            prefix = text[max(0, field.start() - 80):field.start()]
            roles = list(re.finditer(r'\b(?:with|whose|having)\b', prefix, re.I))
            if roles and not re.search(
                    r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                    prefix[roles[-1].end():], re.I):
                predicate_role = True
                break
        if not predicate_role:
            for field in field_matches:
                end = _scope_clause_boundary(text, field.end())
                suffix = text[field.end():end]
                prefix = text[max(0, field.start() - 80):field.start()]
                if (re.search(r'[A-Za-z0-9]', suffix)
                        and not re.match(
                            r'\s*(?:distribution|values?|data\s+available|breakdown|groups?)\b',
                            suffix, re.I)
                        and not re.search(
                            r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                            prefix, re.I)):
                    predicate_role = True
                    break
        if predicate_role or any(re.search(
                r'(?:!=|>=|<=|=|>|<|:)|\b(?:is|was|were|equals?|contains?|starts?\s+with|ends?\s+with)\b',
                atom, re.I) for atom in atoms):
            unparsed.add(prop)
    unique = []
    for candidate in required:
        if not any(_same_constraint(candidate, existing) for existing in unique):
            unique.append(candidate)
    return unique, ambiguous, unparsed


def _raw_donor_filter_completeness_issues(text, constraints, vocabulary):
    required, ambiguous, unparsed = _required_raw_donor_filters(text, vocabulary)
    missing = {candidate['property'] for candidate in required
               if not any(_same_constraint(candidate, constraint)
                          for constraint in constraints)}
    missing.update(ambiguous)
    missing.update(prop for prop in unparsed
                   if not any(constraint.get('entity_type') == 'donor'
                              and constraint.get('property') == prop
                              and _raw_constraint_authorized(constraint, text)
                              for constraint in constraints))
    return [f'A requested donor.{prop} filter is missing, ambiguous, or incomplete; do not execute a broader query.'
            for prop in sorted(missing)]


def _raw_sample_filter_completeness_issues(text, constraints):
    """Fail closed when an explicit supported sample predicate is omitted.

    Free-text sample fields deliberately are not reconstructed here.  The
    planner must retain one exact typed constraint and the field-local request
    authorizer must prove its operator and value against the immutable wording.
    """
    missing = set()
    predicate = re.compile(
        r'(?:!=|<>|>=|<=|=|>|<|\bcontains?|\bmentions?|\bincludes?|'
        r'\bequals?|\bis\b|\bwas\b|\bwere\b|\bstarts?\s+with\b|'
        r'\bends?\s+with\b)', re.I)
    for prop, pattern in _SAMPLE_REQUEST_FIELD_PATTERNS.items():
        fields = list(re.finditer(pattern, text, re.I))
        if not fields:
            continue
        eligible = [field for field in fields if not any(
            start <= field.start() and field.end() <= end
            for start, end, kind in scope_incidental_spans(text)
            if kind == 'projection_literal')]
        expected = [_explicit_field_constraint('Sample_node', prop, text, field)
                    for field in eligible]
        expected = [candidate for candidate in expected if candidate]
        if expected:
            if any(not any(_same_constraint(candidate, constraint)
                           for constraint in constraints)
                   for candidate in expected):
                missing.add(prop)
            continue
        requested = False
        for field in eligible:
            end = _scope_clause_boundary(text, field.end())
            atom = text[field.start():end]
            prefix = text[max(0, field.start() - 80):field.start()]
            roles = list(re.finditer(r'\b(?:with|whose|having)\b', prefix, re.I))
            predicate_role = bool(predicate.search(atom))
            if roles and not re.search(
                    r'\b(?:report|return|display|list|breakdown|group|stratify)\b',
                    prefix[roles[-1].end():], re.I):
                predicate_role = True
            suffix = text[field.end():end]
            if (re.search(r'[A-Za-z0-9]', suffix)
                    and not re.match(
                        r'\s*(?:distribution|values?|data\s+available|breakdown|groups?)\b',
                        suffix, re.I)):
                predicate_role = True
            requested |= predicate_role
        if not requested:
            continue
        valid = [constraint for constraint in constraints
                 if constraint.get('entity_type') == 'Sample_node'
                 and constraint.get('property') == prop
                 and _raw_constraint_authorized(constraint, text)]
        if len(valid) != 1:
            missing.add(prop)
    return [f'A requested Sample_node.{prop} filter is missing, ambiguous, or incomplete; do not execute a broader query.'
            for prop in sorted(missing)]


def _record_mention_polarities(text, record):
    terms = {record.get('requested_alias'), record.get('name'), record.get('id')}
    polarities = set()
    for term in (term for term in terms if isinstance(term, str) and term):
        for match in re.finditer(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I):
            polarities.add('negative' if _negated_at(text, match.start(), match.end()) else 'positive')
    return polarities


def resolve(step, vocabulary, release):
    out=deepcopy(step)
    # Recovery is derived from this resolution, never inherited from a prior
    # failed preview after a user corrects the entity or scope.
    out.pop('recovery', None)
    if not semantic_intent(out): return out
    q=out['question']; lower=q.lower(); constraints=out.setdefault('constraints',[])
    issues=[]; matches=[]; groups=[]
    if release!=RELEASE:
        out['semantic_issues']=['The terminology registry does not match this graph release.']
        out['recovery']={'category':'graph_release_mismatch','title':'The graph service needs attention',
            'message':'The terminology rules and the configured graph release do not match. Your question has been kept; an operator needs to align the service configuration. Changing the biological question will not fix this problem.',
            'retryable':False,'suggestions':[], 'evidence':{'graph_release':release,'registry_release':RELEASE}}
        return out
    def bind(prop, owner, value, operator='=', kind='alias', requested=None):
        # Replace only the same semantic field, preserving unrelated constraints.
        nonlocal constraints
        constraints=[c for c in constraints if not (c.get('property')==prop and c.get('entity_type') in (None,owner))]
        c={'property':prop,'entity_type':owner,'operator':operator,'value':json.dumps(value) if isinstance(value,list) else value}
        constraints.append(c)
        match={'requested':requested or prop,'canonical_binding':deepcopy(c),
               'match_kind':('verified_runtime_' + kind.removeprefix('runtime_')
                             if kind.startswith('runtime_') else kind),
               'registry_version':VERSION,
               'source':(SOURCE if kind=='capability' else
                         'current complete graph categorical inventory' if kind.startswith('runtime_')
                         else 'verified graph categorical values')}
        if kind.startswith('runtime_'):
            match.update(graph_release=release, inventory_sha256=vocabulary.get('inventory_sha256'))
        matches.append(match)
    source_text, trusted_request = _trusted_request(out)
    scope_source_text = scope_intent_text(source_text)
    scope_q = scope_intent_text(q)
    if trusted_request and _unresolved_source_role(scope_source_text, vocabulary):
        issues.append('A requested dataset source is not recorded in the current graph; no broader source query may run.')
    if trusted_request and _unresolved_donor_modifier(scope_source_text, vocabulary):
        issues.append('A requested donor cohort label is not uniquely recorded in the current graph; no broader donor query may run.')
    def stage_mentions(text):
        return [{'match': match, 'number': {'i': '1', 'ii': '2', 'iii': '3'}.get(
                    match.group(1).casefold(), match.group(1)),
                 'negated': (_negated_at(text, match.start(), match.end())
                             or bool(re.match(r'[-–—]\s*negative\b',
                                              text[match.end():match.end()+25], re.I)))}
                for match in re.finditer(
                    r'\bstage\s*(?:[-:]|is\b|equals?\b)?\s*(\d+|iii|ii|i)\b',
                    text, re.I)]
    local_stages = stage_mentions(scope_q)
    requested_stages = stage_mentions(scope_source_text)
    if trusted_request:
        authorized_stage_keys = {(item['number'], item['negated']) for item in requested_stages}
        unauthorized_local_stages = [item for item in local_stages
                                     if (item['number'], item['negated']) not in authorized_stage_keys]
        if unauthorized_local_stages:
            issues.append('A generated T1D stage filter was not authorized by the user request and was removed.')
        authorized_local_stages = [item for item in local_stages
                                   if (item['number'], item['negated']) in authorized_stage_keys]
        # A split check may narrow one of several explicitly requested stages,
        # but generated wording can never introduce a new stage or polarity.
        selected_stages = (authorized_local_stages if authorized_local_stages
                           else requested_stages)
    else:
        selected_stages = local_stages
    distinct_stages = {(item['number'], item['negated']) for item in selected_stages}
    stage = selected_stages[0]['match'] if len(distinct_stages) == 1 else None
    stage_number = selected_stages[0]['number'] if stage else None
    stage_operator = '!=' if stage and selected_stages[0]['negated'] else '='
    if len(distinct_stages) > 1:
        issues.append('Multiple or conflicting T1D stage filters were requested; keep each stage and polarity in a separate check.')
    stage_field_requested = t1d_stage_field_intent(scope_source_text)
    stage_unspecified = (not selected_stages
                         and bool(re.search(r'\bstage\b', scope_source_text, re.I))
                         and not stage_field_requested)
    if stage_unspecified:
        from .query_recovery import stage_recovery
        issues.append('A T1D stage was mentioned without a stage number. Please specify the intended stage; all other filters are kept.')
        out['recovery']=stage_recovery(None,vocabulary,release)
    # A planner-supplied stage absent from the user request is not authority.
    if not selected_stages:
        removed_stage = any(c.get('entity_type') == 'donor' and c.get('property') == 't1d_stage'
                            for c in constraints)
        constraints = [c for c in constraints if not (
            c.get('entity_type') == 'donor' and c.get('property') == 't1d_stage')]
        if removed_stage and not stage_unspecified:
            issues.append('A generated T1D stage filter was not requested and was removed.')
    if stage:
        candidates=[v for v in vocabulary.get('stages',[]) if re.match(r'^Stage '+re.escape(stage_number)+r':',v)]
        if len(candidates)==1:
            bind('t1d_stage', 'donor', candidates[0], operator=stage_operator,
                 kind='runtime_stage', requested=stage.group(0))
        else:
            issues.append('Requested T1D stage cannot be uniquely matched to a recorded stage. No substitute stage was selected.')
            from .query_recovery import stage_recovery
            out['recovery'] = stage_recovery(stage_number, vocabulary, release)
    recorded_sources = [value for value in vocabulary.get('sources', []) if isinstance(value, str)]
    planned_sources = [deepcopy(c) for c in constraints if c.get('property') == 'data_source'
                       and c.get('entity_type') in {None, 'donor', 'Sample_node'}]
    # Source constraints are reconstructed from the authoritative request. An
    # inventory hit proves that a value exists; it does not prove that the user
    # asked for it or that it belongs to the planner-selected owner.
    constraints = [c for c in constraints if not (c.get('property') == 'data_source'
                   and c.get('entity_type') in {None, 'donor', 'Sample_node'})]
    source_mentions = []
    unresolved_source_mentions = []
    for source in recorded_sources:
        for occurrence in re.finditer(r'(?<![\w_])' + re.escape(source) + r'(?![\w_]|[-:.]\d)', scope_source_text, re.I):
            owner = dataset_source_owner(scope_source_text, source, occurrence)
            if owner:
                source_mentions.append((source, owner, _negated_at(
                    scope_source_text, occurrence.start(), occurrence.end())))
            elif _source_scope_candidate(scope_source_text, occurrence):
                unresolved_source_mentions.append(source)
    if unresolved_source_mentions:
        issues.append('A requested dataset source could not be assigned unambiguously to donor or sample records.')
    positive_sources = list(dict.fromkeys((source, owner) for source, owner, negative in source_mentions if not negative))
    negative_sources = list(dict.fromkeys((source, owner) for source, owner, negative in source_mentions if negative))
    conflicting_sources = set(positive_sources) & set(negative_sources)
    if conflicting_sources:
        issues.append('The same dataset source was both required and excluded; clarify the intended source scope.')
        positive_sources = []
        negative_sources = []
    local_sources = [(source, owner) for source, owner in positive_sources
                     if re.search(r'(?<![\w_])' + re.escape(source) + r'(?![\w_]|[-:.]\d)', scope_q, re.I)]
    selected_sources = positive_sources if trusted_request else local_sources
    local_negative_sources = [(source, owner) for source, owner in negative_sources
                              if re.search(r'(?<![\w_])' + re.escape(source) + r'(?![\w_]|[-:.]\d)', scope_q, re.I)]
    selected_negative_sources = negative_sources if trusted_request else local_negative_sources
    def source_value(source, owner):
        owner_values = vocabulary.get('donor_sources' if owner == 'donor' else 'sample_sources')
        if owner_values is None:  # Backward-compatible unowned fixture; runtime inventories provide both.
            owner_values = vocabulary.get('sources')
        candidates = [value for value in owner_values or [] if isinstance(value, str)
                      and value.casefold() == source.casefold()]
        return candidates[0] if isinstance(owner_values, list) and len(candidates) == 1 else None
    positive_owners = [owner for _, owner in positive_sources]
    explicit_dual_owner = (len({source for source, _ in positive_sources}) == 1
                           and {'donor', 'Sample_node'} == set(positive_owners))
    if len(positive_sources) > 1 and (len(set(positive_owners)) != len(positive_owners)
                                      or not explicit_dual_owner):
        selected_sources = []
        issues.append('Multiple dataset sources were requested; keep each source attached to its own explicit check.')
    for source, owner in selected_sources:
        canonical_source = source_value(source, owner)
        if canonical_source is None:
            issues.append('Requested dataset source is not uniquely recorded for its donor/sample owner in the current graph.')
        else:
            bind('data_source', owner, canonical_source, kind='runtime_source', requested=source)
    negative_owners = [owner for _, owner in negative_sources]
    if len(negative_sources) > 1 and len(set(negative_owners)) != len(negative_owners):
        selected_negative_sources = []
        issues.append('Multiple dataset source exclusions were requested; keep each source attached to its own explicit check.')
    selected_positive_owners = {owner for _, owner in selected_sources}
    for source, owner in selected_negative_sources:
        if owner in selected_positive_owners:
            # The explicit positive binding already fixes this single-valued
            # owner field; do not overwrite it with a second inequality.
            continue
        canonical_source = source_value(source, owner)
        if canonical_source is None:
            issues.append('Excluded dataset source is not uniquely recorded for its donor/sample owner in the current graph.')
        else:
            bind('data_source', owner, canonical_source, operator='!=',
                 kind='runtime_source', requested='excluded ' + source)
    if planned_sources and not selected_sources and not selected_negative_sources:
        issues.append('A generated dataset source filter was not authorized by the user request and was removed.')
    # A typed source supplied by a plan must also prove its value and owner from
    # the current runtime inventory; generated prose alone is not authority.
    for constraint in [c for c in constraints if c.get('property') == 'data_source'
                       and c.get('entity_type') in {'donor', 'Sample_node'}]:
        owner = constraint['entity_type']
        values = vocabulary.get('donor_sources' if owner == 'donor' else 'sample_sources')
        if values is None:
            values = vocabulary.get('sources')
        canonical = [value for value in values or [] if isinstance(value, str)
                     and value.casefold() == str(constraint.get('value')).casefold()]
        if (constraint.get('operator', '=') not in {'=', '!=', '<>'} or not isinstance(values, list)
                or len(canonical) != 1):
            issues.append('A dataset source filter cannot be uniquely resolved against the current owner-specific inventory.')
            continue
        constraint['value'] = canonical[0]
        if not any(match.get('canonical_binding') == constraint and match.get('inventory_sha256') == vocabulary.get('inventory_sha256')
                   for match in matches):
            matches.append(_runtime_match(constraint, constraint.get('value'), 'source', vocabulary, release))

    def constraint_values(constraint):
        raw = constraint.get('value')
        if str(constraint.get('operator', '=')).upper() == 'IN':
            try:
                raw = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                return []
            return raw if isinstance(raw, list) else []
        return [raw]
    def values_mentioned(constraint, text):
        values = constraint_values(constraint)
        return bool(values) and all(isinstance(value, str) and re.search(
            r'(?<!\w)' + re.escape(value) + r'(?!\w)', text, re.I) for value in values)

    # Directly named classification fields are resolved against the live
    # owner-specific category inventory. Their operands are not additional
    # disease identities merely because a category happens to contain "T1D".
    for prop in ('diabetes_type', 'derived_diabetes_status'):
        fields = list(re.finditer(_DONOR_REQUEST_FIELD_PATTERNS[prop], source_text, re.I))
        requested = [_explicit_field_constraint('donor', prop, source_text, field)
                     for field in fields]
        requested = [candidate for candidate in requested if candidate]
        if not requested:
            continue
        if len(requested) != 1 or requested[0].get('operator') not in {'=', '!='}:
            issues.append(f'A requested donor.{prop} filter is ambiguous or unsupported; do not execute a broader query.')
            continue
        candidate = requested[0]
        available = _category_values(vocabulary, prop)
        matches_for_value = [value for value in available or []
                             if value.casefold() == str(candidate.get('value')).casefold()]
        if available is None or len(matches_for_value) != 1:
            issues.append(f'A requested donor.{prop} value is not uniquely recorded in the current graph.')
            continue
        bind(prop, 'donor', matches_for_value[0], operator=candidate['operator'],
             kind='runtime_category', requested=deepcopy(candidate))

    disease_scope_text = identity_authorization_text(scope_source_text)
    local_disease_scope_text = identity_authorization_text(scope_q)
    request_mentions = _disease_mentions(disease_scope_text)
    clinical_mentions = [m for m in request_mentions if not _disease_mention_is_stage_label(disease_scope_text, m)]
    if clinical_mentions:
        request_mentions = clinical_mentions
    authoritative_disease_mentions = list(request_mentions)
    local_mentions = _disease_mentions(local_disease_scope_text)
    # A generated stage label is not an added clinical diagnosis, even when
    # the original request abbreviated it to just 'stage 1'.
    local_mentions = [m for m in local_mentions if not _disease_mention_is_stage_label(local_disease_scope_text, m)]
    control_polarity = control_cohort_polarity(scope_source_text)
    control_requested = control_polarity['positive']
    control_excluded = control_polarity['negative']
    if control_requested and control_excluded:
        issues.append('The same ND/healthy control cohort was both required and excluded; clarify the intended cohort.')
        control_requested = False
        control_excluded = False
    derived_status_requested = bool(re.search(
        r'\bderived(?:[_ -]+diabetes)?(?:[_ -]+status|[_ -]+classification)\b',
        scope_source_text, re.I))
    # Existence in the graph inventory is not authorization to add a cohort.
    # Remove planner-added clinical filters that cannot be traced to the raw
    # request; the explicit control/disease logic below reconstructs canonical
    # bindings from the current inventory when they were actually requested.
    kept_constraints = []
    for constraint in constraints:
        owner, prop = constraint.get('entity_type'), constraint.get('property')
        unauthorized = False
        if owner == 'disease' and prop in {'id', 'name'}:
            unauthorized = not request_mentions and not values_mentioned(constraint, source_text)
        elif owner == 'donor' and prop == 'diabetes_type':
            unauthorized = not (re.search(r'\bdiabetes[_ ]type\b', source_text, re.I)
                                and values_mentioned(constraint, source_text))
        elif owner == 'donor' and prop == 'derived_diabetes_status':
            unauthorized = not (derived_status_requested
                                and values_mentioned(constraint, source_text))
        if unauthorized:
            matches.append({'requested': deepcopy(constraint),
                            'match_kind': 'unrequested_generated_filter_removed',
                            'registry_version': VERSION,
                            'source': 'authoritative user request'})
        else:
            kept_constraints.append(constraint)
    constraints = kept_constraints
    if not derived_status_requested:
        constraints = [c for c in constraints if not (
            c.get('entity_type') == 'donor' and c.get('property') == 'derived_diabetes_status')]
    authorized_disease_keys = {(mention['kind'], mention['negated'])
                               for mention in authoritative_disease_mentions}
    unauthorized_local_disease = [mention for mention in local_mentions
                                  if (mention['kind'], mention['negated']) not in authorized_disease_keys]
    if trusted_request and unauthorized_local_disease:
        issues.append('A generated diabetes identity or polarity was not authorized by the user request and was removed.')
    authorized_local_disease = [mention for mention in local_mentions
                                if (mention['kind'], mention['negated']) in authorized_disease_keys]
    comparison = (control_requested and any(not mention['negated'] for mention in request_mentions)
                  and any(re.search(pattern, source_text, re.I) for pattern in
                          schema_module('semantics_modalities')['cohort_comparison_patterns']))
    if comparison:
        # Only the current step may select one side of an explicit comparison.
        local_control_polarity = control_cohort_polarity(scope_q)
        control_requested = local_control_polarity['positive']
        control_excluded = local_control_polarity['negative']
        request_mentions = authorized_local_disease if not unauthorized_local_disease else []
        if control_requested == any(not mention['negated'] for mention in request_mentions):
            issues.append('A control-versus-diabetes comparison must use separate cohort-scoped checks.')
    elif trusted_request and q != source_text:
        positive_kinds = {mention['kind'] for mention in authoritative_disease_mentions
                          if not mention['negated']}
        local_positive_kinds = {mention['kind'] for mention in authorized_local_disease
                                if not mention['negated']}
        if len(positive_kinds) > 1:
            if len(local_positive_kinds) == 1 and not unauthorized_local_disease:
                request_mentions = authorized_local_disease
            else:
                request_mentions = []
                issues.append('Multiple diabetes identities were requested; keep each identity attached to its own explicit check.')
    positive_disease = [mention for mention in request_mentions if not mention['negated']]
    negative_disease = [mention for mention in request_mentions if mention['negated']]
    conflicting_disease_kinds = ({mention['kind'] for mention in positive_disease}
                                 & {mention['kind'] for mention in negative_disease})
    if conflicting_disease_kinds:
        issues.append('The same diabetes identity was both required and excluded; clarify the intended disease scope.')
        positive_disease = [mention for mention in positive_disease
                            if mention['kind'] not in conflicting_disease_kinds]
        negative_disease = [mention for mention in negative_disease
                            if mention['kind'] not in conflicting_disease_kinds]
    positive_kinds = {mention['kind'] for mention in positive_disease}
    if len(positive_kinds) > 1:
        issues.append('Multiple diabetes identities were requested; keep each identity attached to its own explicit check.')
        disease = None
    else:
        disease = next((mention for mention in positive_disease), None)
    diagnosis_text, diagnosis_source = _diagnosis_request_text(out, vocabulary)
    diagnosis_text = scope_intent_text(diagnosis_text)
    explicit_diagnosis = diagnosis_filter_intent(diagnosis_text)
    if diagnosis_source == 'revision_add_clinical_filter':
        explicit_diagnosis = True
        added = [mention for mention in _disease_mentions(diagnosis_text) if not mention['negated']]
        added_kinds = {mention['kind'] for mention in added}
        if len(added_kinds) > 1:
            issues.append('The requested clinical revision names multiple diabetes identities; add one cohort filter at a time.')
            disease = None
        else:
            disease = (added or ([disease] if disease else []))[0] if added or disease else None
    if diagnosis_source == 'ambiguous_clinical_revision':
        issues.append('The requested change to the clinical-disease filter is ambiguous. State whether to add, keep, or remove that filter; recorded stage metadata is kept separately.')
    if diagnosis_source == 'revision_remove_clinical_filter':
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id') or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        matches.append({'requested': out['semantic_request']['revision_instruction'], 'match_kind': 'explicit_clinical_filter_removal',
                        'registry_version': VERSION, 'source': 'original user revision instruction'})
        disease = None
        control_requested = False
    if control_requested and disease:
        issues.append('A single cohort check cannot simultaneously require the recorded control cohort and diagnosed diabetes.')
    if control_requested:
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id')
            or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        category = _control_category(vocabulary)
        if category is None:
            issues.append('The requested ND/healthy donor cohort cannot be uniquely resolved from the current donor.diabetes_type inventory.')
        else:
            bind('diabetes_type', 'donor', category, kind='runtime_category', requested='explicit ND/healthy donor cohort')
    elif control_excluded and not disease:
        constraints=[c for c in constraints if not (
            c.get('entity_type')=='disease' and c.get('property') in ('name','id')
            or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        issues.append('Excluding the ND/healthy control cohort does not uniquely identify a positive donor cohort. Name the intended recorded cohort.')
    elif negative_disease and not disease:
        constraints=[c for c in constraints if not (
            c.get('entity_type')=='disease' and c.get('property') in ('name','id')
            or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        excluded_kinds = {m['kind'] for m in negative_disease}
        categories = _category_values(vocabulary, 'diabetes_type')
        candidates = [value for value in categories or []
                      if {m['kind'] for m in _disease_mentions(re.sub(r'[()]', ' ', value))} == excluded_kinds]
        if stage_field_requested and len(excluded_kinds) == 1 and len(candidates) == 1:
            # The positive recorded-stage population is explicit. Exclude the
            # verified clinical category, never infer a replacement cohort.
            bind('diabetes_type', 'donor', candidates[0], operator='!=',
                 kind='runtime_category', requested=negative_disease[0]['text'])
        else:
            issues.append('Excluding a diabetes type does not uniquely identify a positive donor cohort. Name the intended recorded cohort.')
    separate_t1d_cohort = any(
        mention['kind'] == '1' and not mention['negated']
        and not _disease_mention_is_stage_label(disease_scope_text, mention)
        for mention in request_mentions)
    stage_only_t1d = bool(stage_field_requested and disease
                          and disease['kind'] == '1' and not explicit_diagnosis
                          and not separate_t1d_cohort)
    if stage_only_t1d and diagnosis_source != 'ambiguous_clinical_revision':
        constraints=[c for c in constraints if not (c.get('entity_type')=='disease' and c.get('property') in ('name','id') or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        matches.append({'requested':disease['text'],'canonical_binding':{'entity_type':'donor','property':'t1d_stage'},'match_kind':'recorded_stage_scope','registry_version':VERSION,'source':'verified stage versus disease-category aggregate inventory','explanation':'T1D stage is recorded donor metadata; it does not add a separate diagnosed-diabetes filter.'})
    if disease and not control_requested and not stage_only_t1d and diagnosis_source != 'ambiguous_clinical_revision':
        # Resolve the disease identity from the graph snapshot, never a prompt literal.
        constraints=[c for c in constraints if not (
            c.get('entity_type')=='disease' and c.get('property') in ('name','id')
            or c.get('entity_type')=='donor' and c.get('property')=='diabetes_type')]
        resolved_disease = _disease_category(vocabulary, disease['kind'])
        if resolved_disease is None:
            if 'donor_diseases' not in vocabulary:
                # Legacy callers may omit the aggregate disease inventory. Keep
                # the user's literal for the normal live entity resolver; never
                # substitute an ontology ID from this reusable registry.
                bind('name', 'disease', disease['text'], kind='deferred_entity', requested=disease['text'])
            else:
                issues.append('Requested diabetes disease identity cannot be uniquely resolved from current donor-linked disease nodes.')
        else:
            bind('id','disease',resolved_disease['id'],kind='runtime_entity',requested=disease['text'])
    generic_diseases = [item for item in _generic_donor_disease_mentions(
        scope_source_text, vocabulary)
        if not re.search(r'\b(?:T[12]D(?:M)?|type\s*(?:1|2|I|II)\s+diabetes)\b',
                         item['requested'], re.I)]
    generic_ids = {item['record']['id'] for item in generic_diseases if not item['negated']}
    if any(item['negated'] for item in generic_diseases):
        issues.append('A negative donor-disease relationship filter cannot be represented by the current positive-link template; no broader cohort was substituted.')
    if len(generic_ids) > 1:
        issues.append('Multiple donor diseases were requested; keep each disease attached to its own explicit check.')
    elif len(generic_ids) == 1 and not any(item['negated'] for item in generic_diseases):
        item = next(item for item in generic_diseases if not item['negated'])
        constraints = [constraint for constraint in constraints if not (
            constraint.get('entity_type') == 'disease'
            and constraint.get('property') in {'id', 'name'})]
        bind('id', 'disease', item['record']['id'], kind='runtime_entity',
             requested=item['requested'])
    from .tissue_aliases import matched_tissues
    planned_tissues = [deepcopy(c) for c in constraints
                       if c.get('entity_type') == 'anatomical_structure'
                       and c.get('property') in {'id', 'name'}]
    if trusted_request:
        from .term_clarification import source_role_text
        requested_tissues = matched_tissues(source_role_text(scope_source_text, vocabulary), vocabulary.get('tissues', []))
        local_tissues = matched_tissues(source_role_text(scope_q, vocabulary), vocabulary.get('tissues', []))
        negative_tissues = [tissue for tissue in requested_tissues
                            if 'negative' in _record_mention_polarities(source_text, tissue)]
        if negative_tissues:
            issues.append('Negative sample-tissue filters are not supported by the current same-sample query contract; no excluded tissue was made positive.')
        requested_tissues = [tissue for tissue in requested_tissues
                             if _record_mention_polarities(source_text, tissue) == {'positive'}]
        requested_ids = {tissue.get('id') for tissue in requested_tissues}
        unauthorized_local = [tissue for tissue in local_tissues
                              if tissue.get('id') not in requested_ids]
        if unauthorized_local:
            issues.append('A generated sample-tissue filter was not authorized by the user request and was removed.')
        authorized_local = [tissue for tissue in local_tissues
                            if tissue.get('id') in requested_ids]
        if len(requested_tissues) > 1:
            tissues = []
            issues.append('Multiple sample tissues were named in the user request; keep each tissue attached to its own explicit check.')
        elif len(authorized_local) == 1:
            tissues = authorized_local
        elif len(requested_tissues) == 1 and not unauthorized_local:
            tissues = requested_tissues
        else:
            tissues = []
        if planned_tissues and not requested_tissues:
            issues.append('A generated sample-tissue filter was not requested and was removed.')
        if _unresolved_tissue_role(scope_source_text, vocabulary, requested_tissues):
            issues.append('A requested sample tissue is not uniquely recorded in the current graph; no unrestricted sample query may run.')
    else:
        tissues = matched_tissues(scope_q, vocabulary.get('tissues', []), constraints=constraints)
        negative_tissues = [tissue for tissue in tissues
                            if 'negative' in _record_mention_polarities(q, tissue)]
        if negative_tissues:
            issues.append('Negative sample-tissue filters are not supported by the current same-sample query contract; no excluded tissue was made positive.')
            tissues = [tissue for tissue in tissues
                       if _record_mention_polarities(q, tissue) == {'positive'}]
    # Typed planner values and current-graph existence do not authorize scope.
    # Reconstruct the tissue predicate only from the immutable raw request (or,
    # for legacy untrusted callers, the step wording) before entity resolution.
    if trusted_request or tissues:
        constraints = [c for c in constraints if not (
            c.get('entity_type') == 'anatomical_structure'
            and c.get('property') in {'id', 'name'})]
    if len(tissues)==1:
        # A display name copied into a sample code is a redundant mis-owned
        # representation only when it denotes this SAME request-verified tissue.
        tissue = tissues[0]
        duplicates = [c for c in constraints if (
            c.get('entity_type') == 'Sample_node' and c.get('property') == 'anatomical_structure'
            and c.get('operator', '=') == '=' and c.get('value') == tissue.get('name'))]
        constraints = [c for c in constraints if c not in duplicates]
        for constraint in duplicates:
            matches.append({'requested': deepcopy(constraint), 'match_kind': 'duplicate_tissue_owner_reconciled',
                'canonical_binding': {'entity_type': 'anatomical_structure', 'property': 'id', 'operator': '=', 'value': tissue['id']},
                'registry_version': VERSION, 'source': 'verified requested tissue identity and same-sample relationship',
                'explanation': 'The tissue display name is not a sample tissue code; the verified relationship retains the requested tissue restriction.'})
        bind('id','anatomical_structure',tissues[0]['id'],kind=tissues[0].get('match_kind','exact'),requested=tissues[0].get('requested_alias',tissues[0]['name']))
    elif len(tissues)>1:
        issues.append('Multiple sample tissues were named. Separate the tissue checks so each sample remains attached to its intended tissue.')
    planned_assay = [c for c in constraints
                     if c.get('property') == 'data_modality'
                     or c.get('entity_type') == 'data_modality']
    old_assay = list(planned_assay)
    available = vocabulary.get('modalities', [])
    request = out.get('semantic_request') or {}
    assay_q = scope_q
    if trusted_request:
        raw_positive_assays, raw_negative_assays, raw_assay_intent = _assay_intent_values(
            scope_source_text, available)
        if _unresolved_assay_role(scope_source_text, raw_assay_intent):
            issues.append('A requested sample assay is not uniquely recorded in the current graph; no unrestricted sample query may run.')
        local_positive_assays, local_negative_assays, local_assay_intent = _assay_intent_values(
            scope_q, available)
        unauthorized_local_assays = ((local_positive_assays - raw_positive_assays)
                                     | (local_negative_assays - raw_negative_assays))
        if unauthorized_local_assays or (local_assay_intent and not raw_assay_intent):
            issues.append('A generated sample-assay filter was not authorized by the user request and was removed.')
            assay_q = scope_source_text
        elif not local_assay_intent:
            # Inherit one raw capability envelope rather than accepting a typed
            # planner value whose generated prose omitted the user's assay.
            assay_q = scope_source_text
        authorized_assay = []
        for constraint in old_assay:
            values = _assay_values(constraint)
            operator = str(constraint.get('operator', '=')).upper()
            mapped = {_canonical_assay(value, available) for value in values or []}
            allowed = raw_negative_assays if operator in {'!=', '<>', 'NOT IN'} else raw_positive_assays
            if values and mapped.issubset(allowed):
                authorized_assay.append(constraint)
            else:
                issues.append('A generated sample-assay predicate was not traceable to the immutable user request and was removed.')
        old_assay = authorized_assay
        # Direct exclusions may be synthesized only when the deterministic
        # draft is the immutable request itself. A generated split must carry
        # an already-typed, raw-authorized exclusion rather than gaining one
        # from prose alone.
        already_excluded = {value for c in old_assay if str(c.get('operator', '=')).upper() in {'!=', '<>', 'NOT IN'}
                            for value in (_assay_values(c) or [])}
        synthesized_exclusions = raw_negative_assays if scope_q == scope_source_text else set()
        for value in synthesized_exclusions:
            if value not in already_excluded:
                binding = {'entity_type': 'Sample_node', 'property': 'data_modality', 'operator': '!=', 'value': value}
                old_assay.append(binding)
                matches.append({'requested': source_text, 'canonical_binding': deepcopy(binding),
                    'match_kind': 'raw_request_excluded_assay_literal', 'registry_version': VERSION,
                    'source': 'verified graph categorical values and original user request'})
    negative_assay, positive_assay, unsupported_assay = [], [], []
    for original in old_assay:
        c = deepcopy(original)
        owner = c.get('entity_type')
        operator = str(c.get('operator', '=')).upper()
        values = _assay_values(c)
        if owner not in (None, 'Sample_node', 'data_modality') or c.get('owner_kind') == 'relationship':
            unsupported_assay.append(c)
            issues.append('Unsupported assay property owner; bind data_modality to the linked Sample_node.')
            continue
        if operator not in {'=', 'IN', '!=', '<>', 'NOT IN'} or values is None:
            unsupported_assay.append(c)
            issues.append('Unsupported assay operator or value; preserve its original meaning instead of converting it to a positive assay match.')
            continue
        mapped = [_canonical_assay(value, available) for value in values]
        c.update(entity_type='Sample_node', property='data_modality')
        c.pop('relationship_type', None)
        c.pop('owner_kind', None)
        c['value'] = (json.dumps(mapped) if isinstance(original.get('value'), str) else mapped) if operator in {'IN', 'NOT IN'} else mapped[0]
        matches.append({'requested': deepcopy(original), 'canonical_binding': deepcopy(c), 'match_kind': 'verified_assay_alias',
                        'registry_version': VERSION, 'source': 'verified graph categorical values'})
        if any(value not in available for value in mapped):
            issues.append('Unresolved assay value; no fuzzy substitution or operator change was applied.')
        (negative_assay if operator in {'!=', '<>', 'NOT IN'} else positive_assay).append(c)
        if operator == 'NOT IN':
            issues.append('NOT IN assay predicates are unsupported by the current typed query contract; keep this exclusion explicit rather than substituting positive evidence.')
    constraints = [c for c in constraints if c not in planned_assay] + negative_assay + unsupported_assay
    old_assay = positive_assay
    positive_q = _without_negated_assays(assay_q, available)
    assay_text=' '.join([positive_q]+[str(c.get('value','')) for c in old_assay])
    rna=bool(re.search(r'(?<![a-z])(?:sc|sn)?RNA[\s-]*(?:seq)?|transcriptom',assay_text,re.I))
    atac=bool(re.search(r'ATAC|chromatin accessibility',assay_text,re.I))
    paired=bool(re.search(r'multiom|paired|joint',positive_q,re.I))
    exact=bool(re.search(r'\bstandalone\b|\bexact(?:ly)?\b.{0,20}(?:RNA|ATAC)|(?:RNA[\s-]*seq|ATAC[\s-]*seq)\s+only\b|\bonly\s+(?:sc|sn)?(?:RNA|ATAC)|exclude\s+multiom',assay_q,re.I))
    # A step explicitly naming an assay label is an exact label lookup. A
    # separate capability step may include multiome; this one cannot broaden
    # merely because scRNA-seq contains the letters RNA.
    label_lookup = bool(re.search(r'\b(?:samples?|records?)\s+(?:explicitly\s+)?label(?:ed|led)\b|\b(?:assay|modality)\s+(?:label|value)\s*(?:=|is|of|:)\s*', assay_q, re.I)) or _explicit_modality_label(positive_q, available)
    named_assays = _mentioned_assays(positive_q, available)
    capability_inclusion = bool(re.search(r'\b(?:include|including|also|or)\b[^.!?;\n]{0,120}\b(?:(?:sn)?multiom\w*|(?:RNA|ATAC)\s+components?)', positive_q, re.I))
    if named_assays and (label_lookup or not paired) and not capability_inclusion:
        exact = True
    paired = paired and not exact and not bool(re.search(r'\b(?:include|including|also|or)\b.{0,60}multiom',assay_q,re.I))
    if not unsupported_assay and (old_assay or named_assays or rna or atac or paired):
        if paired:
            groups=[['snMultiomics']]
        elif exact:
            exact_sets = [_assay_values(c) for c in old_assay] if old_assay else [named_assays or ['scATAC-seq' if atac and not rna else 'scRNA-seq']]
            groups = [[_canonical_assay(value, available) for value in values] for values in exact_sets]
        else:
            if rna:groups.append(['scRNA-seq','snMultiomics'])
            if atac:groups.append(['scATAC-seq','snMultiomics'])
            if not groups and old_assay:
                groups = [[_canonical_assay(value, available) for value in _assay_values(c)] for c in old_assay]
            if not groups and named_assays:
                groups = [named_assays]
        covered = {value for group in groups for value in group}
        for c in old_assay:
            extra = [value for value in _assay_values(c) if value not in covered]
            if extra:
                groups.append(extra)
                covered.update(extra)
        excluded_values = {value for c in negative_assay for value in (_assay_values(c) or [])}
        groups = [[value for value in group if value not in excluded_values] for group in groups]
        if any(not group for group in groups):
            issues.append('The requested positive assay and assay exclusion conflict; do not replace this with an unrestricted sample query.')
            groups = [group for group in groups if group]
        for values in groups:
            for i, value in enumerate(values):
                candidates=[m for m in available if m.casefold()==value.casefold()]
                if len(candidates)==1: values[i]=candidates[0]
            unknown=[v for v in values if v not in available]
            if unknown:
                suggestions=get_close_matches(unknown[0],available,n=3,cutoff=.45)
                issues.append('Unresolved assay '+unknown[0]+'. Suggestions: '+', '.join(suggestions)+'. No fuzzy substitution was applied.')
            c={'property':'data_modality','entity_type':'Sample_node','operator':'IN' if len(values)>1 else '=','value':json.dumps(values) if len(values)>1 else values[0]}
            constraints.append(c)
            matches.append({'requested':'RNA/ATAC assay intent','canonical_binding':c,'match_kind':'capability' if not exact and (rna or atac or paired) and (len(values)>1 or paired) else 'alias','registry_version':VERSION,'source':SOURCE})
    if unsupported_assay:
        constraints.extend(positive_assay)
    if positive_q != assay_q and not negative_assay and not groups:
        issues.append('An assay exclusion was requested without a typed negative modality predicate; do not execute an unrestricted sample query.')
    expanded = bool(not exact and (rna or atac or paired) and groups and any(len(group)>1 or paired for group in groups))
    assay_sources = vocabulary.get('assay_donor_sources', {}).get('snMultiomics', [])
    verified_scope = bool(assay_sources) and set(assay_sources) == {'HPAP'}
    if expanded and verified_scope:
        matches.append({'requested': 'multiome capability scope', 'match_kind':'verified_dataset_scope', 'source':SOURCE, 'registry_version':VERSION, 'explanation':'All donor-linked snMultiomics records in this release are from HPAP; other requested assay records remain unrestricted by cohort.'})
    if expanded and not verified_scope and not re.search(r'\bHPAP\b',scope_source_text if trusted_request else scope_q,re.I):
        issues.append('Assay capability mapping is verified for HPAP; specify HPAP or an exact recorded assay before expanding the search.')
    from .donor_categories import resolve_categories, CATEGORICAL_FIELDS
    authorized_constraints = []
    for constraint in constraints:
        categorical = (constraint.get('entity_type') in {None, 'donor'}
                       and constraint.get('property') in CATEGORICAL_FIELDS)
        already_proved = any(match.get('canonical_binding') == constraint
                             and str(match.get('match_kind', '')).startswith('verified_runtime_')
                             for match in matches)
        donor_filter = (constraint.get('entity_type') == 'donor'
                        or constraint.get('entity_type') is None
                        and constraint.get('property') in PROPERTIES['donor'])
        request_authorized = _raw_constraint_authorized(constraint, source_text)
        if trusted_request and donor_filter and not already_proved and not request_authorized:
            matches.append({'requested': deepcopy(constraint),
                            'match_kind': 'unrequested_generated_filter_removed',
                            'registry_version': VERSION,
                            'source': 'authoritative user request'})
            issues.append('A generated donor filter was not authorized by the immutable user request and was removed.')
            continue
        if trusted_request and donor_filter and request_authorized:
            matches.append({'requested': deepcopy(constraint),
                            'canonical_binding': deepcopy(constraint),
                            'match_kind': 'verified_request_filter',
                            'registry_version': VERSION,
                            'source': 'immutable user request'})
        elif categorical and not already_proved and not values_mentioned(constraint, source_text):
            # Legacy callers without an immutable request retain the earlier
            # value-presence guard, but do not gain a reusable authorization.
            matches.append({'requested': deepcopy(constraint),
                            'match_kind': 'unrequested_generated_filter_removed',
                            'registry_version': VERSION,
                            'source': 'step question'})
            continue
        authorized_constraints.append(constraint)
    constraints = authorized_constraints
    constraints, categorical_matches = resolve_categories(constraints, vocabulary, PROPERTIES, release)
    for match in categorical_matches:
        if (trusted_request
                and match.get('requested') != match.get('canonical_binding')
                and any(
                existing.get('match_kind') == 'verified_request_filter'
                and existing.get('canonical_binding') == match.get('requested')
                for existing in matches)):
            matches.append({'requested': deepcopy(match.get('requested')),
                            'canonical_binding': deepcopy(match.get('canonical_binding')),
                            'match_kind': 'verified_request_filter',
                            'registry_version': VERSION,
                            'source': 'immutable user request'})
        if not any(existing.get('canonical_binding') == match.get('canonical_binding')
                   and existing.get('inventory_sha256') == match.get('inventory_sha256')
                   for existing in matches):
            matches.append(match)
    if trusted_request:
        issues.extend(_raw_donor_filter_completeness_issues(
            source_text, constraints, vocabulary))
        issues.extend(_raw_sample_filter_completeness_issues(
            source_text, constraints))
    for constraint in [c for c in constraints if c.get('entity_type') == 'Sample_node'
                       and c.get('property') == 'data_modality']:
        values = _assay_values(constraint)
        if (constraint.get('operator', '=') in {'=', 'IN', '!=', '<>'}
                and values and all(value in available for value in values)):
            if not any(match.get('canonical_binding') == constraint
                       and match.get('inventory_sha256') == vocabulary.get('inventory_sha256')
                       for match in matches):
                matches.append(_runtime_match(constraint, deepcopy(constraint), 'assay', vocabulary, release))
        else:
            issues.append('An assay filter cannot be verified against the current graph modality inventory.')
    controlled = set(CATEGORICAL_FIELDS) | {'t1d_stage'}
    for constraint in [c for c in constraints if c.get('entity_type') == 'donor'
                       and c.get('property') in controlled]:
        if not any(match.get('canonical_binding') == constraint
                   and match.get('inventory_sha256') == vocabulary.get('inventory_sha256')
                   for match in matches):
            issues.append('A donor cohort filter cannot be uniquely verified against the current graph categorical inventory.')
    if trusted_request:
        # Independent authorization proof: current-graph existence/identity is
        # necessary but never sufficient. Every executable predicate in this
        # donor/sample semantic path must also trace to the immutable request or
        # to a reviewed capability/split derivation of that request.
        authorization_kinds = {
            'verified_runtime_source', 'verified_runtime_stage',
            'verified_runtime_category', 'verified_runtime_entity',
            'deferred_entity', 'exact', 'verified_alias', 'dataset_proxy',
            'verified_typed_id', 'typed_canonical_description',
            'verified_assay_alias', 'raw_request_excluded_assay_literal',
            'capability', 'alias', 'verified_request_filter',
        }
        kept, request_bindings = [], []
        request_sha256 = hashlib.sha256(source_text.encode()).hexdigest()
        for index, constraint in enumerate(constraints):
            proofs = [match for match in matches
                      if match.get('canonical_binding') == constraint
                      and match.get('match_kind') in authorization_kinds]
            if not proofs and _raw_constraint_authorized(constraint, source_text):
                proof = {'requested': deepcopy(constraint),
                         'canonical_binding': deepcopy(constraint),
                         'match_kind': 'verified_request_filter',
                         'registry_version': VERSION,
                         'source': 'immutable user request'}
                matches.append(proof)
                proofs = [proof]
            if not proofs:
                issues.append('A query filter lacks one unambiguous immutable-request authorization.')
                # Preserve the proposed filter for review, but do not issue an
                # authorization binding. Semantic issues and the execution gate
                # therefore block it without silently broadening the request.
                kept.append(constraint)
                continue
            proof = sorted(proofs, key=lambda match: (
                match.get('match_kind') != 'verified_request_filter',
                str(match.get('match_kind')), str(match.get('requested'))))[0]
            kept.append(constraint)
            request_bindings.append({
                'constraint_index': len(kept) - 1,
                'canonical_binding': deepcopy(constraint),
                'authorization_kind': proof['match_kind'],
                'source': 'immutable_user_request',
                'request_sha256': request_sha256,
                'graph_release': release,
            })
        constraints = kept
        out['request_filter_bindings'] = request_bindings
    disease_link_requested = bool(re.search(
        r'\b(?:linked?|associated)\s+diseases?\b|'
        r'\bdisease\s+(?:links?|records?|relationships?|evidence)\b|'
        r'\bdiagnos(?:is|es|tic)\s+(?:links?|records?|evidence)\b',
        source_text, re.I))
    if ('HAS_SAMPLE' in (out.get('relation_types') or [])
            and any(c.get('entity_type') == 'donor' for c in constraints)
            and not any(c.get('entity_type') == 'disease' for c in constraints)
            and not disease_link_requested):
        out['relation_types'] = [kind for kind in out.get('relation_types', []) if kind != 'HAS_DONOR']
        matches.append({'requested': 'donor sample cohort structure',
                        'match_kind': 'runtime_relation_scope_normalization',
                        'registry_version': VERSION,
                        'source': 'resolved donor predicates and requested same-sample witness',
                        'canonical_relations': deepcopy(out['relation_types'])})
    out['constraints']=constraints
    out['resolved_constraints']=matches
    donor_required = (any(c.get('entity_type') == 'donor' for c in constraints)
                      or bool(re.search(r'\bdonors?\b', q, re.I))
                      or (any(c.get('entity_type') == 'disease' for c in constraints)
                          and donor_intent(out)))
    out['semantic_registry']={'version':VERSION,'sha256':DIGEST,'graph_release':release,'modality_links_verified':vocabulary.get('modality_links_verified',False),
        'inventory_sha256': vocabulary.get('inventory_sha256'),
        'donor_required': donor_required, 'diagnosis_intent': {'explicit': explicit_diagnosis, 'source': diagnosis_source},
        'clinical_intent': {'control_cohort': control_requested,
                            'excluded_control_cohort': control_excluded,
                            'derived_status_explicit': derived_status_requested,
                            'positive_diabetes_types': [mention['kind'] for mention in positive_disease],
                            'excluded_diabetes_types': [mention['kind'] for mention in negative_disease]}}
    out['semantic_issues']=issues
    out['sample_requirements']={'modality_groups':groups,'paired':paired,'separate_bindings':len(groups)>1,
        'source':SOURCE,'file_availability':'not_verified','excluded_modality_constraints':deepcopy(negative_assay),'capability_scope_verified': bool(expanded and (verified_scope or re.search(r'\bHPAP\b',q,re.I)))}
    if negative_assay and not groups:
        out['semantic_summary'] = 'Match indexed samples with the requested assay exclusions; keep the original operators and donor/tissue filters. Assay exclusions do not exclude donors who also have other assays.'
    if groups:
        out['semantic_summary'] = ('Match only the explicitly requested assay label; do not include related multiome assays.' if exact else
            'Include documented RNA/ATAC components of the recorded assays; show the original assay labels and distinguish indexed samples from downloadable files.' if expanded else
            'Match the recorded assay labels with the requested filters; indexed samples do not verify downloadable files.')
    if groups:
        recorded = [v for group in groups for v in group]
        capability = any(v in ('scRNA-seq','scATAC-seq','snMultiomics') for v in recorded)
        if not capability:
            out['semantic_summary'] = 'Match recorded '+', '.join(recorded)+' assay metadata; indexed samples do not verify file availability or measured functional outcomes.'
            for match in out['resolved_constraints']:
                if match.get('requested') == 'RNA/ATAC assay intent': match['requested'] = 'recorded assay intent'
    if out.get('deferred_sample_scope'):
        # These predicates are checked on the downstream sample task. They must
        # not turn a donor-ID retrieval into an extra mandatory sample join.
        kept_indices = [i for i, c in enumerate(out['constraints'])
                        if c.get('entity_type') not in {'Sample_node', 'data_modality', 'anatomical_structure'}]
        remap = {old: new for new, old in enumerate(kept_indices)}
        out['constraints'] = [out['constraints'][i] for i in kept_indices]
        out['request_filter_bindings'] = [{**b, 'constraint_index': remap[b['constraint_index']]}
            for b in out.get('request_filter_bindings', []) if b['constraint_index'] in remap]
        out['resolved_constraints'] = [m for m in out['resolved_constraints']
            if not m.get('canonical_binding') or m['canonical_binding'] in out['constraints']]
        out['sample_requirements'] = {}
    from .donor_query_guard import normalize_diagnosis
    return normalize_diagnosis(out, vocabulary)


def _positive_request_literal(text, literal):
    if not isinstance(literal, (str, int, float)) or str(literal) == '':
        return False
    for mention in _value_mentions(text, literal):
        prefix = text[max(0, mention.start() - 70):mention.start()]
        if (_negated_at(text, mention.start(), mention.end())
                or re.search(r'\b(?:unrelated|example|examples|previous|previously)\b'
                             r'[^.!?;]{0,35}$', prefix, re.I)):
            continue
        return True
    return False


def _positive_request_phrase(text, phrase):
    """Match one grounded phrase with conservative surface normalization.

    Separators may vary and the final token may take a simple trailing plural
    ``s``.  No stemming, synonym expansion or internal-token inflection is
    allowed, and the matched raw surface still passes the ordinary polarity
    check.  This admits ``ductal cells`` for canonical ``ductal cell`` without
    turning a substring or a negated mention into request authority.
    """
    if _positive_request_literal(text, phrase):
        return True
    words = re.findall(r'[A-Za-z0-9]+', str(phrase))
    if not words:
        return False
    final = re.escape(words[-1])
    if len(words[-1]) >= 3 and not words[-1].casefold().endswith('s'):
        final += 's?'
    pattern = (r'(?<!\w)' + r'[\s_-]+'.join(
        [*(re.escape(word) for word in words[:-1]), final]) + r'(?!\w)')
    for mention in re.finditer(pattern, text, re.I):
        if not _negated_at(text, mention.start(), mention.end()):
            return True
    return False


def _positive_tissue_identity(question, identifier, name=None):
    literals = [name]
    if identifier == 'UBERON_0001264':
        literals.extend(['pancreas', 'pancreatic'])
    text = identity_authorization_text(question)
    return (_positive_request_literal(text, identifier)
            or any(_positive_request_phrase(text, value)
                   for value in literals if value))


def _verified_requested_scope_derivation(constraint, out, question):
    """Validate reviewed compiler outputs, never arbitrary planner metadata."""
    try:
        from .planning_requirements import VERSION as requirements_version
        from .release_schema import DIGEST as schema_digest, REGISTRY as schema
    except ImportError:
        return None
    records = [record for record in out.get('requested_scope_compilation') or []
               if isinstance(record, dict)
               and record.get('version') == requirements_version
               and record.get('graph_release') == out.get('graph_version') == schema.get('release')
               and record.get('schema_digest') == schema_digest
               and record.get('raw_question') == question
               and record.get('canonical_binding') == constraint]
    if len(records) != 1:
        return None
    proof = records[0].get('proof') or {}
    kind = proof.get('kind')
    if kind == 'explicit_unique_qtl_tissue':
        tissue = proof.get('resolved_tissue') or {}
        if (constraint.get('owner_kind') == 'relationship'
                and constraint.get('relationship_type') == 'PART_OF_QTL_SIGNAL'
                and constraint.get('property') == 'tissue_id'
                and constraint.get('operator', '=') == '='
                and constraint.get('value') == tissue.get('id')
                and tissue.get('entity_type') == 'anatomical_structure'
                and proof.get('requested_terms')
                and all(_positive_request_phrase(identity_authorization_text(question), value)
                        for value in proof.get('requested_terms'))):
            return 'verified_request_qtl_tissue_compilation'
    if kind == 'explicit_unique_disease':
        disease = proof.get('resolved_disease') or {}
        if (constraint.get('entity_type') == 'disease'
                and constraint.get('property') == 'id'
                and constraint.get('operator', '=') == '='
                and constraint.get('value') == disease.get('id')
                and disease.get('entity_type') == 'disease'
                and proof.get('requested_terms')
                and all(_positive_request_phrase(identity_authorization_text(question), value)
                        for value in proof.get('requested_terms'))):
            return 'verified_request_disease_compilation'
    if kind == 'explicit_recorded_pathway_source':
        recorded = proof.get('recorded_values') or []
        values = _constraint_values(constraint)
        resolved_gene_ids = {entry.get('id') for entry in out.get('resolved_entities') or []
                             if entry.get('state') == 'resolved'
                             and entry.get('entity_type') == 'Gene'
                             and entry.get('graph_version') == out.get('graph_version')}
        if (constraint.get('owner_kind') == 'relationship'
                and constraint.get('relationship_type') == 'FUNCTION_ANNOTATION'
                and constraint.get('property') == 'data_source'
                and str(constraint.get('operator', '=')).upper() in {'=', 'IN'}
                and values and sorted(values) == sorted(recorded)
                and resolved_gene_ids.intersection(proof.get('gene_ids') or [])
                and all(_positive_request_literal(identity_authorization_text(question), value)
                        for value in values)):
            return 'verified_request_pathway_source_compilation'
    if kind == 'explicit_grounded_go_domain':
        values = _constraint_values(constraint)
        if (constraint.get('entity_type') == 'GO_term'
                and constraint.get('property') == 'go_domain'
                and str(constraint.get('operator', '=')).upper() in {'=', 'IN'}
                and values and sorted(values) == sorted(proof.get('recorded_values') or [])
                and all(_positive_request_phrase(
                    identity_authorization_text(question), requested)
                    for requested in proof.get('requested_terms') or [])):
            return 'verified_request_go_domain_compilation'
    return None


def _verified_qtl_schema_derivation(constraint, index, out, question):
    try:
        from .release_schema import REGISTRY as schema
    except ImportError:
        return None
    matches = [binding for binding in out.get('schema_bindings') or []
               if isinstance(binding, dict)
               and binding.get('version') == 'qtl-tissue-owner-v2'
               and binding.get('graph_version') == out.get('graph_version') == schema.get('release')
               and binding.get('canonical_binding') == constraint]
    if len(matches) != 1:
        return None
    binding = matches[0]
    kind = binding.get('kind')
    if kind == 'verified_qtl_tissue_property':
        tissue = binding.get('resolved_tissue') or {}
        original = binding.get('requested') or {}
        entity = next((entry for entry in out.get('resolved_entities') or []
                       if entry.get('constraint_index') == index), None)
        entity_verified = (isinstance(entity, dict)
                           and entity.get('state') == 'literal_predicate'
                           and entity.get('graph_version') == out.get('graph_version')
                           and entity.get('property_binding') == binding
                           and entity.get('requested') == constraint)
        replay_verified = (entity is None
                           and binding.get('source') ==
                           'release-verified anatomical identity and QTL tissue_id property ownership'
                           and original.get('entity_type') == 'anatomical_structure'
                           and original.get('property') in {'id', 'name'}
                           and original.get('operator', '=') == '=')
        if (constraint.get('owner_kind') == 'relationship'
                and constraint.get('relationship_type') == 'PART_OF_QTL_SIGNAL'
                and constraint.get('property') == 'tissue_id'
                and constraint.get('operator', '=') == '='
                and constraint.get('value') == tissue.get('id')
                and 'anatomical_structure' in (tissue.get('labels') or [])
                and (entity_verified or replay_verified)
                and (_positive_request_phrase(identity_authorization_text(question),
                                               original.get('value'))
                     or _positive_tissue_identity(question, tissue.get('id'),
                                                  tissue.get('name')))):
            return 'verified_request_qtl_tissue_resolution'
    if kind == 'verified_qtl_tissue_category':
        original = binding.get('requested') or {}
        if (constraint.get('owner_kind') == 'relationship'
                and constraint.get('relationship_type') == 'PART_OF_QTL_SIGNAL'
                and constraint.get('property') in {'tissue_id', 'tissue_name'}
                and constraint.get('operator', '=') == '='
                and original.get('property') == 'tissue'
                and original.get('operator', '=') == '='
                and (_raw_constraint_authorized(original, question, out.get('relation_types'))
                     or _positive_tissue_identity(question, constraint.get('value'),
                                                  constraint.get('value')))):
            return 'verified_request_qtl_tissue_category'
    return None


def _verified_anatomy_scope_derivation(constraint, out, question):
    scope = out.get('anatomy_scope') or {}
    membership = scope.get('membership') or {}
    resolved_root = scope.get('resolved_root') or {}
    try:
        from .anatomy_scope import DIGEST as scope_digest, VERSION as scope_version
        from .anatomy_membership import DIGEST as membership_digest
    except ImportError:
        return None
    root = membership.get('requested_root') or {}
    if (scope.get('version') != scope_version or scope.get('digest') != scope_digest
            or scope.get('canonical_cell_binding') != constraint
            or membership.get('state') != 'resolved'
            or membership.get('graph_release') != out.get('graph_version')
            or membership.get('registry_digest') != membership_digest
            or membership.get('match_kind') != 'verified_anatomical_hierarchy_membership'
            or constraint.get('entity_type') != 'anatomical_structure'
            or constraint.get('property') != 'id'
            or constraint.get('operator') != 'IN'
            or constraint.get('value') != membership.get('cell_ids')
            or membership.get('cell_count') != len(membership.get('cell_ids') or [])
            or resolved_root.get('id') != root.get('id')
            or resolved_root.get('entity_type') != 'anatomical_structure'
            or scope.get('request_sha256') != hashlib.sha256(question.encode()).hexdigest()
            or not scope.get('request_terms')
            or not all(_positive_request_phrase(identity_authorization_text(question), term)
                       for term in scope.get('request_terms'))):
        return None
    return 'verified_request_anatomy_scope'


def _verified_coloc_tissue_derivation(constraint, out, question):
    scope = out.get('coloc_tissue_scope') or {}
    try:
        from .coloc_tissue_scope import (MAPPING_DIGEST, RELEASE as coloc_release,
                                         SCOPE, TISSUES, VERSION as scope_version)
    except ImportError:
        return None
    if (scope.get('version') != scope_version
            or scope.get('mapping_sha256') != MAPPING_DIGEST
            or scope.get('graph_release') != out.get('graph_version')
            or scope.get('graph_release') != coloc_release
            or scope.get('request_sha256') != hashlib.sha256(question.encode()).hexdigest()
            or constraint.get('owner_kind') != 'relationship'
            or constraint.get('relationship_type') != 'SIGNAL_COLOC_WITH'
            or constraint.get('property') != 'coloc_dataset'
            or constraint.get('operator') != 'IN'
            or constraint.get('value') != scope.get('datasets')):
        return None
    matches = list(SCOPE.finditer(question))
    if len(matches) != 1 or matches[0][2] and matches[0][2].lower() == 'and':
        return None
    wanted = {TISSUES[word.lower()][1] for word in (matches[0][1], matches[0][3]) if word}
    if (wanted != set(scope.get('tissue_ids') or [])
            or any(_negated_at(question, match.start(), match.end()) for match in matches)):
        return None
    return 'verified_request_coloc_tissue_scope'


def attach_request_authorizations(step):
    """Attach one immutable-request proof to every final executable filter.

    This runs after entity resolution as well as for non-donor/sample steps.
    A live entity hit proves existence, not that a planner-added filter was
    requested.  Structural dependency parameters are handled separately and
    never appear in this constraint list.
    """
    out = deepcopy(step)
    constraints = out.get('constraints') or []
    if not constraints:
        out['request_filter_bindings'] = []
        return out
    request = out.get('semantic_request') or {}
    question = request.get('question')
    issues = list(out.get('semantic_issues') or [])
    if request.get('source') != 'user_request' or not isinstance(question, str) or not question:
        issues.append('Executable filters lack an immutable original user request; do not run this step.')
        out['semantic_issues'] = issues
        out['request_filter_bindings'] = []
        return out
    digest = hashlib.sha256(question.encode()).hexdigest()
    scoped_question = identity_authorization_text(question)
    existing = out.get('request_filter_bindings') or []
    entities = out.get('resolved_entities') or []
    compilations = out.get('constraint_compilation') or []
    from .genomic_scope import is_verified_region_constraint
    bindings = []
    for index, constraint in enumerate(constraints):
        current = [binding for binding in existing
                   if isinstance(binding, dict)
                   and binding.get('constraint_index') == index
                   and binding.get('canonical_binding') == constraint
                   and binding.get('source') == 'immutable_user_request'
                   and binding.get('request_sha256') == digest
                   and binding.get('graph_release') == out.get('graph_version')]
        if len(current) == 1:
            bindings.append(deepcopy(current[0]))
            continue
        kind = None
        if _raw_constraint_authorized(constraint, question, out.get('relation_types')):
            kind = 'verified_request_filter'
        if kind is None and is_verified_region_constraint(constraint, out):
            kind = 'verified_request_region'
        if kind is None:
            kind = _verified_requested_scope_derivation(constraint, out, question)
        if kind is None:
            kind = _verified_qtl_schema_derivation(constraint, index, out, question)
        if kind is None:
            kind = _verified_anatomy_scope_derivation(constraint, out, question)
        if kind is None:
            kind = _verified_coloc_tissue_derivation(constraint, out, question)
        entity = next((entry for entry in entities
                       if isinstance(entry, dict)
                       and entry.get('constraint_index') == index
                       and entry.get('state') == 'resolved'
                       and entry.get('graph_version') == out.get('graph_version')), None)
        if kind is None and entity is not None and _node_identity_constraint_for_request(constraint):
            candidates = [constraint, entity.get('requested'), entity.get('original_requested')]
            candidates.extend(change.get('requested') for change in compilations
                              if isinstance(change, dict)
                              and change.get('constraint_index') == index)
            exact_literals, names = [], []
            for candidate in candidates:
                if isinstance(candidate, dict):
                    raw = candidate.get('value')
                    if isinstance(raw, str):
                        (names if candidate.get('property') == 'name'
                         else exact_literals).append(raw)
            if isinstance(entity.get('id'), str):
                exact_literals.append(entity['id'])
            if isinstance(entity.get('name'), str):
                names.append(entity['name'])
            if (any(_positive_request_literal(scoped_question, literal)
                    for literal in exact_literals)
                    or any(_positive_request_phrase(scoped_question, name)
                           for name in names)):
                kind = 'verified_request_entity'
        if kind is None:
            issues.append('A query filter lacks one unambiguous immutable-request authorization.')
            continue
        bindings.append({
            'constraint_index': index,
            'canonical_binding': deepcopy(constraint),
            'authorization_kind': kind,
            'source': 'immutable_user_request',
            'request_sha256': digest,
            'graph_release': out.get('graph_version'),
        })
    out['request_filter_bindings'] = bindings
    out['semantic_issues'] = list(dict.fromkeys(issues))
    return out


def _node_identity_constraint_for_request(constraint):
    return (constraint.get('property') in {'id', 'name'}
            and constraint.get('owner_kind') in {None, 'node'}
            and not constraint.get('relationship_type')
            and constraint.get('entity_type') in REGISTRY_NODE_TYPES)


REGISTRY_NODE_TYPES = set(PROPERTIES) | {
    'Gene', 'variants', 'GO_term', 'kegg', 'reactome', 'anatomical_structure',
    'disease', 'donor', 'Sample_node', 'data_modality', 'OCR_peak',
}


def sample_lookup_requested(step):
    question, _ = _trusted_request(step)
    return ('HAS_SAMPLE' in step.get('relation_types', [])
            and not step.get('deferred_sample_scope')
            and bool(re.search(r'\bsamples?\b|\bSample_node\b', question, re.I)))


def generation_guidance(step):
    if not step.get('semantic_registry') or not semantic_intent(step):return ''
    notes='\nCanonical bindings above override shorthand stage/assay spellings in the question. A recorded T1D stage does not imply a second disease diagnosis filter: apply only the resolved disease constraint, if present. t1d_stage is a donor property; sample fields: id, data_modality, anatomical_structure (text). No anatomical_structure_id or anatomical_structure_ref. For stage-only questions do not add disease.id or donor.diabetes_type filters, including for stages 1 and 2; those are not necessarily recorded as diagnosed diabetes. Use anatomy -HAS_SAMPLE-> sample and donor -HAS_SAMPLE-> that same sample. Disease -HAS_DONOR-> donor. Return donor/sample nodes and linking evidence; no invented rank or extra sample requirements for donor-only questions.'
    notes+=' Do not filter sample.anatomical_structure: this is descriptive text, not a tissue identifier; constrain the linked anatomy node instead.'
    if step.get('semantic_registry', {}).get('donor_required') is False:
        notes+=' SAMPLE-ONLY lookup: apply the sample assay/source and requested anatomy filters to the same Sample_node. Do not require donor or disease links; neither was requested.'
    elif not sample_lookup_requested(step) and not step.get('sample_requirements',{}).get('modality_groups') and not step.get('sample_requirements',{}).get('excluded_modality_constraints') and not any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[])):
        notes+=' DONOR-ONLY lookup: do not MATCH Sample_node, data_modality, anatomical_structure or HAS_SAMPLE. Return all matching donors and their disease link, without any assay restriction.'
    if step.get('sample_requirements',{}).get('separate_bindings'):notes+=' For RNA AND ATAC bind two sample variables linked to the SAME donor and requested tissue; each must satisfy its corresponding modality group. The two variables may identify the same multiome sample.'
    if step.get('sample_requirements',{}).get('excluded_modality_constraints'):
        notes += ' Apply every negative modality predicate to the same sample linked to the returned donor and requested tissue. Never turn an excluded assay into a positive capability lookup or exclude an entire donor unless explicitly requested.'
    return notes


def meaningful_row(row):
    """Empty graph wrappers are not evidence; scalar zero remains meaningful."""
    if isinstance(row,dict):
        # Preserve scalar nulls as missing measurements, rather than converting
        # an existing result row into a claim that no records matched.
        if any(not isinstance(v,(dict,list,tuple)) for v in row.values()):return bool(row)
        return any(meaningful_row(v) for v in row.values())
    if isinstance(row,(list,tuple)):return any(meaningful_row(v) for v in row)
    return row is not None


def validation_errors(tokens, step, parameters, bindings, paths, predicate, choices):
    errors=[]
    # Reject unsupported properties even when a key happens to exist elsewhere.
    from .graph import _predicate_owner
    for i,t in enumerate(tokens):
        dot=i>=2 and tokens[i-1].value=='.'
        mapped=i>0 and i+1<len(tokens) and tokens[i-1].value in ('{',',') and tokens[i+1].value==':'
        if dot or mapped:
            owner=_predicate_owner(tokens,i)
            labels=bindings.get(owner,set())
            supported=[set(PROPERTIES[l]) for l in labels if l in PROPERTIES]
            if supported and t.kind in {'WORD','IDENT'} and not any(t.value in props for props in supported):
                errors.append('invalid_node_property:'+','.join(sorted(labels))+'.'+t.value)
    if not step.get('semantic_registry') or not semantic_intent(step):return errors
    donors={v for v,labels in bindings.items() if 'donor' in labels}
    def constraints_at(variable, label):
        relevant=[group for c,group in zip(step.get('constraints',[]),choices) if c.get('entity_type')==label]
        return all(any(predicate(tokens,c,parameters,{variable}) for c in group) for group in relevant)
    donors={v for v in donors if constraints_at(v,'donor')}
    diseases={v for v,labels in bindings.items() if 'disease' in labels and constraints_at(v,'disease')}
    disease_required=any(c.get('entity_type')=='disease' for c in step.get('constraints',[]))
    if disease_required:donors={d for d in donors if any(a in diseases and b==d and 'HAS_DONOR' in kinds for a,b,kinds in paths)}
    donor_required = step.get('semantic_registry', {}).get('donor_required', True)
    if donor_required and not donors:errors.append('missing_required_donor_cohort_path')
    requirement=step.get('sample_requirements',{})
    groups=requirement.get('modality_groups',[])
    exclusions=requirement.get('excluded_modality_constraints',[])
    tissue_required=any(c.get('entity_type')=='anatomical_structure' for c in step.get('constraints',[]))
    anatomy={v for v,labels in bindings.items() if 'anatomical_structure' in labels and constraints_at(v,'anatomical_structure')}
    if donor_required and not sample_lookup_requested(step) and not groups and not exclusions and not tissue_required and any('HAS_SAMPLE' in kinds for a,b,kinds in paths):
        errors.append('unrequested_sample_join_for_donor_only_lookup')
    sample_fields = [c for c in step.get('constraints', []) if c.get('entity_type') == 'Sample_node' and c.get('property') != 'data_modality']
    if sample_lookup_requested(step) or groups or exclusions or tissue_required or (not donor_required and sample_fields):
        candidates=[]
        for group in groups or [None]:
            valid=set()
            for sample,labels in bindings.items():
                if 'Sample_node' not in labels:continue
                if not all(predicate(tokens,c,parameters,{sample}) for c in sample_fields):continue
                def excluded_here(c):
                    if predicate(tokens, c, parameters, {sample}):
                        return True
                    return bool(step.get('semantic_registry',{}).get('modality_links_verified') and any(
                        'data_modality' in bindings.get(a,set()) and b==sample and 'HAS_SAMPLE' in kinds
                        and predicate(tokens,{**c,'property':'id'},parameters,{a}) for a,b,kinds in paths))
                if exclusions and not all(excluded_here(c) for c in exclusions):continue
                if group:
                    c={'property':'data_modality','operator':'IN' if len(group)>1 else '=','value':group if len(group)>1 else group[0]}
                    match=predicate(tokens,c,parameters,{sample})
                    if not match and step.get('semantic_registry',{}).get('modality_links_verified'):
                        match=any('data_modality' in bindings.get(a,set()) and b==sample and 'HAS_SAMPLE' in kinds
                            and predicate(tokens,{**c,'property':'id'},parameters,{a}) for a,b,kinds in paths)
                    if not match:continue
                if tissue_required and not any(a in anatomy and b==sample and 'HAS_SAMPLE' in kinds for a,b,kinds in paths):continue
                valid.add(sample)
            candidates.append(valid)
        aliases={tokens[i+1].value:tokens[i-1].value for i,t in enumerate(tokens[1:-1],1)
                 if t.value.upper()=='AS' and tokens[i-1].value in bindings and (i<2 or tokens[i-2].value!='.')}
        def canonical(v):
            seen=set()
            while v in aliases and v not in seen:seen.add(v);v=aliases[v]
            return v
        if donor_required:
            valid_donor=False
            for donor in donors:
                linked={b for a,b,kinds in paths if a==donor and 'HAS_SAMPLE' in kinds}
                sets=[{canonical(v) for v in s & linked} for s in candidates]
                if all(sets) and (not requirement.get('separate_bindings') or len(set.union(*sets))>=len(sets)):
                    valid_donor=True
            if not valid_donor:errors.append('missing_same_donor_sample_tissue_modality_path')
        else:
            sets=[{canonical(v) for v in group} for group in candidates]
            if not all(sets) or (requirement.get('separate_bindings') and len(set.union(*sets)) < len(sets)):
                errors.append('missing_same_tissue_modality_sample_path')
            if donors or any('donor' in bindings.get(a,set()) for a,b,kinds in paths if 'HAS_SAMPLE' in kinds):
                errors.append('unrequested_donor_join_for_sample_only_lookup')
    return errors


def donor_summary(evidence):
    nodes={str(n['id']):n for n in evidence.get('nodes',[]) if n.get('id')}
    donors={i:n for i,n in nodes.items() if 'donor' in n.get('labels',[])}
    if not donors:return None
    samples={i:n for i,n in nodes.items() if 'Sample_node' in n.get('labels',[])}
    links={d:set() for d in donors}
    for edge in evidence.get('edges',[]):
        if edge.get('type')=='HAS_SAMPLE' and str(edge.get('start_id')) in links and str(edge.get('end_id')) in samples:
            links[str(edge['start_id'])].add(str(edge['end_id']))
    return {'unique_donors':len(donors),'unique_samples':len(samples),'counts_scope':'retrieved evidence',
        'rows':[{'donor_id':d,'recorded_stage':donors[d].get('properties',{}).get('t1d_stage'),
        'sample_count':len(links[d]),'recorded_assays':sorted({samples[s].get('properties',{}).get('data_modality','unknown') for s in links[d]}),
        'file_availability':'not_verified'} for d in sorted(donors)],
        'assay_capabilities':{m:{'components':CAPABILITIES.get(m,[]),'source':SOURCE,'scope':'HPAP protocol; not a per-file check'} for m in sorted({n.get('properties',{}).get('data_modality','unknown') for n in samples.values()})}}
