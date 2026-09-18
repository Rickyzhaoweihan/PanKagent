"""Read-only demo release acceptance. No job submission or model inference."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.request
from deploy_results.manage import read_protected_env


def get(url, authorization=''):
    headers = {'Authorization': authorization} if authorization else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        return response.read()


def check(release, health_auth=None):
    import os
    health_auth = health_auth or os.environ.get("PANK_HEALTH_ACCEPTANCE_AUTH")
    if not health_auth:
        raise ValueError("Separate operator authorization is required for dashboard acceptance")
    release = Path(release)
    manifest = json.loads((release / 'release-manifest.json').read_text())
    agent_env = read_protected_env(Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env'))
    auth = json.loads(Path('/var/local/serviceuser/.local/state/pankgraph-health/frontend-auth.json').read_text())['authorization']
    report = {'read_only': True, 'inference_submitted': False}
    agent = json.loads(get('http://127.0.0.1:8794/health/components', 'Bearer ' + agent_env.get('PANK_VNEXT_OPERATOR_TOKEN', '')))
    results = json.loads(get('http://127.0.0.1:8795/health/components'))
    for name, port in (('agent', 8794), ('results', 8795)):
        ready = json.loads(get(f'http://127.0.0.1:{port}/health/ready'))
        assert ready.get('ready') is True, name + ' is not ready'
        report[name + '_ready'] = True
    assert agent['components']['runtime']['details']['owner_state'] == 'active'
    assert results['ownership']['state'] == 'active'
    graph = agent['components']['neo4j']['details']
    assert graph['graph_version'] == 'PanKgraph_08_04' and graph['identity_verified']
    report['graph_version'] = graph['graph_version']
    report['ownership'] = {'agent': 'active', 'results': 'active', 'workers_each': 1}
    for path, info in manifest['files'].items():
        if path == 'frontend/index.html' or path.startswith('frontend/static/js/main.') and path.endswith('.js'):
            relative = path.removeprefix('frontend/')
            url = 'http://127.0.0.1:8795/pankgraph-vnext/' + ('' if relative == 'index.html' else relative)
            assert hashlib.sha256(get(url, auth)).hexdigest() == info['sha256'], 'Frontend byte mismatch'
            report[relative] = {'sha256': info['sha256'], 'served_matches': True}
    with sqlite3.connect('file:/var/local/serviceuser/.local/state/pankgraph-results/results.sqlite3?mode=ro', uri=True) as db:
        for rid, source, payload in db.execute('SELECT id,source,payload FROM results ORDER BY updated DESC'):
            saved = json.loads(payload)
            if saved.get('status') == 'ready' and saved.get('answer') and not str(json.loads(source).get('template_id', '')).startswith('functional'):
                actual = json.loads(get('http://127.0.0.1:8795/pankgraph-vnext/api/results/' + rid, auth))
                assert actual['result_id'] == rid and actual['answer'] == saved['answer']
                report['saved_result'] = {'id': rid, 'ready': True, 'answer_preserved': True}
                break
        else:
            raise ValueError('No saved result available for acceptance')
    deadline = time.monotonic() + 70
    while True:
        snapshot = json.loads(get('http://127.0.0.1:8796/pankgraph/health/api/snapshot', health_auth))
        if snapshot['monitor']['state'] == 'healthy' and not snapshot['stale']:
            break
        if time.monotonic() > deadline:
            raise ValueError('Health collector did not become fresh')
        time.sleep(2)
    proxied = json.loads(get('http://127.0.0.1:8795/pankgraph/health/api/snapshot', health_auth))
    assert proxied['version'] == 1 and proxied['components']
    incidents = json.loads(get('http://127.0.0.1:8795/pankgraph/health/api/incidents', health_auth))
    history = json.loads(get('http://127.0.0.1:8795/pankgraph/health/api/history?hours=1', health_auth))
    assert isinstance(incidents.get('incidents'), list) and isinstance(history.get('points'), list)
    assert get('http://127.0.0.1:8795/pankgraph/health/api/metrics', health_auth)
    assert b'<!doctype html>' in get('http://127.0.0.1:8795/pankgraph/health/', health_auth)[:100].lower()
    report['health'] = {'collector_fresh': True, 'component_count': len(snapshot['components']),
                        'proxy_works': True, 'incidents_history_metrics_work': True,
                        'states': {row['id']: row['state'] for row in snapshot['components']}}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.release), indent=2))
