"""Private, bounded SQLite backups and restoration drills; never overwrite live state."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import time

STATE = Path('/var/local/serviceuser/.local/state')
CONFIG = Path('/var/local/serviceuser/.config')
DATABASES = ('pankagent-vnext/sessions.sqlite3', 'pankagent-vnext/budget.sqlite3',
             'pankgraph-results/results.sqlite3', 'pankgraph-results/resources/associations.sqlite3')
OPTIONAL = ('pankgraph-health/health.sqlite3',)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def logical_database(path):
    """Verify all rows including reservations without exposing their contents."""
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity check failed')
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            rows = []
            for row in db.execute('SELECT * FROM ' + quoted):
                normalized = [dict(blob=x.hex()) if isinstance(x, bytes) else x for x in row]
                rows.append(hashlib.sha256(json.dumps(normalized, ensure_ascii=True, separators=(',', ':')).encode()).digest())
            tables[name] = {'rows': len(rows), 'sha256': hashlib.sha256(b''.join(sorted(rows))).hexdigest()}
        return {'schema_sha256': hashlib.sha256(json.dumps(schema).encode()).hexdigest(), 'tables': tables}


def private_copy(source, target):
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Backup source must be a regular file')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def assert_stopped():
    for port in (8794, 8795, 8796):
        with socket.socket() as probe:
            probe.settimeout(.3)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('Quiescent snapshot requires all owned demo services stopped')


def snapshot(destination, state=STATE, config=CONFIG, quiescent=False):
    destination, state, config = Path(destination), Path(state), Path(config)
    if quiescent:
        assert_stopped()
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    databases = {}
    for name in DATABASES + OPTIONAL:
        source = state / name
        if name in OPTIONAL and not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError('Expected active database missing or symlinked')
        target = destination / 'state' / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        deadline = time.monotonic() + 60
        def progress(*_):
            if time.monotonic() > deadline:
                raise TimeoutError('SQLite backup deadline exceeded')
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=.05)
        target.chmod(0o600)
        databases['state/' + name] = logical_database(target)
    assets = state / 'pankgraph-results/resources/assets'
    if assets.exists():
        for source in sorted(assets.rglob('*')):
            if source.is_symlink():
                raise ValueError('Asset symlink is not supported')
            if source.is_file():
                private_copy(source, destination / 'state' / source.relative_to(state))
    for name in ('pankagent-vnext/runtime.env', 'pankgraph-results/runtime.env'):
        private_copy(config / name, destination / 'config' / name)
    for name in ('frontend-auth.json', 'operator-auth.json'):
        auth = state / 'pankgraph-health' / name
        if auth.exists():
            private_copy(auth, destination / 'state/pankgraph-health' / name)
    files = {str(p.relative_to(destination)): digest(p) for p in sorted(destination.rglob('*')) if p.is_file()}
    manifest = {'version': 1, 'consistency': 'quiescent' if quiescent else 'per-database-online',
                'files': files, 'databases': databases}
    (destination / 'backup-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Backup directory may not be a symlink')
    manifest = json.loads((directory / 'backup-manifest.json').read_text())
    expected = manifest['files']
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('Backup symlink rejected')
        if path.is_file() and path.name != 'backup-manifest.json':
            actual.add(str(path.relative_to(directory)))
    if actual != set(expected):
        raise ValueError('Backup file inventory mismatch')
    for name, wanted in expected.items():
        if Path(name).is_absolute() or '..' in Path(name).parts or digest(directory / name) != wanted:
            raise ValueError('Backup hash mismatch or invalid path')
    for name, wanted in manifest['databases'].items():
        if name not in expected or logical_database(directory / name) != wanted:
            raise ValueError('Restored database contents differ')
    return manifest


def restore_drill(source, destination):
    """Restore to a NEW directory only. Promotion to live stores is deliberately absent."""
    source, destination = Path(source), Path(destination)
    original = verify(source)
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in original['files']:
        private_copy(source / name, destination / name)
    private_copy(source / 'backup-manifest.json', destination / 'backup-manifest.json')
    restored = verify(destination)
    return {'verified': original == restored, 'consistency': original['consistency'],
            'database_count': len(original['databases']), 'file_count': len(original['files']),
            'tables': {name: {table: value['rows'] for table, value in data['tables'].items()}
                       for name, data in original['databases'].items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'verify', 'restore-drill'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--quiescent', action='store_true')
    args = parser.parse_args()
    if args.action == 'snapshot':
        result = snapshot(args.path, quiescent=args.quiescent)
        print(json.dumps({'created': True, 'databases': len(result['databases']), 'consistency': result['consistency']}))
    elif args.action == 'verify':
        result = verify(args.path)
        print(json.dumps({'verified': True, 'databases': len(result['databases'])}))
    else:
        if not args.destination:
            parser.error('--destination is required')
        print(json.dumps(restore_drill(args.path, args.destination)))

if __name__ == '__main__':
    main()
