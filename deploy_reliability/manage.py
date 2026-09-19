"""Manage one owned demo service from an immutable unified release, workers=1."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import subprocess
import time
from deploy_results.manage import read_protected_env, process_start, write_pid

SPECS = {'agent': ('pankagent_vnext.app:create_app', 8794, 'pankagent-vnext'),
         'results': ('pankgraph_results.app:create_app', 8795, 'pankgraph-results')}


def owned(record, service, release):
    try:
        entry, port, _ = SPECS[service]
        pid = int(record['pid'])
        proc = Path('/proc', str(pid))
        argv = (proc / 'cmdline').read_bytes().decode().split('\0')
        return (pid > 1 and record['uid'] == os.geteuid() == proc.stat().st_uid and
                record['start'] == process_start(pid) and record['service'] == service and
                record['release'] == str(release.resolve()) and
                (proc / 'cwd').resolve() == release.resolve() and
                entry in argv and 'uvicorn' in argv and
                argv[argv.index('--port') + 1] == str(port) and
                argv[argv.index('--host') + 1] == '127.0.0.1')
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service', choices=SPECS)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--release', type=Path, required=True)
    args = parser.parse_args()
    if pwd.getpwuid(os.geteuid()).pw_name != 'serviceuser':
        raise ValueError('Run only as serviceuser')
    os.umask(0o077)
    release = args.release.resolve()
    shared = read_protected_env(Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env'))
    result = read_protected_env(Path('/var/local/serviceuser/.config/pankgraph-results/runtime.env'), 'PANK_RESULTS_')
    entry, port, state_name = SPECS[args.service]
    env = {**os.environ, **shared, **result}
    expected_state = Path('/var/local/serviceuser/.local/state') / state_name
    state_key = 'PANK_VNEXT_STATE_DIR' if args.service == 'agent' else 'PANK_RESULTS_STATE_DIR'
    if Path(env[state_key]).resolve() != expected_state or expected_state.is_symlink():
        raise ValueError('Unexpected demo state directory')
    # Only the new release receives this override; prior deployments/config remain intact.
    env['PANK_RESULTS_FRONTEND_DIR'] = str(release.parent / 'frontend')
    env['PANK_VNEXT_PORT'] = '8794'
    env['PANK_RESULTS_PORT'] = '8795'
    if args.service == 'results':
        # The frozen results runtime owns dot as well as its Python bindings.
        env['PATH'] = str(release / '.venv/bin') + os.pathsep + env.get('PATH', os.defpath)
    state = expected_state
    if state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise ValueError('Demo state must be owner-only')
    pidfile = state / ('release-' + args.service + '.pid.json')
    with (state / ('release-' + args.service + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(pidfile.read_text()) if pidfile.exists() else None
        running = owned(record, args.service, release)
        if args.action == 'status':
            print(json.dumps({'running': running, 'port': port, 'pid': record.get('pid') if record else None}))
            return
        if args.action == 'stop':
            if record and not running:
                raise ValueError('PID ownership mismatch; no process signaled')
            if running:
                os.kill(record['pid'], signal.SIGTERM)
                for _ in range(200):
                    if not owned(record, args.service, release):
                        break
                    time.sleep(.1)
                else:
                    raise ValueError('Owned process has not exited; no forced kill attempted')
            pidfile.unlink(missing_ok=True)
            print(json.dumps({'stopped': args.service, 'port': port}))
            return
        if running:
            print(json.dumps({'already_running': args.service, 'port': port}))
            return
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
        if not (release / entry.split('.')[0] / 'app.py').is_file():
            raise ValueError('Release module missing')
        if not (release.parent / 'frontend/index.html').is_file():
            raise ValueError('Release frontend missing')
        argv = [str(release / '.venv/bin/python'), '-m', 'uvicorn', entry, '--factory',
                '--host', '127.0.0.1', '--port', str(port), '--workers', '1', '--no-access-log',
                '--no-proxy-headers', '--timeout-graceful-shutdown', '10']
        with (state / ('release-' + args.service + '.log')).open('ab') as log:
            child = subprocess.Popen(argv, cwd=release, env=env, stdout=log, stderr=log, start_new_session=True)
        time.sleep(1)
        if child.poll() is not None:
            raise ValueError('Demo startup failed; inspect private service log')
        record = {'pid': child.pid, 'uid': os.geteuid(), 'start': process_start(child.pid),
                  'service': args.service, 'release': str(release)}
        write_pid(pidfile, record)
        print(json.dumps({'started': args.service, 'pid': child.pid, 'port': port, 'workers': 1}))

if __name__ == '__main__':
    main()
