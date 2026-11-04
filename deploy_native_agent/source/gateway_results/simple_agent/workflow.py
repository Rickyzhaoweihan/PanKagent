"""Existing frontend protocol around one agent loop; explicit confirmation releases its answer."""
import asyncio
import copy
import json
import inspect
import math
import os
import time
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

TERMINAL = {'completed', 'partial', 'failed', 'cancelled', 'superseded', 'interrupted'}

class Workflow:
    def __init__(self, runner, directory, formatter=None, literature=None, presentation=None, graph_release=None):
        self.runner, self.directory, self.formatter = runner, Path(directory), formatter
        self.literature, self.presentation, self.graph_release = literature, presentation, graph_release
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs, self.tasks, self.clocks, self.presentations = {}, {}, {}, {}
        self.gate = asyncio.Lock()
        for p in self.directory.glob('*.json'):
            run = json.loads(p.read_text())
            if run['status'] in ('queued', 'running'):
                run.update(status='interrupted', stage='Interrupted', _active_since=None, _elapsed_before=run.get('elapsed_ms', 0))
            if 'elapsed_ms' not in run:
                run['elapsed_ms'] = int(1000 * (run.get('_result', {}).get('metrics', {}).get('elapsed_s', 0) + run.get('_formatted', {}).get('elapsed_s', 0)))
                run['_elapsed_before'] = run['elapsed_ms']
                run['_active_since'] = None
            self.runs[run['run_id']] = run

    def save(self, run):
        p = self.directory / (run['run_id'] + '.json')
        temp = p.with_suffix('.tmp')
        temp.write_text(json.dumps(run, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(p)

    def elapsed(self, run):
        started = run.get('_active_since')
        return int(run.get('_elapsed_before', run.get('elapsed_ms', 0)) + (max(0, time.time() - started) * 1000 if started else 0))

    def pause(self, run):
        run['_elapsed_before'] = self.elapsed(run)
        run['_active_since'] = None
        run['elapsed_ms'] = run['_elapsed_before']

    def progress(self, run, stage):
        if run['status'] not in TERMINAL:
            run['stage'] = stage
            self.event(run, 'progress', {'stage': stage})

    async def clock(self, run):
        while run['status'] not in TERMINAL:
            await asyncio.sleep(1)
            if run['status'] in ('queued', 'running'):
                self.event(run, 'heartbeat', {})

    def event(self, run, kind, payload):
        seq = len(run['_events']) + 1
        run['elapsed_ms'] = self.elapsed(run)
        run['event_sequence'] = seq
        run['_events'].append(dict(seq=seq, type=kind, status=run['status'], stage=run['stage'], elapsed_ms=run['elapsed_ms'], payload=payload))
        self.save(run)

    def get(self, identity):
        run = next((r for r in self.runs.values() if identity in (r['run_id'], r['plan_id'])), None)
        if run is None:
            raise HTTPException(404, 'Unknown isolated demo run')
        return run

    def snapshot(self, identity):
        run = self.get(identity)
        return copy.deepcopy({**{k: v for k, v in run.items() if not k.startswith('_')}, 'elapsed_ms': self.elapsed(run)})

    def create(self, question, session_id=None, history=None, include_literature=None):
        if sum(r['status'] in ('queued', 'running') for r in self.runs.values()) >= 8:
            raise HTTPException(429, 'The development agent queue is full')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise HTTPException(422, 'A question of 1–4000 characters is required')
        rid, pid = 'simple-' + uuid.uuid4().hex, 'plan-' + uuid.uuid4().hex
        run = dict(run_id=rid, plan_id=pid, session_id=session_id or uuid.uuid4().hex, question=question,
                   status='queued', stage='queued', event_sequence=0, elapsed_ms=0, _elapsed_before=0, _active_since=time.time(), _events=[], _history=history or [],
                   _include_literature=self.literature is not None if include_literature is None else bool(include_literature))
        run['_literature_override'] = include_literature
        self.runs[rid] = run
        self.event(run, 'progress', {'message': 'The agent is composing a query and checking a bounded graph preview.'})
        self.tasks[rid] = asyncio.create_task(self.prepare(run))
        self.clocks[rid] = asyncio.create_task(self.clock(run))
        return {k: run[k] for k in ('run_id', 'plan_id', 'session_id', 'status')}

    async def prepare(self, run):
        try:
            async with self.gate:
                if run['status'] != 'queued':
                    return
                run.update(status='running', stage='planning')
                self.event(run, 'progress', {'message': 'Inspecting schema, resolving entities and checking the composed query.'})
                result = await asyncio.wait_for(self.runner(run['question'], run['_history'], lambda stage: self.progress(run, stage)), 240)
            if run['status'] in TERMINAL:
                return
            run['_result'] = result
            if run.get('_literature_override') is None and isinstance(result.get('include_literature'), bool):
                run['_include_literature'] = result['include_literature'] and self.literature is not None
            status = result.get('status', 'failed')
            eligible = status in ('complete', 'empty', 'partial') and bool(result.get('metapath'))
            audit = '\n'.join('- ' + a['condition'] + ': ' + a['binding'] for a in result.get('condition_audit', []))
            pattern = result.get('metapath') or {'nodes': [], 'edges': []}
            descriptions = []
            for node in pattern['nodes']:
                filters = ', '.join(f"{f['property']} {f['op']} {json.dumps(f['value'])}" for f in node.get('filters', []))
                descriptions.append(f"{node['key']}: {node['label']}" + (f" ({filters})" if filters else ''))
            for edge in pattern['edges']:
                filters = ', '.join(f"{f['property']} {f['op']} {json.dumps(f['value'])}" for f in edge.get('filters', []))
                direction = '→' if edge.get('direction', 'out') == 'out' else '↔'
                descriptions.append(f"{edge['source']} {direction} {edge['target']}: {edge['type']}" + (f" ({filters})" if filters else ''))
            summary = 'Requested conditions and bindings:\n' + audit + '\n\nGraph to retrieve:\n' + '\n'.join('- ' + d for d in descriptions)
            issues = result.get('unfulfilled_conditions', [])
            if status == 'partial':
                issues = issues + ['The bounded preview is partial; confirmation does not make it complete.']
            run['plan'] = dict(cypher=result.get('cypher'), parameters=result.get('parameters'), metapath=result.get('metapath'), original_question=run['question'], interpreted_question=result.get('standardized_question', run['question']),
                steps=[dict(id='graph', title='Retrieve the requested nodes and relationships', rationale='One agent-composed graph query, checked against schema and development data.', semantic_summary=summary, semantic_issues=issues, resolved_entities=[])], literature=run['_include_literature'],
                literature_intent={'included': run['_include_literature'], 'summary': 'After confirmation, retrieve independent HIRN and GLKB literature context.' if run['_include_literature'] else 'Graph evidence only.'})
            if not eligible:
                run['plan']['clarification'] = result.get('clarification') or result.get('answer') or 'No executable graph plan is ready.'
            version = run['run_id'] + ':checked-preview'
            evidence = dict(graph_version=version, completeness=result.get('retrieval_status') or status, truncated=result.get('retrieval_status') == 'partial')
            run['preview'] = dict(status='complete' if eligible else 'not_requested', confirmation_eligible=eligible, evidence=evidence, steps=[])
            self.pause(run)
            run.update(status='awaiting_confirmation', stage='awaiting_confirmation')
            if run['run_id'] in self.clocks:
                self.clocks[run['run_id']].cancel()
            self.event(run, 'plan_ready', {k: run[k] for k in ('plan', 'plan_id', 'preview')})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if run['status'] not in TERMINAL:
                self.pause(run)
                run.update(status='failed', stage='Failed', errors=[{'message': str(exc)[:500]}])
                self.event(run, 'terminal', {'status': 'failed', 'errors': run['errors']})

    def confirm(self, identity):
        run = self.get(identity)
        if run.get('_confirmed'):
            return {'run_id': run['run_id'], 'status': run['status']}
        if run['status'] != 'awaiting_confirmation' or not run.get('preview', {}).get('confirmation_eligible') or run.get('plan', {}).get('clarification'):
            raise HTTPException(409, 'A reviewed, executable plan is required before confirmation')
        result = run['_result']
        run['_confirmed'] = True
        run['evidence'] = run['preview']['evidence']
        run['_active_since'] = time.time()
        self.clocks[run['run_id']] = asyncio.create_task(self.clock(run))
        run.update(graph_complete=True, status='running', stage='writing_answer')
        self.event(run, 'progress', {'message': 'The independent formatting agent is preparing the final output from the approved graph evidence.'})
        self.tasks[run['run_id']] = asyncio.create_task(self.format(run))
        return {'run_id': run['run_id'], 'status': run['status']}

    async def format(self, run):
        try:
            if self.formatter is None:
                raise RuntimeError('No independent final-output formatter is configured')
            def on_delta(text):
                if run['status'] not in TERMINAL:
                    run['graph_answer'] = run.get('graph_answer', '') + text
                    self.event(run, 'graph_answer', {'text': text, 'delta': True})
            kwargs = {'on_delta': on_delta} if 'on_delta' in inspect.signature(self.formatter).parameters else {}
            formatted = await asyncio.wait_for(self.formatter(run['plan']['interpreted_question'], run['_result'], **kwargs), 150)
            if run['status'] in TERMINAL:
                return
            run['_formatted'] = formatted
            run['graph_answer'] = formatted['answer']
            partial = run['_result']['status'] == 'partial' or formatted.get('stop_reason') == 'max_tokens'
            self.event(run, 'graph_answer', {'answer': run['graph_answer'], 'delta': False, 'evidence': run['evidence']})
            run['followup_ready'] = True
            self.event(run, 'followup_ready', {'followup_ready': True})
            if run.get('_include_literature') and self.literature:
                run.update(stage='searching_literature', literature={'status': 'pending', 'sources': {}, 'perspectives': []})
                self.event(run, 'progress', {'stage': 'searching_literature'})
                def literature_event(kind, payload):
                    if run['status'] in TERMINAL:return
                    if kind == 'literature_sources':run['literature'] = payload
                    self.event(run, kind, payload)
                try:
                    run['literature'] = await asyncio.wait_for(self.literature(run['plan']['interpreted_question'], run['_history'], run['graph_answer'], literature_event), 360)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    run['literature'] = {**run.get('literature', {}), 'status': 'unavailable', 'error_category': type(exc).__name__}
                run['literature_complete'] = True
                self.event(run, 'literature_complete', run['literature'])
                partial = partial or run['literature']['status'] not in ('complete', 'no_evidence')
            self.pause(run)
            run.update(status='partial' if partial else 'completed', stage='Completed')
            self.event(run, 'terminal', {'status': run['status']})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if run['status'] not in TERMINAL:
                self.pause(run)
                run.update(status='partial', stage='Completed', errors=[{'component': 'formatter', 'message': str(exc)[:500]}])
                run['_format_failure'] = getattr(exc, 'metadata', {})
                run['graph_answer'] = run.get('graph_answer') or 'The approved graph is available, but the formatting agent could not produce the final answer.'
                self.event(run, 'graph_answer', {'answer': run['graph_answer'], 'delta': False, 'evidence': run['evidence']})
                self.event(run, 'terminal', {'status': 'partial', 'errors': run['errors']})

    def retry_formatter(self, identity):
        run = self.get(identity)
        if not run.get('_confirmed') or run['status'] != 'partial' or not any(e.get('component') == 'formatter' for e in run.get('errors', [])):
            raise HTTPException(409, 'Only a confirmed run with a formatter failure can retry formatting')
        run['_format_failure_previous'] = run.pop('_format_failure', {})
        run.pop('errors', None)
        run['graph_answer'] = ''
        run['_active_since'] = time.time()
        run.update(status='running', stage='writing_answer')
        self.event(run, 'graph_answer', {'answer': '', 'delta': False, 'evidence': run['evidence']})
        self.clocks[run['run_id']] = asyncio.create_task(self.clock(run))
        self.tasks[run['run_id']] = asyncio.create_task(self.format(run))
        return {'run_id': run['run_id'], 'status': run['status']}

    def interaction(self, identity, body):
        run = self.get(identity)
        if not isinstance(body, dict) or len(json.dumps(body).encode()) > 20000:
            raise HTTPException(422, 'Interaction must be a bounded object')
        run.setdefault('_interactions', []).append({'at': time.time(), 'body': copy.deepcopy(body)})
        self.save(run)
        return {'accepted': True}

    def cancel(self, identity, status='cancelled'):
        run = self.get(identity)
        if run['status'] not in TERMINAL:
            self.pause(run)
            run.update(status=status, stage=status.capitalize())
            self.tasks.get(run['run_id']) and self.tasks[run['run_id']].cancel()
            for key, task in self.presentations.items():
                if key.startswith(run['run_id'] + ':'):task.cancel()
            self.event(run, 'terminal', {'status': status})
        return {'run_id': run['run_id'], 'status': run['status']}

    def revise(self, identity, instruction):
        run = self.get(identity)
        if run['status'] not in ('queued', 'running', 'awaiting_confirmation'):
            raise HTTPException(409, 'This plan can no longer be revised')
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
            raise HTTPException(422, 'A revision instruction is required')
        history = run['_history'] + [{'original_question': run['question'], 'reviewed_plan': run.get('plan'), 'revision_instruction': instruction}]
        self.cancel(identity, 'superseded')
        return self.create(run['question'], run['session_id'], history[-8:], run.get('_include_literature', False))

    def projection(self, identity, phase):
        run = self.get(identity)
        if phase not in ('preview', 'final'):
            raise HTTPException(422, 'Unknown graph phase')
        if phase == 'final' and not run.get('_confirmed'):
            raise HTTPException(409, 'Confirm the plan before requesting its final result')
        if '_result' not in run or not run.get('preview', {}).get('confirmation_eligible'):
            raise HTTPException(409, 'The graph preview is not ready')
        result = run['_result']
        graph = result.get('graph') or {'nodes': [], 'edges': []}
        nodes = [{**n, '~id': str(n.get('~id', n.get('id'))), '~labels': n.get('~labels', n.get('labels', [])), '~properties': n.get('~properties', n.get('properties', {}))} for n in graph['nodes']]
        edges = [{**e, '~id': str(e.get('~id', e.get('id'))), '~start': str(e.get('~start', e.get('source', e.get('start')))), '~end': str(e.get('~end', e.get('target', e.get('end')))), '~type': e.get('~type', e.get('type')), '~properties': e.get('~properties', e.get('properties', {}))} for e in graph['edges']]
        radius = max(100, len(nodes) * 12)
        xy = {n['~id']: {'x': radius * math.cos(2 * math.pi * i / max(1,len(nodes))), 'y': radius * math.sin(2 * math.pi * i / max(1,len(nodes)))} for i,n in enumerate(nodes)}
        value = dict(result_id=run['run_id'] + ':' + phase, status='ready', question=run['question'], title=run['plan']['interpreted_question'],
                    combined_query_result={'nodes': nodes, 'edges': edges}, xy_json=xy, resources_tabs={}, literature=run.get('literature', []) if phase == 'final' else [],
                    answer=run.get('graph_answer', '') if phase == 'final' else '', evidence=run['preview']['evidence'], completeness=result.get('retrieval_status') or result['status'],
                    component_status={'graph': 'available' if nodes else 'empty', 'layout': 'available'},
                    source={'kind': 'agent', 'run_id': run['run_id'], 'phase': phase}, display={'notice': 'Bounded development KG preview. Confirm the reviewed plan to display the final answer.' if phase == 'preview' else 'Showing ' + str(len(nodes)) + ' retrieved nodes and ' + str(len(edges)) + ' relationships. Retrieval status: ' + str(result.get('retrieval_status') or result['status']) + '.'})
        feature = run.get('_presentation', {}).get(phase, {})
        value.update(feature.get('presentation', {}))
        value['resources_tabs'] = feature.get('resources', {}).get('resources_tabs', {})
        value['component_status'] = {**value['component_status'], 'resources': feature.get('resources', {}).get('status', 'pending' if self.presentation else 'not_requested')}
        return value

    def request_presentation(self, identity, phase):
        value = self.projection(identity, phase)
        key = value['result_id']
        run = self.get(identity)
        if self.presentation and key not in self.presentations and phase not in run.get('_presentation', {}):
            self.presentations[key] = asyncio.create_task(self.prepare_presentation(run, phase))
        return value

    async def prepare_presentation(self, run, phase):
        from .formatter import evidence_input
        feature = run.setdefault('_presentation', {}).setdefault(phase, {})
        steps = evidence_input(run['question'], run['_result'], self.graph_release)
        evidence = {'steps': list(steps.values()), 'graph_version': self.graph_release,
                    'completeness': run['_result']['retrieval_status'], 'truncated': run['_result']['retrieval_status'] == 'partial'}
        def receive(kind, payload):
            if kind == 'presentation':feature['presentation'] = payload;self.save(run)
        try:
            feature['resources'] = await asyncio.wait_for(self.presentation(evidence, [], receive), 45)
        except asyncio.CancelledError:
            feature['resources'] = {'status': 'cancelled'}
            raise
        except Exception as exc:
            feature['resources'] = {'status': 'unavailable', 'error_category': type(exc).__name__}
        finally:self.save(run)


def router(workflow):
    api = APIRouter()
    @api.post('/agent/v2/plans', status_code=202)
    async def create(body: dict):
        session = body.get('session_id')
        history = [{'question': r['question'], 'standardized_question': r['plan']['interpreted_question'],
                    'metapath': r['_result'].get('metapath'), 'answer': r.get('graph_answer')}
                   for r in workflow.runs.values() if session and r['session_id'] == session and r.get('_confirmed')]
        return workflow.create(body.get('question'), session, history[-8:] if body.get('include_context', True) else [], body.get('include_literature'))
    @api.get('/agent/v2/runs/{identity}')
    async def read(identity: str):
        return workflow.snapshot(identity)
    @api.get('/agent/v2/plans/{identity}')
    async def plan(identity: str):
        return workflow.snapshot(identity)
    @api.post('/agent/v2/plans/{identity}/confirm')
    async def confirm(identity: str):
        return workflow.confirm(identity)
    @api.post('/agent/v2/plans/{identity}/revise', status_code=202)
    async def revise(identity: str, body: dict):
        return workflow.revise(identity, body.get('revision_instruction') or body.get('question'))
    @api.post('/agent/v2/runs/{identity}/retry-format')
    async def retry_format(identity: str):
        return workflow.retry_formatter(identity)
    @api.post('/agent/v2/runs/{identity}/cancel')
    async def cancel(identity: str):
        return workflow.cancel(identity)
    @api.get('/agent/v2/runs/{identity}/events')
    async def events(identity: str, request: Request, after: int = 0):
        run = workflow.get(identity)
        cursor = max(after, int(request.headers.get('last-event-id', '0')))
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                for event in run['_events']:
                    if event['seq'] > cursor:
                        cursor = event['seq']
                        yield 'id: %s\nevent: %s\ndata: %s\n\n' % (cursor, event['type'], json.dumps(event))
                if run['status'] in TERMINAL:
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})
    @api.post('/results', status_code=202)
    async def project(body: dict):
        value = workflow.request_presentation(body.get('run_id'), body.get('phase', 'final'))
        return {'result_id': value['result_id'], 'status': value['status']}
    @api.get('/results/{identity}')
    async def result(identity: str):
        rid, phase = identity.rsplit(':', 1)
        return workflow.projection(rid, phase)
    @api.post('/agent/v2/runs/{identity}/interactions')
    async def interaction(identity: str, body: dict):
        return workflow.interaction(identity, body)
    @api.post('/results/{identity}/interactions')
    async def result_interaction(identity: str, body: dict):
        return workflow.interaction(identity.rsplit(':', 1)[0], body)
    return api
