"""Deterministic descriptive profiles. Observations never authorize predicates."""
from collections import Counter
import json
import math
import re


def typed_key(value):
    return type(value).__name__ + ':' + json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def profile_grouped(rows, *, total_records, complete=True, pattern=None):
    """Consume (value, owning-record-frequency), without retaining the full domain."""
    distinct = present = empty_string = empty_list = 0
    types = Counter(); examples = []; minimum = maximum = None; matches = mismatches = 0
    for value, count in rows:
        if value is None:
            continue
        distinct += 1; present += count; types[type(value).__name__] += count
        empty_string += count if value == '' else 0
        empty_list += count if value == [] else 0
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
        if pattern and isinstance(value, str):
            if re.fullmatch(pattern, value): matches += count
            else: mismatches += count
        examples.append({'value': value, 'frequency': count})
        examples.sort(key=lambda item: (-item['frequency'], typed_key(item['value'])))
        del examples[4:]
    small = complete and distinct < 5
    result = {'status': 'complete' if complete else 'sampled', 'exhaustive': small,
              'distinct_count': distinct if complete else None, 'observed_distinct_count': distinct,
              'present_records': present, 'missing_records': max(0, total_records-present) if complete else None,
              'stored_types': dict(sorted(types.items())), 'empty_string_records': empty_string,
              'empty_list_records': empty_list, 'examples': examples[:4 if small else 3],
              'observed_numeric_range': None if minimum is None else {'min': minimum, 'max': maximum}}
    if pattern: result['pattern_check'] = {'pattern': pattern, 'matching_records': matches, 'mismatching_records': mismatches}
    return result


def profile(values, *, complete=True, pattern=None):
    counts = Counter(); decoded = {}; elements = Counter(); decoded_elements = {}; lengths=[]
    for value in values:
        key=typed_key(value); counts[key]+=1; decoded[key]=value
        if isinstance(value,list):
            lengths.append(len(value))
            for item in {typed_key(v):v for v in value}.values():
                key=typed_key(item);elements[key]+=1;decoded_elements[key]=item
    result=profile_grouped(((decoded[k],n) for k,n in counts.items()),total_records=len(values),complete=complete,pattern=pattern)
    if lengths:
        result['list_lengths']={'min':min(lengths),'max':max(lengths)}
        result['elements']=profile_grouped(((decoded_elements[k],n) for k,n in elements.items()),total_records=len(lengths),complete=complete)
        # Element membership frequencies may overlap; absence is not computed by summing them.
        result['elements'].pop('missing_records',None)
    return result
