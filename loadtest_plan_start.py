#!/usr/bin/env python3
"""
Pressure test for the *plan-before-confirm* path (POST /plan/start) ONLY.

Drives a gradient of concurrent "users" (single burst per level) against the
running server, captures client latency/throughput/error metrics plus
server-side GPU / /health / server.log sampling, and — by default — scrubs the
synthetic test traffic back out of the persistent stores (sessions.sqlite +
plan_sessions.jsonl) keyed on the session_ids it created.

It NEVER calls /plan/confirm (or /plan/revise) — only /health and /plan/start.

Usage:
    python3 loadtest_plan_start.py                  # full run: 20,50,100,150,200
    python3 loadtest_plan_start.py --dry-run        # quick validate: levels 2,5
    python3 loadtest_plan_start.py --levels 10,30   # custom gradient
    python3 loadtest_plan_start.py --no-scrub       # keep the test footprint
    python3 loadtest_plan_start.py --scrub-only loadtest_results/<ts>   # re-run cleanup

Requires: httpx (installed), nvidia-smi (optional), read access to logs/.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_URL = "http://localhost:8001"
DEFAULT_LEVELS = [20, 50, 100, 150, 200]
DRY_RUN_LEVELS = [2, 5]
DEFAULT_REQUEST_TIMEOUT = 600.0          # generous: queue time is signal, not failure
DEFAULT_COOLDOWN = 20.0                  # between levels, let semaphore/GPU drain
SAMPLE_INTERVAL = 5.0                    # GPU + /health sampling cadence (s)

SESSIONS_DB = os.path.join(ROOT, "logs", "sessions.sqlite")
PLAN_LOG_JSONL = os.path.join(ROOT, "logs", "plan_sessions.jsonl")
SERVER_LOG = os.path.join(ROOT, "server.log")

# ---------------------------------------------------------------------------
# Query pool — real questions, tagged by the code path they exercise.
# (drawn from experience buffer, rollouts, qp_prompts.py, test_server*.py)
# ---------------------------------------------------------------------------
QUERY_POOL = [
    # simple single-step KG
    ("simple_kg", "What are the functions of gene INS?"),
    ("simple_kg", "What are the GO terms associated with the gene PLA2G4A?"),
    ("simple_kg", "What genes are effector genes of type 1 diabetes?"),
    ("simple_kg", "Which SNPs are associated with type 1 diabetes through GWAS signals?"),
    # parallel KG
    ("parallel_kg", "Is CFTR an effector gene for type 1 diabetes?"),
    ("parallel_kg", "What biological processes, functions and pathways is CTLA4 annotated with?"),
    ("parallel_kg", "Which genes are differentially expressed in Alpha Cells compared to Beta Cells?"),
    ("parallel_kg", "What genes are associated with type 1 diabetes?"),
    # KG chain
    ("chain_kg", "Which GO terms are associated with genes that have open chromatin peaks in Beta cells?"),
    ("chain_kg", "Which genes colocalize with type 1 diabetes and are also differentially expressed in Beta cells?"),
    ("chain_kg", "Which PPI partners of CFTR are differentially expressed in Beta cells?"),
    ("chain_kg", "Which genes are differentially expressed in Beta Cell and have GO term insulin secretion?"),
    # cross-source genomic / SQL
    ("cross_source_genomic", "What is the chromosomal position of gene INS?"),
    ("cross_source_genomic", "Which genes are located on chromosome 11?"),
    ("cross_source_genomic", "For T1D GWAS variants, which variants also fine-map as QTLs to genes differentially expressed in Beta cells?"),
    # functional_data REST
    ("functional_data", "What open chromatin regions are active in Beta Cells?"),
    ("functional_data", "Which OCR peaks are located in the gene BCL2L1?"),
    # donor metadata
    ("donor", "How many donors have Type 1 Diabetes?"),
    ("donor", "Which donors are GADA positive with DR3/DR4 HLA genotype?"),
    # literature-heavy
    ("literature", "What is the relationship between beta cell dysfunction and type 1 diabetes?"),
    ("literature", "Explain the molecular mechanisms of insulin secretion in pancreatic beta cells"),
    # deliberate empty-result
    ("empty_result", "What diseases is the SNP rs999999999 associated with through GWAS signals?"),
    ("empty_result", "What fGSEA pathways are enriched in Beta cells?"),
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# A single /plan/start request
# ---------------------------------------------------------------------------
async def one_request(client, base_url, level, idx, category, question, rigor, use_literature):
    rec = {
        "level": level, "idx": idx, "query": question, "category": category,
        "http_status": None, "ok": False, "latency_ms": None, "plan_type": None,
        "n_steps": None, "total_records": None, "neo4j_nonempty": None,
        "session_id": None, "error_field": None, "exception": None,
    }
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{base_url}/plan/start",
            json={"question": question, "rigor": rigor, "use_literature": use_literature},
        )
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        rec["http_status"] = r.status_code
        if r.status_code == 200:
            body = r.json()
            rec["session_id"] = body.get("session_id")
            rec["error_field"] = body.get("error")
            plan_json = body.get("plan_json") or {}
            plan = plan_json.get("plan") or plan_json
            steps = plan.get("steps") if isinstance(plan, dict) else None
            rec["plan_type"] = (plan.get("plan_type") if isinstance(plan, dict) else None)
            rec["n_steps"] = len(steps) if isinstance(steps, list) else None
            # /plan/start does not return neo4j_results inline; the per-step record
            # counts are surfaced in plan_markdown as "**N records**".
            counts = [int(m) for m in re.findall(r"\*\*(\d+)\s+records?\*\*", body.get("plan_markdown") or "")]
            rec["total_records"] = sum(counts) if counts else 0
            rec["neo4j_nonempty"] = rec["total_records"] > 0
            rec["ok"] = rec["error_field"] is None
        else:
            rec["error_field"] = (r.text or "")[:200]
    except Exception as e:  # timeout, connection reset, JSON decode, ...
        rec["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        rec["exception"] = f"{type(e).__name__}: {e}"[:200]
    return rec


# ---------------------------------------------------------------------------
# Background server-side sampler (GPU + /health), runs during each level
# ---------------------------------------------------------------------------
class Sampler:
    def __init__(self, base_url):
        self.base_url = base_url
        self.gpu_rows = []     # dict: ts, level, gpu_index, util, mem_used, mem_total, power
        self.health_rows = []  # dict: ts, level, latency_ms, status_code, ok
        self._level = None
        self._stop = asyncio.Event()
        self._task = None

    def set_level(self, level):
        self._level = level

    async def _loop(self):
        async with httpx.AsyncClient(timeout=10.0) as hc:
            while not self._stop.is_set():
                ts = time.time()
                # GPU
                try:
                    out = subprocess.run(
                        ["nvidia-smi",
                         "--query-gpu=index,utilization.gpu,memory.used,memory.total,power.draw",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=8,
                    )
                    for line in out.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 5:
                            self.gpu_rows.append({
                                "ts": ts, "level": self._level, "gpu_index": parts[0],
                                "util": _f(parts[1]), "mem_used": _f(parts[2]),
                                "mem_total": _f(parts[3]), "power": _f(parts[4]),
                            })
                except Exception:
                    pass
                # /health latency
                h0 = time.monotonic()
                try:
                    hr = await hc.get(f"{self.base_url}/health")
                    self.health_rows.append({
                        "ts": ts, "level": self._level,
                        "latency_ms": round((time.monotonic() - h0) * 1000, 1),
                        "status_code": hr.status_code, "ok": hr.status_code == 200,
                    })
                except Exception as e:
                    self.health_rows.append({
                        "ts": ts, "level": self._level,
                        "latency_ms": round((time.monotonic() - h0) * 1000, 1),
                        "status_code": None, "ok": False, "err": str(e)[:80],
                    })
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=SAMPLE_INTERVAL)
                except asyncio.TimeoutError:
                    pass

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task


def _f(s):
    try:
        return float(s)
    except Exception:
        return None


def server_log_size():
    try:
        return os.path.getsize(SERVER_LOG)
    except OSError:
        return None


def count_log_errors(start_off, end_off):
    """Count ERROR/Traceback/Exception lines appended to server.log in [start,end)."""
    if start_off is None or end_off is None or end_off <= start_off:
        return 0
    try:
        with open(SERVER_LOG, "rb") as f:
            f.seek(start_off)
            chunk = f.read(end_off - start_off).decode("utf-8", "replace")
        return sum(
            1 for ln in chunk.splitlines()
            if (" ERROR " in ln or "Traceback" in ln or "Exception" in ln)
        )
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Level runner — single burst of C concurrent requests
# ---------------------------------------------------------------------------
async def run_level(client, base_url, level, sampler, rigor, use_literature, q_cursor):
    sampler.set_level(level)
    log_start = server_log_size()
    t0 = time.monotonic()
    tasks = []
    for i in range(level):
        category, question = QUERY_POOL[q_cursor[0] % len(QUERY_POOL)]
        q_cursor[0] += 1
        tasks.append(one_request(client, base_url, level, i, category, question,
                                 rigor, use_literature))
    records = await asyncio.gather(*tasks)
    wall = time.monotonic() - t0
    log_end = server_log_size()

    lat_ok = [r["latency_ms"] for r in records if r["ok"] and r["latency_ms"] is not None]
    succ = [r for r in records if r["ok"]]
    fail = [r for r in records if not r["ok"]]
    err_by_cat = {}
    for r in fail:
        err_by_cat[r["category"]] = err_by_cat.get(r["category"], 0) + 1
    nonempty = [r for r in succ if r["neo4j_nonempty"]]

    summary = {
        "level": level,
        "requests": len(records),
        "success": len(succ),
        "failures": len(fail),
        "success_rate": round(len(succ) / len(records), 3) if records else 0,
        "wall_s": round(wall, 1),
        "throughput_rps": round(len(records) / wall, 3) if wall > 0 else None,
        "lat_p50_ms": _r(pct(lat_ok, 50)),
        "lat_p90_ms": _r(pct(lat_ok, 90)),
        "lat_p95_ms": _r(pct(lat_ok, 95)),
        "lat_p99_ms": _r(pct(lat_ok, 99)),
        "lat_min_ms": _r(min(lat_ok)) if lat_ok else None,
        "lat_max_ms": _r(max(lat_ok)) if lat_ok else None,
        "lat_mean_ms": _r(statistics.mean(lat_ok)) if lat_ok else None,
        "mean_steps": _r(statistics.mean([r["n_steps"] for r in succ if r["n_steps"] is not None])
                         if any(r["n_steps"] is not None for r in succ) else None),
        "pct_nonempty": round(len(nonempty) / len(succ), 3) if succ else None,
        "mean_records": _r(statistics.mean([r["total_records"] for r in succ if r["total_records"] is not None])
                           if any(r["total_records"] is not None for r in succ) else None),
        "err_by_category": err_by_cat,
        "server_log_errors": count_log_errors(log_start, log_end),
        "log_bytes_appended": (log_end - log_start) if (log_start is not None and log_end is not None) else None,
    }
    # GPU stats during this level
    g = [r for r in sampler.gpu_rows if r["level"] == level and r["util"] is not None]
    if g:
        summary["gpu_util_mean"] = _r(statistics.mean([r["util"] for r in g]))
        summary["gpu_util_max"] = _r(max(r["util"] for r in g))
        summary["gpu_mem_used_max"] = _r(max(r["mem_used"] for r in g if r["mem_used"] is not None))
    h = [r for r in sampler.health_rows if r["level"] == level and r["latency_ms"] is not None]
    if h:
        summary["health_lat_max_ms"] = _r(max(r["latency_ms"] for r in h))
        summary["health_lat_mean_ms"] = _r(statistics.mean([r["latency_ms"] for r in h]))
    return records, summary


def _r(x):
    return round(x, 1) if isinstance(x, (int, float)) else x


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_outputs(out_dir, config, all_records, summaries, sampler):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "requests.jsonl"), "w") as f:
        for r in all_records:
            f.write(json.dumps(r) + "\n")

    with open(os.path.join(out_dir, "gpu_samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "level", "gpu_index", "util", "mem_used", "mem_total", "power"])
        for r in sampler.gpu_rows:
            w.writerow([r["ts"], r["level"], r["gpu_index"], r["util"],
                        r["mem_used"], r["mem_total"], r["power"]])

    with open(os.path.join(out_dir, "health_samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "level", "latency_ms", "status_code", "ok"])
        for r in sampler.health_rows:
            w.writerow([r["ts"], r["level"], r["latency_ms"], r.get("status_code"), r.get("ok")])

    cols = ["level", "requests", "success", "failures", "success_rate", "throughput_rps",
            "wall_s", "lat_p50_ms", "lat_p90_ms", "lat_p95_ms", "lat_p99_ms",
            "lat_min_ms", "lat_max_ms", "lat_mean_ms", "mean_steps", "pct_nonempty",
            "gpu_util_mean", "gpu_util_max", "gpu_mem_used_max",
            "health_lat_mean_ms", "health_lat_max_ms", "server_log_errors"]
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in summaries:
            w.writerow(s)

    lines = ["# Load test — /plan/start (plan-before-confirm)", ""]
    lines.append("```")
    lines.append(json.dumps(config, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("| C | reqs | ok | fail | ok% | rps | wall_s | p50 | p90 | p95 | p99 | max | steps | nonempty% | gpu_util_max | health_max_ms | log_err |")
    lines.append("|---|------|----|------|-----|-----|--------|-----|-----|-----|-----|-----|-------|-----------|--------------|---------------|---------|")
    for s in summaries:
        lines.append("| {level} | {requests} | {success} | {failures} | {sr} | {rps} | {wall_s} | {p50} | {p90} | {p95} | {p99} | {mx} | {steps} | {ne} | {gu} | {hm} | {le} |".format(
            level=s["level"], requests=s["requests"], success=s["success"], failures=s["failures"],
            sr=s.get("success_rate"), rps=s.get("throughput_rps"), wall_s=s.get("wall_s"),
            p50=s.get("lat_p50_ms"), p90=s.get("lat_p90_ms"), p95=s.get("lat_p95_ms"),
            p99=s.get("lat_p99_ms"), mx=s.get("lat_max_ms"), steps=s.get("mean_steps"),
            ne=s.get("pct_nonempty"), gu=s.get("gpu_util_max"), hm=s.get("health_lat_max_ms"),
            le=s.get("server_log_errors")))
    lines.append("")
    lines.append("Latency is wall-clock per request **including queue wait** behind the server's "
                 "`MAX_CONCURRENT_QUERIES=30` cap — rising p95/p99 above C=30 is backpressure, not error.")
    lines.append("")
    for s in summaries:
        if s["err_by_category"]:
            lines.append(f"- C={s['level']} failures by category: {s['err_by_category']}")
    md = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(md)
    return md


# ---------------------------------------------------------------------------
# Scrub — remove the test footprint, keyed on collected session_ids
# ---------------------------------------------------------------------------
def collect_session_ids(out_dir):
    ids = set()
    path = os.path.join(out_dir, "requests.jsonl")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                sid = json.loads(line).get("session_id")
                if sid:
                    ids.add(sid)
    return ids


def scrub(session_ids):
    if not session_ids:
        print("[scrub] no session_ids collected — nothing to remove.")
        return
    ids = list(session_ids)
    # 1) sqlite (separate connection; server keeps its own; WAL-safe)
    removed = {"plan_sessions": 0, "events": 0}
    try:
        conn = sqlite3.connect(SESSIONS_DB, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        for table in ("plan_sessions", "events"):
            n = 0
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                cur = conn.execute(f"DELETE FROM {table} WHERE session_id IN ({ph})", batch)
                n += cur.rowcount
            removed[table] = n
        conn.commit()
        conn.close()
        print(f"[scrub] sqlite: deleted {removed['plan_sessions']} plan_sessions, "
              f"{removed['events']} events rows.")
    except Exception as e:
        print(f"[scrub] sqlite ERROR (left intact): {e}")

    # 2) plan_sessions.jsonl — filter out our session_ids, atomic replace
    try:
        if os.path.exists(PLAN_LOG_JSONL):
            tmp = PLAN_LOG_JSONL + ".scrub.tmp"
            kept = dropped = 0
            with open(PLAN_LOG_JSONL) as fin, open(tmp, "w") as fout:
                for line in fin:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        sid = json.loads(s).get("session_id")
                    except Exception:
                        sid = None
                    if sid in session_ids:
                        dropped += 1
                    else:
                        fout.write(line)
                        kept += 1
            os.replace(tmp, PLAN_LOG_JSONL)
            print(f"[scrub] plan_sessions.jsonl: dropped {dropped} lines, kept {kept}.")
    except Exception as e:
        print(f"[scrub] jsonl ERROR (left intact): {e}")

    # 3) verify none remain in sqlite
    try:
        conn = sqlite3.connect(SESSIONS_DB, timeout=30)
        ph = ",".join("?" * len(ids[:900]))
        # check a sample (sqlite param limit) — but verify all in batches
        remaining = 0
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            ph = ",".join("?" * len(batch))
            remaining += conn.execute(
                f"SELECT count(*) FROM plan_sessions WHERE session_id IN ({ph})", batch
            ).fetchone()[0]
        conn.close()
        print(f"[scrub] verify: {remaining} test plan_sessions rows remain (expect 0).")
    except Exception as e:
        print(f"[scrub] verify ERROR: {e}")
    print("[scrub] NOTE: server.log left as-is (live process stdout fd; transient/rotated).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async(args):
    base_url = args.base_url.rstrip("/")
    levels = [int(x) for x in args.levels.split(",")] if args.levels else \
             (DRY_RUN_LEVELS if args.dry_run else DEFAULT_LEVELS)
    timeout = 120.0 if args.dry_run else args.timeout

    # warmup / readiness
    try:
        async with httpx.AsyncClient(timeout=10.0) as hc:
            h = await hc.get(f"{base_url}/health")
            h.raise_for_status()
            print(f"[warmup] /health OK: {h.json().get('status')}")
    except Exception as e:
        print(f"[FATAL] server not reachable at {base_url}/health: {e}")
        return 2

    git_commit = _git_commit()
    config = {
        "base_url": base_url, "levels": levels, "request_timeout_s": timeout,
        "cooldown_s": args.cooldown, "rigor": args.rigor, "use_literature": args.use_literature,
        "dry_run": args.dry_run, "scrub": not args.no_scrub, "git_commit": git_commit,
        "started_utc": utc_stamp(), "query_pool_size": len(QUERY_POOL),
    }
    out_dir = os.path.join(ROOT, "loadtest_results", config["started_utc"])
    os.makedirs(out_dir, exist_ok=True)
    print(f"[config] {json.dumps(config)}")
    print(f"[output] {out_dir}")

    sampler = Sampler(base_url)
    sampler.start()
    all_records, summaries = [], []
    limits = httpx.Limits(max_connections=max(levels) + 8,
                          max_keepalive_connections=max(levels) + 8)
    q_cursor = [0]
    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            for li, level in enumerate(levels):
                print(f"\n=== LEVEL C={level} (burst) ===")
                records, summary = await run_level(
                    client, base_url, level, sampler,
                    args.rigor, args.use_literature, q_cursor)
                all_records.extend(records)
                summaries.append(summary)
                print(f"  ok={summary['success']}/{summary['requests']} "
                      f"rps={summary['throughput_rps']} "
                      f"p50={summary['lat_p50_ms']}ms p95={summary['lat_p95_ms']}ms "
                      f"p99={summary['lat_p99_ms']}ms max={summary['lat_max_ms']}ms "
                      f"log_err={summary['server_log_errors']}")
                if summary["failures"]:
                    print(f"  failures by category: {summary['err_by_category']}")
                if li < len(levels) - 1 and args.cooldown > 0:
                    print(f"  cooldown {args.cooldown}s ...")
                    await asyncio.sleep(args.cooldown)
    finally:
        await sampler.stop()

    md = write_outputs(out_dir, config, all_records, summaries, sampler)
    print("\n" + md)

    if not args.no_scrub:
        print("[scrub] removing test footprint ...")
        scrub(collect_session_ids(out_dir))
    else:
        print("[scrub] skipped (--no-scrub).")
    print(f"[done] artifacts in {out_dir}")
    return 0


def _git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True, timeout=5
                              ).stdout.strip() or None
    except Exception:
        return None


def parse_args():
    p = argparse.ArgumentParser(description="Pressure test for /plan/start (plan-before-confirm).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--levels", default=None, help="comma list, e.g. 20,50,100,150,200")
    p.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    p.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN)
    p.add_argument("--rigor", type=lambda s: s.lower() != "false", default=True)
    p.add_argument("--use-literature", dest="use_literature",
                   type=lambda s: s.lower() != "false", default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-scrub", action="store_true")
    p.add_argument("--scrub-only", default=None, metavar="RUN_DIR",
                   help="re-run cleanup using a prior run's requests.jsonl, then exit")
    return p.parse_args()


def main():
    args = parse_args()
    if args.scrub_only:
        run_dir = args.scrub_only
        if not os.path.isabs(run_dir):
            run_dir = os.path.join(ROOT, run_dir)
        print(f"[scrub-only] {run_dir}")
        scrub(collect_session_ids(run_dir))
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
