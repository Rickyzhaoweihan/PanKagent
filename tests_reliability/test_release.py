import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from deploy_reliability.release import MANIFEST, PROJECT_FILES, UX_AUDIT_FILES, build, canonical, verify


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = self.root / "backend"
        self.frontend = self.root / "ui" / "build"
        self.backend.mkdir()
        for name in PROJECT_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Public acceptance harness fixture\n")
        for name in UX_AUDIT_FILES:
            path = self.backend / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Public UX audit fixture\n")
        self.frontend.mkdir(parents=True)
        (self.frontend.parent / "src").mkdir()
        (self.frontend.parent / "src/index.js").write_text('console.log("fixture");')
        (self.frontend.parent / "package.json").write_text('{"scripts":{"build":"react-scripts build"}}')
        (self.frontend.parent / "package-lock.json").write_text('{"lockfileVersion":3}')
        for package in ("pankagent_vnext", "pankgraph_results", "pankgraph_health"):
            folder = self.backend / package
            folder.mkdir()
            (folder / "__init__.py").write_text('"""Package."""\n')
        (self.backend / "pankagent_vnext" / "release_schema.json").write_text('{"version":"fixture"}')
        (self.backend / "requirements-vnext.lock").write_text('example==1.0\n')
        (self.frontend / "index.html").write_text('<script src="main.js"></script>')
        (self.frontend / "main.js").write_text('window.test = true;')
        subprocess.run(["git", "init", "-q", str(self.backend)], check=True)
        self.git("add", ".")
        self.git("-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.backend), *args], check=True, stdout=subprocess.PIPE).stdout

    def make(self, name="release.tar.gz", runtime=None):
        output = self.root / name
        report = build(self.backend, self.frontend, output, runtime)
        return output, report

    def rewrite(self, original, mutate):
        with tarfile.open(original, 'r:gz') as source:
            items = [(member, source.extractfile(member).read()) for member in source]
        items = mutate(items)
        target = self.root / 'modified.tar.gz'
        with target.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for member, data in items:
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
        return target

    def test_same_snapshot_builds_identical_bytes_and_records_dirty_fingerprints(self):
        changed = self.backend / "pankagent_vnext" / "__init__.py"
        changed.write_text('"""Changed."""\n')
        added = self.backend / "pankagent_vnext" / "persistence.py"
        added.write_text('VERSION = 1\n')
        first, report = self.make()
        os.utime(changed, (100000, 100000))
        second, other = self.make("second.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(report['archive_sha256'], other['archive_sha256'])
        source = report['manifest']['source']['backend']
        self.assertEqual(source['commit'], self.git('rev-parse', 'HEAD').decode().strip())
        self.assertTrue(source['dirty'])
        dirty = {row['path']: row for row in source['dirty_files']}
        self.assertEqual(dirty['pankagent_vnext/__init__.py']['sha256'], hashlib.sha256(changed.read_bytes()).hexdigest())
        self.assertIn('pankagent_vnext/persistence.py', dirty)
        self.assertIn('backend/pankagent_vnext/release_schema.json', report['manifest']['schema_manifests'])
        self.assertTrue(verify(first, report['archive_sha256'])['verified'])
        with self.assertRaisesRegex(ValueError, 'Archive SHA-256 mismatch'):
            verify(first, '0' * 64)

    def test_only_explicit_source_and_build_inputs_are_packaged(self):
        for name in ('pankagent_vnext/.env', 'pankagent_vnext/runtime.env', 'pankagent_vnext/._file.py',
                     'pankagent_vnext/state/private.json', 'pankagent_vnext/__pycache__/app.pyc',
                     'pankagent_vnext/data/donors.json', 'pankagent_vnext/access.txt', 'main.py',
                     'unrelated/secret.py', 'ux_audit/unlisted.py', 'ux_audit/results.json', 'ux_audit/data/outbound.json'):
            path = self.backend / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('DO NOT PACKAGE')
        (self.frontend / '.env').write_text('PRIVATE')
        (self.frontend / 'guide.pdf').write_bytes(b'%PDF-fixture')
        (self.frontend / 'help.md').write_text('Public help')
        (self.frontend.parent / 'src/style.scss').write_text('.app { color: teal; }')
        archive, report = self.make()
        names = report['manifest']['files']
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                self.assertNotIn(b'DO NOT PACKAGE', source.extractfile(member).read())
                self.assertNotIn('.env', member.name)
        self.assertNotIn('backend/main.py', names)
        self.assertIn('frontend/main.js', names)
        for name in PROJECT_FILES:
            self.assertIn(name, names)
            self.assertEqual(names[name]["sha256"], hashlib.sha256((self.root / name).read_bytes()).hexdigest())
        self.assertIn('frontend-source/src/index.js', names)
        self.assertIn('frontend-source/package-lock.json', names)
        self.assertIn('frontend/guide.pdf', names)
        self.assertIn('frontend/help.md', names)
        self.assertIn('frontend-source/src/style.scss', names)
        self.assertEqual({name for name in names if name.startswith('backend/ux_audit/')}, {'backend/' + name for name in UX_AUDIT_FILES})
        self.assertGreater(report['manifest']['source']['backend']['unpackaged_dirty_entries'], 0)

    def test_project_harness_is_required_and_no_other_project_docs_are_included(self):
        other = self.root / "docs/unrelated.py"
        other.write_text("PRIVATE DO NOT PACKAGE")
        _, report = self.make()
        self.assertNotIn("docs/unrelated.py", report["manifest"]["files"])
        harness = self.root / next(iter(PROJECT_FILES))
        harness.unlink()
        with self.assertRaisesRegex(ValueError, "Required release inputs"):
            self.make("missing.tar.gz")

    def test_project_harness_directory_symlink_is_rejected(self):
        docs = self.root / "docs"
        target = self.root / "moved-docs"
        docs.rename(target)
        docs.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.make()

    def test_symlink_input_is_rejected_without_following_it(self):
        private = self.root / 'private.py'
        private.write_text('private')
        (self.backend / 'pankagent_vnext' / 'linked.py').symlink_to(private)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.make()

    def test_file_tampering_and_manifest_omissions_are_rejected(self):
        original, _ = self.make()
        def change_file(items):
            return [(m, b'tampered' if m.name == 'frontend/main.js' else d) for m, d in items]
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            verify(self.rewrite(original, change_file))
        def omit_file(items):
            return [(m, d) for m, d in items if m.name != 'frontend/main.js']
        with self.assertRaisesRegex(ValueError, 'file set'):
            verify(self.rewrite(original, omit_file))

    def test_unsafe_duplicate_and_unexpected_archive_entries_are_rejected(self):
        original, _ = self.make()
        for name in ('../escape.py', '/tmp/escape.py', 'backend/../escape.py', 'backend/pankagent_vnext/.env',
                     'backend/unrelated.py', 'frontend/../x.js', 'frontend/state/private.json',
                     'frontend//main.js', 'frontend/main.js', 'docs/unrelated.py', 'backend/ux_audit/unlisted.py', 'backend/ux_audit/results.json'):
            with self.subTest(name=name):
                def add(items):
                    entry = tarfile.TarInfo(name)
                    entry.mode = 0o644
                    return [*items, (entry, b'x')]
                with self.assertRaisesRegex(ValueError, 'unsafe archive entry'):
                    verify(self.rewrite(original, add))

    def test_archive_symlinks_are_rejected_even_with_allowed_name(self):
        original, _ = self.make()
        def link(items):
            entry = tarfile.TarInfo('backend/pankagent_vnext/new.py')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/etc/passwd'
            return [*items, (entry, b'')]
        with self.assertRaisesRegex(ValueError, 'unsafe archive entry'):
            verify(self.rewrite(original, link))

    def test_runtime_inventory_is_explicit_allowlisted_metadata(self):
        path = self.root / 'inventory.json'
        path.write_text(json.dumps({'runtime': {'python': '3.13.12', 'packages': {'fastapi': '0.1'},
                                               'archive_sha256': 'a'*64, 'base_prefix': '/opt/python',
                                               'replay_boundary': 'Same ABI and architecture', 'platform': 'Linux',
                                               'archive_path': '/private/file.tar.gz'},
                                    'secret': 'DO NOT INCLUDE'}))
        _, report = self.make(runtime=path)
        self.assertEqual(report['manifest']['runtime']['packages'], {'fastapi': '0.1'})
        self.assertNotIn('DO NOT INCLUDE', json.dumps(report))
        self.assertEqual(report['manifest']['runtime']['state'], 'supplied_observation')
        self.assertEqual(report['manifest']['runtime']['archive_sha256'], 'a'*64)
        self.assertEqual(report['manifest']['runtime']['replay_boundary'], 'Same ABI and architecture')
        self.assertNotIn('archive_path', report['manifest']['runtime'])

    def test_manifest_source_and_schema_provenance_are_validated(self):
        original, _ = self.make()
        for alteration in ("source", "schema"):
            with self.subTest(alteration=alteration):
                def alter(items):
                    result = []
                    for m, d in items:
                        if m.name == MANIFEST:
                            manifest = json.loads(d)
                            if alteration == "source":
                                manifest["source"] = {}
                            else:
                                manifest["schema_manifests"]["backend/pankagent_vnext/release_schema.json"]["sha256"] = "0" * 64
                            d = canonical(manifest)
                        result.append((m, d))
                    return result
                with self.assertRaises(ValueError):
                    verify(self.rewrite(original, alter))

    def test_deleted_source_file_has_a_null_fingerprint(self):
        (self.backend / "pankagent_vnext" / "release_schema.json").unlink()
        _, report = self.make()
        deleted = report["manifest"]["source"]["backend"]["dirty_files"][0]
        self.assertEqual(deleted["path"], "pankagent_vnext/release_schema.json")
        self.assertIn("D", deleted["status"])
        self.assertIsNone(deleted["sha256"])

    def test_required_payload_cannot_be_removed_even_with_updated_manifest(self):
        original, _ = self.make()
        def remove(items):
            output = []
            for m, d in items:
                if m.name == 'frontend/index.html':
                    continue
                if m.name == MANIFEST:
                    manifest = json.loads(d)
                    manifest['files'].pop('frontend/index.html')
                    d = canonical(manifest)
                output.append((m, d))
            return output
        with self.assertRaisesRegex(ValueError, 'Required release entries'):
            verify(self.rewrite(original, remove))


if __name__ == '__main__':
    unittest.main()
