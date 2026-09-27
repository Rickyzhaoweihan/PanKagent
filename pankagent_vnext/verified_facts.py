"""Deterministic full-record facts; database fields come only from the schema pack."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import re
from .agent_schemas import module


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def build(question, evidence):
    metadata = module('database_schema')['execution_metadata']
    operations = module('semantic_interpretation')['fact_operations']
    facts = []
    for key, result in evidence.items():
        if result.get('error') or result.get('status') not in {'complete', 'empty', 'partial'}:
            continue
        execution = result.get('retrieval_execution') or {}
        complete = (result.get('status') in {'complete', 'empty'} and not result.get('truncated')
                    and execution.get('completed') is True and execution.get('cursor_exhausted') is True)
        edges = result.get('edges') or []
        item = {'evidence_id': result.get('evidence_id', key), 'complete': complete,
                'scope': 'full verified task population' if complete else 'retrieved subset only; no exhaustive claims',
                'typed_node_counts': dict(Counter(label for label, identifier in {(label, str(n['id'])) for n in result.get('nodes', []) for label in n.get('labels', [])})),
                'relationship_records': len(edges), 'relation_counts': dict(Counter(e.get('type') for e in edges)),
                'numeric': [], 'comparisons': [], 'rankings': []}
        for kind, fields in metadata['measurement_fields'].items():
            records = [e for e in edges if e.get('type') == kind]
            if not records:
                continue
            for field in fields:
                conditional = next((r for r in operations.get('conditional_fields', [])
                                    if r['relation'] == kind and r['field'] == field), None)
                if conditional and not re.search(conditional['request_pattern'], question, re.I):
                    continue
                values = [number(e.get('properties', {}).get(field)) for e in records]
                valid = [v for v in values if v is not None]
                if valid:
                    item['numeric'].append({'relation': kind, 'field': field, 'records':len(records),
                        'valid':len(valid), 'missing_or_invalid':len(values)-len(valid),
                        'minimum':str(min(valid)), 'maximum':str(max(valid)),
                        'negative':sum(v<0 for v in valid),'zero':sum(v==0 for v in valid),
                        'positive':sum(v>0 for v in valid)})
        for rule in operations['comparisons']:
            groups = defaultdict(list)
            for edge in edges:
                if edge.get('type') != rule['relation']:
                    continue
                props = edge.get('properties', {})
                a,b = number(props.get(rule['left'])),number(props.get(rule['right']))
                if a is not None and b is not None:
                    groups[str(edge[rule['group_endpoint']])].append({'start_id':edge.get('start_id'), 'end_id':edge.get('end_id'),
                        'record_properties':props, 'left':str(a),'right':str(b),
                        'difference':str(a-b),'direction':'greater' if a>b else 'less' if a<b else 'equal'})
            if groups:
                item['comparisons'].append({'relation':rule['relation'], 'left_field':rule['left'],
                    'right_field':rule['right'],'groups':dict(groups)})
        if complete:
            for rule in operations['ranking']:
                requested = re.search(rule['request_pattern'], question, re.I)
                limit = re.search(rule['limit_pattern'], question, re.I)
                if not requested or not limit:
                    continue
                k = int(limit.group(1))
                if not 1 <= k <= 100:
                    continue
                by_id = {}
                for edge in edges:
                    if edge.get('type') != rule['relation']:
                        continue
                    val = number(edge.get('properties', {}).get(rule['field']))
                    if val is None or not Decimal(rule['valid_min']) <= val <= Decimal(rule['valid_max']):
                        continue
                    identifier = str(edge[rule['endpoint']])
                    if identifier not in by_id or val < by_id[identifier][0]:
                        by_id[identifier] = (val, [edge.get('properties', {})])
                    elif val == by_id[identifier][0] and edge.get('properties', {}) not in by_id[identifier][1]:
                        by_id[identifier][1].append(edge.get('properties', {}))
                if by_id:
                    item['rankings'].append({'relation':rule['relation'],'field':rule['field'],
                        'population':len(by_id),'operation':'minimum per identity; ascending value then identity',
                        'rows':[{'id':identifier,'value':str(value),'supporting_records':props}
                                for identifier,(value,props) in sorted(by_id.items(),key=lambda pair:(pair[1][0],pair[0]))[:k]]})
        facts.append(item)
    return facts
