"""Reuse dev configuration through SSH; no secrets or services copied locally."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "baseline"))
from remote_bridge import REMOTE_SOURCE, REMOTE_COMMAND
from .core import Feedback, quoted

MODEL_SOURCE = r'''
import json,shlex,urllib.request,time,os
from pathlib import Path
env={}
for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1); words=shlex.split(value);env[key]=words[0] if words else ''
req=urllib.request.Request('https://api.anthropic.com/v1/messages',data=json.dumps(REQUEST).encode(),
    headers={'x-api-key':env['ANTHROPIC_API_KEY'],'anthropic-version':'2023-06-01','Content-Type':'application/json'})
reservation=None
if os.environ.get('SIMPLE_AGENT_SHARED_BUDGET')=='1':
    from simple_agent.shared_budget import reserve,settle
    reservation=reserve(REQUEST,env,'simple_query_loop')
provider_started=time.perf_counter()
try:
    with urllib.request.urlopen(req,timeout=60) as response:
        result=json.loads(response.read())
except urllib.error.HTTPError as exc:
    body=json.loads(exc.read());result={'error':body.get('error',{}),'status':exc.code}
result['_provider_elapsed_s']=time.perf_counter()-provider_started
if reservation and 'usage' in result:settle(reservation,result['usage'])
print(json.dumps(result))
'''


async def model_remote(payload):
    if os.environ.get("SIMPLE_AGENT_ALLOW_REMOTE_MODEL") != "1":
        raise RuntimeError("External model tests are paused pending approval of KG/schema transfer to Anthropic. Use the local model demo.")
    transport_started = time.perf_counter()
    argv = [sys.executable, '-'] if os.environ.get('SIMPLE_AGENT_NATIVE') == '1' else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "jieliulab3-codex", REMOTE_COMMAND]
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(("REQUEST=" + repr(payload) + "\n" + MODEL_SOURCE).encode()), 85)
        if process.returncode:
            raise RuntimeError("Model transport failed: " + stderr.decode()[-500:])
        result = json.loads(stdout)
    finally:
        if process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
    if "error" in result:
        raise RuntimeError("Model API error: " + json.dumps(result["error"])[:500])
    result['_model_transport_elapsed_s'] = time.perf_counter() - transport_started
    return result


async def query_remote(payload):
    """Execute the existing read adapter in an owned cancellable child, never a worker thread."""
    argv = [sys.executable, '-'] if os.environ.get('SIMPLE_AGENT_NATIVE') == '1' else ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'jieliulab3-codex', REMOTE_COMMAND]
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(('REQUEST=' + repr(payload) + '\n' + REMOTE_SOURCE).encode()), 25)
        if process.returncode:
            raise RuntimeError('Query transport failed: ' + stderr.decode(errors='replace'))
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


class DevTransport:
    def __init__(self):
        self.indexes = None
        self.index_lock = asyncio.Lock()

    async def initialize(self):
        async with self.index_lock:
            if self.indexes is None:
                raw = await self._raw("SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties RETURN name, type, state, labelsOrTypes, properties", {}, 1000)
                self.indexes = raw["rows"]
        return self.indexes

    async def _fulltext(self, index, text, cap=200):
        raw = await query_remote({"action": "resolver_fulltext", "index": index, "text": text, "cap": min(200, max(1, cap))})
        if raw.get("error_type"):
            raise Feedback(raw["error_type"] + ": " + raw.get("error", ""))
        return raw

    async def call(self, action, args):
        if action == "model":
            if args.get("model", "").startswith("local:"):
                from .local_model import call_model
                return await asyncio.to_thread(call_model, args)
            return await model_remote(args)
        if action == "resolve":
            from .resolver import resolve
            return await resolve(self, args)

        if action == "values":
            field = "x." + quoted(args["property"])
            match = "MATCH (x:" + quoted(args["type"]) + ")" if args["owner"] == "node" else "MATCH ()-[x:" + quoted(args["type"]) + "]->()"
            # Sample BEFORE distinct to bound work; never claim this is a category inventory.
            q = match + " WHERE " + field + " IS NOT NULL WITH " + field + " AS value LIMIT 500 RETURN DISTINCT value LIMIT 31"
            raw = await self._raw(q, {}, 30)
            values = [r["value"] for r in raw["rows"]]
            return {"values": values, "python_types": sorted({type(v).__name__ for v in values}),
                    "coverage": "sample of first 500 populated records; not a complete inventory", "truncated": raw["truncated"]}
        if action == "query":
            raw = await self._raw(args["cypher"], args["parameters"], args["row_cap"])
            nodes = [{"id": n["element_id"], "labels": n["labels"], "properties": n["properties"]} for n in raw["nodes"]]
            edges = [{**e, "id": e["element_id"]} for e in raw["edges"]]
            graph = {"nodes": nodes, "edges": edges}
            byte_cap = 2_000_000
            omitted = 0
            if len(json.dumps(graph, default=str).encode()) > byte_cap:
                # Preserve complete endpoint pairs while dropping whole records. Disclosure travels with the graph.
                by_id = {n["id"]: n for n in nodes}
                keep_nodes, keep_edges = {}, []
                for edge in edges:
                    trial_nodes = {**keep_nodes, **{k: by_id[k] for k in (edge["source"], edge["target"]) if k in by_id}}
                    trial_edges = keep_edges + [edge]
                    if len(json.dumps({"nodes": list(trial_nodes.values()), "edges": trial_edges}, default=str).encode()) > byte_cap:
                        omitted += 1
                        continue
                    keep_nodes, keep_edges = trial_nodes, trial_edges
                if not edges:
                    for node in nodes:
                        if len(json.dumps(list(keep_nodes.values()) + [node], default=str).encode()) > byte_cap:
                            omitted += 1
                        else:
                            keep_nodes[node["id"]] = node
                graph = {"nodes": list(keep_nodes.values()), "edges": keep_edges}
            return {"status": "partial" if raw["truncated"] or omitted else "complete" if raw["row_count"] else "empty",
                    "graph": graph, "row_count": raw["row_count"], "truncated": raw["truncated"], "omitted_records": omitted,
                    "execution_s": raw["elapsed_s"], "notifications": raw["notifications"],
                    "provenance": {"database": "pankgraph", "endpoint": "jieliu3 dev configuration", "access": "read pattern only"}}
        raise Feedback("Unknown transport action")

    async def _raw(self, cypher, parameters, cap):
        raw = await query_remote({"action": "query", "cypher": cypher, "parameters": parameters,
                                                   "row_cap": cap, "query_timeout": 10, "explain": False})
        if raw.get("error_type"):
            raise Feedback(raw["error_type"] + ": " + raw.get("error", ""))
        return raw
