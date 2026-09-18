"""Isolated same-origin PanKgraph gateway and durable results application."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import re
import time
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse, RedirectResponse

from pankagent_vnext.app import CitationFilter, safe_error
from pankagent_vnext.audit import InteractionRequest, recorder
from pankagent_vnext.config import Settings
from pankagent_vnext.persistence import SerializedPersistence, PersistenceBusy, AdmissionContinuations
from pankagent_vnext.ownership import OwnershipError
from pankagent_vnext.llm import ClaudeGateway, STYLE_VERSION
from pankagent_vnext.evidence_status import outcome_message
from pankagent_vnext.transport import JSONResponseLimitMiddleware

from . import functional
from .assembly import assemble
from .auth import DemoAuthentication
from .config import ResultsSettings
from .coordinates import CoordinateLookup
from .health import ResultsHealth
from .inputs import ResultRequest, agent_snapshot, template_snapshot, template_question
from .layout import LayoutService, LAYOUT_VERSION
from .query import QueryService
from .resource_registry import REGISTRY_VERSION
from .resources import ResourceManager, ResourceError
from .store import ResultStore

RESULT_VERSION = "results-2-audit-repair"


class PrefixMiddleware:
    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope["path"] == self.prefix or scope["path"].startswith(self.prefix + "/")):
            scope = {**scope, "path": scope["path"][len(self.prefix):] or "/"}
        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                    (b"content-security-policy", b"connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"),
                    (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]}
            await send(message)
        await self.app(scope, receive, secured_send)


class ResultsRuntime:
    def __init__(self, settings, vnext, *, query=None, layout=None, resources=None, gateway=None, http=None):
        self.settings, self.vnext = settings, vnext
        self.store = ResultStore(settings.state_dir)
        self.io = SerializedPersistence("pank-results-storage")
        self.owner_task = None
        self.shutting_down = False
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(15, read=90), follow_redirects=False)
        self.query = query or QueryService(vnext)
        self.layout = layout or LayoutService(max_nodes=settings.display_nodes, timeout_seconds=settings.layout_timeout)
        self.coordinates = CoordinateLookup(settings.dbsnp_command)
        self.resources = resources or ResourceManager(settings.state_dir / "resources", self.coordinates,
            public_base=settings.public_path + "/api/resources", settings=settings)
        # Both services open the SAME ledger with SQLite atomic reservations.
        self.gateway = gateway or ClaudeGateway(vnext)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent)
        self.admission = AdmissionContinuations()
        self.tasks = {}
        self.plot_refresh_tasks = {}
        self.active = 0
        self.health = ResultsHealth(self)

    async def start(self):
        await self.io.call(self.store.acquire_owner)
        await self.io.call(self.store.interrupt, recovery=True)
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
            for task in [*self.tasks.values(), *self.plot_refresh_tasks.values()]:
                task.cancel()

    def check_owner(self):
        if self.shutting_down or (self.store.owner and self.store.owner.snapshot()["state"] != "active"):
            raise HTTPException(503, "The service no longer owns its execution lease.")

    async def load_run(self, run_id):
        response = await self.http.get(self.settings.agent_url + "/v2/runs/" + str(run_id))
        if response.status_code == 404:
            raise HTTPException(404, "Agent run not found.")
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise HTTPException(502, "Agent snapshot exceeds the transport limit.")
        return response.json()

    async def create(self, body):
        self.check_owner()
        if body.run_id:
            try:
                source = agent_snapshot(await self.load_run(body.run_id), body.phase, self.vnext.graph_version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        else:
            source = template_snapshot(body, self.vnext.graph_version)
        identity = {"source": source, "result_version": RESULT_VERSION, "layout_version": LAYOUT_VERSION,
            "registry_version": REGISTRY_VERSION, "display_nodes": self.settings.display_nodes, "answer_style": STYLE_VERSION, "functional_version": functional.VERSION}
        async def admitted():
            self.check_owner()
            result = await self.io.call(self.store.by_identity, identity)
            if result:
                self.health.count("result_cache_hits")
                return result
            if len(self.tasks) >= self.settings.max_queue + self.settings.max_concurrent:
                raise HTTPException(429, "Result queue is full.")
            result, created = await self.io.call(self.store.create, source, identity)
            if created:
                rid = result["result_id"]
                task = asyncio.create_task(self.execute(rid, source))
                self.tasks[rid] = task
                task.add_done_callback(lambda _: self.tasks.pop(rid, None))
            return result
        return await self.admission.run(admitted)

    async def update(self, rid, **changes):
        return await self.io.call(self.store.update, rid, **changes)

    async def refresh_saved_functional_plot(self, rid, value):
        """Migrate presentation assets only; never rerun retrieval or paid answers."""
        try:
            self.check_owner()
        except HTTPException:
            return value
        if value.get("status") != "ready":
            return value
        source = await self.io.call(self.store.source, rid)
        try:
            self.check_owner()
        except HTTPException:
            return value
        if not source or source.get("template_id") != "functional_traces":
            return value
        if value.get("functional_plot_version") == functional.VERSION:
            return value
        # Failed upstream requests retain the existing image and have a cooldown.
        task = self.plot_refresh_tasks.get(rid)
        if task is None and time.time() - value.get("functional_plot_attempt_at", 0) < 60:
            return value
        if task is None:
            async def refresh():
                self.check_owner()
                await self.update(rid, functional_plot_attempt_at=time.time())
                self.check_owner()
                await self.resolve_resources(rid, {"functional_filters": source["parameters"]})
            task = asyncio.create_task(refresh())
            self.plot_refresh_tasks[rid] = task
            task.add_done_callback(lambda _: self.plot_refresh_tasks.pop(rid, None))
        try:
            await asyncio.shield(task)
        except (HTTPException, OwnershipError):
            return value
        except asyncio.CancelledError:
            if self.shutting_down or (self.store.owner and self.store.owner.snapshot()["state"] != "active"):
                return value
            raise
        return await self.io.call(self.store.get, rid)

    async def resolve_resources(self, rid, evidence):
        self.check_owner()
        started = time.monotonic()
        try:
            if evidence.get("functional_filters"):
                raw, media = await functional.fetch(self.http, "api/charts/cohort-traces.png", {**evidence["functional_filters"], "result_page":"Yes"})
                self.check_owner()
                if not raw.startswith(b"\x89PNG\r\n\x1a\n"): raise ValueError("invalid_functional_plot")
                asset = await asyncio.to_thread(self.resources._save_asset, raw, kind="functional_trace", identity=json.dumps({**evidence["functional_filters"], "result_page":"Yes", "adapter_version":functional.VERSION},sort_keys=True), media_type="image/png", download_name="functional-cohort-traces.png", extra={"source":functional.BASE,"filters":evidence["functional_filters"],"adapter_version":functional.VERSION})
                resources = {"status":"available", "resources_tabs":{"empirical_evidence":{"title":"Selected cohort functional response", "description":"Recorded hormone-response measurements for the selected filters.","status":"available","image_url":asset["url"],"download_url":asset["url"],"link":asset["url"],"link_text":"Download plot","legend":"View"}}}
            else:
                resources = await asyncio.wait_for(self.resources.resolve(evidence), 30)
            await self.update(rid, resources_tabs=resources["resources_tabs"], resources=resources,
                component_status={"resources": resources["status"]})
            if evidence.get("functional_filters"):
                await self.update(rid, functional_plot_version=functional.VERSION, visual_material_kind="functional_traces")
            self.health.record("resources", "healthy" if resources["status"] == "available" else "unknown" if resources["status"] == "not_applicable" else "degraded" if resources["status"] == "partial" else "unavailable", time.monotonic() - started,
                details={"coverage": resources.get("coverage", {}), "status": resources["status"]})
            self.health.count("resources_completed")
        except Exception as exc:
            self.health.count("resources_errors")
            self.health.record("resources", "unavailable", time.monotonic() - started, safe_error(exc)["category"])
            await self.update(rid, resources={"status": "unavailable", "error": safe_error(exc)},
                component_status={"resources": "unavailable"})
        finally:
            self.health.duration("resources", time.monotonic() - started)

    async def answer(self, rid, source, evidence):
        self.check_owner()
        if source["kind"] == "agent":
            return await self.update(rid, component_status={"answer": "available" if source["answer"] else "not_requested"})
        status_message = outcome_message(evidence.get("steps") or [])
        if status_message:
            failed = any(s.get("status") == "failed" for s in evidence.get("steps", []))
            return await self.update(rid, answer=status_message, component_status={"answer": "unavailable" if failed else "empty"},
                answer_validation={"scope": "deterministic_retrieval_status", "inference_used": False})
        started = time.monotonic()
        text = ""
        citations = CitationFilter(len(evidence.get("steps", [])))
        audit_token = recorder.set(lambda kind, payload: self.io.record(self.store.audit_event, rid, kind, payload, on_drop=self.store.audit_drop))
        try:
            # Scope note is explicit in both the human question and step evidence.
            question = source["question"] + ("\nEvidence scope: " + evidence["scope_note"] if evidence.get("scope_note") else "")
            steps = {step.get("step_id", str(index)): step for index, step in enumerate(evidence.get("steps", []), 1)}
            async with asyncio.timeout(25):
                prepared = None
                if source.get("template_id") == "functional_traces":
                    prepared = self.gateway.prepare_answer(question, steps)
                    from dataclasses import replace
                    prepared = replace(prepared, body=functional.synthesis_body(question, steps), profile={**prepared.profile, "context_sampled":False, "functional_full_trace":True})
                    rules = self.gateway.answer_router.files["functional"]
                    prepared.system.append({"type":"text", "text":"Interpret only the supplied functional measurements. Do not infer individual donor variability from cohort means. Verified functional context: " + json.dumps([step.get("functional_metadata",{}) for step in steps.values()]) + ". Apply these measurement definitions: " + json.dumps({k:rules[k] for k in ("core_rules","global_terms")})})
                async for chunk in self.gateway.synthesize(question, steps, **({"prepared":prepared} if prepared else {})):
                    text += citations.feed(chunk)
                    await self.update(rid, answer=text)
            text += citations.feed("", final=True)
            if not text.strip():
                raise ValueError("empty_answer")
            fallback = not citations.seen and not citations.invalid
            if fallback:
                supplied = [f"[G{index}]" for index, step in enumerate(steps.values(), 1) if any(step.get(kind) for kind in ("nodes", "edges", "rows"))]
                if supplied:
                    text += "\n\nGraph evidence supplied: " + ", ".join(supplied) + "."
            await self.update(rid, answer=text, component_status={"answer": "partial" if citations.invalid else "available"},
                answer_validation={"valid": not citations.invalid, "scope": "reference_ids_only", "evidence_references": sorted(citations.seen), "invalid_references_removed": citations.invalid, "application_fallback": fallback})
            self.health.record("synthesis", "healthy", time.monotonic() - started)
            self.health.count("synthesis_completed")
        except Exception as exc:
            error = safe_error(exc)
            await self.update(rid, answer=text, answer_error=error, component_status={"answer": "partial" if text else "unavailable"})
            self.health.record("synthesis", "unavailable", time.monotonic() - started, error["category"])
            self.health.count("synthesis_errors")
        finally:
            recorder.reset(audit_token)
            self.health.duration("synthesis", time.monotonic() - started)

    async def execute(self, rid, source):
        started = time.monotonic()
        optional = []
        async with self.semaphore:
            self.active += 1
            try:
                self.check_owner()
                evidence = source["evidence"] if source["kind"] == "agent" else await functional.evidence(self.http, source["parameters"], source["question"], self.vnext.graph_version) if source.get("template_id") == "functional_traces" else await self.query.execute(source["template_id"], source["parameters"], source["question"])
                self.check_owner()
                if source["kind"] != "agent":
                    failed_steps = [step for step in evidence.get("steps", []) if step.get("status") == "failed"]
                    self.health.record("query_adapter", "degraded" if failed_steps else "healthy", time.monotonic() - started,
                        "query_step_failed" if failed_steps else None,
                        details={"graph_version": evidence.get("graph_version"), "failed_step_count": len(failed_steps)})
                    if failed_steps:
                        self.health.count("query_adapter_errors")
                    if not source.get("question_supplied"):
                        source = {**source, "question": template_question(source["template_id"], source["parameters"], evidence)}
                previous = await self.io.call(self.store.previous_presentation, source["run_id"], rid) if source.get("run_id") else None
                layout_started = time.monotonic()
                presentation = await self.layout.layout(evidence, source["focus_ids"], previous_layout=previous)
                self.check_owner()
                self.health.record("layout_worker", "degraded" if presentation["layout"]["status"] in {"fallback", "partial"} else "healthy", time.monotonic() - layout_started,
                    presentation["layout"].get("fallback_reason"))
                assembled = assemble(source, evidence, presentation)
                if evidence.get("functional_filters"):
                    assembled["visual_material_kind"] = "functional_traces"
                await self.update(rid, **assembled)
                self.health.count("results_completed")
                self.health.duration("graph_presentation", time.monotonic() - started)
                optional = [asyncio.create_task(self.resolve_resources(rid, evidence)), asyncio.create_task(self.answer(rid, source, evidence))]
                await asyncio.gather(*optional)
            except asyncio.CancelledError:
                for task in optional:
                    task.cancel()
                await asyncio.gather(*optional, return_exceptions=True)
                current = await self.io.call(self.store.get, rid)
                pending = {key: "cancelled" for key, status in current["component_status"].items() if status == "pending"}
                await self.update(rid, status="cancelled" if current["status"] == "preparing" else current["status"], component_status=pending)
                raise
            except Exception as exc:
                self.health.count("result_errors")
                await self.update(rid, status="failed", error=safe_error(exc),
                    component_status={"graph": "unavailable", "layout": "unavailable", "resources": "not_requested", "answer": "not_requested"})
            finally:
                self.active -= 1

    async def search(self, kind, term, template_id, params):
        result = await self.query.search(kind, term, template_id, **params)
        variant = term if kind == "variant" else params.get("variant_id")
        if variant:
            indexed = await self.resources.indexed_lookup(snp=variant, gene=params.get("gene_id"), limit=200)
            result["coverage"]["resource_index"] = indexed["coverage"]
            result["coverage"]["complete"] = False  # This index covers registered files, not the full corpus.
            if kind == "variant" and indexed["rows"] and not result["items"]:
                result["items"].append({"id": variant, "snp": variant, "name": variant, "value": variant, "label": variant, "source": "resource_index"})
            if kind == "credible_set":
                known = {(item.get("credible_set"), item.get("data_source")) for item in result["items"]}
                for row in indexed["rows"]:
                    identity = (row.get("credible_set"), row.get("data_source"))
                    if identity in known:
                        continue
                    # Resolve the KG lead from the credible-set identity; never
                    # replace the searched SNP with an invented graph node.
                    gene = row.get("gene")
                    if not gene or not template_id.startswith("qtl_"):
                        continue
                    matches = await self.query.search("credible_set", "", "qtl_by_gene", gene_id=gene,
                        credible_set_id=row["credible_set"], data_source=row["data_source"])
                    for item in matches["items"]:
                        result["items"].append({**item, "searched_snp": variant, "association": row})
                        known.add(identity)
        if kind == "credible_set" and variant:
            for item in result["items"]:
                original_snp, original_pip = item.get("snp"), item.get("pip")
                association = item.get("association") or next((row for row in indexed["rows"]
                    if row.get("credible_set") == item.get("credible_set")
                    and row.get("data_source") == item.get("data_source")), {})
                item["searched_snp"] = variant
                item["searched_pip"] = association.get("pip") if association else (original_pip if original_snp == variant else None)
                item["lead_snp"] = item.get("lead_snp") or original_snp
                if item.get("lead_pip") is None and original_snp == item["lead_snp"]:
                    item["lead_pip"] = original_pip
                item["association"] = association
        return result

    async def close(self):
        self.shutting_down = True
        await self.admission.close()
        await self.health.close()
        tasks = list(self.tasks.values()) + list(self.plot_refresh_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.store.owner and self.store.owner.snapshot()["state"] == "active":
            await self.io.call(self.store.interrupt)
        await asyncio.gather(self.query.close(), self.layout.close(), self.resources.close(), self.gateway.close(), self.http.aclose())
        if self.owner_task:
            self.owner_task.cancel()
            await asyncio.gather(self.owner_task, return_exceptions=True)
        if self.store.owner:
            await self.io.call(self.store.release_owner)
        await self.io.close()

    async def cancel_run_presentations(self, run_id):
        for rid, task in list(self.tasks.items()):
            source = await self.io.call(self.store.source, rid)
            if source.get("run_id") == run_id:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                current = await self.io.call(self.store.get, rid)
                await self.update(rid, status="cancelled" if current["status"] == "preparing" else current["status"],
                    component_status={key: "cancelled" for key, value in current["component_status"].items() if value == "pending"})


def create_app(settings=None, vnext_settings=None, **dependencies):
    settings, vnext = settings or ResultsSettings(), vnext_settings or Settings()
    runtime = ResultsRuntime(settings, vnext, **dependencies)

    @asynccontextmanager
    async def lifespan(app):
        try:
            await runtime.start()
        except BaseException:
            await asyncio.gather(runtime.query.close(), runtime.layout.close(), runtime.resources.close(), runtime.gateway.close(), runtime.http.aclose(), return_exceptions=True)
            await runtime.io.close()
            raise
        yield
        await runtime.close()

    app = FastAPI(title="PanKgraph results", version="1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.add_middleware(JSONResponseLimitMiddleware)
    app.add_middleware(DemoAuthentication, settings=settings)
    app.add_middleware(PrefixMiddleware, prefix=settings.public_path)

    @app.get("/access")
    async def access(request: Request, return_to: str = "/agent-vnext"):
        # A top-level navigation establishes the browser's existing Basic realm.
        # Keep the destination fixed and root-relative; no caller controls a host,
        # query or fragment. The dev frontend restores its own pending local route.
        if return_to != "/agent-vnext" or any(key != "return_to" for key in request.query_params) or len(request.query_params.getlist("return_to")) > 1:
            raise HTTPException(400, "Unsupported return destination.")
        return RedirectResponse(return_to, status_code=303, headers={"Cache-Control": "no-store"})

    @app.get("/api/access")
    async def access_status():
        # DemoAuthentication has already verified ordinary application access.
        # This route does not read saved data or submit any scientific work.
        return JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/results", status_code=202)
    async def create_result(body: ResultRequest):
        return await runtime.create(body)

    @app.post("/api/results/{result_id}/interactions")
    async def result_interaction(result_id: UUID, body: InteractionRequest):
        rid = str(result_id)
        if await runtime.io.call(runtime.store.get, rid) is None:
            raise HTTPException(404, "Result not found.")
        status = await runtime.io.call(runtime.store.audit_event, rid, body.kind,
            body.model_dump(mode="json", exclude={"event_id", "kind"}), str(body.event_id))
        runtime.health.count("audit_interactions_" + status)
        return JSONResponse({"version": 1, "status": status}, status_code=503 if status == "unavailable" else 200)

    @app.get("/api/results/{result_id}")
    async def result(result_id: UUID):
        value = await runtime.io.call(runtime.store.get, str(result_id))
        if value is None:
            raise HTTPException(404, "Result not found.")
        value = await runtime.refresh_saved_functional_plot(str(result_id), value)
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/api/functional/{path:path}")
    async def functional_proxy(path: str, request: Request):
        from starlette.responses import Response
        started = time.monotonic()
        try:
            content, media = await functional.fetch(runtime.http, path, dict(request.query_params))
            runtime.health.record("functional_api", "healthy", time.monotonic()-started, details={"source":functional.BASE,"adapter":functional.VERSION})
            return Response(content, media_type=media, headers={"Cache-Control":"private, max-age=30"})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            runtime.health.record("functional_api", "unavailable", time.monotonic()-started, "upstream_unavailable")
            raise HTTPException(502, "Functional data is temporarily unavailable.") from None

    @app.get("/api/search")
    async def search(request: Request, kind: str, term: str = "", template_id: str = ""):
        params = {k: v for k, v in request.query_params.items() if k not in {"kind", "term", "template_id"}}
        if len(params) > 7 or set(params) - {"gene_id", "variant_id", "disease_id", "credible_set_id", "data_source", "lead_variant_id", "cell_id"}:
            raise HTTPException(422, "Unknown search parameter.")
        try:
            async with asyncio.timeout(15):
                return await runtime.search(kind, term, template_id, params)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/resources/download")
    async def download(source: str, credible_set: str):
        runtime.check_owner()
        try:
            path, media, name = await runtime.resources.download(source, credible_set)
            return FileResponse(path, media_type=media, filename=name)
        except KeyError:
            raise HTTPException(404, "Source or exact object key is not registered.") from None
        except ResourceError:
            raise HTTPException(424, "The registered scientific file is unavailable.") from None

    @app.get("/api/resources/{asset_id}")
    async def resource(asset_id: str):
        try:
            path, media, name = await runtime.resources.asset(asset_id)
        except (KeyError, ResourceError):
            raise HTTPException(404, "Resource not available in the local cache.") from None
        return FileResponse(path, media_type=media, filename=name, content_disposition_type="inline" if media.startswith("image/") else "attachment",
            headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.api_route("/api/agent/{path:path}", methods=["GET", "POST"])
    async def agent_proxy(path: str, request: Request):
        allowed = (request.method == "POST" and path == "v2/plans") or re.fullmatch(
            r"v2/(?:plans/[0-9a-f-]{36}(?:/(?:confirm|revise))?|runs/[0-9a-f-]{36}(?:/(?:events|cancel|interactions))?)", path)
        post = path == "v2/plans" or path.endswith(("/confirm", "/revise", "/cancel", "/interactions"))
        if not allowed or (request.method == "POST") != post:
            raise HTTPException(404, "Unknown agent operation.")
        raw = await request.body()
        if len(raw) > 32000:
            raise HTTPException(413, "Request exceeds the input limit.")
        headers = {name: request.headers[name] for name in ("accept", "content-type", "last-event-id") if name in request.headers}
        query = request.url.query
        if query and not re.fullmatch(r"(?:after|after_sequence)=\d{1,10}", query):
            raise HTTPException(422, "Unsupported replay parameter.")
        upstream_request = runtime.http.build_request(request.method, settings.agent_url + "/" + path + ("?" + query if query else ""), headers=headers, content=raw)
        for sensitive in ("authorization", "cookie", "x-operator-token", "x-api-key"):
            upstream_request.headers.pop(sensitive, None)
        if path.endswith("/interactions") and runtime.vnext.operator_token:
            upstream_request.headers["authorization"] = "Bearer " + runtime.vnext.operator_token
        response = await runtime.http.send(upstream_request, stream=True)
        if path.endswith("/cancel") and response.status_code < 300:
            await runtime.cancel_run_presentations(path.split("/")[2])
        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(chunks(), status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def operator(request):
        local = request.client and request.client.host in {"127.0.0.1", "::1"} and "x-forwarded-for" not in request.headers
        authorized = settings.operator_token and hmac.compare_digest(request.headers.get("x-operator-token", ""), settings.operator_token)
        if not local and not authorized and not settings.testing:
            raise HTTPException(403, "Operator access required.")

    @app.get("/health/live")
    async def live():
        return {"status": "live", "version": 1, "service": "pankgraph-results"}

    @app.get("/health/ready")
    async def ready():
        data = runtime.health.snapshot()
        return JSONResponse({k: data[k] for k in ("ready", "state", "version", "service")}, status_code=200 if data["ready"] else 503)

    @app.get("/health/components")
    async def components(request: Request):
        operator(request)
        return runtime.health.snapshot()

    @app.get("/metrics")
    async def metrics(request: Request):
        operator(request)
        return PlainTextResponse(runtime.health.metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/pankgraph/health")
    @app.get("/pankgraph/health/{path:path}")
    @app.get("/health-dashboard")
    @app.get("/health-dashboard/{path:path}")
    async def health_dashboard(request: Request, path: str = ""):
        if request.url.path.endswith('/health') or request.url.path.endswith('/health-dashboard'):
            destination = settings.public_path + '/health-dashboard/' if request.url.path.endswith('/health-dashboard') else '/pankgraph/health/'
            return RedirectResponse(destination)
        # Fixed monitor origin/path, GET only. Authentication remains server-side
        # and the browser can never choose an upstream host or HTTP method.
        if path not in {"", "dashboard.js", "dashboard.css", "api/snapshot", "api/history", "api/incidents", "api/metrics"}:
            raise HTTPException(404, "Unknown dashboard route.")
        params = dict(request.query_params)
        if set(params) - {"hours", "limit"} or any(not re.fullmatch(r"[0-9]{1,4}", value) for value in params.values()):
            raise HTTPException(422, "Unknown dashboard query.")
        headers = {"authorization": request.headers["authorization"]} if "authorization" in request.headers else {}
        outgoing = runtime.http.build_request("GET", "http://127.0.0.1:8796/pankgraph/health/" + path, params=params, headers=headers, timeout=10)
        for name in ("cookie", "x-api-key", "x-operator-token"):
            outgoing.headers.pop(name, None)
        if "authorization" not in headers:
            outgoing.headers.pop("authorization", None)
        upstream = await runtime.http.send(outgoing)
        from starlette.responses import Response
        response_headers = {key: upstream.headers[key] for key in (
            "www-authenticate", "content-security-policy", "x-content-type-options", "referrer-policy") if key in upstream.headers}
        response_headers["Cache-Control"] = "no-store"
        return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type", "application/json"), headers=response_headers)

    @app.exception_handler(OwnershipError)
    @app.exception_handler(PersistenceBusy)
    async def persistence_unavailable(request, exc):
        return JSONResponse({"detail": "Service persistence is temporarily unavailable."}, status_code=503, headers={"Retry-After": "2"})

    @app.exception_handler(Exception)
    async def failed(request, exc):
        runtime.health.count("http_errors")
        return JSONResponse({"error": safe_error(exc)}, status_code=503)

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith(("api/", "health/", "health-dashboard", "pankgraph/health")) or path == "metrics":
            raise HTTPException(404)
        root = settings.frontend_dir.resolve()
        target = (root / (path or "index.html")).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if not target.is_file():
            if path.startswith("static/") or "." in path.rsplit("/", 1)[-1]:
                raise HTTPException(404)
            target = root / "index.html"
        if not target.is_file():
            raise HTTPException(503, "Frontend build is not staged.")
        return FileResponse(target, headers={"Cache-Control": "no-store" if target.name == "index.html" else "private, max-age=86400"})

    return app
