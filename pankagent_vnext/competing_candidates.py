"""Opt-in candidate arbitration. Selection never depends on result size.

All producers use Graph.execute's existing validation, EXPLAIN and retrieval.
Callbacks only publish immutable results; the preview coordinator owns revisions.
"""
import asyncio
from copy import copy, deepcopy
import json
import time
from .composable_planning import digest, complete, snapshot
from .evidence_status import checked_query_result
from .agent_schemas import active_pack

VERSION = 'competing-candidates-v1'


def record_facts(result):
    return {key: {json.dumps(item, sort_keys=True, default=str) for item in result.get(key, [])}
            for key in ('nodes', 'edges', 'path_records', 'rows')}


def required_evidence(step, result):
    # Only explicit requested relationship categories count; record volume does not.
    requested = set(step.get('relation_types', []))
    return requested & {edge.get('type') for edge in result.get('edges', [])}


def membership(result):
    """Compare typed membership, relationship evidence, paths and scalar answers."""
    absent = object()
    def scalar(value):
        # GraphAdapter already materializes these native graph references into
        # nodes/edges/path_records. Their query aliases and collect-vs-row shape
        # are presentation, not additional membership or scalar evidence.
        if isinstance(value, dict):
            keys = set(value)
            if (keys == {'node_id'} or keys == {'path'}
                    or keys == {'edge', 'fingerprint'}):
                return absent
            clean = {key:item for key,v in value.items() if (item := scalar(v)) is not absent}
            return clean if clean else absent
        if isinstance(value, list):
            clean = [item for v in value if (item := scalar(v)) is not absent]
            return clean if clean else absent
        return value
    scalar_rows = [clean for row in result.get('rows', []) if (clean := scalar(row)) is not absent]
    return digest({
        'nodes': sorted((n['id'], tuple(sorted(n.get('labels', [])))) for n in result.get('nodes', [])),
        'edges': sorted(json.dumps(e, sort_keys=True) for e in result.get('edges', [])),
        'paths': sorted(json.dumps(p, sort_keys=True) for p in result.get('path_records', [])),
        'rows': sorted(json.dumps(p, sort_keys=True, default=str) for p in scalar_rows),
    })


def assessment(step, result, parents):
    checked = checked_query_result(step, result, parents)
    # A bounded partial may be retained for diagnostics; normal readiness still
    # decides whether it can feed a dependent step or a confirmable answer.
    partial = (result.get('status') == 'partial' and not result.get('error')
               and (result.get('validation') or [{}])[-1].get('valid') is True
               and bool(result.get('nodes') or result.get('rows') or result.get('edges'))
               and bool(result.get('queries')))
    if step.get('path_spec') and (result.get('nodes') or result.get('edges')) and not result.get('path_records'):
        checked = partial = False
    return {'usable': checked or partial, 'checked': checked,
            'complete': checked and complete(result),
            'membership': membership(result), 'graph_version': result.get('graph_version')}


class Selection:
    def __init__(self, step, parents):
        self.step, self.parents = deepcopy(step), deepcopy(parents)
        self.result = None
        self.revision = 0
        self.conflict = False
        self.records = []

    def offer(self, result, origin):
        result = deepcopy(result)
        a = assessment(self.step, result, self.parents)
        record = {'origin': origin, 'assessment': a, 'queries': result.get('queries', []),
                  'validation': result.get('validation', []), 'candidate_sha256': snapshot(result),
                  'task_sha256': digest(self.step),
                  'parents': {k: snapshot(v) for k, v in self.parents.items()},
                  'version': VERSION, 'schema': active_pack().identity()}
        self.records.append(record)
        if self.step.get('graph_version') and a['graph_version'] != self.step['graph_version']:
            a['usable'] = False
        if not a['usable']:
            record['decision'] = 'rejected'
            return False
        prior = assessment(self.step, self.result, self.parents) if self.result else None
        if self.conflict:
            record['decision'] = 'unresolved_conflict'
            return False
        if prior and prior['complete'] and a['complete'] and prior['membership'] != a['membership']:
            self.conflict = True
            self.result = {**self.result, 'status': 'failed',
                           'error': {'category': 'candidate_membership_conflict',
                                     'message': 'Complete query candidates disagree; scope requires verification.'}}
            self.revision += 1
            record['decision'] = 'conflict'
            return True
        coverage_gain = False
        if prior and not prior['complete']:
            old, new = record_facts(self.result), record_facts(result)
            coverage_gain = (required_evidence(self.step, self.result) < required_evidence(self.step, result)
                             and all(old[key] <= new[key] for key in old))
        if prior and (prior['complete'] or not a['complete']) and not coverage_gain:
            record['decision'] = 'equivalent' if prior['membership'] == a['membership'] else 'retained_first'
            return False
        self.revision += 1
        record['decision'] = ('selected' if self.result is None else
                              'replaced_missing_evidence' if coverage_gain else 'replaced_partial')
        self.result = result
        return True

    def current(self):
        if self.result is None:
            return None
        return {**deepcopy(self.result), 'candidate_selection': {
            'version': VERSION, 'revision': self.revision, 'conflict': self.conflict,
            'parents': {k: snapshot(v) for k, v in self.parents.items()},
            'records': deepcopy(self.records)}}


async def produce(graph, step, parents, emit, offer, assist, database_slots):
    """Race isolated adapter views; share only transport and guarded DB reads."""
    tasks = []
    reads = {}
    started = time.monotonic()

    async def retrieve(query, parameters, limits):
        key = digest([query, parameters, {k:sorted(v) if isinstance(v,set) else v for k,v in limits.items()}])
        if key not in reads:
            async def run():
                async with database_slots:
                    return await graph._retrieve(query, parameters, limits)
            reads[key] = asyncio.create_task(run())
        return deepcopy(await asyncio.shield(reads[key]))

    async def route(name):
        adapter = copy(graph)
        from .planning_contract import VerifiedCache
        adapter._query_cache = getattr(graph, '_query_cache', None) or VerifiedCache()
        adapter._candidate_route = name
        adapter._retrieve = retrieve
        successful = False
        async def delivered(r):
            nonlocal successful
            successful = successful or assessment(step, r, parents)['usable']
            await offer(deepcopy(r), name)
            return assessment(step, r, parents)['usable']
        adapter._candidate_result = delivered
        async def repair(s, question, failures, candidate):
            return await assist('query_repair', s, {'question': question, 'failures': failures, 'candidate': candidate})
        adapter.query_repair = repair
        result = await adapter.execute(deepcopy(step), deepcopy(parents), emit)
        # Return failures too: useful for audit, never an accepted result.
        await offer(result, name)
        if name == 'local' and not successful:
            proposal = await assist('template', step, {'result': result})
            if proposal:
                corrected = await adapter.execute(proposal, deepcopy(parents), emit)
                await offer(corrected, 'assisted_template')

    async def isolated_route(name):
        try:
            await route(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await emit('candidate_route_failed', {'step_id':step['id'], 'origin':name,
                                                 'reason':type(exc).__name__})

    try:
        tasks = [asyncio.create_task(isolated_route(name)) for name in ('local', 'gpu')]
        await asyncio.gather(*tasks)
    finally:
        for task in tasks + list(reads.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *reads.values(), return_exceptions=True)
        await emit('candidate_batch_finished', {'step_id': step['id'], 'seconds': time.monotonic()-started,
                                               'distinct_database_reads': len(reads)})
