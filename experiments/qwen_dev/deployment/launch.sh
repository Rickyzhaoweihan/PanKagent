#!/bin/bash
set -eo pipefail
source /sw/lmod/lmod/init/bash
module load gcc/13.2.0 cuda/13.1.0
export PATH="$HOME/pank-qwen-direct/tools:$PATH"
export TRITON_CACHE_DIR="$HOME/pank-qwen-direct/cache/triton"
export TORCHINDUCTOR_CACHE_DIR="$HOME/pank-qwen-direct/cache/torchinductor"
export FLASHINFER_WORKSPACE_BASE="$HOME/pank-qwen-direct/cache/flashinfer"
export OMP_NUM_THREADS=8
umask 077
cd "$HOME/pank-qwen-direct"
export TRITON_PTXAS_PATH="$PWD/tools/ptxas"
export TRITON_PTXAS_BLACKWELL_PATH="$PWD/tools/ptxas-blackwell"
export TRITON_CUOBJDUMP_PATH="$PWD/tools/cuobjdump"
export TRITON_NVDISASM_PATH="$PWD/tools/nvdisasm"
export HF_HOME="$PWD/cache"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export VLLM_API_KEY="$(cat api-key)"
VLLM_PY=/nfs/turbo/umms-drjieliu/usr/xiangzhg/kv29/kvshill/envs/vllm/bin/python
MODEL=/nfs/turbo/umms-drjieliu/usr/xiangzhg/kv29/kvshill/hf_home/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
printf 'Job=%s Node=%s CUDA_VISIBLE_DEVICES=%s\n' "$SLURM_JOB_ID" "$SLURMD_NODENAME" "$CUDA_VISIBLE_DEVICES"
nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv
# Shared environment and checkpoint are read only. Nothing is installed into them.
"$VLLM_PY" preflight.py
exec "$VLLM_PY" -m vllm.entrypoints.openai.api_server \
 --model "$MODEL" --served-model-name pank-candidate \
 --host 127.0.0.1 --port 23924 \
 --max-model-len 65536 --max-num-seqs 4 --max-num-batched-tokens 8192 \
 --gpu-memory-utilization 0.90 --dtype bfloat16 \
 --enable-prefix-caching --enable-chunked-prefill \
 --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3
