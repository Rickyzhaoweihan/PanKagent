"""Read-only experiment transport: credentials stay on jieliu3.

Each call invokes the existing development environment without starting or
modifying services. Requests/results may contain private KG records; callers
must save only in the ignored experiment directory with umask 077.
"""
from __future__ import annotations

import json
import subprocess
import os
import sys

REMOTE_PYTHON = "/var/local/serviceuser/projects/pankagent-vnext/.venv/bin/python"
REMOTE_COMMAND = "sudo -n -u serviceuser " + REMOTE_PYTHON + " -"

REMOTE_SOURCE = r'''
import hashlib,json,os,re,shlex,time,urllib.request
from pathlib import Path
from neo4j import GraphDatabase,READ_ACCESS,Query
from neo4j.graph import Node,Relationship,Path as GraphPath
env={}
for line in Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1)
        words=shlex.split(value)
        env[key]=words[0] if words else ''

def transport(request):
    action=request['action']
    if action in ('cypher_info','cypher'):
        url=env['PANK_VNEXT_CYPHER_URL']+('/v1/info' if action=='cypher_info' else '/v1/cypher')
        body=None if action=='cypher_info' else json.dumps(request['body']).encode()
        req=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+env['CYPHER_API_TOKEN'],'Content-Type':'application/json'})
        begin=time.monotonic()
        try:
            with urllib.request.urlopen(req,timeout=request.get('timeout',120)) as response:
                data=json.loads(response.read())
                return {'status':response.status,'elapsed_s':round(time.monotonic()-begin,4),'body':data}
        except Exception as exc:
            return {'error_type':type(exc).__name__,'error':str(exc),'elapsed_s':round(time.monotonic()-begin,4)}
    if action=='identity':
        root=Path('/var/local/serviceuser/projects/pankagent-vnext')
        files=['pankagent_vnext/app.py','pankagent_vnext/llm.py','pankagent_vnext/graph.py','pankagent_vnext/release_schema.json']
        return {'neo4j_uri':env.get('PANK_VNEXT_NEO4J_URI'),'database':env.get('PANK_VNEXT_NEO4J_DATABASE'),'graph_version':env.get('PANK_VNEXT_GRAPH_VERSION'),'model':env.get('PANK_VNEXT_MODEL'),'source_hashes':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}}
    if action=='source_inventory':
        root=Path('/var/local/serviceuser/projects/pankagent-vnext/pankagent_vnext')
        return {p.name:{'lines':len(p.read_bytes().splitlines()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(root.glob('*.py')) if not p.name.startswith('._')}
    if action=='schema_pack':
        return json.loads(Path('/var/local/serviceuser/projects/pankagent-vnext/pankagent_vnext/release_schema.json').read_text())
    if action=='dev_agent':
        route=request['route'];method=request.get('method','GET')
        if not (method=='POST' and route=='/v2/plans' or method=='GET' and re.fullmatch(r'/v2/(?:plans|runs)/[A-Za-z0-9_-]+(?:/audit)?',route)):
            raise ValueError('Only fresh dev-plan submission and readback are allowed')
        body=request.get('body')
        if method=='POST' and (body.get('include_context') is not False or body.get('auto_proceed_seconds') is not None):
            raise ValueError('Baseline plans must disable optional context and never auto-confirm')
        encoded=None if method=='GET' else json.dumps(body).encode()
        req=urllib.request.Request('http://127.0.0.1:8794'+route,data=encoded,method=method,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:
            return {'status':response.status,'body':json.loads(response.read())}
    resolver_fulltext = action == 'resolver_fulltext'
    if resolver_fulltext:
        # Fixed read-only adapter; callers cannot supply Cypher or change caps.
        cap = min(200, max(1, int(request.get('cap', 200))))
        request = {'cypher': 'CALL db.index.fulltext.queryNodes($index, $text, {limit: $cap}) YIELD node, score RETURN elementId(node) AS element_id, labels(node) AS labels, properties(node) AS properties, score LIMIT $cap', 'parameters': {'index': request['index'], 'text': request['text'], 'cap': cap}, 'row_cap': cap, 'query_timeout': 10, 'explain': False}
        action = 'query'
    if action!='query':
        raise ValueError('unknown action')
    cypher=request['cypher'].strip().removesuffix(';').rstrip()
    # Lexical execution safeguard only; interpretation remains the model's job.
    stripped=re.sub(r"'(?:\\.|''|[^'])*'|\"(?:\\.|\"\"|[^\"])*\"|`(?:``|[^`])*`|//[^\n]*|/\*.*?\*/",' ',cypher,flags=re.S)
    forbidden={'CREATE','MERGE','SET','DELETE','DETACH','REMOVE','DROP','LOAD','FOREACH','CSV','ALTER','GRANT','DENY','REVOKE','TERMINATE'}
    words=set(re.findall(r'[A-Za-z_]+',stripped.upper()))
    if forbidden & words or ';' in stripped or ('CALL' in words and not resolver_fulltext and not re.match(r'^\s*CALL\s+db\.schema\.visualization\s*\(',stripped,re.I)):
        return {'error_type':'ReadOnlyGuard','error':'Only bounded read queries are allowed'}
    nodes,edges={},{}
    def value(x):
        if isinstance(x,Node):
            key=x.element_id; item={'element_id':key,'labels':sorted(x.labels),'properties':dict(x)}; nodes[key]=item; return {'node':key}
        if isinstance(x,Relationship):
            key=x.element_id; item={'element_id':key,'type':x.type,'source':x.start_node.element_id,'target':x.end_node.element_id,'properties':dict(x)}; edges[key]=item; value(x.start_node);value(x.end_node);return {'edge':key}
        if isinstance(x,GraphPath):
            return {'nodes':[value(n) for n in x.nodes],'edges':[value(e) for e in x.relationships]}
        if isinstance(x,dict):return {k:value(v) for k,v in x.items()}
        if isinstance(x,(tuple,list)):return [value(v) for v in x]
        if x is None or isinstance(x,(str,int,float,bool)):return x
        return str(x)
    begin=time.monotonic();cap=min(1000,max(1,request.get('row_cap',200)))
    with GraphDatabase.driver(env['PANK_VNEXT_NEO4J_URI'],auth=(env['PANK_VNEXT_NEO4J_USER'],env['PANK_VNEXT_NEO4J_PASSWORD'])) as driver:
        with driver.session(database=env.get('PANK_VNEXT_NEO4J_DATABASE','pankgraph'),default_access_mode=READ_ACCESS) as session:
            try:
                notes=[]; plan_operators=[]
                if request.get('explain',True):
                    explained=session.run(Query('EXPLAIN '+cypher,timeout=15),request.get('parameters',{}));summary=explained.consume()
                    def walk_plan(plan):
                        if plan:
                            plan_operators.append({'operator': plan.get('operatorType'), 'arguments': plan.get('args')})
                            for child in plan.get('children', []): walk_plan(child)
                    walk_plan(summary.plan)
                    notes=[{'code':n.get('code'),'title':n.get('title'),'description':n.get('description')} for n in summary.notifications or []]
                result=session.run(Query(cypher,timeout=min(30,request.get('query_timeout',15))),request.get('parameters',{}))
                rows=[];truncated=False
                for record in result:
                    if len(rows)==cap: truncated=True;break
                    rows.append(value({key:record[key] for key in record.keys()}))
                result.consume()
                return {'elapsed_s':round(time.monotonic()-begin,4),'row_count':len(rows),'node_count':len(nodes),'edge_count':len(edges),'truncated':truncated,'rows':rows,'nodes':list(nodes.values()),'edges':list(edges.values()),'notifications':notes,'plan_operators':plan_operators}
            except Exception as exc:
                return {'elapsed_s':round(time.monotonic()-begin,4),'error_type':type(exc).__name__,'error':str(exc)}

print(json.dumps(transport(REQUEST),default=str))
'''


def call_remote(request: dict, timeout: int = 160) -> dict:
    source = "REQUEST=" + repr(request) + "\n" + REMOTE_SOURCE
    completed = subprocess.run(
        [sys.executable, '-'] if os.environ.get('SIMPLE_AGENT_NATIVE') == '1' else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "jieliulab3-codex", REMOTE_COMMAND],
        input=source, text=True, capture_output=True, timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError("Remote read failed: " + completed.stderr[-1000:])
    return json.loads(completed.stdout)


if __name__ == "__main__":
    import sys
    print(json.dumps(call_remote(json.load(sys.stdin)), indent=2))
