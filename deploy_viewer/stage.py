"""Stage narrow viewer overlays on separately owned immutable dev releases."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile


def replace_once(text, old, new):
    if old not in text and text.count(new) == 1:
        return text
    if text.count(old) != 1:
        raise ValueError('Deployed source does not match the reviewed overlay')
    return text.replace(old, new)


def results_overlay(root):
    path = root / 'pankgraph_results/app.py'
    text = path.read_text()
    for old, new in [
        ('async def load_run(self, run_id):', 'async def load_run(self, run_id, phase="final"):'),
        ('self.settings.agent_url + "/v2/runs/" + str(run_id))',
         'self.settings.agent_url + "/v2/runs/" + str(run_id) + "/graph?phase=" + phase)'),
        ('await self.load_run(body.run_id), body.phase', 'await self.load_run(body.run_id, body.phase), body.phase'),
    ]:
        text = replace_once(text, old, new)
    path.write_text(text)
    path = root / 'pankgraph_results/assembly.py'
    path.write_text(replace_once(path.read_text(),
        '"completeness": evidence.get("completeness", "unknown"),',
        '"completeness": "unavailable" if evidence.get("viewer", {}).get("status") == "unavailable" else evidence.get("completeness", "unknown"),\n        "graph_evidence": evidence.get("viewer", {}),'))


def stage(archive, commit):
    os.umask(0o077)
    state = Path('/var/local/serviceuser/.local/state')
    releases = Path('/var/local/serviceuser/projects/pankgraph-demo/releases')
    report = {'commit': commit, 'services': {}}
    for service, name in [('agent', 'pankagent-vnext'), ('results', 'pankgraph-results')]:
        owner = json.loads((state / name / f'release-{service}.pid.json').read_text())
        old = Path(owner['release'])
        root = releases / ('20260923-viewer-' + commit[:8] + '-' + service) / 'backend'
        if root.exists():
            raise ValueError('Immutable release already exists')
        shutil.copytree(old, root, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
        (root.parent / 'frontend').symlink_to(old.parent / 'frontend', target_is_directory=True)
        if service == 'agent':
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    if member.name not in {'pankagent_vnext/app.py', 'pankagent_vnext/graph.py', 'pankagent_vnext/viewer_evidence.py'} or not member.isfile():
                        raise ValueError('Unexpected overlay member')
                bundle.extractall(root, filter='data')
        else:
            results_overlay(root)
        files = ['pankagent_vnext/app.py', 'pankagent_vnext/graph.py', 'pankagent_vnext/viewer_evidence.py'] if service == 'agent' else ['pankgraph_results/app.py', 'pankgraph_results/assembly.py']
        entry = {'old_owner': owner, 'release': str(root), 'files': {
            f: hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
        (root / 'viewer-overlay.json').write_text(json.dumps({'commit': commit, **entry}, indent=2))
        report['services'][service] = entry
    return report


if __name__ == '__main__':
    report = stage(Path(sys.argv[1]), sys.argv[2])
    Path(sys.argv[3]).write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
