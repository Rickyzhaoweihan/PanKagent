#!/usr/bin/env python3
"""Weighted-least-connections load balancer for the cypher-writer vLLM fleet.

Listens on :8010 (configurable) and proxies OpenAI-compatible requests across
all 5 backends (4 tunnelled cluster GPUs on :7000-7003 + the local L40s on
:8002). The PanKgraph app points at this via VLLM_PORT=8010.

Balancing: among *healthy* backends, route to the one minimizing
``inflight / weight`` (H100 ports carry a larger weight). A background task
health-checks every backend and removes/restores them from rotation. Streaming
(SSE) responses are passed through; non-stream calls retry once on a different
backend if the first errors.

Run via run_router.sh or directly:
    uvicorn load_balancer:app --host 127.0.0.1 --port 8010
    # or:  python3 load_balancer.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import gpu_config as cfg

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log = logging.getLogger("router")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(cfg.LOG_DIR / "router.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]


BACKENDS: list[cfg.Backend] = cfg.parse_backends()

# Headers we must not forward verbatim (httpx recomputes hop-by-hop / length).
_DROP_REQ_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
_DROP_RESP_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #
def _pick_backend(exclude: Optional[set[str]] = None) -> Optional[cfg.Backend]:
    exclude = exclude or set()
    candidates = [b for b in BACKENDS if b.healthy and b.key not in exclude]
    if not candidates:
        # Fall back to any not-excluded backend (better to try than 503 blindly).
        candidates = [b for b in BACKENDS if b.key not in exclude]
    if not candidates:
        return None
    # Weighted least-connections: minimize inflight / weight.
    return min(candidates, key=lambda b: (b.inflight / b.weight, b.inflight))


def _is_stream_request(body: bytes) -> bool:
    if not body:
        return False
    try:
        return bool(json.loads(body).get("stream"))
    except (ValueError, TypeError):
        return False


def _clean_req_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items()
            if k.lower() not in _DROP_REQ_HEADERS}


def _clean_resp_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items()
            if k.lower() not in _DROP_RESP_HEADERS}


# --------------------------------------------------------------------------- #
# Health checking
# --------------------------------------------------------------------------- #
async def _health_sweep(client: httpx.AsyncClient) -> None:
    while True:
        for b in BACKENDS:
            try:
                r = await client.get(cfg.health_url(b.host, b.port),
                                     timeout=cfg.HEALTH_TIMEOUT)
                ok = r.status_code == 200
            except Exception:
                ok = False
            if ok:
                b.consecutive_ok += 1
                b.consecutive_fails = 0
                if not b.healthy and b.consecutive_ok >= cfg.HEALTH_OK_THRESHOLD:
                    b.healthy = True
                    log.info("backend %s (w=%d) -> HEALTHY", b.key, b.weight)
            else:
                b.consecutive_fails += 1
                b.consecutive_ok = 0
                if b.healthy and b.consecutive_fails >= cfg.HEALTH_FAIL_THRESHOLD:
                    b.healthy = False
                    log.warning("backend %s -> UNHEALTHY (dropped from rotation)", b.key)
        await asyncio.sleep(cfg.HEALTH_INTERVAL)


# --------------------------------------------------------------------------- #
# App + lifespan
# --------------------------------------------------------------------------- #
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    timeout = httpx.Timeout(
        connect=cfg.PROXY_CONNECT_TIMEOUT,
        read=cfg.PROXY_READ_TIMEOUT,
        write=cfg.PROXY_READ_TIMEOUT,
        pool=cfg.PROXY_CONNECT_TIMEOUT,
    )
    limits = httpx.Limits(max_connections=256, max_keepalive_connections=64)
    app.state.client = httpx.AsyncClient(timeout=timeout, limits=limits)
    app.state.health_task = asyncio.create_task(_health_sweep(app.state.client))
    log.info("router up on %s:%d with %d backends: %s",
             cfg.ROUTER_HOST, cfg.ROUTER_PORT, len(BACKENDS),
             ", ".join(f"{b.key}(w{b.weight})" for b in BACKENDS))
    try:
        yield
    finally:
        app.state.health_task.cancel()
        with contextlib.suppress(Exception):
            await app.state.health_task
        await app.state.client.aclose()


app = FastAPI(title="cypher-writer GPU router", lifespan=lifespan)


@app.get("/health")
async def health():
    healthy = [b for b in BACKENDS if b.healthy]
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "no_healthy_backends",
            "healthy_backends": len(healthy),
            "total_backends": len(BACKENDS),
            "backends": [
                {"backend": b.key, "weight": b.weight,
                 "healthy": b.healthy, "inflight": b.inflight}
                for b in BACKENDS
            ],
        },
    )


async def _proxy(request: Request, path: str) -> Response:
    client: httpx.AsyncClient = request.app.state.client
    body = await request.body()
    headers = _clean_req_headers(request)
    stream = _is_stream_request(body)
    tried: set[str] = set()

    last_error = "no backends available"
    for attempt in range(cfg.PROXY_MAX_RETRIES + 1):
        backend = _pick_backend(exclude=tried)
        if backend is None:
            break
        tried.add(backend.key)
        target = f"{backend.url}/{path}"

        if stream:
            # Increment now; decrement inside the streaming generator's finally.
            backend.inflight += 1
            log.info("%s %s -> %s [stream] (inflight=%d)",
                     request.method, path, backend.key, backend.inflight)
            try:
                req = client.build_request(request.method, target,
                                           headers=headers, content=body)
                resp = await client.send(req, stream=True)
            except Exception as exc:  # connect failed -> retry on another backend
                backend.inflight -= 1
                last_error = f"{backend.key}: {exc!r}"
                log.warning("stream connect failed on %s: %r (retrying)", backend.key, exc)
                continue

            async def _gen(b=backend, r=resp):
                try:
                    async for piece in r.aiter_raw():
                        yield piece
                finally:
                    with contextlib.suppress(Exception):
                        await r.aclose()
                    b.inflight -= 1

            return StreamingResponse(
                _gen(),
                status_code=resp.status_code,
                headers=_clean_resp_headers(resp.headers),
                media_type=resp.headers.get("content-type"),
            )

        # Non-streaming
        backend.inflight += 1
        log.info("%s %s -> %s (inflight=%d)",
                 request.method, path, backend.key, backend.inflight)
        try:
            resp = await client.request(request.method, target,
                                        headers=headers, content=body)
        except Exception as exc:
            last_error = f"{backend.key}: {exc!r}"
            log.warning("request failed on %s: %r (retrying)", backend.key, exc)
            continue
        finally:
            backend.inflight -= 1

        # Retry once on a 5xx if we have budget; otherwise return as-is.
        if resp.status_code >= 500 and attempt < cfg.PROXY_MAX_RETRIES:
            last_error = f"{backend.key}: HTTP {resp.status_code}"
            log.warning("backend %s returned %d (retrying)", backend.key, resp.status_code)
            continue
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=_clean_resp_headers(resp.headers),
                        media_type=resp.headers.get("content-type"))

    log.error("all backends failed for %s %s: %s", request.method, path, last_error)
    return JSONResponse(status_code=503,
                        content={"error": "no_healthy_backend", "detail": last_error})


# Catch-all for OpenAI-compatible paths (/v1/..., /v1/models, etc.).
@app.api_route("/v1/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v1(request: Request, path: str):
    return await _proxy(request, f"v1/{path}")


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("ERROR: uvicorn not installed. pip install 'uvicorn[standard]'\n")
        sys.exit(2)
    uvicorn.run(app, host=cfg.ROUTER_HOST, port=cfg.ROUTER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
