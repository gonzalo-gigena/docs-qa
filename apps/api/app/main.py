"""api: stateless HTTP service. Run: uvicorn app.main:app"""

import hashlib
import os
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.concurrency import run_in_threadpool

from app.core import DATABASE_URL, REPLICA, S3_BUCKET, WORKSPACE_ID, enqueue, ensure_bucket, log, object_key, queue, s3
from app.extract import KINDS

MAX_UPLOAD = 25 * 1024 * 1024  # keep in sync with request_body in infra/caddy/Caddyfile

REQUESTS = Counter("http_requests_total", "HTTP requests.", ["route", "status"])
DURATIONS = Histogram("http_request_duration_seconds", "HTTP request latency.", ["route"])
QUEUE_DEPTH = Gauge("ingest_queue_depth", "Ingestion jobs waiting in Redis.")

pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "10")))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_in_threadpool(ensure_bucket)
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
    request.state.request_id = request_id
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


DOC_COLUMNS = """id, name, kind, version, attempt, content_hash, status, error, pages,
                 chunks_done, chunks_total, created_at, finished_at"""


async def find_same_content(conn, name: str, digest: str) -> dict | None:
    """The version this upload repeats, if any: the live one, or the latest failed one (to retry)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"SELECT {DOC_COLUMNS} FROM documents WHERE workspace_id = %s AND name = %s ORDER BY version DESC",
        (WORKSPACE_ID, name),
    )
    versions = await cur.fetchall()
    live = next((d for d in versions if d["status"] != "failed"), None)
    if live and live["content_hash"] == digest:
        return live
    if versions and versions[0]["status"] == "failed" and versions[0]["content_hash"] == digest:
        return versions[0]
    return None


@app.post("/api/documents", status_code=202)
async def upload(file: UploadFile, request: Request, response: Response):
    # A document is identified by its file name within the workspace.
    name = unicodedata.normalize("NFC", (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    kind = KINDS.get(os.path.splitext(name)[1].lower())
    if not kind:
        return JSONResponse({"error": "upload a PDF, HTML or Markdown file"}, status_code=415)
    if len(name) > 255 or "\x00" in name:
        return JSONResponse({"error": "invalid file name"}, status_code=400)
    if not file.size:
        return JSONResponse({"error": "the file is empty"}, status_code=400)
    if file.size > MAX_UPLOAD:
        return JSONResponse({"error": "the file is over 25 MB"}, status_code=413)

    digest = (await run_in_threadpool(hashlib.file_digest, file.file, "sha256")).hexdigest()
    async with pool.connection() as conn:
        found = await find_same_content(conn, name, digest)

    doc_id, stored = uuid.uuid4(), False
    if not found:
        # Store before inserting, and outside any transaction: a row always has its object.
        await file.seek(0)
        await run_in_threadpool(
            lambda: s3().put_object(Bucket=S3_BUCKET, Key=object_key(WORKSPACE_ID, doc_id), Body=file.file)
        )
        stored = True

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{WORKSPACE_ID}/{name}",))
        doc = await find_same_content(conn, name, digest)  # again, now that nobody else can change it
        if not doc and not stored:
            return JSONResponse({"error": "the document changed during the upload, try again"}, status_code=409)
        created, unchanged = not doc, bool(doc) and doc["status"] != "failed"
        cur = conn.cursor(row_factory=dict_row)
        if created:
            await cur.execute(
                f"""INSERT INTO documents (id, workspace_id, name, kind, content_hash, version)
                    SELECT %(id)s, %(workspace)s, %(name)s, %(kind)s, %(hash)s, coalesce(max(version), 0) + 1
                    FROM documents WHERE workspace_id = %(workspace)s AND name = %(name)s
                    RETURNING {DOC_COLUMNS}""",
                {"id": doc_id, "workspace": WORKSPACE_ID, "name": name, "kind": kind, "hash": digest},
            )
            doc = await cur.fetchone()
        elif not unchanged:
            await cur.execute(
                f"""UPDATE documents
                    SET status = 'queued', attempt = attempt + 1, error = NULL, finished_at = NULL,
                        chunks_done = 0, chunks_total = NULL
                    WHERE workspace_id = %s AND id = %s RETURNING {DOC_COLUMNS}""",
                (WORKSPACE_ID, doc["id"]),
            )
            doc = await cur.fetchone()

    request_id = request.state.request_id
    if stored and not created:  # lost a race with an identical upload
        try:
            await run_in_threadpool(lambda: s3().delete_object(Bucket=S3_BUCKET, Key=object_key(WORKSPACE_ID, doc_id)))
        except Exception as exc:
            log("warn", "could not delete duplicate object", requestId=request_id, error=repr(exc))

    if doc["status"] != "ready":
        # Also when unchanged: that restores a job Redis lost, and does nothing if the job is there.
        try:
            await run_in_threadpool(enqueue, str(doc["id"]), doc["attempt"], request_id)
        except Exception as exc:
            log("error", "enqueue failed", requestId=request_id, error=repr(exc))
            return JSONResponse(
                {"error": "the file is saved but the job queue is unavailable, upload it again to retry"},
                status_code=503,
            )
    if unchanged:
        response.status_code = 200
    return {**doc, "unchanged": unchanged}


@app.get("/api/documents")
async def list_documents():
    # ponytail: newest 200 and no paging. Add a cursor when a workspace outgrows it.
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT {DOC_COLUMNS} FROM documents WHERE workspace_id = %s ORDER BY created_at DESC LIMIT 200",
            (WORKSPACE_ID,),
        )
        return {"documents": await cur.fetchall()}


@app.get("/metrics")
async def metrics():
    try:
        QUEUE_DEPTH.set(await run_in_threadpool(lambda: len(queue)))
    except Exception as exc:
        QUEUE_DEPTH.set(float("nan"))
        log("warn", "queue depth unavailable", error=repr(exc))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
