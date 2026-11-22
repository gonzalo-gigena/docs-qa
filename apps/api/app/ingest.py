"""The ingestion job: parse -> chunk -> embed -> swap. Safe to run any number of times."""

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from itertools import batched

import psycopg
from psycopg.rows import dict_row

from app.chunking import chunk
from app.core import DATABASE_URL, S3_BUCKET, log, object_key, s3
from app.extract import Unreadable, extract

BATCH = 64
EMBEDDER_URL = os.environ.get("EMBEDDER_URL", "http://embedder:8080")


class EmbedderUnavailable(Exception):
    pass


def embed(texts: list[str], request_id: str) -> list[list[float]]:
    request = urllib.request.Request(
        f"{EMBEDDER_URL}/embed",
        json.dumps({"texts": texts}).encode(),
        {"content-type": "application/json", "x-request-id": request_id},
    )
    for attempt in range(10):  # waits about a minute in total, enough for an embedder restart
        try:
            # A new connection per batch, so Docker DNS spreads batches across embedder replicas.
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)["vectors"]
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise
        except (OSError, http.client.HTTPException):
            pass
        if attempt < 9:
            time.sleep(min(2**attempt, 10))
    raise EmbedderUnavailable


def connect() -> psycopg.Connection:
    # autocommit: nothing holds a transaction open while we parse or wait on the embedder.
    return psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=5, row_factory=dict_row)


def set_failed(conn: psycopg.Connection, doc_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE documents SET status = 'failed', error = %s, finished_at = now() WHERE id = %s AND status <> 'ready'",
        (reason, doc_id),
    )


def ingest(doc_id: str, request_id: str = "") -> None:
    with connect() as conn:
        # One run per document version, so a duplicate job can't interleave with this one.
        # Session lock: released when the connection closes, also if the process is killed.
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (doc_id,))
        doc = conn.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
        if not doc or doc["status"] in ("ready", "failed"):
            return  # already done, or replaced by a newer version
        try:
            run(conn, doc, request_id)
        except Unreadable as exc:
            set_failed(conn, doc_id, str(exc))
            log("warn", "document unreadable", docId=doc_id, requestId=request_id, reason=str(exc))
        except psycopg.errors.ForeignKeyViolation:
            # A newer version finished and deleted this one while we were embedding.
            log("info", "superseded", docId=doc_id, requestId=request_id)
        except EmbedderUnavailable:
            set_failed(conn, doc_id, "the embedder is unavailable, upload the file again to retry")
            log("error", "embedder unavailable", docId=doc_id, requestId=request_id)
        except Exception as exc:
            set_failed(conn, doc_id, f"ingestion failed ({type(exc).__name__}), upload the file again to retry")
            log("error", "ingestion failed", docId=doc_id, requestId=request_id, error=repr(exc))


def run(conn: psycopg.Connection, doc: dict, request_id: str) -> None:
    doc_id, workspace_id, name = doc["id"], doc["workspace_id"], doc["name"]
    began = time.perf_counter()
    conn.execute(
        """UPDATE documents SET status = 'parsing', error = NULL, chunks_done = 0, chunks_total = NULL,
                  started_at = now() WHERE id = %s""",
        (doc_id,),
    )
    data = s3().get_object(Bucket=S3_BUCKET, Key=object_key(workspace_id, doc_id))["Body"].read()
    parsed = extract(doc["kind"], data)
    del data
    chunks = chunk(parsed)
    if not chunks:
        raise Unreadable("no text found")
    parse_seconds = time.perf_counter() - began
    conn.execute(
        "UPDATE documents SET status = 'embedding', pages = %s, chunks_total = %s WHERE id = %s",
        (len(parsed.page_starts) or None, len(chunks), doc_id),
    )

    # Chunks are written as they're embedded. Nobody reads them until status = 'ready', which is the swap.
    conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
    done = 0
    for batch in batched(chunks, BATCH):
        vectors = embed([c.text for c in batch], request_id)
        rows = [
            (workspace_id, doc_id, c.ordinal, c.text, c.heading_path, c.page, c.char_start, c.char_end, str(v))
            for c, v in zip(batch, vectors, strict=True)
        ]
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks
                     (workspace_id, doc_id, ordinal, text, heading_path, page, char_start, char_end, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)""",
                rows,
            )
            done += len(batch)
            cur.execute("UPDATE documents SET chunks_done = %s WHERE id = %s", (done, doc_id))

    stale = swap(conn, doc)
    for old_id in stale:
        try:
            s3().delete_object(Bucket=S3_BUCKET, Key=object_key(workspace_id, old_id))
        except Exception as exc:  # an orphan object is harmless, the swap already committed
            log("warn", "could not delete old object", docId=str(old_id), error=repr(exc))
    log("info", "ingested", docId=str(doc_id), name=name, requestId=request_id, chunks=len(chunks),
        pages=len(parsed.page_starts), parseSeconds=round(parse_seconds, 2),
        seconds=round(time.perf_counter() - began, 2))


def swap(conn: psycopg.Connection, doc: dict) -> list:
    """In one transaction: this version becomes ready and older ones go. Returns the deleted ids."""
    key = (doc["workspace_id"], doc["name"])
    with conn.transaction():
        # The same lock uploads of this document take.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{key[0]}/{key[1]}",))
        newer_ready = conn.execute(
            """SELECT 1 FROM documents
               WHERE workspace_id = %s AND name = %s AND version > %s AND status = 'ready'""",
            (*key, doc["version"]),
        ).fetchone()
        if newer_ready:
            conn.execute("DELETE FROM documents WHERE id = %s", (doc["id"],))
            return [doc["id"]]
        conn.execute(
            "UPDATE documents SET status = 'ready', error = NULL, finished_at = now() WHERE id = %s", (doc["id"],)
        )
        old = conn.execute(
            "DELETE FROM documents WHERE workspace_id = %s AND name = %s AND version < %s RETURNING id",
            (*key, doc["version"]),
        ).fetchall()
    return [row["id"] for row in old]


def crashed(job, *_exc) -> None:
    """RQ calls this when a worker died mid-job, before it uses up one of the job's retries."""
    doc_id = job.args[0]
    with connect() as conn:
        if job.retries_left:
            conn.execute(
                "UPDATE documents SET status = 'queued' WHERE id = %s AND status IN ('parsing', 'embedding')",
                (doc_id,),
            )
        else:
            set_failed(conn, doc_id, "the worker stopped repeatedly (out of memory?), upload the file again to retry")
    log("error", "worker died mid-job", docId=doc_id, retriesLeft=job.retries_left)
