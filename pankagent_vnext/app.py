"""Isolated graph-first workflow and replayable HTTP/SSE interface."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .answer_router import followup_questions
from .graph_contract import DIGEST as CONTRACT_DIGEST
from .evidence_status import (outcome_message, confirmation_eligible, query_readiness,
                              required_query_steps, checked_query_result, QUERY_READINESS_VERSION)
from .audit import InteractionRequest, deployment_identity, recorder
from .config import Settings
from .persistence import SerializedPersistence, PersistenceBusy, AdmissionContinuations
from .ownership import OwnershipError
from .health import HealthMonitor, Metrics, error_category
from .literature_policy import apply_literature_policy, preserve_revision_preference
from .plan_constraints import repair_step_constraints
from .store import ACTIVE, TERMINAL, Store
from .transport import JSONResponseLimitMiddleware, sse_event_bytes


class RevisionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=6000)
    include_context: bool = True
    revision_instruction: str | None = Field(default=None, max_length=6000)
    revision_mode: Literal["replacement_question", "instruction", "legacy_replacement"] = "legacy_replacement"
    event_source: Literal["user", "audit_replay", "synthetic_fault"] = "user"


class PlanRequest(RevisionRequest):
    auto_proceed_seconds: int | None = Field(default=None, ge=5, le=120)
    session_id: str | None = Field(default=None, max_length=100)


def public_payload(value, *, context=None):
    """Return one public copy for REST snapshots and live/replayed events.

    Stored evidence remains private and complete for integrity checks.  Every
    public transport uses this same boundary so replaying an older raw event
    cannot bypass the current donor-classification policy.
    """
    from .output_scope import aggregate_only, enabled, project
    source = context if isinstance(context, dict) else value if isinstance(value, dict) else {}
    aggregate = enabled(source) or aggregate_only(str(source.get('question') or ''))
    if aggregate:
        return project(value, context=source)
    # Record-level output may keep requested identifiers, but unrelated donor
    # classifications never become public merely because they were present on
    # a returned node.
    from .answer_facts import (requested_classification_fields,
                               sanitize_unrequested_classifications)
    allowed = requested_classification_fields(source)
    for step in ((source.get('plan') or {}).get('steps') or []):
        allowed.update(requested_classification_fields(step))
    return sanitize_unrequested_classifications(value, allowed)


def public_run(run: dict) -> dict:
    result={key: value for key, value in run.items() if key not in {"created_epoch", "preview_cache"}}
    plan=run.get('plan') or {}
    if plan.get('contract_sha256')!=CONTRACT_DIGEST and plan.get('steps'):
        result['rerun_advisory']='This saved result predates corrected terminology and relationship-path checks. Rerun the question before using its conclusion.'
    return public_payload(result, context=run)


def safe_error(exc: BaseException) -> dict:
    category = error_category(exc)
    messages = {
        "budget_exhausted": "The development evaluation budget is exhausted.",
        "timeout": "This stage exceeded its deadline; available evidence is preserved.",
        "authentication": "A dependency rejected its configured credential.",
        "authorization": "A dependency denied the requested access.",
        "rate_limited": "A dependency is temporarily rate limited.",
        "query_validation": "The generated query did not satisfy validation.",
        "graph_identity": "The configured graph identity could not be verified.",
    }
    return {"category": category, "message": messages.get(category, "A dependency could not complete this stage.")}


class CitationFilter:
    """Buffer incomplete citation markers so invalid step IDs never reach the UI."""

    def __init__(self, count):
        self.allowed = set(range(1, count + 1)) if isinstance(count, int) else {int(x[1:]) for x in count}
        self.pending, self.invalid = "", False
        self.seen: set[int] = set()

    def feed(self, value: str, final: bool = False) -> str:
        value = self.pending + value
        self.pending = ""
        if not final:
            start = value.rfind("[")
            if start >= 0 and "]" not in value[start:] and len(value) - start < 80:
                self.pending, value = value[start:], value[:start]

        def replace(match):
            if int(match.group(1)) in self.allowed:
                self.seen.add(int(match.group(1)))
                return match.group(0)
            self.invalid = True
            return "[unverified reference]"

        return re.sub(r"\[G(\d+)\]", replace, value)


def normalize_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("Invalid structured plan")
    if len(plan["steps"]) > 12:
        return {**plan, "steps": [], "clarification": "Please narrow this to at most twelve graph investigations."}
    from .composable_planning import normalize
    plan = normalize(plan)
    seen = set()
    for index, step in enumerate(plan["steps"]):
        if not isinstance(step, dict) or not step.get("id") or not isinstance(step.get("question"), str):
            raise ValueError("Invalid graph step")
        if step["id"] in seen or any(dependency not in seen for dependency in step.get("depends_on", [])):
            raise ValueError("Invalid step dependencies")
        seen.add(step["id"])
        step.setdefault("depends_on", [])
        step.setdefault("constraints", [])
        step.setdefault("complete", True)
        plan["steps"][index] = repair_step_constraints(step)
    plan.setdefault("literature", False)
    plan.setdefault("clarification", None)
    if not plan["steps"] and not plan["clarification"] and plan.get("plan_mode") not in {"literature_only", "session_summary"}:
        from .plan_recovery import mark_failure
        plan = mark_failure(plan)
    from .investigations import group_plan
    plan = group_plan(plan)
    for index, step in enumerate(plan["steps"]):
        step["evidence_id"] = f"G{index + 1}"
    return plan


def aggregate_evidence(previous: dict, plan=None) -> dict:
    from .composable_planning import answer_results
    steps = list(answer_results(plan or {}, previous).values())
    result = {"steps": list(previous.values()), "nodes": [], "edges": [], "queries": [], "provenance": [], "graph_version": None}
    for kind in ("nodes", "edges"):
        seen = set()
        for step in steps:
            for item in step.get(kind, []):
                identity = str(item.get("id", item.get("element_id", ""))) if isinstance(item, dict) else ""
                identity = identity or json.dumps(item, sort_keys=True, default=str)
                if identity not in seen:
                    seen.add(identity)
                    result[kind].append(item)
    for index, step in enumerate(steps):
        from .evidence_identity import evidence_id
        step["evidence_id"] = evidence_id(step, index)
        result["queries"].extend(step.get("queries", []))
        provenance = step.get("provenance", [])
        result["provenance"].extend(provenance if isinstance(provenance, list) else [provenance])
        result["graph_version"] = result["graph_version"] or step.get("graph_version")
    from .evidence_status import aggregate_outcome_status
    status_summary = aggregate_outcome_status(steps)
    result["completeness"] = status_summary["completeness"]
    result["truncated"] = status_summary["truncated"]
    result["retrieval"] = {"completeness": result["completeness"], "truncated": result["truncated"],
                           "checks": len(steps), "failed_checks": status_summary["failed_checks"],
                           "incomplete_checks": status_summary["incomplete_checks"],
                           "node_count": len(result["nodes"]), "edge_count": len(result["edges"])}
    return result


class Runtime:
    def __init__(self, settings, gateway, graph, literature):
        self.settings, self.gateway, self.graph, self.literature = settings, gateway, graph, literature
        if hasattr(gateway, "repair_cypher"):
            self.graph.query_repair = gateway.repair_cypher
        self.store = Store(settings.state_dir)
        self.io = SerializedPersistence("pank-agent-storage")
        self.owner_task = None
        self.admission = AdmissionContinuations()
        self.audit_identity = deployment_identity(settings)
        self.tasks: dict[str, asyncio.Task] = {}
        self.started_graph_checks: dict[str, set[str]] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent)
        self.active = 0
        self.metrics = Metrics()
        self.health = HealthMonitor(settings, gateway, graph, literature, self.store, self.queue_snapshot, io=self.io)
        self.shutting_down = False
        self.auto_proceed_tasks = set()

    async def start(self):
        await self.io.call(self.store.acquire_owner)
        await self.io.call(self.store.interrupt_active, recovery=True)
        await self.health.refresh_persistence()
        self.owner_task = asyncio.create_task(self.maintain_owner())
        self.health.start()

    async def maintain_owner(self):
        try:
            while True:
                await asyncio.sleep(10)
                await self.io.call(self.store.renew_owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.shutting_down = True
            if self.store.owner:
                self.store.owner.state = "lost"
            # The successor will mark expired-owner work interrupted. We must
            # never write past the fence or restart uncertain provider requests.
            for task in list(self.tasks.values()):
                task.cancel()

    def schedule_auto_proceed(self, run_id, seconds):
        """Only an explicit request option authorizes the checked-plan timer."""
        async def later():
            await asyncio.sleep(seconds)
            run = (await self.io.call(self.store.get, run_id))
            if self.shutting_down or not run or run['status'] != 'awaiting_confirmation':
                return
            try:
                await self.confirm_plan(run['plan_id'])
                (await self.io.call(self.store.audit_event, run_id, 'auto_proceeded', {'delay_seconds':seconds,'authorization':'request_option'}))
            except HTTPException as exc:
                (await self.io.call(self.store.audit_event, run_id, 'auto_proceed_skipped', {'http_status':exc.status_code}))
        task=asyncio.create_task(later())
        self.auto_proceed_tasks.add(task)
        task.add_done_callback(self.auto_proceed_tasks.discard)

    def queue_snapshot(self):
        return {"active_queries": self.active, "queue_depth": max(0, len(self.tasks) - self.active), "capacity": self.settings.max_concurrent, "audit_dropped": self.store.audit_dropped, "owner_state": self.store.owner.snapshot()["state"] if self.store.owner else "unclaimed", "owner_epoch": self.store.owner.epoch if self.store.owner else None, "persistence_queue_depth": self.io.pending}

    def check_capacity(self, replacing=None):
        if self.store.owner and self.store.owner.snapshot()["state"] != "active":
            raise HTTPException(503, "The service no longer owns its execution lease.")
        queued = len(self.tasks) - int(replacing in self.tasks)
        if self.shutting_down or queued >= self.settings.max_concurrent + getattr(self.settings, "max_queue", 8):
            raise HTTPException(429, "The development queue is full; retry shortly.", headers={"Retry-After": "2"})
        budget = self.gateway.budget.snapshot()
        if not budget or budget.get("remaining_usd", 0) <= 0:
            raise HTTPException(503, "The development evaluation budget is unavailable or exhausted.")

    def check_active(self, run_id: str):
        if self.shutting_down or (self.store.owner and self.store.owner.snapshot()["state"] != "active"):
            raise asyncio.CancelledError
        # Some upstream clients suppress task cancellation while cleaning up.
        # A terminal durable state remains authoritative even if they return.
        if self.store.get(run_id)["status"] in TERMINAL:
            raise asyncio.CancelledError

    def launch(self, run_id: str, coroutine):
        async def audited():
            token = recorder.set(lambda kind, payload: self.io.record(self.store.audit_event, run_id, kind, payload, on_drop=self.store.audit_drop))
            try:
                return await coroutine
            finally:
                recorder.reset(token)
        task = asyncio.create_task(audited(), name=f"vnext-{run_id}")
        self.tasks[run_id] = task

        def finished(done):
            if self.tasks.get(run_id) is done:
                self.tasks.pop(run_id, None)
            if not done.cancelled():
                # Retrieve exceptions without logging provider payloads or credentials.
                done.exception()

        task.add_done_callback(finished)

    async def emit(self, run_id: str, event_type: str, payload: dict):
        run = (await self.io.call(self.store.get, run_id))
        if run["status"] in TERMINAL:
            return
        stage = payload.get("stage") if event_type == "progress" else None
        if stage in {"planning", "resolving_entities", "preparing_preview", "preparing_execution", "reusing_preview", "generating_cypher", "validating", "querying_graph", "writing_answer", "searching_literature", "queued"}:
            (await self.io.call(self.store.update_if_active, run_id, stage=stage))
        payload = public_payload(payload, context=run)
        (await self.io.call(self.store.event_if_active, run_id, event_type, payload))

    async def heartbeat(self, run_id: str):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            run = (await self.io.call(self.store.get, run_id))
            if run is None or run["status"] not in ACTIVE:
                return
            (await self.io.call(self.store.event_if_active, run_id, "heartbeat", {"activity": run["stage"]}))

    def planning_history(self, run):
        from .revision_context import parent_context
        history = self.store.history(run["session_id"])
        metadata = self.store.audit_metadata(run["run_id"]) or {}
        parent = self.store.get(metadata.get("parent_run_id")) if metadata.get("parent_run_id") else None
        if parent and metadata.get("revision_mode") == "instruction":
            parent_plan, preview_summary = parent_context(parent)
            history.append({"revision_context": {
                "original_question": metadata.get("original_question", parent["question"]),
                "parent_plan": parent_plan,
                "preview_summary": preview_summary,
                "instruction": metadata.get("revision_instruction") or run["question"],
                **({"retry_of_run_id": metadata["retry_of_run_id"],
                    "previous_revision_instruction": metadata.get("retry_prior_revision_instruction")}
                   if metadata.get("retry_of_run_id") else {}),
                "preview_status": (parent.get("preview") or {}).get("status"),
                "rule": "Revise this existing investigation. Preserve all unrelated entities, filters, completeness and explicit preferences. Return a standalone revised biological question and executable steps."}})
        return history

    async def planning(self, run_id: str):
        beat = asyncio.create_task(self.heartbeat(run_id))
        started = time.monotonic()
        entered = False
        claude_pending = False
        try:
            async with self.semaphore:
                (await self.io.call(self.check_active, run_id))
                self.active += 1
                entered = True
                run = (await self.io.call(self.store.get, run_id))
                await self.emit(run_id, "progress", {"stage": "planning"})
                from .metadata_guard import recovery as metadata_recovery
                unsupported_metadata = metadata_recovery(
                    {"question": run["question"]}, self.settings.graph_version)
                if unsupported_metadata:
                    (await self.io.call(self._terminal, run_id, "failed", error={
                        "category": unsupported_metadata["category"],
                        "message": unsupported_metadata["message"],
                        "recovery": unsupported_metadata}))
                    self.metrics.count("plans_metadata_blocked")
                    return
                from .planning_fastpath import literature_only_revision, enable_literature
                metadata = (await self.io.call(self.store.audit_metadata, run_id)) or {}
                parent = (await self.io.call(self.store.get, metadata.get("parent_run_id"))) if metadata.get("parent_run_id") else None
                instruction = metadata.get('revision_instruction') or run['question']
                revision = None
                grounding = None
                fast = parent and parent.get('plan') and metadata.get('revision_mode') == 'instruction' and literature_only_revision(instruction)
                if parent and not fast and metadata.get('revision_mode') == 'instruction' and hasattr(self.gateway, 'interpret_revision'):
                    current_question = (parent.get('plan') or {}).get('interpreted_question') or metadata.get('original_question') or parent['question']
                    revision = await asyncio.wait_for(self.gateway.interpret_revision(current_question, instruction, parent.get('plan') or {}), self.settings.plan_timeout)
                    await self.io.call(self.store.audit_event, run_id, 'revision_interpreted', revision)
                    if revision['execution'] == 'clarify':
                        recovery = {'category': 'revision_needs_clarification', 'title': 'Clarify the requested change',
                            'message': revision['reason'], 'suggestions': [revision['recommended_question']] if revision['recommended_question'] else [], 'retryable': False}
                        await self.io.call(self._terminal, run_id, 'failed', error={'category': recovery['category'], 'message': recovery['message'], 'recovery': recovery})
                        return
                    run = {**run, 'question': revision['new_question']}
                from .planning_fastpath import literature_request_plan, unsupported_analysis_plan, genomic_neighborhood_plan
                literature_plan = literature_request_plan(run["question"])
                if revision and parent.get('plan', {}).get('steps'):
                    literature_plan = None
                from .session_summary import plan as summary_plan
                prior_answer = await self.io.call(self.store.latest_answered_run, run['session_id']) if run['include_context'] else None
                summary = summary_plan(run['question'], prior_answer)
                unsupported = unsupported_analysis_plan(run['question']) or (genomic_neighborhood_plan(run['question']) if not literature_plan else None)
                if unsupported:
                    proposed = unsupported
                elif summary:
                    proposed = summary
                elif literature_plan and not fast:
                    proposed = literature_plan
                elif fast:
                    proposed = deepcopy(parent['plan'])
                    self.metrics.count('planning_model_bypassed')
                    (await self.io.call(self.store.audit_event, run_id, 'planning_model_bypassed', {'reason':'literature_only_revision'}))
                else:
                    claude_pending = True
                    plan_options = {}
                    if hasattr(self.graph, 'ground_question'):
                        grounding_started = time.monotonic()
                        from .followup_scope import grounding_question
                        scoped_question = grounding_question(run['question'], prior_answer)
                        grounding = await self.graph.ground_question(scoped_question)
                        if scoped_question != run['question']:
                            grounding['session_scope_question'] = scoped_question
                        (await self.io.call(self.store.audit_event, run_id, 'preplanning_grounding', grounding))
                        self.metrics.observe('preplanning_grounding', time.monotonic() - grounding_started)
                        import inspect
                        if 'grounding' in inspect.signature(self.gateway.plan).parameters:
                            plan_options['grounding'] = grounding
                    model_started = time.monotonic()
                    proposed = await asyncio.wait_for(self.gateway.plan(run["question"], [] if revision else (await self.io.call(self.planning_history, run)), **plan_options), self.settings.plan_timeout)
                    self.metrics.observe("model_plan", time.monotonic() - model_started)
                    (await self.io.call(self.check_active, run_id))
                    if proposed.get('planning_route', {}).get('claude_calls') == 0:
                        self.metrics.count('planning_model_bypassed')
                        (await self.io.call(self.store.audit_event, run_id, 'planning_model_bypassed', proposed['planning_route']))
                    else:
                        self.health.record_inference("claude", True)
                    claude_pending = False
                from .plan_recovery import recover_empty_plan
                proposed = await recover_empty_plan(self.gateway, proposed, run["question"], (await self.io.call(self.planning_history, run)), self.settings.plan_timeout, grounding=grounding)
                if revision:
                    from .revision_interpreter import reuse_step_ids
                    if revision['execution'] == 'parallel_extend':
                        proposed = reuse_step_ids(proposed, parent.get('plan') or {})
                    proposed['revision_execution'] = revision['execution']
                    proposed['effective_question'] = revision['new_question']
                plan = normalize_plan(proposed)
                from .followup_scope import preserve_original_sources
                plan = preserve_original_sources(plan, run["question"], prior_answer)
                if parent and metadata.get("revision_mode") == "instruction":
                    plan = preserve_revision_preference(plan, parent.get("plan") or {}, metadata.get("revision_instruction") or run["question"])
                    from .revision_guard import preserve_additive_scope
                    plan = preserve_additive_scope(plan, parent.get("plan") or {}, metadata.get("revision_instruction") or run["question"])
                    plan["revision_trace"] = {"parent_plan_id": parent["plan_id"],
                        "original_question": metadata.get("original_question"),
                        "instruction": metadata.get("revision_instruction") or run["question"],
                        "before_steps": (parent.get("plan") or {}).get("steps", []),
                        "after_steps": deepcopy(plan["steps"])}
                plan = enable_literature(plan)
                requested_literature = apply_literature_policy(plan, run["question"])
                revision_preference = (parent and metadata.get('revision_mode') == 'instruction'
                    and (plan.get('literature_intent') or {}).get('reason') in {'explicit_opt_out', 'explicit_request', 'inherited_explicit'})
                if not revision_preference and requested_literature['literature_intent']['reason'] in {'explicit_request', 'explicit_opt_out'}:
                    plan['literature'] = requested_literature['literature']
                    plan['literature_intent'] = requested_literature['literature_intent']
                if plan.get('plan_mode') == 'session_summary':
                    plan['literature'] = False
                    plan['literature_intent'] = {'included':False,'reason':'reuse_session_summary','policy_version':plan.get('session_summary',{}).get('version','unknown')}
                plan.pop("review_ready", None)
                plan["contract_sha256"] = CONTRACT_DIGEST
                plan["original_question"] = metadata.get("original_question", run["question"])
                plan["include_context"] = run["include_context"]
                from .output_scope import aggregate_only, VERSION as OUTPUT_SCOPE_VERSION
                if aggregate_only(run["question"]):
                    plan["output_scope"] = {"mode": "aggregate_only", "version": OUTPUT_SCOPE_VERSION}
                (await self.io.call(self.store.update_if_active, run_id, plan=plan))
                preview_started = time.monotonic()
                try:
                    await asyncio.wait_for(self.preflight(run_id, plan), self.settings.grouped_preview_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    (await self.io.call(self.check_active, run_id))
                    error = safe_error(exc)
                    if isinstance(exc, TimeoutError):
                        component = "cypher" if (await self.io.call(self.store.get, run_id))["stage"] == "generating_cypher" else "neo4j"
                        self.health.record_inference(component, False, "timeout")
                    self.metrics.count("preview_preparation_errors")
                    current = (await self.io.call(self.store.get, run_id))
                    plan = current["plan"]
                    previous = {step["step_id"]: step for step in (current["preview"] or {}).get("evidence", {}).get("steps", [])}
                    for step in plan["steps"]:
                        previous.setdefault(step["id"], self.failed_step(step, error))
                    previous = {step["id"]: previous[step["id"]] for step in plan["steps"]}
                    cache = current["preview_cache"] or {"identity": None, "step_completed_epochs": {}}
                    (await self.io.call(self.save_preview, run_id, deepcopy(previous), deepcopy(cache), error=error, preparation_complete=True))
                finally:
                    self.metrics.observe("preview", time.monotonic() - preview_started)
                (await self.io.call(self.check_active, run_id))
                current = (await self.io.call(self.store.get, run_id))
                if not confirmation_eligible(current["plan"], current.get("preview")):
                    recovery = self.preview_recovery(current)
                    (await self.io.call(self._terminal, run_id, "failed", error={"category": recovery["category"],
                        "message": recovery["message"], "recovery": recovery}))
                    self.metrics.count("plans_query_blocked")
                    return
                plan = current["plan"]
                if plan.get('planning_route', {}).get('kind') in {'verified_signal_pattern', 'verified_schema_pattern'}:
                    from .plan_verification import review_verified_local_coloc
                    review_started = time.monotonic()
                    review = review_verified_local_coloc(plan, current['preview'])
                    if review is not None:
                        await self.emit(run_id, 'progress', {'stage': 'verifying_plan'})
                        self.metrics.count('local_plan_verification')
                    elif hasattr(self.gateway, 'review_grounded_plan'):
                        await self.emit(run_id, 'progress', {'stage': 'verifying_plan'})
                        claude_pending = True
                        review = await asyncio.wait_for(self.gateway.review_grounded_plan(
                            run['question'], plan, current['preview']), min(20, self.settings.plan_timeout))
                        (await self.io.call(self.check_active, run_id))
                        self.health.record_inference('claude', True)
                        claude_pending = False
                    if review is not None:
                        self.metrics.observe('post_retrieval_plan_verification', time.monotonic() - review_started)
                        (await self.io.call(self.store.audit_event, run_id, 'post_retrieval_plan_verification', review))
                        if not review['approved']:
                            reasons = ' '.join(str(item.get('reason', '')) for item in review.get('issues', [])[:2])
                            (await self.io.call(self._terminal, run_id, 'failed', error={'category': 'plan_scope_verification_failed',
                                'message': 'The prepared search did not pass its scope check. ' + reasons}))
                            return
                plan["review_ready"] = True
                (await self.io.call(self.store.update_if_active, run_id, plan=plan, status="awaiting_confirmation", stage="awaiting_confirmation"))
                (await self.io.call(self.store.event_if_active, run_id, "plan_validated", {"plan_id": run["plan_id"], "plan": plan,
                    "validation_scope": "Only the verified checks succeeded; remaining checks are unavailable." if current["preview"]["query_readiness"].get("partial_ready") else "required graph queries executed successfully; zero matches are valid checked outcomes",
                    "query_readiness": current["preview"]["query_readiness"]}))
                (await self.io.call(self.store.event_if_active, run_id, "plan_ready", {"plan_id": run["plan_id"], "plan": plan, "preview": current["preview"]}))
                delay = ((await self.io.call(self.store.audit_metadata, run_id)) or {}).get('requested_options',{}).get('auto_proceed_seconds')
                if delay and not current['preview']['query_readiness']['blocked_step_ids']:
                    (await self.io.call(self.store.event_if_active, run_id, 'auto_proceed_scheduled', {'delay_seconds':delay,'plan_id':run['plan_id']}))
                    self.schedule_auto_proceed(run_id, delay)
                self.metrics.count("plans_ready")
                self.metrics.observe("plan_ready", time.monotonic() - started)
        except asyncio.CancelledError:
            (await self.io.call(self._cancelled, run_id))
            raise
        except Exception as exc:
            error = safe_error(exc)
            if claude_pending:
                self.health.record_inference("claude", False, error["category"])
            else:
                self.metrics.count("plan_preparation_errors")
            (await self.io.call(self._terminal, run_id, "failed", error=error))
        finally:
            if entered:
                self.active -= 1
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    @staticmethod
    def failed_step(step, error):
        return {"step_id": step["id"], "question": step["question"], "status": "failed", "error": error,
                "nodes": [], "edges": [], "rows": [], "validation": [{"valid": False, "reasons": [error["category"]]}],
                "requested_scope": {"relation_types": deepcopy(step.get("relation_types", [])),
                                    "constraints": deepcopy(step.get("constraints", [])),
                                    "complete": step.get("complete", True),
                                    **({"path_spec": deepcopy(step["path_spec"])}
                                       if step.get("path_spec") is not None else {})},
                **{key: step[key] for key in ('purpose', 'context_for', 'title') if key in step}}

    def preview_identity(self, plan):
        """Bind private reuse metadata to the exact approved plan and graph setup."""
        fields = ("graph_version", "neo4j_uri", "neo4j_database", "neo4j_user", "cypher_url",
                  "max_nodes", "max_edges", "max_rows", "max_bytes", "graph_timeout", "cypher_timeout")
        graph_identity = {field: getattr(self.settings, field, None) for field in fields}
        if hasattr(self.graph, "preview_identity"):
            graph_identity["adapter"] = self.graph.preview_identity()
        # Credential changes can change accessible data. Hashes remain private;
        # credentials and the reusable-cache metadata never enter public events.
        access = [getattr(self.settings, field, "") for field in ("neo4j_user", "neo4j_password", "cypher_token")]
        raw = json.dumps({"version": 4, "readiness_contract": QUERY_READINESS_VERSION, "validator_contract": CONTRACT_DIGEST,
                          "plan": {key: value for key, value in plan.items() if key != "review_ready"}, "graph": graph_identity, "access": access}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def annotate_plan_evidence(plan, previous):
        for index, step in enumerate(plan.get('steps', [])):
            if step['id'] in previous:
                previous[step['id']].setdefault('evidence_id', f'G{index + 1}')
        from .coloc_scope import summarize_linkage
        linkage = summarize_linkage(plan, previous)
        for group in linkage.get('groups', []):
            primary_id = group.get('step_ids', {}).get('primary')
            if primary_id in previous:
                previous[primary_id]['coloc_linkage'] = deepcopy(group)
        return linkage

    def save_preview(self, run_id, previous, cache, *, error=None, preparation_complete=False):
        self.check_active(run_id)
        linkage = self.annotate_plan_evidence(self.store.get(run_id)['plan'] or {}, previous)
        evidence = aggregate_evidence(previous, self.store.get(run_id)["plan"] or {})
        if linkage.get('groups'):
            evidence['coloc_linkage'] = linkage
        from .investigations import coverage
        evidence["category_outcomes"] = coverage(self.store.get(run_id)["plan"] or {}, previous)
        states = [step.get("status") for step in previous.values()]
        status = "not_requested" if not states else "failed" if error or all(state == "failed" for state in states) else evidence["completeness"]
        plan = self.store.get(run_id)["plan"] or {}
        pending = [step["id"] for step in plan.get("steps", []) if step["id"] not in previous]
        if pending:
            evidence["completeness"] = "partial"
            if states:
                status = "partial"
        completed = list(cache.get("step_completed_epochs", {}).values())
        epoch = min(completed) if completed else time.time()
        preview = {"status": status, "evidence": evidence, "pending_step_ids": pending,
                   "preparation_complete": preparation_complete,
                   "created_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                   "reusable_until": datetime.fromtimestamp(epoch + self.settings.preview_ttl_seconds, timezone.utc).isoformat() if cache.get("identity") else None}
        if error:
            preview["error"] = error
            if states and any(state != "failed" for state in states):
                preview["status"] = "partial"
        from .query_recovery import retrieval_recovery
        recovery = retrieval_recovery(preview)
        if recovery:
            preview["recovery"] = recovery
        resource_overrun = (len(evidence['nodes']) > self.settings.max_nodes
            or len(evidence['edges']) > self.settings.max_edges
            or sum(len(item.get('rows', [])) for item in previous.values()) > getattr(self.settings, 'max_rows', 1000)
            or sum(item.get('materialized_bytes', 0) for item in previous.values()) > self.settings.max_bytes)
        if resource_overrun:
            preview['error'] = {'category': 'run_graph_materialization_limit',
                'message': 'The combined graph checks exceeded the allowed evidence size.'}
            preview['query_resource_limit_exceeded'] = True
        preview["query_readiness"] = query_readiness(plan, preview)
        preview["confirmation_eligible"] = preview["query_readiness"]["ready"]
        self.store.update(run_id, preview=preview, preview_cache=cache)
        return preview

    async def execute_step(self, run_id, step, previous, before_query=None):
        (await self.io.call(self.check_active, run_id))
        prior_generation = getattr(self.graph, "last_generation_success", None)
        prior_query = getattr(self.graph, "last_query_success", None)
        tracking = (await self.io.call(self.store.get, run_id))["status"] == "running"
        if tracking:
            self.started_graph_checks.setdefault(run_id, set()).add(step["id"])
        try:
            async def progress(kind, payload):
                if before_query and payload.get('stage') == 'querying_graph':
                    await before_query()
                    (await self.io.call(self.check_active, run_id))
                await self.emit(run_id, kind, payload)
            if step.get('operation'):
                from .composable_planning import combine
                result = combine(step, previous)
            else:
                result = await self.graph.execute(step, previous, progress)
            result['context_mode'] = step.get('context_mode', 'auto')
            result['input_bindings'] = deepcopy(step.get('input_bindings', []))
            (await self.io.call(self.check_active, run_id))
            result.setdefault("step_id", step["id"])
            plan = (await self.io.call(self.store.get, run_id)).get("plan") or {}
            position = next((i for i, item in enumerate(plan.get("steps", [])) if item["id"] == step["id"]), 0)
            result["evidence_id"] = step.get("evidence_id", f"G{position + 1}")
            execution = result.get('retrieval_execution') or {}
            from .evidence_status import executed_without_records
            state = ('skipped_empty_dependency' if result.get('execution_status') == 'skipped_empty_dependency'
                else 'failed' if result.get('status') in {'failed', 'blocked', 'unavailable'}
                else 'truncated' if result.get('truncated') else 'executed_empty'
                if executed_without_records(result) or result.get('status') == 'empty' and execution.get('completed') is True
                else 'executed' if execution.get('completed') is True else 'unknown')
            result['execution_provenance'] = {'version': 'execution-provenance-v2', 'outcome': state,
                'completed': execution.get('completed'), 'cursor_exhausted': execution.get('cursor_exhausted')}
            result.setdefault("question", step["question"])
            if result.get("status") != "failed":
                if (getattr(self.graph, "last_generation_success", None) != prior_generation
                        or (not hasattr(self.graph, "last_generation_success") and result.get("validation"))):
                    self.health.record_inference("cypher", True)
                if (getattr(self.graph, "last_query_success", None) != prior_query
                        or (not hasattr(self.graph, "last_query_success") and result.get("queries") and result.get("status") in {"complete", "partial", "empty"})):
                    self.health.record_inference("neo4j", True)
            if result.get("status") == "failed":
                reasons = [reason for check in result.get("validation", []) for reason in check.get("reasons", [])]
                self.metrics.count("graph_validation_failures")
                if any(str(reason).startswith("graph_execution_failed") for reason in reasons):
                    self.health.record_inference("neo4j", False, "query_failed")
                elif any(check.get("candidate_cypher") for check in result.get("validation", [])):
                    self.health.record_inference("cypher", False, "validation_rejected")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = safe_error(exc)
            component = "cypher" if (await self.io.call(self.store.get, run_id))["stage"] == "generating_cypher" else "neo4j"
            self.health.record_inference(component, False, error["category"])
            return self.failed_step(step, error)

    async def preflight(self, run_id, plan):
        preflight_started = time.monotonic()
        await self.emit(run_id, "progress", {"stage": "preparing_preview"})
        if plan.get("steps") and not plan.get("clarification") and hasattr(self.graph, "prepare_plan"):
            try:
                plan = normalize_plan(await asyncio.wait_for(
                    self.graph.prepare_plan(deepcopy(plan), lambda kind, payload: self.emit(run_id, kind, payload)),
                    self.settings.preview_timeout))
                (await self.io.call(self.check_active, run_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.record_inference("neo4j", False, safe_error(exc)["category"])
                raise
            (await self.io.call(self.store.update_if_active, run_id, plan=plan))
        if plan.get('execution_mode') in {'chain', 'parallel', 'mixed'}:
            queries = [s for s in plan['steps'] if not s.get('operation') and s.get('purpose') != 'context']
            if queries:
                selected = next((s for s in queries if not s.get('path_spec')), queries[0])
                selected['gpu_participation_required'] = True
        plan.pop('review_ready', None)
        (await self.io.call(self.store.update_if_active, run_id, plan=plan))
        cache = {"identity": self.preview_identity(plan), "step_completed_epochs": {}, "step_identities": {}}
        previous = {}
        if plan.get("clarification"):
            metadata = (await self.io.call(self.store.audit_metadata, run_id)) or {}
            parent = (await self.io.call(self.store.get, metadata.get("parent_run_id"))) if plan.get('retained_previous_plan') and metadata.get('parent_run_id') else None
            if parent:
                previous = {s['step_id']: deepcopy(s) for s in ((parent.get('preview') or {}).get('evidence') or {}).get('steps', [])}
            (await self.io.call(self.save_preview, run_id, previous, {"identity": None, "step_completed_epochs": {}}, preparation_complete=True))
            return
        metadata = (await self.io.call(self.store.audit_metadata, run_id)) or {}
        parent = (await self.io.call(self.store.get, metadata.get("parent_run_id"))) if metadata.get("parent_run_id") else None
        parent_cache = (parent or {}).get("preview_cache") or {}
        parent_steps = {s["step_id"]: s for s in (((parent or {}).get("preview") or {}).get("evidence") or {}).get("steps", [])}
        reused = set()
        preview_steps = plan['steps']
        done = {step['id']: asyncio.Event() for step in preview_steps}
        # Prioritize primary checks. Optional context never occupies a generation
        # slot ahead of an independent primary; the adapter retains its global cap.
        required_ids = {step['id'] for step in required_query_steps(plan)}
        primary_tasks_done = asyncio.Event()
        remaining_required = set(required_ids)
        execution_slots = asyncio.Semaphore(2)
        from .revision_interpreter import retrieval_signature
        async def worker(index, step):
            for dependency in step.get('depends_on', []):
                await done[dependency].wait()
            if step['id'] not in required_ids:
                await primary_tasks_done.wait()
            (await self.io.call(self.check_active, run_id))
            fingerprint = self.preview_identity({"steps": [step], "contract_sha256": CONTRACT_DIGEST})
            old = parent_steps.get(step["id"], {})
            epoch = parent_cache.get("step_completed_epochs", {}).get(step["id"], 0)
            can_reuse = (plan.get("revision_execution") != "chain_restart" and checked_query_result(step, old, {key: previous[key] for key in reused})
                and (parent_cache.get("step_identities", {}).get(step["id"]) == fingerprint
                     or (plan.get('revision_execution') == 'parallel_extend'
                         and retrieval_signature(step)
                         == retrieval_signature(next((s for s in (parent.get('plan') or {}).get('steps', []) if s['id'] == step['id']), {}))))
                and 0 <= time.time() - epoch < self.settings.preview_ttl_seconds
                and set(step.get("depends_on", [])) <= reused)
            if can_reuse:
                result = deepcopy(old)
                reused.add(step["id"])
                self.metrics.count("revision_preview_reused")
                await self.emit(run_id, "preview_reused", {"step_id": step["id"], "source": "parent_plan"})
            else:
                failed_dependencies = [dependency for dependency in step.get('depends_on', [])
                    if not checked_query_result(next(item for item in preview_steps if item['id'] == dependency),
                        previous.get(dependency), previous)]
                if failed_dependencies:
                    result = self.failed_step(step, {'category': 'dependency_unavailable',
                        'message': 'A required earlier check could not be completed.'})
                    result['blocked_by'] = failed_dependencies
                else:
                    async with execution_slots:
                        result = await self.execute_step(run_id, step, previous)
                epoch = time.time()
            (await self.io.call(self.check_active, run_id))
            result.setdefault('purpose', step.get('purpose', 'primary'))
            previous[step["id"]] = result
            cache["step_completed_epochs"][step["id"]] = epoch
            cache["step_identities"][step["id"]] = fingerprint
            preview = (await self.io.call(self.save_preview, run_id, deepcopy(previous), deepcopy(cache)))
            await self.emit(run_id, "preview_step", {"step_id": step["id"], "evidence": previous[step["id"]], "preview": preview})
            done[step['id']].set()
            remaining_required.discard(step['id'])
            if not remaining_required:
                primary_tasks_done.set()
        if not remaining_required:
            primary_tasks_done.set()
        tasks = [asyncio.create_task(worker(i, step)) for i, step in enumerate(preview_steps)]
        # Resolution and retrieval share one budget. Grouping cannot add a
        # second full deadline after preparation has already consumed time.
        deadline = (self.settings.grouped_preview_timeout if len(required_ids) > 2
                    else self.settings.preview_timeout)
        remaining = max(0, deadline - (time.monotonic() - preflight_started))
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), remaining)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.preview_identity(plan) != cache["identity"]:
            cache["identity"] = None
        previous = {step['id']: previous[step['id']] for step in preview_steps}
        (await self.io.call(self.save_preview, run_id, deepcopy(previous), deepcopy(cache), preparation_complete=True))

    def preview_recovery(self, run, reason=None):
        """Return the existing popup contract without labeling an empty result failed."""
        if reason:
            return {'category': 'preview_revalidation_required', 'title': 'The checked plan needs refreshing',
                'message': 'The checked evidence has expired or no longer matches this plan and graph release. '
                           'Retry the same question to check it again before confirmation. Your filters are retained.',
                'retryable': True, 'suggestions': [], 'evidence': {'reason': reason}}
        plan, preview = run.get('plan') or {}, run.get('preview') or {}
        if plan.get('recovery'):
            return plan['recovery']
        if plan.get('clarification'):
            return {'category': 'scope_needs_clarification', 'title': 'A detail needs your review',
                    'message': plan['clarification'], 'retryable': False, 'suggestions': []}
        from .query_recovery import retrieval_recovery, oversized_preview_recovery
        readiness = query_readiness(plan, preview)
        oversized = oversized_preview_recovery(preview)
        if oversized:
            oversized['evidence']['query_readiness'] = readiness
            return oversized
        blocked = set(readiness['blocked_step_ids'])
        relevant = deepcopy(preview)
        relevant['status'] = 'failed'
        relevant['preparation_complete'] = True
        relevant['evidence'] = {**(preview.get('evidence') or {}), 'steps': [
            {**step, 'status': 'failed', 'purpose': 'primary'}
            for step in (preview.get('evidence') or {}).get('steps', []) if step.get('step_id') in blocked]}
        recovery = retrieval_recovery(relevant) or {
            'category': 'query_validation', 'title': 'The requested checks could not all finish',
            'message': 'At least one required graph query has not been successfully checked. '
                       'No conclusion about missing data can be drawn. Retry the same question; your filters are kept.',
            'retryable': True, 'suggestions': []}
        recovery.setdefault('evidence', {})['query_readiness'] = readiness
        return recovery

    def confirmation_preview_issue(self, run, *, check_freshness=True):
        plan, preview, cache = run.get('plan') or {}, run.get('preview') or {}, run.get('preview_cache') or {}
        if (preview.get('query_readiness') or {}).get('version') != QUERY_READINESS_VERSION:
            return 'readiness_contract_changed'
        if not confirmation_eligible(plan, preview):
            return 'required_query_not_checked'
        try:
            if not cache.get('identity') or cache['identity'] != self.preview_identity(plan):
                return 'plan_or_graph_identity_changed'
        except Exception:
            return 'graph_identity_unavailable'
        if check_freshness:
            for step in required_query_steps(plan):
                age = time.time() - cache.get('step_completed_epochs', {}).get(step['id'], 0)
                if not 0 <= age < self.settings.preview_ttl_seconds:
                    return 'checked_evidence_expired'
        return None

    def _terminal(self, run_id, status, **fields):
        run = self.store.get(run_id)
        if run["status"] in TERMINAL:
            return
        if status in {"cancelled", "interrupted"} and run["status"] in {"queued", "running"} and (run.get("plan") or {}).get("steps"):
            from .interrupted_evidence import complete_interrupted_evidence
            fields["evidence"] = complete_interrupted_evidence(run["plan"], fields.get("evidence", run["evidence"]),
                {"category": status}, self.started_graph_checks.get(run_id, set()))
        self.store.update(run_id, status=status, stage=status, **fields)
        self.store.event(run_id, "terminal", {"status": status, **({"error": fields["error"]} if fields.get("error") else {})})
        self.metrics.count(f"runs_{status}")

    def _cancelled(self, run_id):
        self._terminal(run_id, "interrupted" if self.shutting_down else "cancelled")

    def preview_reuse_reason(self, step, cached, cache, matching, reused, previous, *, check_freshness=True):
        if not matching:
            return "identity_changed"
        if step["id"] not in cache.get("step_completed_epochs", {}):
            return "not_previewed"
        age = time.time() - cache.get("step_completed_epochs", {}).get(step["id"], 0)
        if check_freshness and not 0 <= age < self.settings.preview_ttl_seconds:
            return "expired"
        if not set(step.get("depends_on", [])) <= reused:
            return "dependency_changed"
        checks = cached.get("validation") or []
        if cached.get("status") not in {"complete", "empty", "partial"} or not checks or checks[-1].get("valid") is not True:
            return "preview_failed_or_unvalidated"
        # A retried earlier step can consume more of the run budget, even when
        # this cached step has no semantic dependency on it.
        combined = [*previous.values(), cached]
        nodes = {str(node["id"]) for item in combined for node in item.get("nodes", [])}
        edges = {json.dumps(edge, sort_keys=True, separators=(",", ":")) for item in combined for edge in item.get("edges", [])}
        rows = sum(len(item.get("rows", [])) for item in combined)
        size = sum(item.get("materialized_bytes", 0) for item in combined)
        if (len(nodes) > self.settings.max_nodes or len(edges) > self.settings.max_edges
                or rows > getattr(self.settings, "max_rows", 1000) or size > self.settings.max_bytes):
            return "materialization_budget_changed"
        return None

    async def graph_answer(self, run_id: str, run: dict) -> tuple[dict, bool]:
        if run['plan'].get('plan_mode') == 'session_summary':
            from .session_summary import answer as summarize, VERSION
            source = await self.io.call(self.store.get, run['plan']['session_summary']['source_run_id'])
            if not source or source['session_id'] != run['session_id']:
                raise ValueError('summary_source_unavailable')
            answer = summarize(source)
            evidence = deepcopy(source.get('evidence') or {})
            evidence['session_summary'] = {**run['plan']['session_summary'], 'new_search_performed':False}
            evidence['answer_validation'] = {'valid':True,'scope':'deterministic_session_facts','version':VERSION}
            await self.io.call(self.store.update_if_active, run_id, evidence=evidence, graph_answer=answer)
            await self.emit(run_id, 'graph_answer', {'answer':answer,'evidence':evidence,'delta':False})
            return evidence, source['status']=='completed'
        if run["plan"].get("plan_mode") == "literature_only":
            evidence = {"steps": [], "nodes": [], "edges": [], "completeness": "not_requested", "literature_mode": "literature_only"}
            await self.io.call(self.store.update_if_active, run_id, evidence=evidence, graph_answer="")
            return evidence, True
        issue = self.confirmation_preview_issue(run, check_freshness=False)
        if issue:
            recovery = self.preview_recovery(run, issue)
            (await self.io.call(self.store.update_if_active, run_id, error={'category': recovery['category'], 'message': recovery['message'], 'recovery': recovery}))
            raise ValueError('checked_preview_identity_changed')
        previous = {}
        required_ids = {step['id'] for step in required_query_steps(run['plan'])}
        cache = run.get("preview_cache") or {}
        preview = run.get("preview") or {}
        cached_steps = {step["step_id"]: step for step in preview.get("evidence", {}).get("steps", [])}
        reused = set()
        reuse_info = {"reused_step_ids": [], "retrieved_step_ids": [], "unreused_reasons": {}}
        done = {step['id']: asyncio.Event() for step in run['plan']['steps']}
        async def retrieve(index, step):
            async def wait_prior():
                if index: await done[run['plan']['steps'][index-1]['id']].wait()
            for dependency in step.get('depends_on', []):
                await done[dependency].wait()
            (await self.io.call(self.check_active, run_id))
            try:
                matching = bool(cache.get("identity")) and cache["identity"] == self.preview_identity(run["plan"])
            except Exception:
                matching = False
            cached = cached_steps.get(step["id"], {})
            reason = self.preview_reuse_reason(step, cached, cache, matching, reused, previous,
                check_freshness=step['id'] not in required_ids)
            accepted_failed = (matching and preview.get('query_readiness',{}).get('partial_ready')
                and step['id'] in preview['query_readiness']['blocked_step_ids']
                and step['id'] in preview['query_readiness'].get('retained_failed_step_ids', []))
            if reason is not None and step['id'] in required_ids and not accepted_failed:
                recovery = self.preview_recovery(run, reason)
                (await self.io.call(self.store.update_if_active, run_id, error={'category': recovery['category'], 'message': recovery['message'], 'recovery': recovery}))
                raise ValueError('checked_preview_identity_changed')
            if accepted_failed:
                previous[step["id"]] = deepcopy(cached)
                reuse_info["unreused_reasons"][step["id"]] = "retained_failed_check"
            elif reason is None:
                previous[step["id"]] = deepcopy(cached)
                reused.add(step["id"])
                reuse_info["reused_step_ids"].append(step["id"])
                self.metrics.count("preview_reused_steps")
                await self.emit(run_id, "progress", {"stage": "reusing_preview"})
                await self.emit(run_id, "preview_reused", {"step_id": step["id"], "source": "plan_preview", "created_at": preview.get("created_at")})
            elif cached.get("status") == "failed" and cached.get("generator_attempts"):
                previous[step["id"]] = deepcopy(cached)
                reuse_info["unreused_reasons"][step["id"]] = "candidate_budget_exhausted"
            else:
                reuse_info["retrieved_step_ids"].append(step["id"])
                reuse_info["unreused_reasons"][step["id"]] = reason
                self.metrics.count("preview_retrieved_steps")
                result = await self.execute_step(run_id, step, previous, before_query=wait_prior)
                await wait_prior()
                previous[step["id"]] = result
            previous[step["id"]]["evidence_id"] = cached.get("evidence_id", f"G{index + 1}") if reason is None else f"G{index + 1}"
            evidence = aggregate_evidence(previous, run["plan"])
            evidence["preview_reuse"] = reuse_info
            (await self.io.call(self.store.update_if_active, run_id, evidence=evidence))
            await self.emit(run_id, "graph_step", {"step_id": step["id"], "evidence": previous[step["id"]], "reused_preview": reason is None})

            done[step['id']].set()
        tasks = [asyncio.create_task(retrieve(i, step)) for i, step in enumerate(run['plan']['steps'])]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), 120 if len(tasks)>2 else self.settings.run_timeout)
        except TimeoutError:
            # The shared execution handler retains evidence and records each
            # interrupted or unattempted check without starting synthesis.
            raise
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        previous = {step['id']: previous[step['id']] for step in run['plan']['steps']}

        linkage = self.annotate_plan_evidence(run['plan'], previous)
        evidence = aggregate_evidence(previous, self.store.get(run_id)["plan"] or {})
        if linkage.get('groups'):
            evidence['coloc_linkage'] = linkage
        evidence["preview_reuse"] = reuse_info
        from .investigations import coverage
        evidence["category_outcomes"] = coverage(run["plan"], previous)
        await self.emit(run_id, "progress", {"stage": "writing_answer"})
        answer = ""
        citation_filter = CitationFilter([step["evidence_id"] for step in previous.values()])
        synthesis_error = None
        synthesis_started = False
        from .population_completeness import population_recovery
        population_issue = population_recovery(run, previous)
        if population_issue:
            evidence["recovery"] = population_issue
            evidence["population_completeness"] = population_issue["evidence"]
            (await self.io.call(self.store.update_if_active, run_id, evidence=evidence, error={
                "category": population_issue["category"], "message": population_issue["message"],
                "recovery": population_issue}))
            (await self.io.call(self.store.audit_event, run_id, "population_answer_guard", population_issue["evidence"]))
        status_message = population_issue["message"] if population_issue else outcome_message(previous)
        from .evidence_status import synthesis_evidence
        from .composable_planning import answer_results
        answer_evidence = synthesis_evidence(answer_results(run["plan"], previous))
        async def tokens():
            missing = [step for step in previous.values() if step.get('purpose') != 'context' and (step.get('status') in {'failed','blocked','unavailable'} or step.get('truncated'))]
            if preview.get('query_readiness',{}).get('partial_ready') and missing:
                total = len(required_ids)
                yield f"Partial answer: {total-len(missing)} of {total} requested checks completed. The remaining checks could not finish; their failure does not establish that evidence is absent.\n\n"
            if status_message:
                yield status_message
            else:
                async for token in self.gateway.synthesize(question, answer_evidence, **options):
                    yield token
        try:
            question = run["plan"].get("interpreted_question", run["question"])
            options = {}
            if not status_message and hasattr(self.gateway, "prepare_answer"):
                prepared = self.gateway.prepare_answer(question, answer_evidence)
                evidence["answer_profile"] = prepared.profile
                (await self.io.call(self.store.update_if_active, run_id, evidence=evidence))
                self.metrics.observe("answer_skill_selection", prepared.profile["timing_ms"]["total"] / 1000)
                self.metrics.count("answer_skill_cache_hit" if prepared.profile["cache_hit"] else "answer_skill_cache_miss")
                await self.emit(run_id, "answer_profile", {"profile": prepared.profile})
                options["prepared"] = prepared
            synthesis_started = not bool(status_message)
            async for token in tokens():
                (await self.io.call(self.check_active, run_id))
                visible = citation_filter.feed(token)
                if visible:
                    answer += visible
                    (await self.io.call(self.store.update_if_active, run_id, graph_answer=answer))
                    await self.emit(run_id, "graph_answer", {"text": visible, "delta": True})
            tail = citation_filter.feed("", final=True)
            (await self.io.call(self.check_active, run_id))
            if tail:
                answer += tail
                (await self.io.call(self.store.update_if_active, run_id, graph_answer=answer))
                await self.emit(run_id, "graph_answer", {"text": tail, "delta": True})

            if synthesis_started:
                self.health.record_inference("claude", True)
            generation = getattr(options.get('prepared'), 'generation', {})
            if generation:
                evidence['answer_generation'] = deepcopy(generation)
            if generation.get('truncated'):
                synthesis_error = {'category':'answer_output_limit',
                                   'message':'The written answer reached its length limit before finishing. The retrieved evidence is preserved.'}
                evidence['answer_incomplete'] = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            synthesis_error = safe_error(exc)
            if synthesis_started:
                self.health.record_inference("claude", False, synthesis_error["category"])
            else:
                # A local context failure did not attempt provider inference.
                synthesis_error = {"category": "answer_preparation", "message": "The retrieved evidence could not be prepared for an answer."}
                self.metrics.count("answer_preparation_errors")
                evidence["answer_preparation_error"] = synthesis_error
            answer = answer + "\n\n" + synthesis_error["message"] if answer else synthesis_error["message"] + " The graph evidence is available below."
        if not answer.strip():
            answer = "No answer text was returned. Inspect the graph evidence and step outcomes below."
            synthesis_error = {"category": "invalid_response", "message": "No answer text was returned."}
        reference_validation = {
            "valid": not citation_filter.invalid, "scope": "reference_ids_only",
            "model_references_present": bool(citation_filter.seen), "application_fallback": False,
        }
        if citation_filter.invalid:
            reference_validation["invalid_references_removed"] = True
        elif synthesis_error is None and not citation_filter.seen:
            # Identify supplied evidence without asserting claim-level support.
            supplied = [f"[{step['evidence_id']}]" for step in previous.values()
                        if step.get("status") in {"complete", "partial"}
                        and any(step.get(kind) for kind in ("nodes", "edges", "rows"))]
            if supplied:
                footer = "\n\nGraph evidence supplied: " + ", ".join(supplied) + "."
                answer += footer
                reference_validation["application_fallback"] = True
                (await self.io.call(self.store.update_if_active, run_id, graph_answer=answer))
                await self.emit(run_id, "graph_answer", {"text": footer, "delta": True})

        evidence["follow_up_questions"] = [] if status_message else followup_questions(previous)
        evidence["answer_reference_validation"] = reference_validation
        if synthesis_error:
            evidence["synthesis_error"] = synthesis_error
        (await self.io.call(self.store.update_if_active, run_id, graph_answer=answer, evidence=evidence))
        await self.emit(run_id, "graph_answer", {"answer": answer, "evidence": evidence, "delta": False})
        return evidence, not population_issue and synthesis_error is None and evidence["completeness"] != "partial" and not citation_filter.invalid

    async def execution(self, run_id: str):
        beat = asyncio.create_task(self.heartbeat(run_id))
        literature_task = None
        entered = False
        graph_visible = False
        literature_buffer = []
        literature_perspectives = []
        started = time.monotonic()
        try:
            async with self.semaphore:
                (await self.io.call(self.check_active, run_id))
                self.active += 1
                entered = True
                run = (await self.io.call(self.store.update_if_active, run_id, status="running", stage="preparing_execution"))
                await self.emit(run_id, "progress", {"stage": "preparing_execution"})

                async def literature_emit(kind, payload):
                    (await self.io.call(self.check_active, run_id))
                    if kind == "literature_perspective":
                        literature_perspectives.append(payload)
                        if graph_visible:
                            (await self.io.call(self.store.update_if_active, run_id, literature={"status": "partial", "perspectives": list(literature_perspectives)}))
                            await self.emit(run_id, kind, payload)
                        else:
                            literature_buffer.append((kind, payload))
                    elif kind == "literature_progress":
                        await self.emit(run_id, kind, {**payload, "parallel": True})

                async def retrieve_literature():
                    try:
                        answer = await asyncio.wait_for(self.literature.search(run["plan"].get("interpreted_question", run["question"]), (await self.io.call(self.store.history, run["session_id"])), literature_emit), self.settings.literature_timeout)
                        self.health.record_inference("hirn", answer.get("status") not in {"failed", "unavailable", "timeout"})
                        return answer
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        error = safe_error(exc)
                        self.health.record_inference("hirn", False, error["category"])
                        return {"status": "unavailable", "perspectives": list(literature_perspectives), "error": error}

                try:
                    _, graph_ok = await asyncio.wait_for(self.graph_answer(run_id, run), 160 if len(run["plan"]["steps"]) > 2 else self.settings.run_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = safe_error(exc)
                    current = (await self.io.call(self.store.get, run_id))
                    if (current.get('error') or {}).get('category') == 'preview_revalidation_required':
                        error = current['error']
                    answer = current["graph_answer"] or error["message"]
                    from .interrupted_evidence import complete_interrupted_evidence
                    evidence = complete_interrupted_evidence(run["plan"], current["evidence"], error,
                        self.started_graph_checks.get(run_id, set()))
                    evidence["error"] = error
                    (await self.io.call(self.store.update_if_active, run_id, graph_answer=answer, evidence=evidence, error=error))
                    await self.emit(run_id, "graph_answer", {"answer": answer, "evidence": evidence, "delta": False})
                    graph_ok = False
                from .literature_gate import literature_gate, VERSION as LITERATURE_GATE_VERSION
                current = (await self.io.call(self.store.get, run_id))
                allowed, gate_reason = literature_gate(run["plan"], current.get("evidence") or {}, current.get("graph_answer"))
                (await self.io.call(self.store.event_if_active, run_id, "literature_gate", {"allowed": allowed, "reason": gate_reason, "version": LITERATURE_GATE_VERSION}))
                if allowed and (graph_ok or (run["plan"].get("literature_intent") or {}).get("reason") == "explicit_request" or run["plan"].get("plan_mode") == "literature_only"):
                    (await self.io.call(self.store.event_if_active, run_id, "progress", {"stage": "searching_literature", "parallel": False}))
                    literature_task = asyncio.create_task(retrieve_literature())
                graph_visible = True
                self.metrics.observe("graph_answer", time.monotonic() - started)
                if literature_perspectives:
                    (await self.io.call(self.store.update_if_active, run_id, literature={"status": "partial", "perspectives": list(literature_perspectives)}))
                for kind, payload in literature_buffer:
                    await self.emit(run_id, kind, payload)
                if literature_task:
                    if not literature_task.done():
                        await self.emit(run_id, "progress", {"stage": "searching_literature"})
                    literature_answer = await literature_task
                    (await self.io.call(self.check_active, run_id))
                    (await self.io.call(self.store.update_if_active, run_id, literature=literature_answer))
                    await self.emit(run_id, "literature_complete", literature_answer)
                    literature_ok = literature_answer.get("status") not in {"failed", "unavailable", "timeout", "partial"}
                else:
                    literature_answer = {"status": "not_requested", "perspectives": [], "reason": gate_reason if graph_ok else "graph_answer_failed", "policy_version": LITERATURE_GATE_VERSION}
                    (await self.io.call(self.store.update_if_active, run_id, literature=literature_answer))
                    literature_ok = True
                self.metrics.observe("run_complete", time.monotonic() - started)
                (await self.io.call(self._terminal, run_id, "completed" if graph_ok and literature_ok else "partial"))
        except asyncio.CancelledError:
            (await self.io.call(self._cancelled, run_id))
            raise
        except Exception as exc:
            (await self.io.call(self._terminal, run_id, "failed", error=safe_error(exc)))
        finally:
            self.started_graph_checks.pop(run_id, None)
            if entered:
                self.active -= 1
            for task in (beat, literature_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (beat, literature_task) if task is not None], return_exceptions=True)

    async def close(self):
        self.shutting_down = True
        await self.admission.close()
        if self.store.owner and self.store.owner.snapshot()["state"] == "active":
            await self.io.call(self.store.interrupt_active, self.started_graph_checks)
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for task in self.auto_proceed_tasks:
            task.cancel()
        await asyncio.gather(*self.auto_proceed_tasks, return_exceptions=True)
        await self.health.stop()
        await asyncio.gather(*(adapter.close() for adapter in (self.gateway, self.graph, self.literature)), return_exceptions=True)
        if self.owner_task:
            self.owner_task.cancel()
            await asyncio.gather(self.owner_task, return_exceptions=True)
        if self.store.owner:
            await self.io.call(self.store.release_owner)
        await self.io.call(self.store.close)
        await self.io.close()


def create_app(settings=None, gateway=None, graph=None, literature=None) -> FastAPI:
    settings = settings or Settings()
    if gateway is None:
        from .llm import ClaudeGateway
        gateway = ClaudeGateway(settings)
    if graph is None:
        from .graph import GraphAdapter
        graph = GraphAdapter(settings)
    if literature is None:
        from .literature import LiteratureAdapter
        literature = LiteratureAdapter(settings)
    runtime = Runtime(settings, gateway, graph, literature)

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
        except BaseException:
            # A refused second owner must not run shutdown recovery/release on
            # behalf of the existing owner.
            await runtime.io.call(runtime.store.close)
            await runtime.io.close()
            await asyncio.gather(*(adapter.close() for adapter in (runtime.gateway, runtime.graph, runtime.literature)), return_exceptions=True)
            raise
        warm_task = None
        if hasattr(runtime.graph, 'ground_question'):
            from .preplanning_grounding import warm_grounding
            async def warm():
                try:
                    await warm_grounding(runtime.graph)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    runtime.metrics.count('grounding_warmup_failed')
            warm_task = asyncio.create_task(warm())
        try:
            yield
        finally:
            if warm_task is not None and not warm_task.done():
                warm_task.cancel()
            if warm_task is not None:
                await asyncio.gather(warm_task, return_exceptions=True)
            await runtime.close()

    app = FastAPI(title="PanKagent vNext", version="2.0.0", lifespan=lifespan)
    app.add_middleware(JSONResponseLimitMiddleware)
    app.state.runtime = runtime

    @app.exception_handler(OwnershipError)
    @app.exception_handler(PersistenceBusy)
    async def persistence_unavailable(request, exc):
        return JSONResponse({"detail": "Service persistence is temporarily unavailable."}, status_code=503, headers={"Retry-After": "2"})

    def get_run(run_id):
        run = runtime.store.get(run_id)
        if run is None:
            raise HTTPException(404, "Run not found.")
        return run

    def operator(request: Request):
        if settings.operator_token:
            provided = request.headers.get("authorization", "").removeprefix("Bearer ")
            if hmac.compare_digest(provided, settings.operator_token):
                return
        else:
            try:
                if request.client and ipaddress.ip_address(request.client.host).is_loopback:
                    return
            except ValueError:
                pass
        raise HTTPException(403, "Operator access required.")

    def admitted(function):
        from functools import wraps
        @wraps(function)
        async def wrapped(*args, **kwargs):
            return await runtime.admission.run(function, *args, **kwargs)
        return wrapped

    @app.post("/v2/plans", status_code=202)
    @admitted
    async def plan(body: PlanRequest, request: Request):
        if body.event_source != "user":
            operator(request)
        (await runtime.io.call(runtime.check_capacity))
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question must not be blank.")
        try:
            run = (await runtime.io.call(runtime.store.create, question, body.session_id, include_context=body.include_context,
                audit={"original_question": body.question, "source": body.event_source, "versions": runtime.audit_identity,
                       "requested_options": {"include_context": body.include_context, "auto_proceed_seconds":body.auto_proceed_seconds}},
                legacy_retry_submission=body.question if body.revision_mode == "legacy_replacement" and body.revision_instruction is None else None))
        except KeyError:
            raise HTTPException(404, "Session not found.") from None
        try:
            await runtime.io.call(runtime.store.event_if_active, run["run_id"], "progress", {"stage": "queued"})
        finally:
            runtime.launch(run["run_id"], runtime.planning(run["run_id"]))
        runtime.metrics.count("plans_requested")
        return {key: run[key] for key in ("plan_id", "run_id", "session_id", "status")} | {"events_url": f'/v2/runs/{run["run_id"]}/events', "plan_url": f'/v2/plans/{run["plan_id"]}'}

    @app.post("/v2/plans/{plan_id}/revise", status_code=202)
    @admitted
    async def revise(plan_id: str, body: RevisionRequest, request: Request):
        if body.event_source != "user":
            operator(request)
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question must not be blank.")
        old = (await runtime.io.call(runtime.store.by_plan, plan_id))
        if old is None:
            raise HTTPException(404, "Plan not found.")
        if old["status"] not in {"planning", "awaiting_confirmation"}:
            raise HTTPException(409, "Only an unconfirmed plan can be revised; create a new plan.")
        (await runtime.io.call(runtime.check_capacity, replacing=old["run_id"]))
        try:
            old, run = (await runtime.io.call(runtime.store.revise, plan_id, question, include_context=body.include_context if "include_context" in body.model_fields_set else old["include_context"],
                audit={"source": body.event_source, "versions": runtime.audit_identity,
                       "revision_instruction": body.revision_instruction, "revision_mode": body.revision_mode}))
        except ValueError:
            raise HTTPException(409, "This plan was already confirmed or ended.") from None
        old_task = runtime.tasks.get(old["run_id"])
        if old_task:
            old_task.cancel()
        try:
            await runtime.io.call(runtime.store.event_if_active, old["run_id"], "terminal", {"status": "superseded", "replacement_run_id": run["run_id"]})
            await runtime.io.call(runtime.store.event_if_active, run["run_id"], "progress", {"stage": "queued"})
        finally:
            runtime.launch(run["run_id"], runtime.planning(run["run_id"]))
        runtime.metrics.count("plans_revised")
        runtime.metrics.count("plans_requested")
        return {key: run[key] for key in ("plan_id", "run_id", "session_id", "status")} | {"events_url": f'/v2/runs/{run["run_id"]}/events', "plan_url": f'/v2/plans/{run["plan_id"]}'}

    @app.get("/v2/plans/{plan_id}")
    async def plan_state(plan_id: str):
        run = (await runtime.io.call(runtime.store.by_plan, plan_id))
        if run is None:
            raise HTTPException(404, "Plan not found.")
        return public_run((await runtime.io.call(runtime.store.snapshot, run["run_id"])))

    @app.post("/v2/plans/{plan_id}/confirm", status_code=202)
    @admitted
    async def confirm(plan_id: str):
        run = (await runtime.io.call(runtime.store.by_plan, plan_id))
        if run is None:
            raise HTTPException(404, "Plan not found.")
        if run["status"] == "planning":
            raise HTTPException(409, "The plan is still being prepared.")
        if run["status"] == "awaiting_confirmation":
            if public_run(run).get('rerun_advisory'):
                raise HTTPException(409, 'Revise this saved plan to validate initial evidence.')
            if run.get("preview") is None:
                raise HTTPException(409, "Revise this saved plan to validate initial evidence.")
            if run["plan"].get("clarification"):
                raise HTTPException(409, "The plan needs clarification; submit a narrower question.")
            issue = runtime.confirmation_preview_issue(run)
            if issue:
                recovery = runtime.preview_recovery(run, issue)
                (await runtime.io.call(runtime._terminal, run['run_id'], 'failed', error={'category': recovery['category'],
                    'message': recovery['message'], 'recovery': recovery}))
                raise HTTPException(409, {'category': recovery['category'], 'message': recovery['message'], 'recovery': recovery})
            (await runtime.io.call(runtime.check_capacity))
            if (await runtime.io.call(runtime.store.confirm, run["run_id"])):
                try:
                    await runtime.io.call(runtime.store.event_if_active, run["run_id"], "progress", {"stage": "queued"})
                finally:
                    runtime.launch(run["run_id"], runtime.execution(run["run_id"]))
        elif run["status"] in {"cancelled", "interrupted", "failed", "superseded"}:
            raise HTTPException(409, "This run has ended; create a new plan.")
        current = (await runtime.io.call(runtime.store.get, run["run_id"]))
        if current["status"] in {"cancelled", "interrupted", "failed", "superseded"}:
            raise HTTPException(409, "This run has ended; create a new plan.")
        return {"run_id": run["run_id"], "status": current["status"], "events_url": f'/v2/runs/{run["run_id"]}/events'}

    runtime.confirm_plan = confirm

    @app.get("/v2/runs/{run_id}")
    async def run_state(run_id: str):
        (await runtime.io.call(get_run, run_id))
        return public_run((await runtime.io.call(runtime.store.snapshot, run_id)))

    @app.post("/v2/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = (await runtime.io.call(get_run, run_id))
        (await runtime.io.call(runtime.store.audit_event, run_id, "cancellation_requested", {"stage": run["stage"], "status": run["status"], "upstream_completion": "unknown"}))
        if run["status"] not in TERMINAL:
            (await runtime.io.call(runtime._terminal, run_id, "cancelled"))
            task = runtime.tasks.get(run_id)
            if task:
                task.cancel()
        return {"run_id": run_id, "status": (await runtime.io.call(runtime.store.get, run_id))["status"]}

    @app.post("/v2/runs/{run_id}/interactions")
    async def interaction(run_id: str, body: InteractionRequest, request: Request):
        operator(request)
        (await runtime.io.call(get_run, run_id))
        payload = body.model_dump(mode="json", exclude={"event_id", "kind"})
        metadata = (await runtime.io.call(runtime.store.audit_metadata, run_id)) or {}
        payload["source"] = metadata.get("source", "unknown")
        status = (await runtime.io.call(runtime.store.audit_event, run_id, body.kind, payload, str(body.event_id)))
        runtime.metrics.count("audit_interactions_" + status)
        return JSONResponse({"version": 1, "status": status}, status_code=503 if status == "unavailable" else 200)

    @app.get("/v2/runs/{run_id}/audit")
    async def audit(run_id: str, request: Request):
        operator(request)
        (await runtime.io.call(get_run, run_id))
        return (await runtime.io.call(runtime.store.audit_snapshot, run_id))

    @app.get("/v2/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        (await runtime.io.call(get_run, run_id))
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Last-Event-ID must be a nonnegative sequence number.") from None
        if cursor < 0:
            raise HTTPException(400, "Last-Event-ID must be nonnegative.")

        async def stream():
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                batch = (await runtime.io.call(runtime.store.events_after, run_id, cursor))
                for event in batch:
                    cursor = event["sequence"]
                    snapshot = await runtime.io.call(runtime.store.get, run_id)
                    event = public_payload(event, context=snapshot)
                    yield sse_event_bytes(event)
                if not batch and (await runtime.io.call(runtime.store.get, run_id))["status"] in TERMINAL:
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.get("/health/live")
    async def live():
        return {"version": 2, "state": "healthy", "service": "pankagent-vnext"}

    @app.get("/health/ready")
    async def ready():
        health = runtime.health.snapshot()
        return JSONResponse({"version": 2, "ready": health["ready"], "state": health["state"]}, status_code=200 if health["ready"] else 503)

    @app.get("/health/components")
    async def components(request: Request):
        operator(request)
        return runtime.health.snapshot()

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request):
        operator(request)
        return PlainTextResponse(runtime.metrics.render(runtime.queue_snapshot(), runtime.health.budget_snapshot()), media_type="text/plain; version=0.0.4")

    @app.get("/demo", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    async def demo():
        return FileResponse(Path(__file__).parent / "assets" / "index.html")

    return app
