"""Bound model context without changing retained conversations or evidence."""
import copy
import hashlib
import json


def model_messages(messages, max_bytes=90000):
    wire = copy.deepcopy(messages)
    size = lambda: len(json.dumps(wire, ensure_ascii=False).encode())
    before = size()
    calls = {}
    candidates = []
    for i, message in enumerate(wire):
        content = message.get('content')
        if not isinstance(content, list): continue
        for block in content:
            if block.get('type') == 'tool_use':
                calls[block['id']] = {'name': block['name'], 'input': block['input']}
            elif block.get('type') == 'tool_result' and isinstance(block.get('content'), str):
                call = calls.get(block['tool_use_id'], {})
                # Preserve recent output and schema definitions as long as possible.
                candidates.append((i == len(wire)-1, call.get('name') == 'schema', i, block, call))
    count = 0
    for _, _, _, block, call in sorted(candidates, key=lambda x:x[:3]):
        if size() <= max_bytes: break
        raw = block['content']
        if len(raw.encode()) < 2000: continue
        try: value = json.loads(raw)
        except (ValueError, TypeError): continue
        if not isinstance(value, dict): continue
        summary = {k: copy.deepcopy(value[k]) for k in (
            'query_id', 'purpose', 'status', 'row_count', 'truncated', 'next_offset',
            'next_cursor', 'selected_queries', 'release', 'sha256', 'feedback') if k in value}
        if 'candidates' in value: summary['candidate_count'] = len(value['candidates'])
        if 'graph' in value:
            summary['retained_graph_counts'] = {k:len(v) for k,v in value['graph'].items() if isinstance(v,list)}
        recover = copy.deepcopy(call)
        if value.get('query_id'):
            recover = {'name':'inspect_result','input':{'query_id':value['query_id'],'kind':'nodes','limit':10}}
        elif call.get('name') == 'finish':
            recover = {'name':'schema','input':{}}
        summary.update(context_compacted=True, original_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                       recover_with=recover,
                       note='Full output remains in the run audit and retained query/schema registry. This is context compaction, not empty or missing evidence. Read retained nodes/edges/evidence with inspect_result; repeat schema/resolve_entity for exact definitions or identities. Original tool calls and their arguments are preserved.')
        compact = json.dumps(summary, ensure_ascii=False)
        if len(compact.encode()) >= len(raw.encode()): continue
        block['content'] = compact
        count += 1
    after = size()
    if after > max_bytes:
        raise ValueError('Conversation structure exceeds model context envelope; no silent conversation or evidence truncation')
    return wire, {'original_bytes':before,'sent_bytes':after,'compacted_tool_results':count}
