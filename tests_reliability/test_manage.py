"""Process-control regressions without inspecting or signaling real processes."""
import contextlib
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy_reliability import manage


class OwnedTests(unittest.TestCase):
    def setUp(self):
        self.release = Path('/fixture/release/backend')
        self.uid = 1234
        self.process_uid = self.uid
        self.cwd = self.release
        self.start = '98765'
        self.argv = ['python', '-m', 'uvicorn', 'pankagent_vnext.app:create_app', '--factory',
                     '--host', '127.0.0.1', '--port', '8794', '--workers', '1']
        self.record = {'pid': 2468, 'uid': self.uid, 'start': self.start,
                       'service': 'agent', 'release': str(self.release)}
        owner = self
        class Proc:
            def __init__(self, child=''):
                self.child = child
            def __truediv__(self, child):
                return Proc(child)
            def stat(self):
                return SimpleNamespace(st_uid=owner.process_uid)
            def resolve(self):
                return owner.cwd
            def read_bytes(self):
                return ('\0'.join(owner.argv) + '\0').encode()
        self.proc = Proc()

    def check(self, record=None, service='agent'):
        with patch.object(manage, 'Path', side_effect=lambda *parts: self.proc if parts[0] == '/proc' else Path(*parts)), \
             patch.object(manage.os, 'geteuid', return_value=self.uid), \
             patch.object(manage, 'process_start', return_value=self.start):
            return manage.owned(self.record if record is None else record, service, self.release)

    def test_exact_owned_process_matches(self):
        self.assertTrue(self.check())

    def test_wrong_record_identity_never_matches(self):
        for changes in ({'pid': 1}, {'pid': 'invalid'}, {'uid': 999}, {'start': 'recycled'},
                        {'service': 'results'}, {'release': '/different/release'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.check({**self.record, **changes}))
        self.assertFalse(self.check({}, 'agent'))
        self.assertFalse(self.check(service='results'))

    def test_wrong_process_owner_or_directory_never_matches(self):
        self.process_uid = 999
        self.assertFalse(self.check())
        self.process_uid = self.uid
        self.cwd = Path('/other/worktree')
        self.assertFalse(self.check())

    def test_wrong_module_port_host_and_substring_lookalikes_never_match(self):
        original = list(self.argv)
        for find, replacement in (('pankagent_vnext.app:create_app', 'pankgraph_results.app:create_app'),
                                  ('pankagent_vnext.app:create_app', 'prefix-pankagent_vnext.app:create_app'),
                                  ('8794', '8795'), ('127.0.0.1', '0.0.0.0'), ('uvicorn', 'not-uvicorn')):
            with self.subTest(replacement=replacement):
                self.argv = [replacement if value == find else value for value in original]
                self.assertFalse(self.check())
        self.argv = ['python', 'pankagent_vnext.app:create_app', 'uvicorn', '--port']
        self.assertFalse(self.check())

    def test_missing_process_is_not_owned(self):
        with patch.object(manage, 'Path', side_effect=OSError('gone')):
            self.assertFalse(manage.owned(self.record, 'agent', self.release))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.release = self.root / 'release' / 'backend'
        self.release.mkdir(parents=True)
        for module in ('pankagent_vnext', 'pankgraph_results'):
            (self.release / module).mkdir()
            (self.release / module / 'app.py').write_text('# fixture\n')
        (self.release.parent / 'frontend').mkdir()
        (self.release.parent / 'frontend' / 'index.html').write_text('fixture')
        self.state = self.root / 'state'
        for name in ('pankagent-vnext', 'pankgraph-results'):
            path = self.state / name
            path.mkdir(parents=True, mode=0o700)
            path.chmod(0o700)
        self.pidfile = self.state / 'pankagent-vnext' / 'release-agent.pid.json'
        self.record = {'pid': 2468, 'uid': 1234, 'start': '98765', 'service': 'agent', 'release': str(self.release)}

    def invoke(self, action, owned=False, socket_error=None, service='agent'):
        def mapped_path(*parts):
            if parts and parts[0] == '/var/local/serviceuser/.local/state':
                return self.state.joinpath(*parts[1:])
            return Path(*parts)
        def protected(path, *args):
            if 'pankgraph-results' in str(path):
                return {'PANK_RESULTS_STATE_DIR': str(self.state / 'pankgraph-results')}
            return {'PANK_VNEXT_STATE_DIR': str(self.state / 'pankagent-vnext')}
        child = Mock(pid=5678)
        child.poll.return_value = None
        sock = Mock()
        sock.bind.side_effect = socket_error
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(manage, 'Path', side_effect=mapped_path))
            stack.enter_context(patch.object(manage.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='serviceuser')))
            stack.enter_context(patch.object(manage, 'read_protected_env', side_effect=protected))
            owner_mock = stack.enter_context(patch.object(manage, 'owned', **({'side_effect': owned} if isinstance(owned, list) else {'return_value': owned})))
            stack.enter_context(patch.object(manage.os, 'umask'))
            kill = stack.enter_context(patch.object(manage.os, 'kill'))
            popen = stack.enter_context(patch.object(manage.subprocess, 'Popen', return_value=child))
            write_pid = stack.enter_context(patch.object(manage, 'write_pid'))
            stack.enter_context(patch.object(manage, 'process_start', return_value='new-start'))
            stack.enter_context(patch.object(manage.time, 'sleep'))
            socket = stack.enter_context(patch.object(manage.socket, 'socket'))
            socket.return_value.__enter__.return_value = sock
            stack.enter_context(patch('sys.argv', ['manage', service, action, '--release', str(self.release)]))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    manage.main()
                    error = None
                except (ValueError, OSError) as exc:
                    error = exc
        return SimpleNamespace(kill=kill, popen=popen, write_pid=write_pid, owner=owner_mock, error=error, output=output.getvalue())

    def test_unknown_or_wrong_owner_record_is_never_signaled(self):
        self.pidfile.write_text(json.dumps({**self.record, 'service': 'results'}))
        result = self.invoke('stop', owned=False)
        self.assertIsInstance(result.error, ValueError)
        self.assertIn('PID ownership mismatch', str(result.error))
        result.kill.assert_not_called()
        result.popen.assert_not_called()
        self.assertTrue(self.pidfile.exists())

    def test_owned_stop_signals_only_recorded_pid_with_sigterm(self):
        self.pidfile.write_text(json.dumps(self.record))
        result = self.invoke('stop', owned=[True, False])
        self.assertIsNone(result.error)
        result.kill.assert_called_once_with(2468, signal.SIGTERM)
        result.popen.assert_not_called()
        self.assertFalse(self.pidfile.exists())

    def test_unresponsive_owned_stop_never_escalates_to_sigkill(self):
        self.pidfile.write_text(json.dumps(self.record))
        result = self.invoke('stop', owned=True)
        self.assertIsInstance(result.error, ValueError)
        result.kill.assert_called_once_with(2468, signal.SIGTERM)
        self.assertIn('no forced kill', str(result.error))
        self.assertTrue(self.pidfile.exists())

    def test_start_binds_only_expected_service_with_one_worker(self):
        for service, port, entry in (('agent', '8794', 'pankagent_vnext.app:create_app'),
                                     ('results', '8795', 'pankgraph_results.app:create_app')):
            with self.subTest(service=service):
                result = self.invoke('start', service=service)
                self.assertIsNone(result.error)
                result.kill.assert_not_called()
                argv = result.popen.call_args.args[0]
                self.assertEqual(argv[argv.index('--workers') + 1], '1')
                self.assertEqual(argv[argv.index('--port') + 1], port)
                self.assertEqual(argv[argv.index('--host') + 1], '127.0.0.1')
                self.assertIn(entry, argv)
                self.assertIn('--no-proxy-headers', argv)
                self.assertEqual(result.popen.call_args.kwargs['cwd'], self.release)
                record = result.write_pid.call_args.args[1]
                self.assertEqual(record['service'], service)
                self.assertEqual(record['pid'], 5678)
                self.assertEqual(record['start'], 'new-start')

    def test_busy_port_does_not_start_or_signal_any_process(self):
        result = self.invoke('start', socket_error=OSError('busy'))
        self.assertIsInstance(result.error, OSError)
        result.popen.assert_not_called()
        result.kill.assert_not_called()
        result.write_pid.assert_not_called()

    def test_only_results_prefers_its_owned_runtime_executables(self):
        original = '/fixture/original/bin'
        with patch.dict(manage.os.environ, {'PATH': original}):
            results = self.invoke('start', service='results')
            agent = self.invoke('start', service='agent')
            self.assertIsNone(results.error)
            self.assertIsNone(agent.error)
            self.assertEqual(results.popen.call_args.kwargs['env']['PATH'],
                             str(self.release / '.venv/bin') + manage.os.pathsep + original)
            self.assertEqual(agent.popen.call_args.kwargs['env']['PATH'], original)
            self.assertEqual(manage.os.environ['PATH'], original)

    def test_already_owned_start_is_idempotent(self):
        self.pidfile.write_text(json.dumps(self.record))
        result = self.invoke('start', owned=True)
        self.assertIsNone(result.error)
        result.popen.assert_not_called()
        result.kill.assert_not_called()
        self.assertEqual(json.loads(result.output)['already_running'], 'agent')


if __name__ == '__main__':
    unittest.main()
