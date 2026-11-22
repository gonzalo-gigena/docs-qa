"""worker: same image as the api. Run: python -m app.worker (healthcheck: python -m app.worker health)"""

import logging
import sys
from datetime import datetime, timezone

from rq import Worker
from rq.serializers import JSONSerializer

from app.core import REPLICA, enqueue, log, queue, redis
from app.ingest import connect, crashed

WORKER_TTL = 90  # also how long an idle worker blocks, so how soon it notices a dead sibling's job


class JsonLogs(logging.Handler):
    """RQ, pypdf and warnings log through stdlib logging. Same JSON lines as ours."""

    def emit(self, record: logging.LogRecord) -> None:
        log(record.levelname.lower(), record.getMessage().strip(), logger=record.name)


def healthy() -> bool:
    now = datetime.now(timezone.utc)
    for worker in Worker.all(connection=redis, serializer=JSONSerializer):
        if worker.hostname == REPLICA and worker.last_heartbeat:
            beat = worker.last_heartbeat.replace(tzinfo=timezone.utc)
            if (now - beat).total_seconds() < WORKER_TTL + 30:
                return True
    return False


def main() -> None:
    logging.basicConfig(handlers=[JsonLogs()], level=logging.WARNING)
    logging.captureWarnings(True)
    with connect() as conn:
        # Anything unfinished whose job Redis no longer has (lost data, api died before enqueueing).
        for doc in conn.execute(
            "SELECT id, attempt FROM documents WHERE status IN ('queued', 'parsing', 'embedding')"
        ).fetchall():
            enqueue(str(doc["id"]), doc["attempt"])
    log("info", "worker started")
    # RQ forks per job and handles SIGTERM: it stops taking jobs and finishes the current one.
    Worker(
        [queue],
        connection=redis,
        serializer=JSONSerializer,
        worker_ttl=WORKER_TTL,
        maintenance_interval=60,
        work_horse_killed_handler=lambda job, *_: crashed(job),
    ).work(logging_level="WARNING")
    log("info", "worker stopped")


if __name__ == "__main__":
    if sys.argv[1:] == ["health"]:
        sys.exit(0 if healthy() else 1)
    main()
