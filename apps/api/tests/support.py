"""Test helpers: build a text PDF, talk to the real stack."""

import io
import json
import time
import urllib.error
import urllib.request
import uuid

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

API = "http://caddy:8080/api/documents"


def make_pdf(pages: list[list[str]], bookmarks: list[tuple[str, int, int | None]] = ()) -> bytes:
    """pages: lines per page. bookmarks: (title, page index, index of the parent bookmark)."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for lines in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        ops = b"BT /F1 10 Tf 50 750 Td 14 TL\n"
        for line in lines:
            safe = line.encode("latin-1", "replace")
            for char in (b"\\", b"(", b")"):
                safe = safe.replace(char, b"\\" + char)
            ops += b"(" + safe + b") Tj T*\n"
        stream = DecodedStreamObject()
        stream.set_data(ops + b"ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    added = []
    for title, page_index, parent in bookmarks:
        parent_item = None if parent is None else added[parent]
        added.append(writer.add_outline_item(title=title, page_number=page_index, parent=parent_item))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def upload(name: str, data: bytes) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    )
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(API, body, {"content-type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                return exc.code, json.load(exc)
            except ValueError:
                return exc.code, {}  # Caddy's own errors have no body


def documents(name: str) -> list[dict]:
    with urllib.request.urlopen(API, timeout=10) as response:
        return [doc for doc in json.load(response)["documents"] if doc["name"] == name]


def wait_for(doc_id: str, name: str, status: str = "ready", timeout: float = 180, every: float = 0.25) -> dict:
    deadline, doc = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        doc = next((d for d in documents(name) if d["id"] == doc_id), None)
        if doc and doc["status"] == status:
            return doc
        if doc and doc["status"] in ("ready", "failed"):
            raise AssertionError(f"wanted {status}, got {doc}")
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {status}: {doc}")
