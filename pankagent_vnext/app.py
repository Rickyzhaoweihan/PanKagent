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
from .evidence_status import outcome_message, confirmation_eligible
from .audit import InteractionRequest, deployment_identity, recorder
from .config import Settings
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
    session_id: str | None = Field(default=None, max_length=100)


def public_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if key not in {"created_epoch", "preview_cache"}}


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

    def __init__(self, count: int):
        self.count, self.pending, self.invalid = count, "", False
        self.seen: set[int] = set()

    def feed(self, value: str, final: bool = False) -> str:
        value = self.pending + value
        self.pending = ""
        if not final:
            start = value.rfind("[")
            if start >= 0 and "]" not in value[start:] and len(value) - start < 80:
                self.pending, value = value[start:], value[:start]

        def replace(match):
            if 1 <= int(match.group(1)) <= self.count:
                self.seen.add(int(match.group(1)))
                return match.group(0)
            self.invalid = True
            return "[unverified reference]"

        return re.sub(r"\[G(\d+)\]", replace, value)


def normalize_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("Invalid structured plan")
    if len(plan["steps"]) > 3:
        return {**plan, "steps": [], "clarification": "Please narrow this to at most three graph investigations."}
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
    if not plan["steps"] and not plan["clarification"]:
        plan["clarification"] = "Please provide a concrete entity or graph question."
    return plan


def aggregate_evidence(previous: dict) -> dict:
    steps = list(previous.values())
    result = {"steps": steps, "nodes": [], "edges": [], "queries": [], "provenance": [], "graph_version": None}
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
        step["evidence_id"] = f"G{index + 1}"
        result["queries"].extend(step.get("queries", []))
        provenance = step.get("provenance", [])
        result["provenance"].extend(provenance if isinstance(provenance, list) else [provenance])
        result["graph_version"] = result["graph_version"] or step.get("graph_version")
    states = [step.get("status", "complete") for step in steps]
    result["completeness"] = "partial" if any(state in {"failed", "partial"} for state in states) else "empty" if states and all(state == "empty" for state in states) else "complete"
    result["truncated"] = any(step.get("truncated") for step in steps)
    return result


class Runtime:
    def __init__(self, settings, gateway, graph, literature):
        self.settings, self.gateway, self.graph, self.literature = settings, gateway, graph, literature
        self.store = Store(settings.state_dir)
        self.audit_identity = deployment_identity(settings)
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent)
        self.active = 0
        self.metrics = Metrics()
        self.health = HealthMonitor(settings, gateway, graph, literature, self.store, self.queue_snapshot)
        self.shutting_down = False

    def queue_snapshot(self):
        return {"active_queries": self.active, "queue_depth": max(0, len(self.tasks) - self.active), "capacity": self.settings.max_concurrent, "audit_dropped": self.store.audit_dropped}

    def check_capacity(self, replacing=None):
        queued = len(self.tasks) - int(replacing in self.tasks)
        if self.shutting_down or queued >= self.settings.max_concurrent + getattr(self.settings, "max_queue", 8):
            raise HTTPException(429, "The development queue is full; retry shortly.", headers={"Retry-After": "2"})
        budget = self.health.budget_snapshot()
        if not budget or budget.get("remaining_usd", 0) <= 0:
            raise HTTPException(503, "The development evaluation budget is unavailable or exhausted.")

    def check_active(self, run_id: str):
        # Some upstream clients suppress task cancellation while cleaning up.
        # A terminal durable state remains authoritative even if they return.
        if self.store.get(run_id)["status"] in TERMINAL:
            raise asyncio.CancelledError

    def launch(self, run_id: str, coroutine):
        async def audited():
            token = recorder.set(lambda kind, payload: self.store.audit_event(run_id, kind, payload))
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
        run = self.store.get(run_id)
        if run["status"] in TERMINAL:
            return
        stage = payload.get("stage") if event_type == "progress" else None
        if stage in {"planning", "resolving_entities", "preparing_preview", "preparing_execution", "reusing_preview", "generating_cypher", "validating", "querying_graph", "writing_answer", "searching_literature", "queued"}:
            self.store.update(run_id, stage=stage)
        self.store.event(run_id, event_type, payload)

    async def heartbeat(self, run_id: str):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            run = self.store.get(run_id)
            if run is None or run["status"] not in ACTIVE:
                return
            self.store.event(run_id, "heartbeat", {"activity": run["stage"]})

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
                self.check_active(run_id)
                self.active += 1
                entered = True
                run = self.store.get(run_id)
                await self.emit(run_id, "progress", {"stage": "planning"})
                claude_pending = True
                model_started = time.monotonic()
                proposed = await asyncio.wait_for(self.gateway.plan(run["question"], self.planning_history(run)), self.settings.plan_timeout)
                self.metrics.observe("model_plan", time.monotonic() - model_started)
                self.check_active(run_id)
                self.health.record_inference("claude", True)
                claude_pending = False
                plan = apply_literature_policy(normalize_plan(proposed), run["question"])
                metadata = self.store.audit_metadata(run_id) or {}
                parent = self.store.get(metadata.get("parent_run_id")) if metadata.get("parent_run_id") else None
                if parent and metadata.get("revision_mode") == "instruction":
                    plan = preserve_revision_preference(plan, parent.get("plan") or {}, metadata.get("revision_instruction") or run["question"])
                    from .revision_guard import preserve_additive_scope
                    plan = preserve_additive_scope(plan, parent.get("plan") or {}, metadata.get("revision_instruction") or run["question"])
                    plan["revision_trace"] = {"parent_plan_id": parent["plan_id"],
                        "original_question": metadata.get("original_question"),
                        "instruction": metadata.get("revision_instruction") or run["question"],
                        "before_steps": (parent.get("plan") or {}).get("steps", []),
                        "after_steps": deepcopy(plan["steps"])}
                plan["contract_sha256"] = CONTRACT_DIGEST
                plan["original_question"] = metadata.get("original_question", run["question"])
                plan["include_context"] = run["include_context"]
                self.store.update(run_id, plan=plan)
                preview_started = time.monotonic()
                try:
                    await asyncio.wait_for(self.preflight(run_id, plan), self.settings.preview_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.check_active(run_id)
                    error = safe_error(exc)
                    if isinstance(exc, TimeoutError):
                        component = "cypher" if self.store.get(run_id)["stage"] == "generating_cypher" else "neo4j"
                        self.health.record_inference(component, False, "timeout")
                    self.metrics.count("preview_preparation_errors")
                    current = self.store.get(run_id)
                    plan = current["plan"]
                    previous = {step["step_id"]: step for step in (current["preview"] or {}).get("evidence", {}).get("steps", [])}
                    for step in plan["steps"]:
                        previous.setdefault(step["id"], self.failed_step(step, error))
                    cache = current["preview_cache"] or {"identity": None, "step_completed_epochs": {}}
                    self.save_preview(run_id, previous, cache, error=error)
                finally:
                    self.metrics.observe("preview", time.monotonic() - preview_started)
                self.check_active(run_id)
                current = self.store.get(run_id)
                self.store.update(run_id, status="awaiting_confirmation", stage="awaiting_confirmation")
                self.store.event(run_id, "plan_ready", {"plan_id": run["plan_id"], "plan": current["plan"], "preview": current["preview"]})
                self.metrics.count("plans_ready")
                self.metrics.observe("plan_ready", time.monotonic() - started)
        except asyncio.CancelledError:
            self._cancelled(run_id)
            raise
        except Exception as exc:
            error = safe_error(exc)
            if claude_pending:
                self.health.record_inference("claude", False, error["category"])
            else:
                self.metrics.count("plan_preparation_errors")
            self._terminal(run_id, "failed", error=error)
        finally:
            if entered:
                self.active -= 1
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    @staticmethod
    def failed_step(step, error):
        return {"step_id": step["id"], "question": step["question"], "status": "failed", "error": error,
                "nodes": [], "edges": [], "rows": [], "validation": [{"valid": False, "reasons": [error["category"]]}]}

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
        raw = json.dumps({"version": 1, "plan": plan, "graph": graph_identity, "access": access}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def save_preview(self, run_id, previous, cache, *, error=None):
        evidence = aggregate_evidence(previous)
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
                   "created_at": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                   "reusable_until": datetime.fromtimestamp(epoch + self.settings.preview_ttl_seconds, timezone.utc).isoformat() if cache.get("identity") else None}
        if error:
            preview["error"] = error
            if states and any(state != "failed" for state in states):
                preview["status"] = "partial"
        preview["confirmation_eligible"] = confirmation_eligible(plan, preview)
        self.store.update(run_id, preview=preview, preview_cache=cache)
        return preview

    async def execute_step(self, run_id, step, previous):
        self.check_active(run_id)
        prior_generation = getattr(self.graph, "last_generation_success", None)
        prior_query = getattr(self.graph, "last_query_success", None)
        try:
            result = await self.graph.execute(step, previous, lambda kind, payload: self.emit(run_id, kind, payload))
            self.check_active(run_id)
            result.setdefault("step_id", step["id"])
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
            component = "cypher" if self.store.get(run_id)["stage"] == "generating_cypher" else "neo4j"
            self.health.record_inference(component, False, error["category"])
            return self.failed_step(step, error)

    async def preflight(self, run_id, plan):
        await self.emit(run_id, "progress", {"stage": "preparing_preview"})
        if plan.get("steps") and not plan.get("clarification") and hasattr(self.graph, "prepare_plan"):
            try:
                plan = normalize_plan(await self.graph.prepare_plan(deepcopy(plan), lambda kind, payload: self.emit(run_id, kind, payload)))
                self.check_active(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.record_inference("neo4j", False, safe_error(exc)["category"])
                raise
            self.store.update(run_id, plan=plan)
        cache = {"identity": self.preview_identity(plan), "step_completed_epochs": {}, "step_identities": {}}
        previous = {}
        if plan.get("clarification"):
            metadata = self.store.audit_metadata(run_id) or {}
            parent = self.store.get(metadata.get("parent_run_id")) if plan.get('retained_previous_plan') and metadata.get('parent_run_id') else None
            if parent:
                previous = {s['step_id']: deepcopy(s) for s in ((parent.get('preview') or {}).get('evidence') or {}).get('steps', [])}
            self.save_preview(run_id, previous, {"identity": None, "step_completed_epochs": {}})
            return
        metadata = self.store.audit_metadata(run_id) or {}
        parent = self.store.get(metadata.get("parent_run_id")) if metadata.get("parent_run_id") else None
        parent_cache = (parent or {}).get("preview_cache") or {}
        parent_steps = {s["step_id"]: s for s in (((parent or {}).get("preview") or {}).get("evidence") or {}).get("steps", [])}
        reused = set()
        for step in plan["steps"]:
            fingerprint = self.preview_identity({"steps": [step], "contract_sha256": CONTRACT_DIGEST})
            old = parent_steps.get(step["id"], {})
            epoch = parent_cache.get("step_completed_epochs", {}).get(step["id"], 0)
            can_reuse = (old.get("status") in {"complete", "empty"}
                and parent_cache.get("step_identities", {}).get(step["id"]) == fingerprint
                and 0 <= time.time() - epoch < self.settings.preview_ttl_seconds
                and set(step.get("depends_on", [])) <= reused)
            if can_reuse:
                previous[step["id"]] = deepcopy(old)
                reused.add(step["id"])
                self.metrics.count("revision_preview_reused")
                await self.emit(run_id, "preview_reused", {"step_id": step["id"], "source": "parent_plan"})
            else:
                previous[step["id"]] = await self.execute_step(run_id, step, previous)
                epoch = time.time()
            self.check_active(run_id)
            cache["step_completed_epochs"][step["id"]] = epoch
            cache["step_identities"][step["id"]] = fingerprint
            preview = self.save_preview(run_id, previous, cache)
            await self.emit(run_id, "preview_step", {"step_id": step["id"], "evidence": previous[step["id"]], "preview": preview})
        if self.preview_identity(plan) != cache["identity"]:
            cache["identity"] = None
        self.save_preview(run_id, previous, cache)

    def _terminal(self, run_id, status, **fields):
        run = self.store.get(run_id)
        if run["status"] in TERMINAL:
            return
        self.store.update(run_id, status=status, stage=status, **fields)
        self.store.event(run_id, "terminal", {"status": status, **({"error": fields["error"]} if fields.get("error") else {})})
        self.metrics.count(f"runs_{status}")

    def _cancelled(self, run_id):
        self._terminal(run_id, "interrupted" if self.shutting_down else "cancelled")

    def preview_reuse_reason(self, step, cached, cache, matching, reused, previous):
        if not matching:
            return "identity_changed"
        age = time.time() - cache.get("step_completed_epochs", {}).get(step["id"], 0)
        if not 0 <= age < self.settings.preview_ttl_seconds:
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
        previous = {}
        cache = run.get("preview_cache") or {}
        preview = run.get("preview") or {}
        cached_steps = {step["step_id"]: step for step in preview.get("evidence", {}).get("steps", [])}
        reused = set()
        reuse_info = {"reused_step_ids": [], "retrieved_step_ids": [], "unreused_reasons": {}}
        for step in run["plan"]["steps"]:
            self.check_active(run_id)
            try:
                matching = bool(cache.get("identity")) and cache["identity"] == self.preview_identity(run["plan"])
            except Exception:
                matching = False
            cached = cached_steps.get(step["id"], {})
            reason = self.preview_reuse_reason(step, cached, cache, matching, reused, previous)
            if reason is None:
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
                previous[step["id"]] = await self.execute_step(run_id, step, previous)
            evidence = aggregate_evidence(previous)
            evidence["preview_reuse"] = reuse_info
            self.store.update(run_id, evidence=evidence)
            await self.emit(run_id, "graph_step", {"step_id": step["id"], "evidence": previous[step["id"]], "reused_preview": reason is None})

        evidence = aggregate_evidence(previous)
        evidence["preview_reuse"] = reuse_info
        await self.emit(run_id, "progress", {"stage": "writing_answer"})
        answer = ""
        citation_filter = CitationFilter(len(previous))
        synthesis_error = None
        synthesis_started = False
        status_message = outcome_message(previous)
        async def tokens():
            if status_message:
                yield status_message
            else:
                async for token in self.gateway.synthesize(question, previous, **options):
                    yield token
        try:
            question = run["plan"].get("interpreted_question", run["question"])
            options = {}
            if not status_message and hasattr(self.gateway, "prepare_answer"):
                prepared = self.gateway.prepare_answer(question, previous)
                evidence["answer_profile"] = prepared.profile
                self.store.update(run_id, evidence=evidence)
                self.metrics.observe("answer_skill_selection", prepared.profile["timing_ms"]["total"] / 1000)
                self.metrics.count("answer_skill_cache_hit" if prepared.profile["cache_hit"] else "answer_skill_cache_miss")
                await self.emit(run_id, "answer_profile", {"profile": prepared.profile})
                options["prepared"] = prepared
            synthesis_started = not bool(status_message)
            async for token in tokens():
                self.check_active(run_id)
                visible = citation_filter.feed(token)
                if visible:
                    answer += visible
                    self.store.update(run_id, graph_answer=answer)
                    await self.emit(run_id, "graph_answer", {"text": visible, "delta": True})
            tail = citation_filter.feed("", final=True)
            self.check_active(run_id)
            if tail:
                answer += tail
                await self.emit(run_id, "graph_answer", {"text": tail, "delta": True})
            if synthesis_started:
                self.health.record_inference("claude", True)
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
            supplied = [f"[G{index}]" for index, step in enumerate(previous.values(), 1)
                        if step.get("status") in {"complete", "partial"}
                        and any(step.get(kind) for kind in ("nodes", "edges", "rows"))]
            if supplied:
                footer = "\n\nGraph evidence supplied: " + ", ".join(supplied) + "."
                answer += footer
                reference_validation["application_fallback"] = True
                self.store.update(run_id, graph_answer=answer)
                await self.emit(run_id, "graph_answer", {"text": footer, "delta": True})
        evidence["follow_up_questions"] = [] if status_message else followup_questions(previous)
        evidence["answer_reference_validation"] = reference_validation
        if synthesis_error:
            evidence["synthesis_error"] = synthesis_error
        self.store.update(run_id, graph_answer=answer, evidence=evidence)
        await self.emit(run_id, "graph_answer", {"answer": answer, "evidence": evidence, "delta": False})
        return evidence, synthesis_error is None and evidence["completeness"] != "partial" and not citation_filter.invalid

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
                self.check_active(run_id)
                self.active += 1
                entered = True
                run = self.store.update(run_id, status="running", stage="preparing_execution")
                await self.emit(run_id, "progress", {"stage": "preparing_execution"})

                async def literature_emit(kind, payload):
                    self.check_active(run_id)
                    if kind == "literature_perspective":
                        literature_perspectives.append(payload)
                        if graph_visible:
                            self.store.update(run_id, literature={"status": "partial", "perspectives": list(literature_perspectives)})
                            await self.emit(run_id, kind, payload)
                        else:
                            literature_buffer.append((kind, payload))
                    elif kind == "literature_progress":
                        await self.emit(run_id, kind, {**payload, "parallel": True})

                async def retrieve_literature():
                    try:
                        answer = await asyncio.wait_for(self.literature.search(run["plan"].get("interpreted_question", run["question"]), self.store.history(run["session_id"]), literature_emit), self.settings.literature_timeout)
                        self.health.record_inference("hirn", answer.get("status") not in {"failed", "unavailable", "timeout"})
                        return answer
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        error = safe_error(exc)
                        self.health.record_inference("hirn", False, error["category"])
                        return {"status": "unavailable", "perspectives": list(literature_perspectives), "error": error}

                if run["plan"].get("literature"):
                    self.store.event(run_id, "progress", {"stage": "searching_literature", "parallel": True})
                    literature_task = asyncio.create_task(retrieve_literature())
                try:
                    _, graph_ok = await asyncio.wait_for(self.graph_answer(run_id, run), self.settings.run_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = safe_error(exc)
                    current = self.store.get(run_id)
                    answer = current["graph_answer"] or error["message"]
                    evidence = current["evidence"] or {"steps": [], "nodes": [], "edges": [], "completeness": "partial"}
                    evidence["error"] = error
                    self.store.update(run_id, graph_answer=answer, evidence=evidence, error=error)
                    await self.emit(run_id, "graph_answer", {"answer": answer, "evidence": evidence, "delta": False})
                    graph_ok = False
                graph_visible = True
                self.metrics.observe("graph_answer", time.monotonic() - started)
                if literature_perspectives:
                    self.store.update(run_id, literature={"status": "partial", "perspectives": list(literature_perspectives)})
                for kind, payload in literature_buffer:
                    await self.emit(run_id, kind, payload)
                if literature_task:
                    if not literature_task.done():
                        await self.emit(run_id, "progress", {"stage": "searching_literature"})
                    literature_answer = await literature_task
                    self.check_active(run_id)
                    self.store.update(run_id, literature=literature_answer)
                    await self.emit(run_id, "literature_complete", literature_answer)
                    literature_ok = literature_answer.get("status") not in {"failed", "unavailable", "timeout", "partial"}
                else:
                    literature_answer = {"status": "not_requested", "perspectives": []}
                    self.store.update(run_id, literature=literature_answer)
                    literature_ok = True
                self.metrics.observe("run_complete", time.monotonic() - started)
                self._terminal(run_id, "completed" if graph_ok and literature_ok else "partial")
        except asyncio.CancelledError:
            self._cancelled(run_id)
            raise
        except Exception as exc:
            self._terminal(run_id, "failed", error=safe_error(exc))
        finally:
            if entered:
                self.active -= 1
            for task in (beat, literature_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (beat, literature_task) if task is not None], return_exceptions=True)

    async def close(self):
        self.shutting_down = True
        self.store.interrupt_active()
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        await self.health.stop()
        await asyncio.gather(*(adapter.close() for adapter in (self.gateway, self.graph, self.literature)), return_exceptions=True)
        self.store.close()


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
        runtime.store.interrupt_active()
        runtime.health.start()
        yield
        await runtime.close()

    app = FastAPI(title="PanKagent vNext", version="2.0.0", lifespan=lifespan)
    app.add_middleware(JSONResponseLimitMiddleware)
    app.state.runtime = runtime

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

    @app.post("/v2/plans", status_code=202)
    async def plan(body: PlanRequest, request: Request):
        if body.event_source != "user":
            operator(request)
        runtime.check_capacity()
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question must not be blank.")
        try:
            run = runtime.store.create(question, body.session_id, include_context=body.include_context,
                audit={"original_question": body.question, "source": body.event_source, "versions": runtime.audit_identity,
                       "requested_options": {"include_context": body.include_context}})
        except KeyError:
            raise HTTPException(404, "Session not found.") from None
        runtime.store.event(run["run_id"], "progress", {"stage": "queued"})
        runtime.launch(run["run_id"], runtime.planning(run["run_id"]))
        runtime.metrics.count("plans_requested")
        return {key: run[key] for key in ("plan_id", "run_id", "session_id", "status")} | {"events_url": f'/v2/runs/{run["run_id"]}/events', "plan_url": f'/v2/plans/{run["plan_id"]}'}

    @app.post("/v2/plans/{plan_id}/revise", status_code=202)
    async def revise(plan_id: str, body: RevisionRequest, request: Request):
        if body.event_source != "user":
            operator(request)
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Question must not be blank.")
        old = runtime.store.by_plan(plan_id)
        if old is None:
            raise HTTPException(404, "Plan not found.")
        if old["status"] not in {"planning", "awaiting_confirmation"}:
            raise HTTPException(409, "Only an unconfirmed plan can be revised; create a new plan.")
        runtime.check_capacity(replacing=old["run_id"])
        try:
            old, run = runtime.store.revise(plan_id, question, include_context=body.include_context if "include_context" in body.model_fields_set else old["include_context"],
                audit={"source": body.event_source, "versions": runtime.audit_identity,
                       "revision_instruction": body.revision_instruction, "revision_mode": body.revision_mode})
        except ValueError:
            raise HTTPException(409, "This plan was already confirmed or ended.") from None
        runtime.store.event(old["run_id"], "terminal", {"status": "superseded", "replacement_run_id": run["run_id"]})
        old_task = runtime.tasks.get(old["run_id"])
        if old_task:
            old_task.cancel()
        runtime.store.event(run["run_id"], "progress", {"stage": "queued"})
        runtime.launch(run["run_id"], runtime.planning(run["run_id"]))
        runtime.metrics.count("plans_revised")
        runtime.metrics.count("plans_requested")
        return {key: run[key] for key in ("plan_id", "run_id", "session_id", "status")} | {"events_url": f'/v2/runs/{run["run_id"]}/events', "plan_url": f'/v2/plans/{run["plan_id"]}'}

    @app.get("/v2/plans/{plan_id}")
    async def plan_state(plan_id: str):
        run = runtime.store.by_plan(plan_id)
        if run is None:
            raise HTTPException(404, "Plan not found.")
        return public_run(runtime.store.snapshot(run["run_id"]))

    @app.post("/v2/plans/{plan_id}/confirm", status_code=202)
    async def confirm(plan_id: str):
        run = runtime.store.by_plan(plan_id)
        if run is None:
            raise HTTPException(404, "Plan not found.")
        if run["status"] == "planning":
            raise HTTPException(409, "The plan is still being prepared.")
        if run["status"] == "awaiting_confirmation":
            if run.get("preview") is None:
                raise HTTPException(409, "Revise this saved plan to validate initial evidence.")
            if run["plan"].get("clarification"):
                raise HTTPException(409, "The plan needs clarification; submit a narrower question.")
            if run["preview"].get("confirmation_eligible") is False:
                raise HTTPException(409, "Initial graph retrieval is blocked. Revise the plan before confirmation.")
            runtime.check_capacity()
            if runtime.store.confirm(run["run_id"]):
                runtime.store.event(run["run_id"], "progress", {"stage": "queued"})
                runtime.launch(run["run_id"], runtime.execution(run["run_id"]))
        elif run["status"] in {"cancelled", "interrupted", "failed", "superseded"}:
            raise HTTPException(409, "This run has ended; create a new plan.")
        current = runtime.store.get(run["run_id"])
        if current["status"] in {"cancelled", "interrupted", "failed", "superseded"}:
            raise HTTPException(409, "This run has ended; create a new plan.")
        return {"run_id": run["run_id"], "status": current["status"], "events_url": f'/v2/runs/{run["run_id"]}/events'}

    @app.get("/v2/runs/{run_id}")
    async def run_state(run_id: str):
        get_run(run_id)
        return public_run(runtime.store.snapshot(run_id))

    @app.post("/v2/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        run = get_run(run_id)
        runtime.store.audit_event(run_id, "cancellation_requested", {"stage": run["stage"], "status": run["status"], "upstream_completion": "unknown"})
        if run["status"] not in TERMINAL:
            runtime._terminal(run_id, "cancelled")
            task = runtime.tasks.get(run_id)
            if task:
                task.cancel()
        return {"run_id": run_id, "status": runtime.store.get(run_id)["status"]}

    @app.post("/v2/runs/{run_id}/interactions")
    async def interaction(run_id: str, body: InteractionRequest, request: Request):
        operator(request)
        get_run(run_id)
        payload = body.model_dump(mode="json", exclude={"event_id", "kind"})
        metadata = runtime.store.audit_metadata(run_id) or {}
        payload["source"] = metadata.get("source", "unknown")
        status = runtime.store.audit_event(run_id, body.kind, payload, str(body.event_id))
        runtime.metrics.count("audit_interactions_" + status)
        return JSONResponse({"version": 1, "status": status}, status_code=503 if status == "unavailable" else 200)

    @app.get("/v2/runs/{run_id}/audit")
    async def audit(run_id: str, request: Request):
        operator(request)
        get_run(run_id)
        return runtime.store.audit_snapshot(run_id)

    @app.get("/v2/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        get_run(run_id)
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
                batch = runtime.store.events_after(run_id, cursor)
                for event in batch:
                    cursor = event["sequence"]
                    yield sse_event_bytes(event)
                if not batch and runtime.store.get(run_id)["status"] in TERMINAL:
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
