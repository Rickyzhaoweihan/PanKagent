"""Freeze independent reference memberships privately; no model calls."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


async def main():
    from deploy_results.manage import read_protected_env
    os.environ.update(read_protected_env(Path(os.environ['PANK_ACCEPTANCE_ENV'])))
    from pankagent_vnext.config import Settings
    from pankagent_vnext.graph import GraphAdapter
    os.umask(0o077)
    root = Path(os.environ['PANK_ACCEPTANCE_ROOT'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / 'reference-memberships.json'
    if target.exists():
        raise RuntimeError('Do not overwrite frozen reference evidence')
    manifest = json.loads((REPO / 'tests_vnext/fixtures/acceptance/regression.json').read_text())
    graph = GraphAdapter(Settings())
    try:
        rows = await graph._small_query('MATCH (d:donor) WHERE d.data_source=$source '
                                       'RETURN DISTINCT d.t1d_stage AS stage,d.diabetes_type AS clinical', {'source':'HPAP'})
        inventory = {}
        for stage in ('1','2','3'):
            values = {r['stage'] for r in rows if str(r.get('stage') or '').startswith('Stage '+stage+':')}
            if len(values) != 1: raise ValueError('Ambiguous recorded stage inventory')
            inventory['stage'+stage] = next(iter(values))
        values = {r['clinical'] for r in rows if str(r.get('clinical') or '').casefold()
                  in {'t1d','type 1 diabetes','diabetes (type i)'}}
        if len(values) != 1: raise ValueError('Ambiguous recorded T1D category inventory')
        inventory['t1d_category'] = next(iter(values))
        records = []
        for case in manifest['cases']:
            if case['suite'] != 'correctness' or not case.get('reference_query'): continue
            params = {**case['parameters'], **inventory}
            rows = await graph._small_query(case['reference_query'], params)
            ids = sorted({str(row['id']) for row in rows})
            records.append({'case':case['key'],'query':case['reference_query'],'parameters':params,
                            'query_sha256':hashlib.sha256(case['reference_query'].encode()).hexdigest(),
                            'entity_type':case['kind'],'ids':ids,'count':len(ids)})
        target.write_text(json.dumps({'graph_release':graph.settings.graph_version,'references':records},indent=2)+'\n')
        print(json.dumps({'reference_cases':len(records),'model_calls':0,
                          'cases':[{'case':r['case'],'count':r['count']} for r in records]}))
    finally:
        await graph.close()


if __name__ == '__main__':
    asyncio.run(main())
