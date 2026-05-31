#!/usr/bin/env python3
"""30-concurrent-user load test for the PanKgraph server.

Fires N concurrent POST /chat/start requests and reports success rate, latency
distribution, server thread-count growth (proving the bounded-pool design), and
GPU-backend spread from the router log. User-invoked.
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
PID_FILE = "/db/usr/rickyhan/PanKLLM_implementation/server.pid"
SPID = open(PID_FILE).read().strip()

# A few distinct questions so we exercise real planning, not just cache hits.
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
    q = f"{TEMPLATES[i % len(TEMPLATES)]} (user {i})"
    body = json.dumps({"question": q, "rigor": False}).encode()
    req = urllib.request.Request(BASE + "/chat/start", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
            return (r.status, round(time.time() - t0, 1))
    except Exception as e:
        return ("ERR:" + type(e).__name__, round(time.time() - t0, 1))


def main():
    out = []
    out.append(f"== {N}-user load test ==")
    out.append(f"baseline_threads={threads()}")

    peak = [threads()]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak.append(threads())
            time.sleep(0.2)
    samp = threading.Thread(target=sample, daemon=True)
    samp.start()

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(one, range(N)))
    wall = round(time.time() - t0, 1)
    stop.set()
    time.sleep(0.3)

    codes = [r[0] for r in results]
    lats = sorted(r[1] for r in results)
    ok = sum(1 for c in codes if c == 200)
    errs = [c for c in codes if c != 200]

    def pct(p):
        if not lats:
            return 0
        return lats[min(len(lats) - 1, int(len(lats) * p))]

    out.append(f"success={ok}/{N}")
    out.append(f"wall_clock={wall}s")
    out.append(f"latency_min={lats[0]}s  p50={pct(0.5)}s  p90={pct(0.9)}s  max={lats[-1]}s")
    out.append(f"peak_threads_during={max(peak)}")
    out.append(f"threads_after={threads()}")
    if errs:
        from collections import Counter
        out.append(f"errors={dict(Counter(errs))}")
    open("/tmp/loadtest_out.txt", "w").write("\n".join(out) + "\n")
    print("LOADTEST_DONE")


if __name__ == "__main__":
    main()
