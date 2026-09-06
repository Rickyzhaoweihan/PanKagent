#!/usr/bin/env python3
"""Start/stop ONLY the isolated vNext process, with PID ownership verification."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import time

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('action',choices=['start','stop','status'])
p.add_argument('--app-dir',type=Path,default=Path('/var/local/serviceuser/projects/pankagent-vnext'))
p.add_argument('--env-file',type=Path,default=Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env'))
a=p.parse_args()
env=os.environ.copy()
for line in a.env_file.read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        k,v=line.split('=',1);parts=shlex.split(v)
        env[k]=parts[0] if parts else ''
state=Path(env['PANK_VNEXT_STATE_DIR']);state.mkdir(mode=0o700,parents=True,exist_ok=True)
pidfile=state/'service.pid';port=int(env.get('PANK_VNEXT_PORT','8794'))
def owned(pid):
    try:
        args=Path('/proc')/str(pid)/'cmdline'
        tokens=args.read_bytes().decode().split('\0')
        return 'pankagent_vnext.app:create_app' in tokens and str(port) in tokens and Path('/proc',str(pid),'cwd').resolve()==a.app_dir.resolve()
    except OSError:return False
pid=int(pidfile.read_text()) if pidfile.exists() else None
if a.action=='status':
    print(json.dumps({'running':bool(pid and owned(pid)),'pid':pid,'port':port}));raise SystemExit()
if a.action=='stop':
    if pid and owned(pid):
        os.kill(pid,signal.SIGTERM)
        for _ in range(50):
            if not owned(pid):break
            time.sleep(.1)
        if owned(pid):raise SystemExit('New service has not exited; no forced kill attempted.')
    elif pid:raise SystemExit('PID is not owned by this service; refusing to signal it.')
    pidfile.unlink(missing_ok=True);print('Only isolated vNext process stopped.');raise SystemExit()
if pid and owned(pid): print('Isolated vNext is already running.');raise SystemExit()
s=socket.socket()
try:
    # Match uvicorn's restart behavior: TIME_WAIT is reusable, a live listener is not.
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind(('127.0.0.1',port))
except OSError:raise SystemExit('Port already occupied; no existing process changed.')
finally:s.close()
with (state/'service.log').open('ab') as log:
    child=subprocess.Popen([str(a.app_dir/'.venv/bin/python'),'-m','uvicorn','pankagent_vnext.app:create_app',
      '--factory','--host','127.0.0.1','--port',str(port),'--workers','1','--no-access-log',
      '--timeout-graceful-shutdown','2'],cwd=a.app_dir,env=env,
      stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    time.sleep(.5)
    if child.poll() is not None:raise SystemExit('New service failed at startup; inspect its private log.')
    pidfile.write_text(str(child.pid));pidfile.chmod(0o600)
print(json.dumps({'started':True,'pid':child.pid,'port':port}))
