import json
import platform
import sys
import pytest
from deploy_reliability.freeze_runtime import digest, verify


def runtime(tmp_path):
    root = tmp_path / 'runtime'
    package = root / 'venv/lib/example.py'
    package.parent.mkdir(parents=True)
    package.write_text('VERSION = 1\n')
    manifest = {'python': sys.version, 'base_prefix': sys.base_prefix, 'machine': platform.machine(),
                'platform': platform.platform(), 'files': {'lib/example.py': digest(package)}, 'links': {}, 'packages': {}}
    (root / 'runtime-manifest.json').write_text(json.dumps(manifest))
    return root


def test_verify_exact_inventory_and_reject_extra(tmp_path):
    root = runtime(tmp_path)
    assert verify(root)['verified']
    (root / 'venv/lib/injected.py').write_text('unexpected')
    with pytest.raises(ValueError, match='inventory'):
        verify(root)


def test_reject_manifest_traversal_before_reading(tmp_path):
    root = runtime(tmp_path)
    path = root / 'runtime-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files']['../../outside'] = 'not-read'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='manifest path'):
        verify(root)


def test_reject_platform_drift_and_unexpected_symlink(tmp_path):
    root = runtime(tmp_path)
    (root / 'venv/lib/link').symlink_to('/tmp')
    with pytest.raises(ValueError, match='inventory'):
        verify(root)
    (root / 'venv/lib/link').unlink()
    path = root / 'runtime-manifest.json'
    manifest = json.loads(path.read_text()); manifest['platform'] = 'different platform'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='architecture'):
        verify(root)
