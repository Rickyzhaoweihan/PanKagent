"""Own only the dashboard process. Never signal agent/results/shared services."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from .settings import Settings


def start_time(pid):
    return Path('/proc',str(pid),'stat').read_text().rsplit(') ',1)[1].split()[19]


def owned(record):
    try:
        pid=int(record['pid'])
        return pid>1 and record['uid']==os.geteuid() and start_time(pid)==record['start'] and (
            Path('/proc',str(pid),'cwd').resolve()==Path(record['cwd']).resolve()) and (
            b'pankgraph_health.supervisor\x00run' in Path('/proc',str(pid),'cmdline').read_bytes())
    except (OSError,ValueError,KeyError):return False


def check_port(port):
    # Match uvicorn's listener semantics: TIME_WAIT is reusable, an active
    # listener is not. A plain bind falsely blocks ordinary dashboard upgrades.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind(('127.0.0.1',port))


def run(settings):
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    failures=0
    while not stopped:
        child=subprocess.Popen([sys.executable,'-m','uvicorn','pankgraph_health.app:create_app','--factory','--host','127.0.0.1','--port',str(settings.port),'--workers','1','--no-access-log','--no-proxy-headers'],cwd=Path(__file__).resolve().parent.parent)
        born=time.monotonic();bad=0
        try:
            while not stopped and child.poll() is None:
                for _ in range(100):
                    if stopped or child.poll() is not None:break
                    time.sleep(.1)
                if stopped or child.poll() is not None:break
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{settings.port}/health/live',timeout=4) as response:data=json.load(response)
                    age=data.get('collector_age_seconds')
                    good=data.get('service')=='pankgraph-health' and (time.monotonic()-born<120 or isinstance(age,(int,float)) and age<120)
                except Exception:good=False
                bad=0 if good else bad+1
                if bad>=3:break
        finally:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=12)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
        if stopped:break
        failures=failures+1 if time.monotonic()-born<180 else 1
        # Bounded crash-loop restart delay; no new processes outside dashboard.
        delay=min(60,2**min(failures,6))
        print(json.dumps({'event':'dashboard_worker_restart','delay_seconds':delay}),flush=True)
        for _ in range(delay*10):
            if stopped:break
            time.sleep(.1)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','run','stop','status']);args=parser.parse_args()
    settings=Settings.load();record_path=settings.state_dir/'supervisor.pid.json'
    if args.action=='run':run(settings);return
    import fcntl
    with (settings.state_dir/'manager.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=json.loads(record_path.read_text()) if record_path.exists() else {}
        running=owned(record)
        if args.action=='status':print(json.dumps({'running':running,'pid':record.get('pid'),'port':settings.port}));return
        if args.action=='stop':
            if record and not running:raise ValueError('Dashboard process ownership mismatch; no process signaled')
            if running:
                os.kill(record['pid'],signal.SIGTERM)
                for _ in range(200):
                    if not owned(record):break
                    time.sleep(.1)
                else:raise ValueError('Dashboard supervisor has not exited; no forced supervisor kill')
            record_path.unlink(missing_ok=True);print('Only dashboard supervisor stopped');return
        if running:print('Dashboard already running');return
        check_port(settings.port)
        log_path=settings.state_dir/'dashboard.log'
        # Bound startup log size without discarding the previous file.
        if log_path.exists() and log_path.stat().st_size>5*1024*1024:log_path.replace(settings.state_dir/'dashboard.previous.log')
        with log_path.open('ab') as log:
            child=subprocess.Popen([sys.executable,'-m','pankgraph_health.supervisor','run'],stdout=log,stderr=log,start_new_session=True)
        time.sleep(.5)
        if child.poll() is not None:raise ValueError('Dashboard supervisor failed to start')
        record={'pid':child.pid,'uid':os.geteuid(),'start':start_time(child.pid),'cwd':str(Path.cwd())}
        temp=record_path.with_suffix('.tmp');temp.write_text(json.dumps(record));temp.replace(record_path)
        print(json.dumps({'started':True,'pid':child.pid,'port':settings.port}))

if __name__=='__main__':main()
