#!/usr/bin/env python3
"""30-concurrent plan-only load test: POST /plan/start, never /plan/confirm.

Exercises the planning + Neo4j/vLLM path under concurrency without running the
format/reasoning pipeline. Verifies each plan came back with steps and a
confirm session id (which we deliberately leave unconfirmed). User-invoked.
"""
import json
import os
import sys
import time
import threading
import urllib.request
import concurrent.futures as cf

BASE = "http://127.0.0.1:8001"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
SPID = open("/db/usr/rickyhan/PanKLLM_implementation/server.pid").read().strip()

TEMPLATES = [
    "what genes are associated with type 1 diabetes?",
    "which SNPs are linked to T1D risk?",
    "what cell types express INS in the pancreas?",
    "which pathways are enriched in beta cells?",
    "what are the effector genes for type 1 diabetes?",
]


def threads():
    try:
        return len(os.listdir(f"/proc/{SPID}/task"))
    except Exception:
        return -1


def one(i):
    q = f"{TEMPLATES[i % len(TEMPLATES)]} (plan user {i})"
    body = json.dumps({"question": q, "rigor": False, "use_literature": False}).encode()
    req = urllib.request.Request(BASE + "/plan/start", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
        dt = round(time.time() - t0, 1)
        pj = payload.get("plan_json") or {}
        if isinstance(pj, str):
            try:
                pj = json.loads(pj)
            except Exception:
                pj = {}
        steps = len(pj.get("steps", []) or []) if isinstance(pj, dict) else 0
        confirm_id = payload.get("session_id") or ""
        had_err = bool(payload.get("error"))
        return (r.status, dt, steps, bool(confirm_id) and not had_err)
    except Exception as e:
        return ("ERR:" + type(e).__name__, round(time.time() - t0, 1), 0, False)


def main():
    out = []
    out.append(f"== {N}-user PLAN-ONLY load test (no confirm) ==")
    out.append(f"baseline_threads={threads()}")

    peak = [threads()]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak.append(threads())
            time.sleep(0.2)
    threading.Thread(target=sample, daemon=True).start()

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(one, range(N)))
    wall = round(time.time() - t0, 1)
    stop.set(); time.sleep(0.3)

    codes = [r[0] for r in results]
    lats = sorted(r[1] for r in results)
    ok = sum(1 for c in codes if c == 200)
    with_steps = sum(1 for r in results if r[0] == 200 and r[2] > 0)
    with_confirm = sum(1 for r in results if r[0] == 200 and r[3])
    errs = [c for c in codes if c != 200]

    def pct(p):
        return lats[min(len(lats) - 1, int(len(lats) * p))] if lats else 0

    out.append(f"success={ok}/{N}")
    out.append(f"plans_with_steps={with_steps}/{N}")
    out.append(f"plans_with_confirm_id(left UNCONFIRMED)={with_confirm}/{N}")
    out.append(f"wall_clock={wall}s")
    out.append(f"latency_min={lats[0]}s p50={pct(0.5)}s p90={pct(0.9)}s max={lats[-1]}s")
    out.append(f"peak_threads_during={max(peak)}")
    out.append(f"threads_after={threads()}")
    if errs:
        from collections import Counter
        out.append(f"errors={dict(Counter(errs))}")
    open("/tmp/plan_loadtest_out.txt", "w").write("\n".join(out) + "\n")
    print("PLAN_LOADTEST_DONE")


if __name__ == "__main__":
    main()
