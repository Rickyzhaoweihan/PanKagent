"""Assemble protected task cards; never infer scientific correctness from success."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path

DIMENSIONS = ['task_completion', 'planning_control', 'waiting_continuity', 'readability', 'evidence_inspection', 'scientific_reliability']


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def elapsed(start, end):
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except (ValueError, TypeError):
        return None


def measures(capture):
    events = capture.get('events', [])
    result = {'layer': 'server events / API observation; not browser visibility', 'official_cache_state': 'unknown'}
    start = (capture.get('created') or {}).get('run_id')
    relevant = [e for e in events if not start or e.get('run_id') == start]
    for key, event_type in [('first_progress_s', 'progress'), ('plan_ready_s', 'plan_ready'),
                            ('answer_text_s', 'graph_answer'), ('literature_completion_s', 'literature_complete')]:
        eligible = [e for e in relevant if e.get('type') == event_type and not e.get('payload', {}).get('delta')]
        if eligible:
            event = eligible[-1] if key == 'answer_text_s' else eligible[0]
            result[key] = event.get('elapsed_ms', 0) / 1000
            if key in ['answer_text_s', 'literature_completion_s']:
                result[key.replace('_s', '_after_confirmation_s')] = elapsed(capture.get('confirmed_at'), event.get('timestamp'))
    initial = capture.get('initial') or {}
    preview_steps = (((initial.get('preview') or {}).get('evidence') or {}).get('steps') or [])
    result['validated_preview_s'] = result.get('plan_ready_s') if preview_steps and all(s.get('status') == 'complete' for s in preview_steps) else None
    result['graph_nodes_present'] = bool((capture.get('final') or {}).get('evidence', {}).get('nodes')) if (capture.get('final') or {}).get('evidence') else False
    result['confirmation_to_terminal_s'] = capture.get('confirmation_to_terminal_s')
    result['request_durations_s'] = [{'method': r['method'], 'path': r['path'], 'seconds': r.get('elapsed_s'),
                                    'http_status': r.get('http_status')} for r in capture.get('requests', []) if r['method'] == 'POST']
    if capture.get('harness_correction'):
        result['exclude_total_latency'] = True
        result['reason'] = 'harness correction / delayed continuation'
    result['saved_revisit_recorded'] = 'revisit' in capture
    return result


def assemble(root):
    manifest = json.loads((root/'manifest.json').read_text())
    reviews = json.loads((root/'reviews.json').read_text()) if (root/'reviews.json').exists() else {}
    cards = root/'task-cards'
    cards.mkdir(exist_ok=True, mode=0o700)
    all_cards = []
    for task in manifest['tasks']:
        card = {'version': 1, 'task': task, 'study_type': 'agent-run usability audit',
                'classification': 'not comparable', 'acceptance': 'not established', 'sides': {}}
        for side in ['official', 'vnext']:
            path = root/'cases'/f'{task["id"]}-{side}.json'
            if not path.exists():
                path = root/'cases'/f'{task.get("parent_task", task["id"])}-{side}.json'
            capture = json.loads(path.read_text()) if path.exists() else {}
            final = capture.get('final') or {}
            evidence = final.get('evidence') or {}
            side_review = reviews.get(task['id'], {}).get(side, {})
            scores = {dimension: {'score': None, 'reason': 'Not observed or requires domain review.', 'reference': None} for dimension in DIMENSIONS}
            scores.update(side_review.get('scores', {}))
            card['sides'][side] = {
                'capture': str(path.relative_to(root)) if path.exists() else None,
                'capture_sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
                'capture_status': capture.get('status', 'not_captured'), 'run_status': final.get('status'),
                'outcome': side_review.get('outcome', 'See captured response; no scientific pass inferred.'),
                'blockers': list(side_review.get('blockers', [])), 'scores': scores, 'timing': measures(capture),
                'graph_steps': [{'id': s.get('step_id'), 'status': s.get('status'), 'nodes': len(s.get('nodes', [])),
                                 'edges': len(s.get('edges', [])), 'truncated': s.get('truncated')} for s in evidence.get('steps', [])],
                'revision_chain': [{'instruction': r.get('instruction'), 'run': r.get('run'), 'response': r.get('response')} for r in capture.get('revisions', [])],
                'references': side_review.get('references', []),
                'scientific_review': side_review.get('scientific_review', 'Unresolved: source-level review is not complete.'),
            }
            if not capture:
                card['sides'][side]['blockers'].append('No API capture; any browser-only observation is identified separately.')
            if (task['holdout'] or task['id'] in ['E02', 'E06', 'E07']) and not side_review:
                card['sides'][side]['blockers'].append('Reserved evidence is captured but detailed judgment is deferred; excluded from improvement selection.')
            if capture.get('error_category'):
                card['sides'][side]['blockers'].append('Capture stopped: '+capture['error_category'])
            if final.get('status') in ['failed', 'awaiting_confirmation']:
                card['sides'][side]['blockers'].append('No completed graph answer; inspect recorded error or clarification.')
            if task.get('parent_task') and not side_review:
                card['sides'][side]['blockers'].append('Parent evidence captured; this inspection/recovery action lacks a completed observation.')
        review = reviews.get(task['id'], {})
        card.update({k: review[k] for k in ['classification', 'newcomer_checklist', 'finding_ids', 'reproduction'] if k in review})
        if task['holdout'] or task['id'] in ['E02', 'E06', 'E07']:
            card['holdout_policy'] = 'Frozen captures; excluded from prompt tuning and improvement selection.'
        write(cards/(task['id']+'.json'), card)
        lines = [f'# {task["id"]}: {task["goal"]}', '', 'Agent-run usability audit; no human satisfaction measure.', '',
                 '**Question:** '+task['question'], '', '**Classification:** '+card['classification'], '',
                 '**Source:** `'+json.dumps(task['source'], ensure_ascii=False)+'`', '']
        if task.get('revisions'):
            lines += ['**Exact historical revision sequence:**', '']+[f'{i}. {r["instruction"]}' for i,r in enumerate(task['revisions'],1)]+['']
        for side, value in card['sides'].items():
            lines += [f'## {side}', '', value['outcome'], '', f'Capture: `{value["capture"]}`; state: `{value["run_status"] or value["capture_status"]}`.', '',
                      '| Dimension | Score / 3 | Observable reason |', '|---|---|---|']
            for dimension, score in value['scores'].items():
                lines.append(f'| {dimension} | {score["score"] if score["score"] is not None else "unscored"} | {score["reason"]} ({score.get("reference") or "review required"}) |')
            lines += ['', 'Scientific review: '+value['scientific_review'], '', 'Blockers: '+('; '.join(value['blockers']) or 'None recorded; acceptance still requires complete review.'), '']
        (cards/(task['id']+'.md')).write_text('\n'.join(lines))
        (cards/(task['id']+'.md')).chmod(0o600)
        all_cards.append(card)
    bundle = {'version': 1, 'manifest_sha256': hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest(),
              'baseline_accepted': False, 'tasks': all_cards}
    write(root/'audit-bundle.json', bundle)
    summary = {'version': 1, 'task_count': len(all_cards), 'baseline_accepted': False,
               'classification_counts': dict(Counter(c['classification'] for c in all_cards)),
               'outcomes': [{'id': c['task']['id'], 'holdout': c['task']['holdout'], 'classification': c['classification'],
                             'sides': {s: {'capture_status': v['capture_status'], 'run_status': v['run_status'], 'scores': v['scores'],
                                          'outcome': v['outcome'], 'blockers': v['blockers'],
                                          'references': v['references'], 'scientific_review': v['scientific_review'],
                                          'capture_sha256': v['capture_sha256'],
                                          'timing': {k: value for k, value in v['timing'].items() if k != 'request_durations_s'}} for s,v in c['sides'].items()}} for c in all_cards]}
    write(root/'sanitized-summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    summary = assemble(args.root)
    print(json.dumps({k:v for k,v in summary.items() if k != 'outcomes'}))
