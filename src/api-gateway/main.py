"""
API Gateway — FastAPI application.

Responsibilities (phases.md §API Gateway):
- Authentication forwarding
- Request routing to downstream microservices
- Rate limiting (simple in-memory counter for MVP)
- Health aggregation

All public API traffic enters through this gateway.
"""
import logging
import os
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES: Dict[str, str] = {
    "auth":       os.getenv("AUTH_SERVICE_URL",       "http://localhost:8001"),
    "repository": os.getenv("REPOSITORY_SERVICE_URL", "http://localhost:8002"),
    "document":   os.getenv("DOCUMENT_SERVICE_URL",   "http://localhost:8003"),
    "embedding":  os.getenv("EMBEDDING_SERVICE_URL",  "http://localhost:8004"),
    "graph":      os.getenv("GRAPH_SERVICE_URL",      "http://localhost:8005"),
    "search":     os.getenv("SEARCH_SERVICE_URL",     "http://localhost:8006"),
    "event":      os.getenv("EVENT_SERVICE_URL",      "http://localhost:8007"),
}

# ---------------------------------------------------------------------------
# Rate limiter (simple sliding window — MVP)
# ---------------------------------------------------------------------------

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
_request_times: Dict[str, list] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - 60
    times = [t for t in _request_times[client_ip] if t > window_start]
    _request_times[client_ip] = times
    if len(times) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    _request_times[client_ip].append(now)


# ---------------------------------------------------------------------------
# Proxy helper
# ---------------------------------------------------------------------------

async def _proxy(request: Request, target_url: str) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    body = await request.body()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail=f"Upstream service unavailable: {target_url}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Upstream service timed out.")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="API Gateway",
    description="Single entry point for all Engineering Intelligence Platform APIs.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Operations"])
def health_check():
    return {"status": "ok", "service": "api-gateway", "upstream": list(SERVICES.keys())}


@app.get("/health/all", tags=["Operations"])
async def health_all():
    """Aggregate health check across all downstream services."""
    statuses = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, base_url in SERVICES.items():
            try:
                r = await client.get(f"{base_url}/health")
                statuses[name] = "ok" if r.status_code == 200 else "degraded"
            except Exception:
                statuses[name] = "unreachable"
    overall = "ok" if all(v == "ok" for v in statuses.values()) else "degraded"
    return {"status": overall, "services": statuses}


# ---------------------------------------------------------------------------
# Routing — /api/v1/{service}/{path}
# ---------------------------------------------------------------------------

@app.api_route(
    "/api/v1/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    tags=["Routing"],
)
async def route(request: Request, service: str, path: str):
    """
    Dynamic reverse proxy.
    URL format: /api/v1/{service-name}/{downstream-path}
    Example:    /api/v1/repository/repositories → http://repository-service/repositories
    """
    _check_rate_limit(request.client.host if request.client else "unknown")

    base_url = SERVICES.get(service)
    if base_url is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown service '{service}'. Available: {list(SERVICES.keys())}",
        )

    target = f"{base_url}/{path}"
    logger.info("Routing %s /api/v1/%s/%s → %s", request.method, service, path, target)
    return await _proxy(request, target)
