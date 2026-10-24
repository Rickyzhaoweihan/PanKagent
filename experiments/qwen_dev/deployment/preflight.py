import os,json,subprocess,importlib.metadata as md
from pathlib import Path
import torch
r={'versions':{n:md.version(n) for n in ['vllm','torch','triton','flashinfer-python']},'cuda_home':os.environ.get('CUDA_HOME'),'torch_cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0),'available':torch.cuda.is_available()}
for name,args in [('nvcc',['nvcc','--version']),('gcc',['gcc','--version']),('ninja',['ninja','--version']),('ptxas',[os.environ['TRITON_PTXAS_PATH'],'--version'])]:
 r[name]=subprocess.check_output(args,text=True).strip()
a=torch.ones((128,128),device='cuda');assert (a@a).sum().item()==2097152
del a;torch.cuda.empty_cache()
model=Path('/nfs/turbo/umms-drjieliu/usr/xiangzhg/kv29/kvshill/hf_home/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0')
idx=json.loads((model/'model.safetensors.index.json').read_text());files=set(idx['weight_map'].values())
for name in files:
 with (model/name).open('rb') as f: assert f.read(1)
r['readable_weight_shards']=len(files)
Path('preflight-'+os.environ['SLURM_JOB_ID']+'.json').write_text(json.dumps(r,indent=2));print(json.dumps(r))
