#!/bin/bash
set -u
umask 077
ROOT=/home/runbomao/pank-qwen-direct
cd "$ROOT"
exec 9>direct-relay.lock
flock -n 9 || exit 0
child=''
stop() { if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; fi; exit 0; }
trap stop TERM INT HUP
printf 'Direct relay started job=%s node=%s\n' "$SLURM_JOB_ID" "$(hostname)"
while true; do
  ssh -N -T -o BatchMode=yes -o IdentitiesOnly=yes \
    -i "$ROOT/jieliu-tunnel-rsa" \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$ROOT/jieliu-known-hosts" \
    -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o ControlMaster=no -o ControlPath=none \
    -R 127.0.0.1:33924:127.0.0.1:23924 \
    runbomao@jieliulab3.dcmb.med.umich.edu &
  child=$!
  wait "$child"; rc=$?; child=''
  printf '%s relay exited rc=%s; reconnecting in 10 seconds\n' "$(date -u +%FT%TZ)" "$rc"
  sleep 10 & child=$!; wait "$child"; child=''
done
