"""Freeze exact installed demo dependencies for offline replay on the same Python ABI."""
import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tarfile
import io


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def freeze(output):
    root = Path(sys.prefix)
    if root == Path(sys.base_prefix):
        raise ValueError('Run with the existing demo virtualenv Python')
    files, links = {}, {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if '__pycache__' in relative.parts or path.name.startswith('._') or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            target = os.readlink(path)
            if path.resolve().is_file():
                links[str(relative)] = {'target': target, 'resolved_sha256': digest(path)}
            elif path.resolve() == root / 'lib':
                links[str(relative)] = {'target': target}
            else:
                raise ValueError('Unexpected runtime symlink')
        elif path.is_file():
            files[str(relative)] = digest(path)
    manifest = {'version': 1, 'python': sys.version, 'base_prefix': sys.base_prefix,
                'platform': platform.platform(), 'machine': platform.machine(),
                'packages': dict(sorted((d.metadata['Name'], d.version) for d in importlib.metadata.distributions())),
                'files': files, 'links': links,
                'replay_boundary': 'Same OS architecture and existing base Python binary; symlink target hashes must match.'}
    with Path(output).open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as gz:
            with tarfile.open(fileobj=gz, mode='w') as archive:
                for name in sorted(files):
                    path = root / name
                    entry = tarfile.TarInfo('venv/' + name)
                    entry.size = path.stat().st_size
                    entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with path.open('rb') as content:
                        archive.addfile(entry, content)
                for name, data in sorted(links.items()):
                    entry = tarfile.TarInfo('venv/' + name)
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = data['target']
                    entry.mode = 0o777
                    archive.addfile(entry)
                data = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
                entry = tarfile.TarInfo('runtime-manifest.json')
                entry.size = len(data)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(data))
    return {'sha256': digest(output), 'files': len(files), 'packages': manifest['packages'],
            'python': sys.version, 'platform': manifest['platform'], 'base_prefix': sys.base_prefix}


def verify(root):
    root = Path(root)
    if root.is_symlink() or (root / 'runtime-manifest.json').is_symlink():
        raise ValueError('Runtime manifest/root may not be a symlink')
    manifest = json.loads((root / 'runtime-manifest.json').read_text())
    if (sys.version != manifest['python'] or platform.machine() != manifest['machine'] or
            platform.platform() != manifest['platform'] or sys.base_prefix != manifest['base_prefix']):
        raise ValueError('Runtime Python/architecture differs')
    expected_names = set(manifest['files']) | set(manifest['links'])
    for name in expected_names:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name:
            raise ValueError('Invalid runtime manifest path')
    actual_names = set()
    for path in (root / 'venv').rglob('*'):
        relative = path.relative_to(root / 'venv')
        if path.is_symlink():
            actual_names.add(str(relative))
        elif path.is_file():
            if '__pycache__' in relative.parts and path.suffix == '.pyc':
                # Interpreter-created caches are not release input. No source-less modules.
                original = path.parent.parent / (path.name.split('.')[0] + '.py')
                if not original.is_file():
                    raise ValueError('Unexpected source-less bytecode')
                continue
            actual_names.add(str(relative))
    if actual_names != expected_names or set(manifest['files']) & set(manifest['links']):
        raise ValueError('Frozen runtime inventory differs')
    for name, expected in manifest['files'].items():
        path = root / 'venv' / name
        if path.is_symlink() or digest(path) != expected:
            raise ValueError('Frozen runtime file differs')
        if path.suffix == '.egg-link':
            raise ValueError('Editable external dependencies cannot be replayed')
        if path.suffix == '.pth':
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith(('#', 'import ', 'import\t')):
                    target = (path.parent / line).resolve()
                    if not target.is_relative_to((root / 'venv').resolve()):
                        raise ValueError('External dependency search path cannot be replayed')
    for name, data in manifest['links'].items():
        path = root / 'venv' / name
        if os.readlink(path) != data['target'] or ('resolved_sha256' in data and digest(path) != data['resolved_sha256']):
            raise ValueError('Frozen base interpreter/link differs')
    return {'verified': True, 'files': len(manifest['files']), 'packages': len(manifest['packages'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'verify'))
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.path) if args.action == 'freeze' else verify(args.path)))
