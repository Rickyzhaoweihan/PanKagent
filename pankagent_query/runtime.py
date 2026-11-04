"""Manage only the caller-owned query-dev process; never databases or old agents."""
import argparse,fcntl,json,os,signal,subprocess,sys,time,uuid
from pathlib import Path
from urllib.request import urlopen


def save(path,value):
    temp=path.with_suffix('.partial');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def owned_pid(root,name,needle):
    try:
        pid=json.loads((root/'private'/name).read_text())['pid'];p=Path('/proc')/str(pid)
        command=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode()
        if p.stat().st_uid==os.getuid() and needle in command and (p/'cwd').resolve()==(root/'code').resolve():return pid
    except (OSError,ValueError,KeyError):pass
    return None


def control(config,action):
    root=config.parent;cfg=json.loads(config.read_text());port=cfg['port']
    worker=owned_pid(root,'worker.pid.json','pankagent_query.runtime')
    if action=='start':
        if worker:return {'status':'already_running','pid':worker}
        with (root/'logs/worker.log').open('ab') as log:
            p=subprocess.Popen([sys.executable,'-m','pankagent_query.runtime','--config',str(config),'serve'],cwd=root/'code',stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        return {'status':'started','pid':p.pid}
    if action=='stop':
        api=owned_pid(root,'api.pid.json','uvicorn pankagent_query.api:app')
        for pid in [worker,api]:
            if pid:
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass
        return {'status':'stopped','old_services_changed':False}
    try:
        with urlopen('http://127.0.0.1:'+str(port)+'/health/ready',timeout=15) as r:health=json.load(r)
    except Exception:health={'ready':False}
    return {'worker_pid':worker,'health':health}


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('action',choices=['start','stop','status','serve']);a=p.parse_args()
    config=a.config.resolve();root=config.parent;os.umask(0o077)
    assert config.stat().st_uid==os.getuid() and root.stat().st_uid==os.getuid()
    if a.action!='serve':print(json.dumps(control(config,a.action)));return
    lock=(root/'private/worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    save(root/'private/worker.pid.json',{'pid':os.getpid()})
    cfg=json.loads(config.read_text());env={**os.environ,'PANK_QUERY_AGENT_CONFIG':str(config)}
    delay=3
    while True:
        log_path=root/'logs/api.log'
        if log_path.exists() and log_path.stat().st_size>16*1024*1024:
            log_path.rename(root/'logs'/('api-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.log'))
        with log_path.open('ab') as log:
            process=subprocess.Popen([sys.executable,'-m','uvicorn','pankagent_query.api:app','--host','127.0.0.1','--port',str(cfg['port']),'--no-access-log'],env=env,cwd=root/'code',stdout=log,stderr=log)
            save(root/'private/api.pid.json',{'pid':process.pid})
            began=time.monotonic();code=process.wait()
        save(root/'private/runtime-status.json',{'status':'retrying','exit_code':code,'release_id':cfg['release_id']})
        delay=3 if time.monotonic()-began>60 else min(delay*2,60);time.sleep(delay)
if __name__=='__main__':main()
