import os
from pathlib import Path
from deploy_reliability import manage_gpt6 as manager


def test_gpt_manager_refuses_other_ports_releases_and_process_identity(tmp_path,monkeypatch):
    release=tmp_path/'release';release.mkdir()
    proc=tmp_path/'proc/123';proc.mkdir(parents=True)
    (proc/'cwd').symlink_to(release,target_is_directory=True)
    argv=['python','-m','uvicorn',manager.ENTRY,'--host','127.0.0.1','--port','8798']
    def write_args(): (proc/'cmdline').write_bytes('\0'.join(argv).encode())
    write_args()
    real_path=Path
    monkeypatch.setattr(manager,'Path',lambda *args:real_path(tmp_path/'proc') if args==('/proc',) else real_path(*args))
    monkeypatch.setattr(manager,'process_start',lambda pid:'start-1')
    record={'pid':123,'uid':os.geteuid(),'start':'start-1','service':'dev.pankgraph.gpt6','release':str(release)}
    assert manager.owned(record,release)
    assert not manager.owned({**record,'start':'old-process'},release)
    assert not manager.owned({**record,'service':'agent'},release)
    assert not manager.owned(record,tmp_path/'other')
    argv[-1]='8794';write_args()
    assert not manager.owned(record,release)
