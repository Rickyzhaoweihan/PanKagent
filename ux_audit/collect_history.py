"""Read-only, selected production-history export. Output must remain private."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

SELECTED = [47, 297, 300, 2804, 3090, 3182, 3204, 3261, 3418, 3489, 3582, 3596]
KEEP = {'question', 'interpreted_question', 'user_prompt', 'prompt', 'revision_prompt',
        'plan', 'plan_markdown', 'answer_markdown', 'cypher_queries', 'error',
        'processing_time_ms', 'use_literature', 'plan_type', 'route', 'rigor'}

def collect(path, match_session=None):
    raw = path.read_bytes()
    rows = [dict(json.loads(line), source_line=i) for i, line in enumerate(raw.splitlines(), 1) if line.strip()]
    chosen = {i: rows[i-1] for i in SELECTED}
    ids = {row['session_id'] for row in chosen.values()}
    # Chat events point to child plan sessions; follow only recorded IDs, never
    # infer a user/session identity from matching question text.
    changed = True
    while changed:
        before = len(ids)
        for row in rows:
            links = {row.get('session_id')}
            links.update(v for k, v in row.get('data', {}).items() if k.endswith('session_id') and isinstance(v, str))
            if links & ids: ids.update(links)
        changed = len(ids) != before
    def clean(row):
        return {'line': row['source_line'], 'session_hash': hashlib.sha256(row['session_id'].encode()).hexdigest(),
                'event': row['event'], 'timestamp': row['timestamp'],
                'links': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in row.get('data', {}).items() if k.endswith('session_id') and isinstance(v, str)},
                'data': {k: v for k, v in row.get('data', {}).items() if k in KEEP}}
    selected = [clean(row) for row in rows if row['session_id'] in ids]
    # Preserve production provenance without pretending every record is a human.
    for row in selected: row['actor_identity'] = 'unknown_production_session'
    empty = [clean(row) for row in rows if 'confirm' in row['event'] and any(t in str(row.get('data',{}).get('answer_markdown','')).lower() for t in ['no results', 'no matching', 'no data found'])]
    return {'version': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
            'source_path': str(path), 'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_bytes': len(raw),
            'event_counts': dict(Counter(row['event'] for row in rows)),
            'selected_revision_lines': SELECTED, 'records': selected, 'empty_candidates': empty[-8:],
            'official_session_match': [clean(row) for row in rows if match_session and row['session_id'] in {match_session, 'chat_'+match_session}]}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--match-session', help='Optional recorded browser session used to verify production routing; never commit its value.')
    args = p.parse_args()
    result = collect(args.source, args.match_session)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({'records': len(result['records']), 'events': result['event_counts'], 'sha256': result['source_sha256']}))
