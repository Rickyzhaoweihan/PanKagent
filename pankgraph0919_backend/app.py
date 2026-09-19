"""Standalone FastAPI entry point; never imports or starts legacy applications."""
from contextlib import asynccontextmanager
import hmac
import json
import logging
import threading
import time
from typing import Annotated

from cakg.identity import ID_RE
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .graph import GraphStore, safe, validate_cypher
from .models import Filters, GeneSearch, QueryRequest, RecordSearch
from .postgres import PostgresStore

LOGGER = logging.getLogger("pankgraph0919")


class BoundaryMiddleware:
    """Authentication and bounded request buffering before JSON parsing."""
    def __init__(self, app, token):
        self.app = app
        self.token = "Bearer " + token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        health = scope.get("path") == "/health" and scope.get("method") == "GET"
        authorized = hmac.compare_digest(headers.get(b"authorization", b""), self.token.encode())
        if not health and not authorized:
            return await JSONResponse({"detail": "Authentication required"}, 401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        scope["pank0919_authorized"] = authorized
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 64_000:
                return await JSONResponse({"detail": "Request body exceeds the size limit"}, 413)(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class Runtime:
    def __init__(self, settings, graph=None, postgres=None):
        self.settings = settings
        self.graph = graph
        self.postgres = postgres
        self.ready = False
        self.pg_ready = False
        self.graph_ready = False
        self.accepted_counts = None
        self.checked_at = 0.0
        self.check_lock = threading.Lock()
        self.components = {}

    def verify(self):
        with self.check_lock:
            components = {}
            try:
                pg = self.postgres.probe()
                if pg.get("snapshot_id") != self.settings.snapshot_id or pg.get("status") != "accepted":
                    raise HTTPException(409, "Snapshot acceptance mismatch")
                self.accepted_counts = pg["object_counts"]
                components["postgres"] = pg
                self.pg_ready = True
            except Exception as exc:
                self.pg_ready = False
                # Never log a driver message, parameters, DSN or secret.
                LOGGER.warning("Postgres snapshot gate unavailable: %s", type(exc).__name__)
            try:
                if self.accepted_counts is None:
                    raise HTTPException(409, "Snapshot acceptance has never been verified")
                graph = self.graph.probe(self.accepted_counts)
                if graph.get("snapshot_id") != self.settings.snapshot_id or graph.get("object_counts") != self.accepted_counts:
                    raise HTTPException(409, "Graph snapshot mismatch")
                components["neo4j"] = graph
                self.graph_ready = True
            except Exception as exc:
                self.graph_ready = False
                LOGGER.warning("Graph snapshot gate unavailable: %s", type(exc).__name__)
            self.components = components
            self.ready = self.pg_ready and self.graph_ready
            self.checked_at = time.monotonic()
        return self.ready

    def require_snapshot(self, requested=None, graph_required=True):
        if requested is not None and requested != self.settings.snapshot_id:
            raise HTTPException(409, "Requested snapshot differs from the configured release")
        if not (self.graph_ready if graph_required else self.pg_ready):
            raise HTTPException(503, "Accepted snapshot is unavailable")


def create_app(settings=None, graph=None, postgres=None):
    settings = settings or Settings.from_env()
    runtime = Runtime(settings, graph, postgres)

    @asynccontextmanager
    async def lifespan(app):
        runtime.graph = runtime.graph or GraphStore(settings)
        runtime.postgres = runtime.postgres or PostgresStore(settings)
        # Start a sanitized health endpoint even on a failed gate. No query
        # route is available until a successful authenticated revalidation.
        runtime.verify()
        yield
        runtime.graph.close()
        runtime.postgres.close()

    app = FastAPI(title="PanKgraph 0919 isolated query API", version="0.1.0",
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(BoundaryMiddleware, token=settings.api_token)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                                        for e in exc.errors()]}, 422)

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        LOGGER.warning("Request failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "Query service unavailable or query could not be executed"}, 503)

    def respond(payload):
        result = safe({"snapshot_id": settings.snapshot_id, **payload})
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > settings.max_bytes:
            raise HTTPException(413, "Response exceeds the byte limit; narrow the request")
        return JSONResponse(result)

    @app.get("/health")
    def health(request: Request, detail: bool = False):
        if detail:
            if not request.scope.get("pank0919_authorized"):
                raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
            runtime.verify()
            return JSONResponse({"status": "ready" if runtime.ready else "unavailable",
                                 "snapshot_id": settings.snapshot_id, "components": runtime.components},
                                200 if runtime.ready else 503)
        if time.monotonic() - runtime.checked_at >= 15:
            runtime.verify()
        return JSONResponse({"status": "ready" if runtime.ready else "unavailable"},
                            200 if runtime.ready else 503)

    @app.post("/query")
    def query(body: QueryRequest):
        runtime.require_snapshot(body.snapshot_id, graph_required=body.cypher is not None)
        if body.cypher is not None:
            validate_cypher(body.cypher, body.parameters)
            result = runtime.graph.query(body.cypher, body.parameters, body.limit)
            if result.get("graph_snapshot_ids") and result["graph_snapshot_ids"] != [settings.snapshot_id]:
                raise HTTPException(409, "Graph snapshot mismatch")
            if body.mode == "detail":
                ids = result["object_ids"]
                edges = [identifier for identifier in ids if identifier.startswith("cakg:e:")]
                selected = edges or ids
                result["expansion_policy"] = "returned_edges" if edges else "returned_nodes" if ids else "no_graph_objects"
                result["evidence"] = runtime.postgres.records(selected, body.filters, body.limit, body.offset) if selected else {
                    "items": [], "total": 0, "object_ids": [], "match_status": "no_graph_objects"}
            return respond({"mode": body.mode, "query_source": "neo4j", **result})
        genes = runtime.postgres.search_genes(body.postgres_search)
        result = {"mode": body.mode, "query_source": "postgres_search", "genes": genes}
        if body.mode == "detail":
            ids = [item["object_id"] for item in genes["items"]]
            result["evidence"] = runtime.postgres.records(ids, body.filters, body.limit, body.offset) if ids else {
                "items": [], "total": 0, "object_ids": [], "match_status": "no_matching_genes"}
        return respond(result)

    @app.post("/genes/search")
    def genes_search(body: GeneSearch):
        runtime.require_snapshot(graph_required=False)
        return respond(runtime.postgres.search_genes(body))

    @app.post("/records/search")
    def records_search(body: RecordSearch):
        runtime.require_snapshot(graph_required=False)
        selected = Filters(**body.model_dump(exclude={"limit", "offset"}))
        return respond(runtime.postgres.records(None, selected, body.limit, body.offset))

    def filters(collection_id, context_id, context_kind, condition, cell_type_id, record_status):
        return Filters(collection_id=collection_id, context_id=context_id, context_kind=context_kind,
                       condition=condition, cell_type_id=cell_type_id, record_status=record_status)

    @app.get("/contexts")
    def contexts(
            collection_id: Annotated[str | None, Query(max_length=200)] = None,
            context_id: Annotated[str | None, Query(max_length=200)] = None,
            context_kind: Annotated[str | None, Query(max_length=200)] = None,
            condition: Annotated[str | None, Query(max_length=500)] = None,
            cell_type_id: Annotated[str | None, Query(max_length=200)] = None,
            record_status: Annotated[str | None, Query(pattern="^(source_reported|validated|quarantined)$")] = None,
            limit: Annotated[int, Query(ge=1, le=200)] = 50,
            offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0):
        runtime.require_snapshot(graph_required=False)
        return respond(runtime.postgres.contexts(filters(collection_id, context_id, context_kind, condition,
                                                         cell_type_id, record_status), limit, offset))

    @app.get("/objects/{object_id}/records")
    def object_records(
            object_id: str,
            collection_id: Annotated[str | None, Query(max_length=200)] = None,
            context_id: Annotated[str | None, Query(max_length=200)] = None,
            context_kind: Annotated[str | None, Query(max_length=200)] = None,
            condition: Annotated[str | None, Query(max_length=500)] = None,
            cell_type_id: Annotated[str | None, Query(max_length=200)] = None,
            record_status: Annotated[str | None, Query(pattern="^(source_reported|validated|quarantined)$")] = None,
            limit: Annotated[int, Query(ge=1, le=200)] = 50,
            offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0):
        runtime.require_snapshot(graph_required=False)
        if not ID_RE.fullmatch(object_id) or not object_id.startswith(("cakg:n:", "cakg:e:")):
            raise HTTPException(422, "Expected a canonical node or relationship ID")
        return respond(runtime.postgres.records([object_id], filters(collection_id, context_id, context_kind,
                                                                    condition, cell_type_id, record_status), limit, offset))

    @app.get("/sources")
    def sources(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0):
        runtime.require_snapshot(graph_required=False)
        return respond(runtime.postgres.sources(limit, offset))

    return app
