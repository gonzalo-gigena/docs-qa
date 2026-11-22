"""embedder: text in, 384-dim normalized vectors out. Stateless and CPU-bound, so scale it on its own."""

import json
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastembed import TextEmbedding
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

REPLICA = socket.gethostname()
THREADS = int(os.environ.get("EMBED_THREADS", "4"))
TEXTS = Counter("embedder_texts_total", "Texts embedded.")
DURATIONS = Histogram("embedder_batch_seconds", "Time to embed one request.")

model = TextEmbedding("BAAI/bge-small-en-v1.5", specific_model_path="/models/bge-small", threads=THREADS)
# ponytail: one inference at a time per replica. It already uses every thread, and parallel
# batches multiply peak memory. More throughput: --scale embedder=N.
inference = threading.Lock()


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=128)


def log(level: str, msg: str, **extra) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"ts": ts, "level": level, "msg": msg, "replica": REPLICA, **extra}), flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log("info", "listening on :8080", threads=THREADS)
    yield
    log("info", "SIGTERM received, draining")


app = FastAPI(lifespan=lifespan)


@app.post("/embed")
def embed(body: EmbedRequest, request: Request):
    with inference:
        start = time.perf_counter()
        # Small ONNX batches: memory grows with batch size, and speed on CPU doesn't.
        vectors = [vector.tolist() for vector in model.passage_embed(body.texts, batch_size=16)]
    elapsed = time.perf_counter() - start
    TEXTS.inc(len(vectors))
    DURATIONS.observe(elapsed)
    log("info", "embedded", requestId=request.headers.get("x-request-id"), texts=len(vectors), ms=round(elapsed * 1000))
    return {"vectors": vectors}


@app.get("/healthz")
async def healthz():
    return {"ok": True}  # the model loads at import, so answering means ready


@app.get("/readyz")
async def readyz():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
