"""Own only dev.pankgraph.gpt6 on loopback 8798, separate from existing dev.

Usage: python -m deploy_reliability.manage_gpt6 start|stop|status --release PATH
The comparison ledger is shared with all GPT development/replay runs. No reset.
"""
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

PORT=8798
ENTRY='pankagent_vnext.app:create_app'
ROOT=Path('/db/pankagent-vnext-private/operations/model-comparison-20260925')
STATE=ROOT/'dev.pankgraph.gpt6'


def owned(record,release):
    try:
        proc=Path('/proc')/str(record['pid']);argv=(proc/'cmdline').read_bytes().decode().split('\0')
        return (record['uid']==os.geteuid()==proc.stat().st_uid and record['start']==process_start(record['pid'])
            and record['service']=='dev.pankgraph.gpt6' and record['release']==str(release.resolve())
            and (proc/'cwd').resolve()==release.resolve() and ENTRY in argv and 'uvicorn' in argv
            and argv[argv.index('--port')+1]==str(PORT) and argv[argv.index('--host')+1]=='127.0.0.1')
    except (OSError,ValueError,KeyError,IndexError,TypeError):return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status']);parser.add_argument('--release',required=True,type=Path)
    a=parser.parse_args();release=a.release.resolve()
    if pwd.getpwuid(os.geteuid()).pw_name!='serviceuser':raise ValueError('serviceuser required')
    if not release.is_relative_to(ROOT):raise ValueError('isolated comparison release required')
    os.umask(0o077);STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid!=os.geteuid() or STATE.stat().st_mode&0o077:raise ValueError('private owned state required')
    pidfile=STATE/'service.pid.json'
    with (STATE/'manage.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=json.loads(pidfile.read_text()) if pidfile.exists() else None
        running=record is not None and owned(record,release)
        if a.action=='status':print(json.dumps({'running':running,'port':PORT,'record':record}));return
        if record and not running:raise ValueError('ownership mismatch; no process touched')
        if a.action=='stop':
            if running:
                os.kill(record['pid'],signal.SIGTERM)
                for _ in range(200):
                    if not owned(record,release):break
                    time.sleep(.1)
                else:raise RuntimeError('owned service did not exit; no forced signal')
            pidfile.unlink(missing_ok=True);print('stopped dev.pankgraph.gpt6');return
        if running:print('already running dev.pankgraph.gpt6');return
        with socket.socket() as probe:probe.bind(('127.0.0.1',PORT))
        budget=ROOT/'budgets/gpt'
        if not (budget/'budget.sqlite3').is_file():raise ValueError('existing cumulative GPT ledger required')
        env={**os.environ,**read_protected_env(Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env'))}
        env.update(PANK_VNEXT_PORT=str(PORT),PANK_VNEXT_STATE_DIR=str(STATE),PANK_VNEXT_MODEL='gpt-6-sol',
            PANK_VNEXT_BUDGET_DIR=str(budget),PANK_VNEXT_BUDGET_USD='20',PANK_VNEXT_REASONING_EFFORT='none')
        argv=[str(release/'.venv/bin/python'),'-B','-m','uvicorn',ENTRY,'--factory','--host','127.0.0.1','--port',str(PORT),
              '--workers','1','--no-access-log','--no-proxy-headers','--timeout-graceful-shutdown','10']
        with (STATE/'service.log').open('ab') as log:child=subprocess.Popen(argv,cwd=release,env=env,stdout=log,stderr=log,start_new_session=True)
        time.sleep(1)
        if child.poll() is not None:raise RuntimeError('isolated service startup failed; inspect private log')
        record={'pid':child.pid,'uid':os.geteuid(),'start':process_start(child.pid),'service':'dev.pankgraph.gpt6','release':str(release)}
        write_pid(pidfile,record);print(json.dumps(record))

if __name__=='__main__':main()
