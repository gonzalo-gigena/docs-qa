"""Config, logging and clients shared by the api and the worker."""

import functools
import json
import os
import socket
import sys
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from redis import Redis
from rq import Callback, Queue, Retry
from rq.exceptions import DuplicateJobError, NoSuchJobError
from rq.job import Job
from rq.serializers import JSONSerializer

REPLICA = socket.gethostname()


def env(name: str) -> str:
    return os.environ.get(name) or sys.exit(f"missing env var {name}")


DATABASE_URL = env("DATABASE_URL")
REDIS_URL = env("REDIS_URL")
S3_ENDPOINT = env("S3_ENDPOINT")
S3_BUCKET = env("S3_BUCKET")
S3_ACCESS_KEY = env("S3_ACCESS_KEY")
S3_SECRET_KEY = env("S3_SECRET_KEY")

# Seeded by migration 000002. Real workspaces and auth arrive in phase 4.
WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def log(level: str, msg: str, **extra) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(json.dumps({"ts": ts, "level": level, "msg": msg, "replica": REPLICA, **extra}), flush=True)


redis = Redis.from_url(REDIS_URL, socket_connect_timeout=3, health_check_interval=30)
queue = Queue("ingest", connection=redis, serializer=JSONSerializer)  # JSON, so Redis never holds pickles


def enqueue(doc_id: str, attempt: int, request_id: str = "") -> None:
    """Queue one ingestion attempt. Calling it again while that attempt is queued or running does nothing."""
    job_id = f"{doc_id}-{attempt}"
    try:
        job = Job.fetch(job_id, connection=redis, serializer=JSONSerializer)
        if job.get_status() in ("queued", "started", "scheduled", "deferred"):
            return
        job.delete()
    except NoSuchJobError:
        pass
    try:
        queue.enqueue_call(
            "app.ingest.ingest",
            args=[doc_id, request_id],
            job_id=job_id,
            unique=True,
            timeout=1800,
            result_ttl=3600,
            # ingest() records ordinary failures itself, so retries only cover a worker that died mid-job.
            retry=Retry(max=2),
            on_failure=Callback("app.ingest.crashed"),
        )
    except DuplicateJobError:
        pass  # another replica enqueued the same attempt first


@functools.cache
def s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=30,
            retries={"mode": "standard", "max_attempts": 4},
        ),
    )


def object_key(workspace_id, doc_id) -> str:
    return f"{workspace_id}/{doc_id}"  # one object per version, so deleting an old one can't hit a new upload


def ensure_bucket() -> None:
    try:
        s3().head_bucket(Bucket=S3_BUCKET)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        try:
            s3().create_bucket(Bucket=S3_BUCKET)
        except ClientError as race:
            if race.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
