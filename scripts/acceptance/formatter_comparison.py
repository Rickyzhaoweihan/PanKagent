"""Compare formatters on identical frozen live-query evidence, no new retrieval.
Run after model_comparison.py; all calls share its cumulative provider ledgers.
"""
import asyncio, hashlib, json, os, sys, time
from dataclasses import replace
from pathlib import Path
ROOT=Path(os.environ['PANK_COMPARISON_ROOT'])
OUT=ROOT/os.environ.get('PANK_FORMATTER_OUTPUT','formatter-v3');OUT.mkdir(mode=0o700);os.umask(0o077)
sys.path.insert(0,os.environ.get('PANK_ACCEPTANCE_CODE',str(Path(__file__).resolve().parents[2])))
from deploy_results.manage import read_protected_env
os.environ.update(read_protected_env(Path('/var/local/serviceuser/.config/pankagent-vnext/runtime.env')))
from pankagent_vnext.config import Settings
from pankagent_vnext.llm import ClaudeGateway
from pankagent_vnext.composable_planning import answer_results
from pankagent_vnext.audit import recorder

async def main():
    rows=[]
    for index,(case,source_provider) in enumerate([('definition','claude'),('stage-case','gpt'),('cftr-paraphrase','gpt'),('cftr-sqtl','claude'),('shared-partners','gpt')]):
        source=ROOT/f'paired-v3-{source_provider}-r1'/f'{case}.json'
        saved=json.loads(source.read_text());run=saved['run']
        evidence=answer_results(run['plan'],{s['step_id']:s for s in run['evidence']['steps']})
        question=saved['case']['question']
        for provider in (['gpt','claude'] if index%2==0 else ['claude','gpt']):
            state=ROOT/'budgets'/provider;assert (state/'budget.sqlite3').is_file()
            settings=replace(Settings(),state_dir=state,budget_dir=str(state),budget_usd=20 if provider=='gpt' else 10,model='gpt-6-sol' if provider=='gpt' else 'claude-sonnet-5',reasoning_effort='none')
            gateway=ClaudeGateway(settings);events=[]
            token=recorder.set(lambda kind,payload:events.append({'kind':kind,'payload':payload,'seconds':time.monotonic()-start}))
            answer='';first=None;start=time.monotonic()
            try:
                prepared=gateway.prepare_answer(question,evidence)
                async for chunk in gateway.synthesize(question,evidence,prepared=prepared):
                    if first is None:first=time.monotonic()-start
                    answer+=chunk
                elapsed=time.monotonic()-start
                settled=[e['payload'] for e in events if e['kind']=='model_settled']
                row={'case':case,'provider':provider,'model':settings.model,'seconds':elapsed,'first_text_seconds':first,'cost_usd':sum(e['actual_usd'] for e in settled),'usage':[e['usage'] for e in settled],'answer':answer,'input_body_sha256':hashlib.sha256(prepared.body.encode()).hexdigest(),'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'events':events}
                (OUT/f'{case}-{provider}.json').write_text(json.dumps(row,indent=2));rows.append(row)
                print(json.dumps({k:v for k,v in row.items() if k not in ['answer','events','usage']}),flush=True)
            finally:
                recorder.reset(token);await gateway.close()
    (OUT/'summary.json').write_text(json.dumps(rows,indent=2))

asyncio.run(main())
