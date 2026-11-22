"""Time a 300-page PDF from upload to ready: python -m tests.bench_ingest [pages] [runs]

The PDF is synthetic: real English prose and code (stdlib module docs), 45 lines a page,
with a bookmark per module. Each run gets different content, so none is a cached no-op.
"""

import importlib
import pydoc
import sys
import textwrap
import time
import uuid

from tests.support import make_pdf, upload, wait_for

MODULES = """argparse asyncio subprocess logging json pathlib collections datetime decimal email.message
    http.client inspect ipaddress os shutil socket statistics tarfile threading typing unittest zipfile
    calendar codecs configparser csv difflib enum fractions heapq mailbox optparse pickle pprint random
    selectors shlex sqlite3 string tempfile textwrap timeit traceback uuid wave weakref zipimport""".split()
LINES_PER_PAGE = 45


def sample_pdf(count: int) -> bytes:
    run = uuid.uuid4().hex
    pages: list[list[str]] = []
    bookmarks = []
    while len(pages) < count:
        for name in MODULES:
            text = pydoc.render_doc(importlib.import_module(name), renderer=pydoc.plaintext)
            lines = [part for line in text.splitlines() if line.strip() for part in textwrap.wrap(line, 85)]
            lines = [f"Module {name}", f"Run {run}", *lines]
            bookmarks.append((f"Module {name}", len(pages), None))
            pages += [lines[i:i + LINES_PER_PAGE] for i in range(0, len(lines), LINES_PER_PAGE)]
            if len(pages) >= count:
                break
    return make_pdf(pages[:count], bookmarks)


def main(pages: int = 300, runs: int = 3) -> None:
    for run in range(1, runs + 1):
        name, data = f"bench-{uuid.uuid4().hex[:8]}.pdf", sample_pdf(pages)
        start = time.perf_counter()
        status, doc = upload(name, data)
        assert status == 202, doc
        doc = wait_for(doc["id"], name, timeout=1800, every=0.5)
        took = time.perf_counter() - start

        start = time.perf_counter()
        status, again = upload(name, data)
        noop = time.perf_counter() - start
        assert status == 200 and again["unchanged"] and again["id"] == doc["id"], again
        print(
            f"run {run}: {doc['pages']} pages, {len(data) / 1e6:.2f} MB, {doc['chunks_total']} chunks: "
            f"ready in {took:.1f}s ({pages * 60 / took:.0f} pages/min), re-upload no-op in {noop * 1000:.0f} ms",
            flush=True,
        )


if __name__ == "__main__":
    main(*(int(arg) for arg in sys.argv[1:]))
