"""Preserve explicit genomic intervals using release-bound coordinate metadata.

The parser does not resolve named loci to two example genes. Coordinate types,
chromosome spelling and a default assembly come from a complete public Gene
metadata aggregate, never from a schema field name or a model's assumption.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import time

from .release_schema import REGISTRY

VERSION = 'genomic-scope-v1'
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_CHR = re.compile(r'\bchr(?:omosome)?\s*[: ]?\s*(?P<chromosome>\d{1,2}|X|Y|MT|M)\b', re.I)
_NUMBER = r'\d+(?:,\d{3})*(?:\.\d+)?'
_RANGE = re.compile(r'(?<![\w.+-])(?P<start>' + _NUMBER + r')\s*(?P<start_unit>bp|kb|mb|gb)?\s*'
                    r'(?:[-–—]|\.\.|\bto\b)\s*(?P<end>' + _NUMBER + r')\s*(?P<end_unit>bp|kb|mb|gb)?\b', re.I)
_ASSEMBLY = re.compile(r'\b(?:GRCh\d+(?:\.p\d+)?|hg\d+|T2T[- ]CHM13(?:v[\d.]+)?)\b', re.I)
_LOCUS = re.compile(r'\b([A-Za-z][A-Za-z0-9]*(?:/[A-Za-z][A-Za-z0-9]*)*)\s+locus\b', re.I)
_SCALE = {'bp': 1, 'kb': 1000, 'mb': 1000000, 'gb': 1000000000}
_REGION_PROPERTIES = {'chr', 'start_loc', 'end_loc', 'assembly', 'genome_assembly'}
_IDENTITY_PROPERTIES = {'id', 'name', 'hgnc_symbol'}

# Only public metadata; no donor/sample fields and no scientific evidence rows.
COORDINATE_METADATA_QUERY = '''MATCH (g:Gene)
RETURN count(g) AS total,
 count(CASE WHEN g.chr IS NOT NULL AND g.start_loc = toInteger(g.start_loc)
   AND g.end_loc = toInteger(g.end_loc) AND g.start_loc >= 0 AND g.end_loc >= g.start_loc THEN 1 END) AS coordinate_count,
 collect(DISTINCT g.chr) AS chromosomes,
 count(g.assembly) AS assembly_count, collect(DISTINCT g.assembly) AS assembly_values,
 count(g.genome_assembly) AS genome_assembly_count,
 collect(DISTINCT g.genome_assembly) AS genome_assembly_values'''


def genomic_scope(question):
    """Return an exact, non-executable interpretation of a Gene region request."""
    chromosomes = list(_CHR.finditer(question))
    if not chromosomes or not re.search(r'\bgenes?\b|\bEnsembl\s+IDs?\b|\blocus\b', question, re.I):
        return None
    labels = [{'surface': m[1], 'span': list(m.span(1))} for m in _LOCUS.finditer(question)]
    ranges = list(_RANGE.finditer(question))
    scope = {'version': VERSION, 'raw_question': question, 'entity_type': 'Gene',
             'chromosome': chromosomes[0]['chromosome'].upper(), 'locus_labels': labels,
             'selection': 'contained' if re.search(r'\b(?:entirely|fully|wholly)\s+(?:inside|within|contained)|\bcontained\s+(?:in|within)\b', question, re.I) else 'overlap',
             'coordinate_convention': ('0-based_half-open' if re.search(r'0[- ]based|half[- ]open', question, re.I)
                                       else '1-based_inclusive' if re.search(r'1[- ]based', question, re.I) else 'as_written'),
             'collection_scope': bool(re.search(r'\b(?:genes|Ensembl\s+IDs|gene\s+IDs)\b', question, re.I)),
             'complete_collection': not bool(re.search(r'\b(?:top|bottom|first)\s+\d+\b|\bexamples?\b', question, re.I)),
             'state': 'missing_bounds'}
    assemblies = list(dict.fromkeys(m[0] for m in _ASSEMBLY.finditer(question)))
    scope['requested_assembly'] = assemblies[0] if len(assemblies) == 1 else None
    if len(chromosomes) != 1 or len(assemblies) > 1:
        scope['state'] = 'ambiguous_region'
        return scope
    if re.search(r'\boutside\b|\bexcept\b|\bexclud\w*\b|\bnot\s+(?:in|within|overlapping)\b', question, re.I):
        scope['state'] = 'unsupported_region_complement'
        return scope
    nearby = [m for m in ranges if min(abs(m.start() - chromosomes[0].end()), abs(chromosomes[0].start() - m.end())) <= 120]
    if len(nearby) != 1:
        if nearby:
            scope['state'] = 'ambiguous_region'
        return scope
    interval = nearby[0]
    try:
        unit = (interval['end_unit'] or interval['start_unit'] or 'bp').lower()
        start = Decimal(interval['start'].replace(',', '')) * _SCALE[(interval['start_unit'] or unit).lower()]
        end = Decimal(interval['end'].replace(',', '')) * _SCALE[(interval['end_unit'] or unit).lower()]
        if start < 0 or end <= start or start != int(start) or end != int(end):
            raise ValueError('invalid_coordinates')
        scope.update(start=int(start), end=int(end), coordinate_unit='bp',
                     requested_interval=interval[0].rstrip(), state='parsed')
    except (InvalidOperation, ValueError):
        scope['state'] = 'invalid_coordinates'
    return scope


def coordinate_metadata(rows, identity):
    """Validate a complete aggregate; absent or partial fields are not proof."""
    result = {'state': 'unavailable', 'identity': deepcopy(identity), 'version': VERSION,
              'source': 'complete_public_Gene_coordinate_aggregate',
              'query_sha256': hashlib.sha256(COORDINATE_METADATA_QUERY.encode()).hexdigest()}
    if len(rows) != 1 or identity.get('graph_release') != REGISTRY['release']:
        return result
    row = rows[0]
    result['coverage'] = deepcopy(row)
    total = row.get('total', 0)
    chromosomes = row.get('chromosomes', [])
    if (not isinstance(total, int) or total <= 0 or row.get('coordinate_count') != total
            or not chromosomes or not all(isinstance(v, str) and v for v in chromosomes)):
        result['reason'] = 'coordinate_coverage_or_numeric_storage_unverified'
        return result
    result.update(state='verified', coordinate_storage='numeric', chromosomes=chromosomes)
    fields = {field: row.get(field + '_values', []) for field in ('genome_assembly', 'assembly')}
    all_values = {str(value).casefold() for values in fields.values() for value in values}
    for field, values in fields.items():
        if (row.get(field + '_count') == total and len(values) == 1
                and isinstance(values[0], str) and values[0] and len(all_values) == 1):
            result['default_assembly'] = {'property': field, 'value': values[0],
                'basis': 'unique_value_with_complete_Gene_coverage_for_verified_release'}
            break
    return result


async def load_coordinate_metadata(graph, identity):
    """Cache one read-only aggregate per verified runtime identity for five minutes."""
    key = json.dumps(identity, sort_keys=True, default=str)
    if not hasattr(graph, '_genomic_metadata_lock'):
        graph._genomic_metadata_lock = asyncio.Lock()
    async with graph._genomic_metadata_lock:
        cached = getattr(graph, '_genomic_coordinate_metadata', None)
        if cached and cached[0] == key and time.monotonic() - cached[1] < 300:
            return deepcopy(cached[2])
        try:
            value = coordinate_metadata(await graph._small_query(COORDINATE_METADATA_QUERY), identity)
        except Exception:
            value = {'state': 'unavailable', 'identity': deepcopy(identity), 'version': VERSION,
                     'reason': 'coordinate_metadata_read_unavailable'}
        graph._genomic_coordinate_metadata = (key, time.monotonic(), value)
        return deepcopy(value)


def _gene_step(step):
    kinds = step.get('relation_types', [])
    return (not kinds or any('Gene' in path['source'] + path['target']
            for kind in kinds for path in REGISTRY['relations'].get(kind, {}).get('paths', [])))


def _region_binding(question, grounding):
    scope = genomic_scope(question)
    if not scope:
        return None, None
    if scope['state'] != 'parsed':
        return None, 'unresolved_genomic_scope:' + scope['state'] + ': Preserve the whole locus; obtain explicit chromosome bounds instead of substituting named genes.'
    metadata = grounding.get('genomic_coordinate_metadata', {})
    if (metadata.get('state') != 'verified' or metadata.get('identity') != grounding.get('identity')
            or metadata.get('coordinate_storage') != 'numeric'
            or coordinate_metadata([metadata.get('coverage', {})], metadata.get('identity', {})) != metadata):
        return None, 'unresolved_genomic_scope:coordinate_metadata_unverified'
    if scope['coordinate_convention'] != 'as_written':
        return None, 'unresolved_genomic_scope:coordinate_convention_unverified: Preserve the requested convention; numeric storage alone cannot authorize a coordinate conversion.'
    matches = [value for value in metadata.get('chromosomes', [])
               if re.sub(r'^chr', '', value, flags=re.I).upper() == scope['chromosome']]
    if len(matches) != 1:
        return None, 'unresolved_genomic_scope:chromosome_value_unverified'
    assembly = metadata.get('default_assembly')
    requested = scope['requested_assembly']
    if not assembly:
        return None, 'unresolved_genomic_scope:assembly_unknown_or_mixed: Ask for a verified reference assembly; do not assume a build.'
    if requested and requested.casefold() != assembly['value'].casefold():
        return None, 'unresolved_genomic_scope:assembly_mismatch: Requested and recorded builds differ; no coordinate liftover is authorized.'
    predicates = [{'entity_type': 'Gene', 'owner_kind': 'node', 'property': 'chr', 'operator': '=', 'value': matches[0]},
                  {'entity_type': 'Gene', 'owner_kind': 'node', 'property': assembly['property'], 'operator': '=', 'value': assembly['value']}]
    if scope['selection'] == 'contained':
        coordinates = [('start_loc', '>=', scope['start']), ('end_loc', '<=', scope['end'])]
    else:
        coordinates = [('start_loc', '<=', scope['end']), ('end_loc', '>=', scope['start'])]
    predicates.extend({'entity_type': 'Gene', 'owner_kind': 'node', 'property': prop,
                       'operator': operator, 'value': value} for prop, operator, value in coordinates)
    return {'version': VERSION, 'graph_release': REGISTRY['release'], 'scope': scope,
            'metadata': deepcopy(metadata), 'assembly_source': 'user_and_verified_release' if requested else 'verified_release_default',
            'predicates': predicates}, None


def _same_predicate(left, right):
    if left.get('owner_kind') not in (None, 'node') or left.get('relationship_type'):
        return False
    if any(left.get(key, '=' if key == 'operator' else None) != right.get(key)
           for key in ('entity_type', 'property', 'operator')):
        return False
    value = left.get('value')
    if isinstance(right['value'], int) and isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            return False
    return not isinstance(value, bool) and value == right['value']


def has_verified_region_scope(step):
    """Authorize only a complete, internally consistent interval tuple."""
    contract = step.get('genomic_scope_contract', {})
    metadata = contract.get('metadata', {})
    identity = metadata.get('identity', {})
    if (contract.get('version') != VERSION or step.get('graph_version') != REGISTRY['release']
            or identity.get('graph_release') != REGISTRY['release']
            or coordinate_metadata([metadata.get('coverage', {})], identity) != metadata):
        return False
    expected, issue = _region_binding(contract.get('scope', {}).get('raw_question', ''),
        {'identity': identity, 'genomic_coordinate_metadata': metadata})
    if issue or expected != contract:
        return False
    constraints = step.get('constraints', [])
    if any(not any(_same_predicate(c, wanted) and c.get('owner_kind') == 'node' for c in constraints)
           for wanted in expected['predicates']):
        return False
    if any(c.get('entity_type') == 'Gene' and c.get('property') in _REGION_PROPERTIES
           and not any(_same_predicate(c, wanted) for wanted in expected['predicates']) for c in constraints):
        return False
    if expected['scope']['collection_scope'] and any(c.get('entity_type') == 'Gene'
            and c.get('property') in _IDENTITY_PROPERTIES for c in constraints):
        return False
    return True


def is_verified_region_constraint(constraint, step):
    return has_verified_region_scope(step) and any(_same_predicate(constraint, wanted)
        for wanted in step['genomic_scope_contract']['predicates'])


def compile_genomic_scope(question, grounding, plan):
    """Bind a proven interval on existing Gene checks; never invent a relation."""
    result = deepcopy(plan)
    if (not isinstance(grounding, dict) or grounding.get('status') != 'ready'
            or grounding.get('identity', {}).get('graph_release') != REGISTRY['release']
            or result.get('clarification') or result.get('answer_mode')):
        return result, None
    binding, issue = _region_binding(question, grounding)
    if issue or not binding:
        return result, issue
    scope = binding['scope']
    locus_forms = {label['surface'].casefold() for label in scope['locus_labels']}
    locus_forms |= {part for value in list(locus_forms) for part in value.split('/')}
    for mention in grounding.get('mentions', []):
        if mention.get('context_role', {}).get('kind') == 'genomic_locus_label':
            locus_forms.update(str(c[key]).casefold() for c in mention.get('candidates', []) for key in ('id', 'name') if c.get(key))
    for step in result.get('steps', []):
        if step.get('purpose') == 'context' or not _gene_step(step):
            continue
        if any('Gene' in path['source'] and 'Gene' in path['target']
               for kind in step.get('relation_types', [])
               for path in REGISTRY['relations'].get(kind, {}).get('paths', [])):
            return result, 'unresolved_genomic_scope:multiple_Gene_roles: Keep interval genes distinct from interaction partners.'
        existing = step.get('constraints', [])
        retained, replaced = [], []
        for constraint in existing:
            prop = str(constraint.get('property', '')).split('.')[-1]
            if constraint.get('entity_type') == 'Gene' and prop in _REGION_PROPERTIES:
                # A raw interval proves the whole requested span, not a narrower
                # model-selected span. Keep the original predicates in the audit.
                replaced.append(constraint)
            elif (scope['collection_scope'] and constraint.get('entity_type') == 'Gene'
                  and prop in _IDENTITY_PROPERTIES):
                from .constraint_values import list_value
                value = constraint.get('value')
                try:
                    values = list_value(value) if constraint.get('operator') == 'IN' else [value]
                except ValueError:
                    values = []
                if (constraint.get('operator', '=') in {'=', 'IN'} and values
                        and all(str(v).casefold() in locus_forms for v in values)):
                    replaced.append(constraint)
                else:
                    retained.append(constraint)
            else:
                retained.append(constraint)
        step['constraints'] = retained + deepcopy(binding['predicates'])
        step['genomic_scope_contract'] = deepcopy(binding)
        if replaced != binding['predicates']:
            step.setdefault('requested_scope_compilation', []).append({
                'version': VERSION, 'raw_question': question, 'original_predicates': deepcopy(replaced),
                'canonical_bindings': deepcopy(binding['predicates']), 'proof': binding['assembly_source']})
        if scope['collection_scope'] and scope['complete_collection']:
            step['complete'] = True
    primary = [s for s in result.get('steps', []) if s.get('purpose') != 'context']
    if len(primary) == 1 and primary[0].get('genomic_scope_contract'):
        result['interpreted_question'] = question
        primary[0]['question'] = question
        if binding['assembly_source'] == 'verified_release_default':
            primary[0]['question'] += ' Reference assembly: ' + binding['metadata']['default_assembly']['value'] + ' (verified release default).'
    return result, None


def region_scope_issue(question, grounding, plan):
    binding, issue = _region_binding(question, grounding)
    if issue or not binding:
        return issue
    steps = [s for s in plan.get('steps', []) if s.get('purpose') != 'context' and _gene_step(s)]
    if not steps:
        return 'missing_requested_scope:genomic_region:Gene_check'
    for step in steps:
        constraints = step.get('constraints', [])
        if any(not any(_same_predicate(c, wanted) for c in constraints) for wanted in binding['predicates']):
            return 'missing_requested_scope:genomic_region:' + str(step.get('id', 'step'))
        if binding['scope']['collection_scope'] and any(c.get('entity_type') == 'Gene' and
                str(c.get('property', '')).split('.')[-1] in _IDENTITY_PROPERTIES for c in constraints):
            return 'narrowed_requested_scope:genomic_region:gene_identity_filter'
        if binding['scope']['collection_scope'] and binding['scope']['complete_collection'] and step.get('complete') is False:
            return 'narrowed_requested_scope:genomic_region:incomplete_collection'
    return None
