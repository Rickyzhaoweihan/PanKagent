"""Revision-aware speculative preview DAG, active only behind the feature flag."""
import asyncio
from copy import deepcopy
import time
from .competing_candidates import Selection, produce
from .composable_planning import combine, digest, snapshot
from .evidence_status import checked_query_result, confirmation_eligible
from .query_assistance import advise


class PreviewRace:
    def __init__(self, runtime, run_id, plan, timeout):
        self.runtime, self.run_id, self.plan = runtime, run_id, deepcopy(plan)
        self.results, self.signatures, self.jobs = {}, {}, {}
        self.revisions = {}
        self.changed = asyncio.Event()
        self.ready = asyncio.Event()
        self.publish_enabled = asyncio.Event()
        self.database_slots = asyncio.Semaphore(2)
        self.initial_saved = False
        self.timeout = timeout
        self.task = None
        self.closed = False
        self.started = time.monotonic()
        self.first_seconds = None
        self.published = None
        self.cache = {'identity':runtime.preview_identity(plan), 'step_completed_epochs':{},
                      'step_identities':{}, 'competing_candidates':True}

    def start(self):
        self.task = asyncio.create_task(self.drive())
        return self

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

    def descendants(self, key):
        affected = {key}
        for s in self.plan['steps']:
            if affected.intersection(s.get('depends_on', [])):
                affected.add(s['id'])
        return affected

    def invalidate(self, key):
        for k in self.descendants(key):
            self.results.pop(k, None)
            self.signatures.pop(k, None)
            self.cache['step_completed_epochs'].pop(k, None)
            if k in self.jobs:
                self.jobs[k].cancel()

    async def assistance(self, kind, step, packet, parents):
        try:
            value = await asyncio.wait_for(advise(self.runtime, self.run_id, kind, step, packet, parents),
                                           min(20, self.runtime.settings.plan_timeout))
            if isinstance(value, dict) and value.get('id'):
                if value.get('operation'):
                    self.plan['combine_operations'] = [deepcopy(value['operation']) if op['id']==value['id'] else op
                                                       for op in self.plan.get('combine_operations', [])]
                if value['id'] != step['id']:
                    self.plan['steps'] = [value if s['id']==value['id'] else s for s in self.plan['steps']]
                    self.invalidate(value['id'])
                    self.changed.set()
                    return None
                # A task change invalidates any result selected under its old
                # semantics; rebuild this task rather than mixing task versions.
                if digest(value) != digest(step):
                    self.plan['steps'] = [value if s['id']==step['id'] else s for s in self.plan['steps']]
                    self.invalidate(step['id'])
                    self.changed.set()
                    return None
            return value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.runtime.io.call(self.runtime.store.audit_event, self.run_id, 'assistance_rejected',
                                       {'step_id':step['id'],'kind':kind,'reason':str(exc)[:300]})
            return None

    async def worker(self, step, parents, signature):
        key = step['id']
        selection = Selection(step, parents)
        async def offer(result, origin):
            if self.closed or self.signatures.get(key) != signature:
                return
            changed = selection.offer(result, origin)
            await self.runtime.io.call(self.runtime.store.audit_event, self.run_id, 'query_candidate', selection.records[-1])
            if changed:
                self.revisions[key] = self.revisions.get(key, 0) + 1
                self.results[key] = selection.current()
                self.results[key]['candidate_selection']['revision'] = self.revisions[key]
                self.cache['step_completed_epochs'][key] = time.time()
                self.cache['step_identities'][key] = self.runtime.preview_identity({'steps':[step], 'contract_sha256': self.plan.get('contract_sha256')})
                self.changed.set()
                if selection.conflict:
                    # Diagnosis cannot choose arbitrary membership. Only an
                    # approved structural patch and re-execution can resolve it.
                    await self.assistance('conflict', step, {'candidates':selection.records}, parents)
        async def emit(kind, payload):
            # Speculation does not change public run.stage or regress a confirmed run.
            await self.runtime.io.call(self.runtime.store.audit_event, self.run_id, kind, payload)
        try:
            failed = [d for d in step.get('depends_on', []) if not checked_query_result(
                next(s for s in self.plan['steps'] if s['id']==d), parents.get(d), parents)]
            if failed:
                result = self.runtime.failed_step(step, {'category':'dependency_unavailable','message':'Parent evidence is not verified.'})
                self.results[key] = result
            elif step.get('operation'):
                try:
                    result = combine(step, parents)
                except ValueError as exc:
                    result = self.runtime.failed_step(step, {'category':str(exc),'message':'Combination failed validation.'})
                if not checked_query_result(step, result, parents):
                    await self.assistance('combination', step, {'result':result}, parents)
                if self.signatures.get(key)==signature:
                    self.results[key] = result
                    self.cache['step_completed_epochs'][key] = time.time()
            elif step.get('session_input'):
                self.results[key] = await self.runtime.execute_step(self.run_id, step, parents)
                self.cache['step_completed_epochs'][key] = time.time()
            else:
                await produce(self.runtime.graph, step, parents, emit, offer,
                    lambda kind,s,packet:self.assistance(kind,s,packet,parents),self.database_slots)
            if self.signatures.get(key)==signature and key not in self.results:
                self.results[key] = self.runtime.failed_step(step, {'category':'no_verified_candidate','message':'No candidate passed the task contract.'})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.signatures.get(key)==signature and key not in self.results:
                self.results[key] = self.runtime.failed_step(step, {'category':type(exc).__name__,'message':'Candidate generation failed.'})
        finally:
            self.changed.set()

    async def publish(self, settled=False):
        if len(self.results) != len(self.plan['steps']):
            return
        # A coherent snapshot contains exactly the current parent's revisions.
        for step in self.plan['steps']:
            parents = {d:self.results[d] for d in step.get('depends_on', [])}
            if self.signatures.get(step['id']) != digest([step, {k:snapshot(v) for k,v in parents.items()}]):
                return
        fingerprint = digest([self.plan,self.results])
        if fingerprint == self.published:
            return
        if self.initial_saved and not self.publish_enabled.is_set():
            return
        self.cache['identity'] = self.runtime.preview_identity(self.plan)
        self.cache['candidate_timing'] = {'first_usable_seconds':self.first_seconds,
                                         'settled_seconds':time.monotonic()-self.started if settled else None}
        preview = await self.runtime.io.call(self.runtime.save_preview, self.run_id,
            deepcopy(self.results),deepcopy(self.cache),preparation_complete=True,
            candidate_plan=deepcopy(self.plan))
        if not self.initial_saved and not confirmation_eligible(self.plan, preview) and not settled:
            return
        self.published = fingerprint
        if not self.initial_saved:
            self.initial_saved = True
            self.first_seconds = time.monotonic()-self.started
            self.ready.set()

    async def drive(self):
        try:
            async with asyncio.timeout(self.timeout):
                while not self.closed:
                    self.changed.clear()
                    run = await self.runtime.io.call(self.runtime.store.get, self.run_id)
                    if not run or run['status'] not in {'planning','awaiting_confirmation'}:
                        return
                    for step in self.plan['steps']:
                        key = step['id']
                        deps = step.get('depends_on', [])
                        if not all(d in self.results for d in deps):
                            continue
                        parents = {d:deepcopy(self.results[d]) for d in deps}
                        sig = digest([step, {k:snapshot(v) for k,v in parents.items()}])
                        if self.signatures.get(key) != sig:
                            if key in self.jobs:
                                self.jobs[key].cancel()
                                await asyncio.gather(self.jobs[key],return_exceptions=True)
                            self.results.pop(key,None)
                            self.signatures[key] = sig
                            self.jobs[key] = asyncio.create_task(self.worker(deepcopy(step),parents,sig))
                    settled = len(self.jobs)==len(self.plan['steps']) and all(j.done() for j in self.jobs.values())
                    await self.publish(settled)
                    if settled and (self.publish_enabled.is_set() or not self.initial_saved):
                        return
                    # The event is also set when initial planning publishes plan_ready.
                    await self.changed.wait()
        except TimeoutError:
            if not self.initial_saved:
                for s in self.plan['steps']:
                    parents = {d:self.results[d] for d in s.get('depends_on', [])}
                    sig = digest([s, {k:snapshot(v) for k,v in parents.items()}])
                    if self.signatures.get(s['id']) != sig or s['id'] not in self.results:
                        self.results[s['id']] = self.runtime.failed_step(s,{'category':'timeout','message':'Candidate deadline exhausted.'})
                    self.signatures[s['id']] = sig
                await self.publish(True)
            # Preserve the last coherent preview when a replacement misses its deadline.
        finally:
            for j in self.jobs.values():
                if not j.done():j.cancel()
            await asyncio.gather(*self.jobs.values(),return_exceptions=True)
            await self.runtime.io.call(self.runtime.store.audit_event, self.run_id, 'candidate_preview_timing', {
                'first_usable_seconds':self.first_seconds, 'elapsed_seconds':time.monotonic()-self.started,
                'closed':self.closed, 'selected_revisions':{k:(v.get('candidate_selection') or {}).get('revision') for k,v in self.results.items()}})
            self.ready.set()


async def preflight(runtime, run_id, plan, remaining):
    race = PreviewRace(runtime,run_id,plan,remaining).start()
    runtime.candidate_previews[run_id] = race
    try:
        await race.ready.wait()
        if race.task.done():
            # Surface programming errors; never hide a failed background task.
            await race.task
    except BaseException:
        await race.close()
        raise
