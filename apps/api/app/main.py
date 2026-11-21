"""api: stateless HTTP service. Run: uvicorn app.main:app"""

import json
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from psycopg_pool import AsyncConnectionPool

REPLICA = socket.gethostname()
DATABASE_URL = os.environ.get("DATABASE_URL") or sys.exit("missing env var DATABASE_URL")

REQUESTS = Counter("http_requests_total", "HTTP requests.", ["route", "status"])
DURATIONS = Histogram("http_request_duration_seconds", "HTTP request latency.", ["route"])

pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "10")))


def log(level: str, msg: str, **extra) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"ts": ts, "level": level, "msg": msg, "replica": REPLICA, **extra}), flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await pool.open()
    log("info", "listening on :8080")
    yield
    log("info", "SIGTERM received, draining")  # uvicorn finishes in-flight requests first
    await pool.close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        response = await call_next(request)
    except Exception as exc:  # anything unhandled becomes a logged 500
        log("error", str(exc), requestId=request_id, path=request.url.path)
        response = JSONResponse({"error": "internal error", "requestId": request_id}, status_code=500)

    route = request.scope.get("route")
    label = route.path if route else "unmatched"  # fixed label set, no cardinality blow-up
    elapsed = time.perf_counter() - start
    REQUESTS.labels(label, str(response.status_code)).inc()
    DURATIONS.labels(label).observe(elapsed)
    response.headers["x-replica"] = REPLICA
    response.headers["x-request-id"] = request_id
    if not request.url.path.endswith("z"):
        log("info", "request", requestId=request_id, method=request.method, path=request.url.path,
            status=response.status_code, ms=round(elapsed * 1000))
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    try:
        async with pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        return JSONResponse({"ok": False, "reason": "db unreachable"}, status_code=503)
    return {"ok": True}


@app.get("/api/hello")
async def hello():
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT message FROM greetings ORDER BY id LIMIT 1")).fetchone()
    return {"message": row[0] if row else None, "replica": REPLICA}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
