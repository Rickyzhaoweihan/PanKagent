"""Build a portable paired-answer viewer from protected workflow50 outputs."""
import argparse
import json
from pathlib import Path
from workflow50 import evaluate, summarize


def build(root, output):
    manifest = json.loads((root/'run-manifest.json').read_text())['manifest']
    rows = [json.loads(line) for line in (root/'report.jsonl').read_text().splitlines()]
    output.parent.mkdir(parents=True,exist_ok=True)
    references = json.loads((root/'references.json').read_text())['cases']
    review_path = output.parent/'review-annotations.json'
    reviews = json.loads(review_path.read_text()) if review_path.exists() else {}
    pairs = []
    for case in manifest['cases']:
        item = {'case':case,'arms':{},'review':reviews.get(case['key'])}
        for arm in ('original','candidate'):
            report = next((r for r in rows if r['case']==case['key'] and r['arm']==arm),None)
            path = root/arm/(case['key']+'.json')
            if report:
                artifact = json.loads(path.read_text()) if path.exists() else {'run':{}}
                run = artifact['run']
                if case.get('parent') and not run.get('include_context'):
                    report['benchmark_invalid'] = 'Parent session supplied with conversation context disabled'
                if ((run.get('error') or {}).get('category') == 'budget_exhausted' or
                        (run.get('graph_answer') or '').startswith('The development evaluation budget is exhausted.')):
                    report['benchmark_invalid'] = 'Cumulative API budget prevented a model stage from running'
                # Early harness snapshots preceded a failed confirmation. The
                # persisted terminal event is authoritative; retain raw files.
                if report.get('confirm_http_status',202) != 202:
                    report['settled_eligible'] = False
                    terminals = [e['payload'] for e in artifact.get('events',[]) if e['type']=='terminal']
                    if terminals:
                        run = {**run,**terminals[-1]}
                        report['status'] = run['status']
                        report['confirmation_error'] = run.get('error')
                report['evaluation'] = evaluate(case,references[case['key']],run)
                evidence = run.get('evidence') or (run.get('preview') or {}).get('evidence') or {}
                item['arms'][arm] = {'report':report,'answer':run.get('graph_answer') or '',
                    'plan':run.get('plan'),'error':run.get('error'),
                    'steps':[{'id':s['step_id'],'status':s['status'],'error':s.get('error'),
                              'nodes':len(s.get('nodes',[])),'edges':len(s.get('edges',[])),
                              'queries':s.get('queries',[])} for s in evidence.get('steps',[])]}
        pairs.append(item)
    summary = json.loads((root/'summary.json').read_text())
    valid_rows = [row for row in rows if not row.get('benchmark_invalid')]
    summary['arms'] = summarize(valid_rows)
    paired_keys = {case['key'] for case in manifest['cases']
                   if {row['arm'] for row in valid_rows if row['case']==case['key']} == {'original','candidate'}}
    summary['paired_cases'] = len(paired_keys)
    summary['paired_arms'] = summarize([row for row in valid_rows if row['case'] in paired_keys])
    summary['unpaired_valid_attempts'] = [{'case':row['case'],'arm':row['arm']} for row in valid_rows if row['case'] not in paired_keys]
    summary['complete'] = len(valid_rows) == 2*len(manifest['cases'])
    summary['excluded_attempts'] = {'count':len(rows)-len(valid_rows),
        'cost_usd':sum(row.get('accounting',{}).get('settled_cost_usd',0) for row in rows if row.get('benchmark_invalid')),
        'cases':[{'case':row['case'],'arm':row['arm'],'reason':row['benchmark_invalid']} for row in rows if row.get('benchmark_invalid')]}
    summary['scoring_note'] = ('Predeclared expected clarifications are excluded from exact membership denominators. '
        'Failed confirmations are not settled-eligible. Matched latency uses cases confirmable in both arms. '
        'First preview means publication; publication alone does not prove confirmation will succeed.')
    (output.parent/'scored-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (output.parent/'scored-cases.json').write_text(json.dumps(rows,indent=2)+'\n')
    payload = json.dumps({'summary':summary,'pairs':pairs},ensure_ascii=False).replace('<','\\u003c')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('''<!doctype html><meta charset="utf-8"><title>PanKagent · 50-question comparison</title>
<style>
body{font:16px/1.55 system-ui;margin:0;color:#17243b;background:#f6f8fc}header,main{max-width:1500px;margin:auto;padding:24px}
h1{font-size:30px;margin:0}header p{max-width:1000px;color:#526079}select,input,button{font:inherit;padding:10px;border:1px solid #c3cad8;border-radius:6px;background:white}
select{width:72%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.card{background:white;border:1px solid #d5dceb;border-radius:12px;padding:22px;min-width:0}
.answer{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}.metrics{display:flex;gap:18px;flex-wrap:wrap;color:#3e5271}.fail{color:#a72537}.pass{color:#146b49}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 monospace;background:#f3f5fa;padding:14px}details{margin:12px 0}h2{font-size:22px}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #e0e4ed;padding:8px}
@media(max-width:900px){.grid{grid-template-columns:1fr}select{width:100%}}
</style><header><h1>PanKagent · original vs competing candidates</h1>
<p>50-question suite · same Sonnet 5 planning/formatting model · graph workflow only · first published preview and confirmable settled preview measured separately. The new feature remains disabled in dev.</p><p id="completion"></p>
<table><thead><tr><th>Measure</th><th>Original</th><th>New candidate flow</th></tr></thead><tbody id="metrics"></tbody></table>
<p>Latency medians show their eligible denominators. Costs include failed attempts; GPU infrastructure cost is not priced. This is one paired pass, not a repeated performance estimate.</p>
<details><summary>Full metrics and budget</summary><pre id="summary"></pre></details>
<label>Filter <input id="filter" placeholder="gene, cohort, source or question"></label>
<p><button id="prev">←</button> <select id="questions"></select> <button id="next">→</button></p></header>
<main><h2 id="question"></h2><p id="source"></p><details><summary>Case review requirements</summary><ul id="rubric"></ul></details><details><summary>Qualitative review against returned evidence</summary><pre id="review"></pre></details><div class="grid" id="answers"></div></main>
<script type="application/json" id="data">'''+payload+'''</script><script>
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(n,d=2)=>n==null?'—':Number(n).toFixed(d); let visible=[];
$('summary').textContent=JSON.stringify(data.summary,null,2);
const arms=data.summary.paired_arms;
$('completion').textContent=data.summary.paired_cases+' of 50 questions have valid paired tests. The table uses only those paired questions; excluded setup/budget attempts and any unpaired run remain in the full metrics.';
const measures=[['Attempted questions',s=>s.attempted],['Published previews',s=>s.ready],['Confirmable settled previews',s=>s.settled_eligible],['Written answers',s=>s.answers],['Exact-reference cases passed',s=>s.exact_reference_passes+' / '+s.exact_reference_cases],...['first_preview_s','settled_preview_s','elapsed_s'].map(key=>[['First preview','Settled preview','Written answer'][['first_preview_s','settled_preview_s','elapsed_s'].indexOf(key)]+' · matched median',s=>fmt(arms.matched_successes[key][s===arms.original?'original':'candidate'])+' s (n='+arms.matched_successes[key].n+')']),['API cost, all attempts',s=>'$'+fmt(s.cost_usd_all_attempts,4)],['Mean API cost per attempted question',s=>'$'+fmt(s.cost_usd_all_attempts/s.attempted,4)],['Backend evidence reads',s=>s.database_reads],['GPU generation requests',s=>s.gpu_requests]];
$('metrics').innerHTML=measures.map(([name,get])=>'<tr><td>'+esc(name)+'</td><td>'+esc(get(arms.original))+'</td><td>'+esc(get(arms.candidate))+'</td></tr>').join('');
function options(){const q=$('filter').value.toLowerCase();visible=data.pairs.filter(p=>JSON.stringify([p.case.question,p.case.group,p.case.key]).toLowerCase().includes(q));$('questions').innerHTML=visible.map((p,i)=>`<option value="${i}">${esc(p.case.key)} · ${esc(p.case.question)}</option>`).join('');show();}
function show(){const p=visible[Number($('questions').value)];if(!p)return;$('question').textContent=p.case.question;$('source').textContent=p.case.group+' · '+p.case.key;
$('rubric').innerHTML=p.case.review_requirements.map(r=>'<li>'+esc(r)+'</li>').join('');
$('review').textContent=p.review?JSON.stringify(p.review,null,2):'No separate qualitative annotation recorded.';
$('answers').innerHTML=['original','candidate'].map(arm=>{const a=p.arms[arm];if(!a)return `<section class="card"><h2>${arm}</h2>Not run</section>`;const r=a.report,e=r.evaluation||{},c=r.accounting||{};
return `<section class="card"><h2>${arm==='original'?'Original workflow':'New competing candidates'}</h2>${r.benchmark_invalid?'<p class="fail">Excluded from comparison: '+esc(r.benchmark_invalid)+'</p>':''}<div class="metrics"><span>First preview <b>${fmt(r.first_preview_s)} s</b></span><span>Settled <b>${fmt(r.settled_preview_s)} s</b></span><span>API <b>$${fmt(c.settled_cost_usd,4)}</b></span></div><p class="${r.settled_eligible?'pass':'fail'}">${r.settled_eligible?'Settled preview confirmable':'No confirmable settled preview'} · ${esc(r.status||r.harness_error)}</p><p>Exact reference: ${e.exact_reference_pass==null?'manual/supporting-evidence review':e.exact_reference_pass?'pass':'not passed'}</p><div class="answer">${esc(a.answer||'No written answer produced.')}</div><details><summary>Reference checks</summary><pre>${esc(JSON.stringify(e.checks,null,2))}</pre></details><details><summary>Plan and executed query diagnostics</summary><pre>${esc(JSON.stringify({plan:a.plan,steps:a.steps,error:a.error},null,2))}</pre></details><details><summary>Usage and timings</summary><pre>${esc(JSON.stringify(r,null,2))}</pre></details></section>`;}).join('');}
$('questions').onchange=show;$('filter').oninput=options;
$('prev').onclick=()=>{$('questions').selectedIndex=Math.max(0,$('questions').selectedIndex-1);show()};$('next').onclick=()=>{$('questions').selectedIndex=Math.min(visible.length-1,$('questions').selectedIndex+1);show()};options();
</script>''')
    return len(rows)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();print(build(args.root,args.output))
