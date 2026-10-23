"""Existing frontend protocol around one agent loop; explicit confirmation releases its answer."""
import asyncio
import copy
import json
import inspect
import hashlib
import math
import os
import time
import traceback
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
        # Model phases end only when the model finishes or the user cancels.
        self.enrichment_seconds = None
        self.requests_directory = self.directory / 'requests'
        self.requests_directory.mkdir(mode=0o700, exist_ok=True)
        self.requests = {p.stem: json.loads(p.read_text()) for p in self.requests_directory.glob('*.json')}
        for p in self.directory.glob('*.json'):
            run = json.loads(p.read_text())
            unfinished = run['status'] in ('queued', 'running')
            if unfinished:
                run.update(status='interrupted', stage='Interrupted', _active_since=None, _elapsed_before=run.get('elapsed_ms', 0))
            if 'elapsed_ms' not in run:
                run['elapsed_ms'] = int(1000 * (run.get('_result', {}).get('metrics', {}).get('elapsed_s', 0) + run.get('_formatted', {}).get('elapsed_s', 0)))
                run['_elapsed_before'] = run['elapsed_ms']
                run['_active_since'] = None
            self.runs[run['run_id']] = run
            if run.get('request_id') and run['request_id'] not in self.requests:
                # Repair admission interrupted between atomic run and request writes.
                self.save_request({**{k: run[k] for k in ('request_id', 'run_id', 'plan_id', 'session_id', 'status')}, '_payload_hash': run.get('_request_hash')})
            for feature in run.get('_presentation', {}).values():
                if feature.get('resources', {}).get('status') in (None, 'pending', 'queued', 'running'):
                    feature['resources'] = {**feature.get('resources', {}), 'status': 'interrupted'}
                    self.save(run)
            if unfinished:
                self.record_interruption(run, RuntimeError('Service restarted before the model finished'),
                                         'enrich' if run.get('_confirmed') else 'prepare')

    def save(self, run):
        p = self.directory / (run['run_id'] + '.json')
        temp = p.with_suffix('.tmp')
        with temp.open('w') as handle:
            temp.chmod(0o600)
            json.dump(run, handle, ensure_ascii=False)
            handle.flush(); os.fsync(handle.fileno())
        temp.replace(p)
        parent_fd = os.open(p.parent, os.O_RDONLY)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)

    def save_request(self, record):
        p = self.requests_directory / (record['request_id'] + '.json')
        temp = p.with_suffix('.tmp')
        with temp.open('w') as handle:
            temp.chmod(0o600)
            json.dump(record, handle)
            handle.flush(); os.fsync(handle.fileno())
        temp.replace(p)
        parent_fd = os.open(p.parent, os.O_RDONLY)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
        self.requests[record['request_id']] = record

    def request_key(self, request_id):
        if request_id is None:
            return None
        try:
            return str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, 'request_id must be a UUID')

    def request_record(self, request_id):
        key = self.request_key(request_id)
        record = self.requests.get(key)
        if record is None:
            raise HTTPException(404, 'Request not admitted')
        value = {k: v for k, v in record.items() if not k.startswith('_')}
        if record.get('run_id') in self.runs:
            value['status'] = self.runs[record['run_id']]['status']
        return value

    def repeat_request(self, request_id, payload):
        key = self.request_key(request_id)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        previous = self.requests.get(key)
        if previous:
            if previous.get('_payload_hash') and previous['_payload_hash'] != digest:
                raise HTTPException(409, 'request_id already belongs to a different intent')
            if previous.get('cancel_requested'):
                raise HTTPException(409, 'Request cancelled before admission')
            return key, digest, self.request_record(key)
        return key, digest, None

    def cancel_request(self, request_id):
        key = self.request_key(request_id)
        record = self.requests.get(key, {'request_id': key, 'status': 'cancelled'})
        record['cancel_requested'] = True
        self.save_request(record)
        if record.get('run_id') in self.runs:
            self.cancel(record['run_id'])
        return self.request_record(key)

    def commit_graph(self, run, result, phase):
        graph = result.get('graph') or {'nodes': [], 'edges': []}
        canonical = {kind: sorted(records, key=lambda item: str(item.get('id', item.get('~id')))) for kind, records in graph.items()}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
        if run.get('_graph_hash') != digest:
            run['graph_revision'] = run.get('graph_revision', 0) + 1
            run['_graph_hash'] = digest
        revision = run.get('graph_revision', 1)
        run.setdefault('_graphs', {})[str(revision)] = copy.deepcopy(result)
        run.setdefault('_phase_revisions', {})[phase] = revision
        run['kg_release'] = self.graph_release
        return revision

    def graph_metadata(self, run, phase):
        result = self.graph_result(run, phase)
        graph = result.get('graph') or {'nodes': [], 'edges': []}
        revision = run.get('_phase_revisions', {}).get(phase, run.get('graph_revision', 0))
        return {'run_id': run['run_id'], 'result_id': run['run_id'] + ':' + phase,
                'graph_revision': revision, 'kg_release': self.graph_release,
                'graph_complete': phase == 'final', 'readiness': 'ready',
                'completeness': result.get('retrieval_status') or result.get('status'),
                'node_count': len(graph['nodes']), 'edge_count': len(graph['edges']),
                'evidence': run.get('evidence') if phase == 'final' else run.get('preview', {}).get('evidence'),
                'enrichment': run.get('enrichment')}

    def graph_result(self, run, phase, revision=None):
        selected = revision if revision is not None else run.get('_phase_revisions', {}).get(phase)
        if selected is not None:
            result = run.get('_graphs', {}).get(str(selected))
            if result is None:
                raise HTTPException(409, 'Graph revision is not committed')
            return result
        # Historical runs and final reads during enrichment retain approved evidence.
        return run.get('_committed_result', run.get('_approved_result', run['_result'])) if phase == 'final' else run.get('_approved_result', run['_result'])

    def elapsed(self, run):
        started = run.get('_active_since')
        return int(run.get('_elapsed_before', run.get('elapsed_ms', 0)) + (max(0, time.time() - started) * 1000 if started else 0))

    def queue_elapsed(self, run):
        waiting = run['status'] in ('queued', 'running') and run.get('_active_since') is None
        return int(run.get('queue_elapsed_ms', 0) + (1000 * max(0, time.time() - run.get('_queued_at', time.time())) if waiting else 0))

    def pause(self, run):
        run['queue_elapsed_ms'] = self.queue_elapsed(run)
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
        run['_events'].append(dict(seq=seq, run_id=run['run_id'], graph_revision=run.get('graph_revision', 0), queue_elapsed_ms=self.queue_elapsed(run), type=kind, status=run['status'], stage=run['stage'], elapsed_ms=run['elapsed_ms'], payload=payload))
        self.save(run)

    def get(self, identity):
        run = next((r for r in self.runs.values() if identity in (r['run_id'], r['plan_id'])), None)
        if run is None:
            raise HTTPException(404, 'Unknown isolated demo run')
        return run

    def snapshot(self, identity):
        run = self.get(identity)
        return copy.deepcopy({**{k: v for k, v in run.items() if not k.startswith('_')}, 'elapsed_ms': self.elapsed(run), 'queue_elapsed_ms': self.queue_elapsed(run)})

    def create(self, question, session_id=None, history=None, include_literature=None, request_id=None, intent=None):
        payload = intent or dict(question=question, session_id=session_id, include_literature=include_literature)
        key, digest, previous = self.repeat_request(request_id, payload)
        if previous:
            return previous
        if sum(r['status'] in ('queued', 'running') for r in self.runs.values()) >= 8:
            raise HTTPException(429, 'The development agent queue is full')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise HTTPException(422, 'A question of 1–4000 characters is required')
        rid, pid = 'simple-qwen-' + uuid.uuid4().hex, 'plan-qwen-' + uuid.uuid4().hex
        run = dict(run_id=rid, plan_id=pid, session_id=session_id or uuid.uuid4().hex, question=question,
                   status='queued', stage='queued', event_sequence=0, elapsed_ms=0, _elapsed_before=0, _active_since=None, _queued_at=time.time(), queue_elapsed_ms=0, _events=[], _history=history or [],
                   _include_literature=self.literature is not None if include_literature is None else bool(include_literature))
        run['_literature_override'] = include_literature
        run['request_id'] = key
        run['_request_hash'] = digest
        self.runs[rid] = run
        self.save(run)
        if key:
            self.save_request(dict(request_id=key, run_id=rid, plan_id=pid, session_id=run['session_id'], status='queued', _payload_hash=digest))
        self.event(run, 'progress', {'message': 'The agent is composing a query and checking a bounded graph preview.'})
        self.tasks[rid] = asyncio.create_task(self.prepare(run))
        self.clocks[rid] = asyncio.create_task(self.clock(run))
        return {k: run[k] for k in ('run_id', 'plan_id', 'session_id', 'request_id', 'status')}

    def record_interruption(self, run, exc, phase):
        # Infrastructure cannot decide scientific failure or commit unfinished
        # enrichment. Keep the latest checkpoint and the approved graph intact.
        record = {'component': 'workflow', 'category': 'infrastructure',
                  'phase': phase, 'type': type(exc).__name__, 'message': str(exc),
                  'model_decision': False, 'at': time.time(),
                  'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}
        run.setdefault('_runtime_errors', []).append(record)
        public_record = {key: value for key, value in record.items() if key != 'traceback'}
        run.setdefault('errors', []).append(public_record)
        self.pause(run)
        run.update(status='interrupted', stage='Service interrupted',
                   interruption={'phase': phase, 'category': 'infrastructure',
                                 'model_decision': False, 'checkpoint_saved': '_checkpoint' in run,
                                 'approved_evidence_preserved': '_approved_result' in run})
        if phase == 'enrich':
            run['enrichment'] = {'status': 'interrupted', 'error_category': type(exc).__name__,
                                 'model_decision': False}
        self.event(run, 'terminal', {'status': 'interrupted', 'errors': run['errors'],
                                    'interruption': run['interruption']})

    async def prepare(self, run):
        def checkpoint(result):
            if run['status'] not in TERMINAL:
                run['_checkpoint'] = copy.deepcopy(result)
                self.save(run)
        try:
            async with self.gate:
                if run['status'] != 'queued':
                    return
                run.update(status='running', stage='planning', _active_since=time.time(), queue_elapsed_ms=int(1000 * (time.time() - run['_queued_at'])))
                self.event(run, 'progress', {'message': 'Inspecting schema, resolving entities and checking the composed query.'})
                result = await self.runner(run['question'], run['_history'],
                    lambda stage: self.progress(run, stage), on_checkpoint=checkpoint)
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
                filters = ', '.join(f"{f['property']} {f['op']}" + (f" {json.dumps(f['value'])}" if 'value' in f else '') for f in node.get('filters', []))
                descriptions.append(f"{node['key']}: {node['label']}" + (f" ({filters})" if filters else ''))
            for edge in pattern['edges']:
                filters = ', '.join(f"{f['property']} {f['op']}" + (f" {json.dumps(f['value'])}" if 'value' in f else '') for f in edge.get('filters', []))
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
            revision = self.commit_graph(run, result, 'preview')
            version = run['run_id'] + ':checked-preview'
            evidence = dict(graph_revision=revision, kg_release=self.graph_release, graph_version=version, completeness=result.get('retrieval_status') or status, truncated=result.get('retrieval_status') == 'partial')
            run.setdefault('_graph_evidence', {})[str(revision)] = copy.deepcopy(evidence)
            run['preview'] = dict(status='complete' if eligible else 'not_requested', confirmation_eligible=eligible, evidence=evidence, steps=[])
            run.update({k: v for k, v in self.graph_metadata(run, 'preview').items() if k not in ('evidence', 'enrichment')})
            self.pause(run)
            run.update(status='awaiting_confirmation', stage='awaiting_confirmation')
            if run['run_id'] in self.clocks:
                self.clocks[run['run_id']].cancel()
            self.event(run, 'plan_ready', {k: run[k] for k in ('plan', 'plan_id', 'preview', 'graph_revision', 'kg_release')})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if run['status'] not in TERMINAL:
                self.record_interruption(run, exc, 'prepare')

    def confirm(self, identity):
        run = self.get(identity)
        if run.get('_confirmed'):
            return {'run_id': run['run_id'], 'status': run['status']}
        if run['status'] != 'awaiting_confirmation' or not run.get('preview', {}).get('confirmation_eligible') or run.get('plan', {}).get('clarification'):
            raise HTTPException(409, 'A reviewed, executable plan is required before confirmation')
        result = run['_result']
        run['_approved_result'] = copy.deepcopy(result)
        run['_confirmed'] = True
        run['evidence'] = run['preview']['evidence']
        run['_active_since'] = None
        run['_queued_at'] = time.time()
        self.clocks[run['run_id']] = asyncio.create_task(self.clock(run))
        run.update(graph_complete=False, status='running', stage='enriching_graph', enrichment={'status': 'pending'})
        self.event(run, 'progress', {'message': 'Reading saved evidence and retrieving linked context before formatting.'})
        self.tasks[run['run_id']] = asyncio.create_task(self.enrich_and_format(run))
        return {'run_id': run['run_id'], 'status': run['status']}

    async def enrich_and_format(self, run):
        started = None
        def checkpoint(result):
            if run['status'] not in TERMINAL:
                run['_checkpoint'] = copy.deepcopy(result)
                # A checkpoint is private until the model finishes this phase.
                if result.get('graph') is not None:
                    run['_result'] = copy.deepcopy(result)
                self.save(run)
        try:
            async with self.gate:
                if run['status'] in TERMINAL:
                    return
                started = time.monotonic()
                run['_active_since'] = time.time()
                run['queue_elapsed_ms'] += int(1000 * (time.time() - run['_queued_at']))
                result = await self.runner(run['question'], run['_history'],
                    lambda stage: self.progress(run, stage), phase='enrich', seed=run['_approved_result'],
                    on_checkpoint=checkpoint)
                checkpoint(result)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if run['status'] not in TERMINAL:
                self.record_interruption(run, exc, 'enrich')
            return
        if run['status'] in TERMINAL:
            return
        run['_phase_metrics'] = {'prepare': run['_approved_result'].get('metrics', {}),
                                'enrich': {**run['_result'].get('metrics', {}), 'elapsed_s': round(time.monotonic() - (started or time.monotonic()), 3)}}
        # Publish one immutable snapshot; checkpoints remain private until this commit.
        run['_committed_result'] = copy.deepcopy(run['_result'])
        revision = self.commit_graph(run, run['_committed_result'], 'final')
        run['enrichment'] = run['_result'].get('enrichment', {'status': 'complete'})
        version = run['run_id'] + ':enriched'
        run['evidence'] = {**run['preview']['evidence'], 'graph_version': version, 'graph_revision': revision, 'kg_release': self.graph_release,
                           'completeness': run['_result'].get('retrieval_status'),
                           'truncated': run['_result'].get('retrieval_status') == 'partial'}
        run.setdefault('_graph_evidence', {})[str(revision)] = copy.deepcopy(run['evidence'])
        task = self.presentations.pop(run['run_id'] + ':final', None)
        if task:
            task.cancel()
        run.setdefault('_presentation', {}).pop('final', None)
        run.update(graph_complete=True, stage='writing_answer')
        metadata = self.graph_metadata(run, 'final')
        run.update(metadata)
        self.event(run, 'graph_step', metadata)
        self.event(run, 'progress', {'message': 'The independent formatting agent is preparing the final output from the selected evidence.'})
        await self.format(run)

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
            run.setdefault('_phase_metrics', {})['formatter'] = {
                k: formatted.get(k) for k in ('elapsed_s', 'provider_elapsed_s', 'usage', 'model')}
            run['graph_answer'] = formatted['answer']
            partial = (run['_result']['status'] == 'partial'
                       or run.get('enrichment', {}).get('status') == 'model_reported_error'
                       or formatted.get('stop_reason') == 'max_tokens')
            self.event(run, 'graph_answer', {'answer': run['graph_answer'], 'delta': False, **self.graph_metadata(run, 'final')})
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
                    literature = await asyncio.wait_for(self.literature(run['plan']['interpreted_question'], run['_history'], run['graph_answer'], literature_event), 360)
                    if run['status'] in TERMINAL: return
                    run['literature'] = literature
                except asyncio.CancelledError:raise
                except Exception as exc:
                    if run['status'] in TERMINAL: return
                    run['literature'] = {**run.get('literature', {}), 'status': 'unavailable', 'error_category': type(exc).__name__}
                run['literature_complete'] = True
                self.event(run, 'literature_complete', run['literature'])
                partial = partial or run['literature']['status'] not in ('complete', 'no_evidence')
            if run['status'] in TERMINAL: return
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
                self.event(run, 'graph_answer', {'answer': run['graph_answer'], 'delta': False, **self.graph_metadata(run, 'final')})
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
            run.update(status=status, stage=status.capitalize(), _cancel_requested=True)
            self.event(run, 'terminal', {'status': status})
            self.tasks.get(run['run_id']) and self.tasks[run['run_id']].cancel()
        if not run.get('_cancel_requested'):
            run['_cancel_requested'] = True
            self.save(run)
        # Optional presentation can still be active after the core run ends.
        for key, task in self.presentations.items():
            if key.startswith(run['run_id'] + ':'): task.cancel()
        return {'run_id': run['run_id'], 'status': run['status'], 'cancel_accepted': True, 'cleanup': 'requested'}

    def revise(self, identity, instruction, request_id=None, include_context=True):
        key, digest, previous = self.repeat_request(request_id, dict(revision_of=identity, instruction=instruction, include_context=include_context))
        if previous:
            return previous
        run = self.get(identity)
        if run['status'] not in ('queued', 'running', 'awaiting_confirmation'):
            raise HTTPException(409, 'This plan can no longer be revised')
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
            raise HTTPException(422, 'A revision instruction is required')
        history = run['_history'] + [{'original_question': run['question'], 'reviewed_plan': run.get('plan'), 'revision_instruction': instruction}]
        replacement = self.create(run['question'], run['session_id'], history if include_context else [], run.get('_include_literature', False), key, dict(revision_of=identity, instruction=instruction, include_context=include_context))
        self.cancel(identity, 'superseded')
        return replacement

    def projection(self, identity, phase, graph_revision=None):
        run = self.get(identity)
        if phase not in ('preview', 'final'):
            raise HTTPException(422, 'Unknown graph phase')
        if phase == 'final' and not run.get('_confirmed'):
            raise HTTPException(409, 'Confirm the plan before requesting its final result')
        if '_result' not in run or not run.get('preview', {}).get('confirmation_eligible'):
            raise HTTPException(409, 'The graph preview is not ready')
        if graph_revision is not None and (type(graph_revision) is not int or graph_revision < 1):
            raise HTTPException(422, 'graph_revision must be a positive integer')
        result = self.graph_result(run, phase, graph_revision)
        graph = result.get('graph') or {'nodes': [], 'edges': []}
        nodes = [{**n, '~id': str(n.get('~id', n.get('id'))), '~labels': n.get('~labels', n.get('labels', [])), '~properties': n.get('~properties', n.get('properties', {}))} for n in graph['nodes']]
        edges = [{**e, '~id': str(e.get('~id', e.get('id'))), '~start': str(e.get('~start', e.get('source', e.get('start')))), '~end': str(e.get('~end', e.get('target', e.get('end')))), '~type': e.get('~type', e.get('type')), '~properties': e.get('~properties', e.get('properties', {}))} for e in graph['edges']]
        radius = max(100, len(nodes) * 12)
        xy = {n['~id']: {'x': radius * math.cos(2 * math.pi * i / max(1,len(nodes))), 'y': radius * math.sin(2 * math.pi * i / max(1,len(nodes)))} for i,n in enumerate(nodes)}
        revision = graph_revision if graph_revision is not None else run.get('_phase_revisions', {}).get(phase, run.get('graph_revision', 0))
        value = dict(graph_revision=revision, kg_release=self.graph_release, readiness='ready', run_id=run['run_id'], result_id=run['run_id'] + ':' + phase, status='ready', question=run['question'], title=run['plan']['interpreted_question'],
                    combined_query_result={'nodes': nodes, 'edges': edges}, xy_json=xy, resources_tabs={}, literature=run.get('literature', []) if phase == 'final' else [],
                    answer=run.get('graph_answer', '') if phase == 'final' else '', evidence=run.get('evidence', run['preview']['evidence']) if phase == 'final' else run['preview']['evidence'], completeness=result.get('retrieval_status') or result['status'],
                    component_status={'graph': 'available' if nodes else 'empty', 'layout': 'available'},
                    source={'kind': 'agent', 'run_id': run['run_id'], 'phase': phase}, display={'notice': 'Bounded development KG preview. Confirm the reviewed plan to display the final answer.' if phase == 'preview' else 'Showing ' + str(len(nodes)) + ' retrieved nodes and ' + str(len(edges)) + ' relationships. Retrieval status: ' + str(result.get('retrieval_status') or result['status']) + '.'})
        feature = run.get('_presentation', {}).get(f'{phase}:{revision}', run.get('_presentation', {}).get(phase, {}) if not revision else {})
        value.update(feature.get('presentation', {}))
        value['resources_tabs'] = feature.get('resources', {}).get('resources_tabs', {})
        value.update(graph_revision=revision, kg_release=self.graph_release, readiness='ready', node_count=len(graph['nodes']), edge_count=len(graph['edges']), enrichment=run.get('enrichment'))
        if graph_revision is not None and revision != run.get('_phase_revisions', {}).get(phase):
            value['evidence'] = run.get('_graph_evidence', {}).get(str(revision), value['evidence'])
            value['completeness'] = value['evidence'].get('completeness', value['completeness'])
        value['component_status'] = {**value['component_status'], 'resources': feature.get('resources', {}).get('status', 'pending' if self.presentation else 'not_requested')}
        return value

    def request_presentation(self, identity, phase, graph_revision=None):
        value = self.projection(identity, phase, graph_revision)
        revision = value['graph_revision']
        key = value['result_id'] + ':' + str(revision)
        run = self.get(identity)
        if self.presentation and key not in self.presentations and f'{phase}:{revision}' not in run.get('_presentation', {}) and not (revision == 0 and phase in run.get('_presentation', {})) and not run.get('_cancel_requested') and run['status'] not in ('cancelled', 'interrupted', 'superseded'):
            self.presentations[key] = asyncio.create_task(self.prepare_presentation(run, phase, revision))
        return value

    async def prepare_presentation(self, run, phase, revision):
        from .formatter import evidence_input
        feature = run.setdefault('_presentation', {}).setdefault(f'{phase}:{revision}', {})
        result = copy.deepcopy(self.graph_result(run, phase, revision))
        steps = evidence_input(run['question'], result, self.graph_release)
        evidence = {'steps': list(steps.values()), 'graph_version': self.graph_release,
                    'completeness': result['retrieval_status'], 'truncated': result['retrieval_status'] == 'partial'}
        def receive(kind, payload):
            if kind == 'presentation' and not run.get('_cancel_requested') and run['status'] not in ('cancelled', 'interrupted', 'superseded'):feature['presentation'] = payload;self.save(run)
        try:
            resources = await asyncio.wait_for(self.presentation(evidence, [], receive), 45)
            if not run.get('_cancel_requested') and run['status'] not in ('cancelled', 'interrupted', 'superseded'): feature['resources'] = resources
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
        return workflow.create(body.get('question'), session, history if body.get('include_context', True) else [], body.get('include_literature'), body.get('request_id'), {k: body.get(k) for k in ('question', 'session_id', 'include_context', 'include_literature')})
    @api.get('/agent/v2/requests/{request_id}')
    async def read_request(request_id: str):
        return workflow.request_record(request_id)
    @api.post('/agent/v2/requests/{request_id}/cancel')
    async def cancel_request(request_id: str):
        return workflow.cancel_request(request_id)
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
        return workflow.revise(identity, body.get('revision_instruction') or body.get('question'), body.get('request_id'), body.get('include_context', True))
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
        value = workflow.request_presentation(body.get('run_id'), body.get('phase', 'final'), body.get('graph_revision'))
        return {'result_id': value['result_id'], 'status': value['status'], 'graph_revision': value['graph_revision']}
    @api.get('/results/{identity}')
    async def result(identity: str, graph_revision: int | None = None):
        rid, phase = identity.rsplit(':', 1)
        return workflow.projection(rid, phase, graph_revision)
    @api.post('/agent/v2/runs/{identity}/interactions')
    async def interaction(identity: str, body: dict):
        return workflow.interaction(identity, body)
    @api.post('/results/{identity}/interactions')
    async def result_interaction(identity: str, body: dict):
        return workflow.interaction(identity.rsplit(':', 1)[0], body)
    return api
