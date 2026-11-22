"""End to end on the real stack: python -m tests.e2e_ingest (inside the api image, see CI)."""

import http.client
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from botocore.exceptions import ClientError

from app.core import DATABASE_URL, S3_BUCKET, WORKSPACE_ID, object_key, s3
from tests.support import documents, make_pdf, upload, wait_for


def markdown(word: str) -> bytes:
    body = " ".join(f"The {word} service handles request {i} and records it." for i in range(150))
    return f"# Manual\n\n## Operations\n\n{body}\n".encode()


class Ingestion(unittest.TestCase):
    def setUp(self):
        self.name = f"e2e-{uuid.uuid4().hex}"
        self.db = psycopg.connect(DATABASE_URL, autocommit=True)
        self.addCleanup(self.db.close)

    def chunks(self, doc_id: str) -> list[tuple]:
        return self.db.execute(
            """SELECT id, workspace_id::text, heading_path, page, vector_dims(embedding), vector_norm(embedding),
                      tsv <> ''::tsvector
               FROM chunks WHERE doc_id = %s ORDER BY ordinal""",
            (doc_id,),
        ).fetchall()

    def ingest(self, name: str, data: bytes) -> dict:
        status, doc = upload(name, data)
        self.assertEqual(status, 202, doc)
        return wait_for(doc["id"], name)

    def test_reupload_is_a_noop(self):
        name = f"{self.name}.md"
        doc = self.ingest(name, markdown("billing"))
        before = self.chunks(doc["id"])
        self.assertGreater(len(before), 1)
        self.assertEqual(doc["chunks_total"], len(before))
        for _, workspace, path, page, dims, norm, has_tsv in before:
            self.assertEqual((workspace, path, page, dims), (WORKSPACE_ID, ["Manual", "Operations"], None, 384))
            self.assertAlmostEqual(norm, 1, places=3)
            self.assertTrue(has_tsv)

        status, again = upload(name, markdown("billing"))
        self.assertEqual((status, again["unchanged"], again["id"]), (200, True, doc["id"]))
        self.assertEqual(documents(name), [doc])  # same version, same finished_at
        self.assertEqual(self.chunks(doc["id"]), before)  # same chunk ids: nothing was rewritten

    def test_changed_document_replaces_the_old_one(self):
        name = f"{self.name}.md"
        old = self.ingest(name, markdown("billing"))
        status, new = upload(name, markdown("shipping"))
        self.assertEqual(status, 202)

        # Until the swap the old version stays ready, so there is never a moment with none.
        while True:
            versions = documents(name)
            ready = [d["id"] for d in versions if d["status"] == "ready"]
            self.assertEqual(len(ready), 1, versions)
            if ready == [new["id"]]:
                break
            self.assertNotIn("failed", [d["status"] for d in versions], versions)

        self.assertEqual([d["version"] for d in documents(name)], [2])
        self.assertEqual(self.chunks(old["id"]), [])
        self.assertTrue(self.chunks(new["id"]))
        for _ in range(50):  # the old object is deleted just after the commit
            try:
                s3().head_object(Bucket=S3_BUCKET, Key=object_key(WORKSPACE_ID, old["id"]))
            except ClientError as exc:
                self.assertEqual(exc.response["ResponseMetadata"]["HTTPStatusCode"], 404)
                return
        self.fail("the old object was not deleted")

    def test_simultaneous_identical_uploads_make_one_document(self):
        name, data = f"{self.name}.md", markdown("search")
        with ThreadPoolExecutor(8) as threads:
            results = list(threads.map(lambda _: upload(name, data), range(8)))
        self.assertEqual(sorted(status for status, _ in results), [200] * 7 + [202])
        self.assertEqual(len({doc["id"] for _, doc in results}), 1)
        doc = wait_for(results[0][1]["id"], name)
        self.assertEqual(len(self.chunks(doc["id"])), doc["chunks_total"])
        stored = s3().list_objects_v2(Bucket=S3_BUCKET, Prefix=f"{WORKSPACE_ID}/")["Contents"]
        known = {str(row[0]) for row in self.db.execute("SELECT id FROM documents")}
        self.assertEqual([o["Key"] for o in stored if o["Key"].split("/")[-1] not in known], [], "orphan objects")

    def test_pdf_keeps_pages(self):
        name = f"{self.name}.pdf"
        pdf = make_pdf([["Guide", "Intro."], ["Install", "Run it."]], [("Guide", 0, None), ("Install", 1, 0)])
        doc = self.ingest(name, pdf)
        self.assertEqual(doc["pages"], 2)
        self.assertEqual([(c[2], c[3]) for c in self.chunks(doc["id"])], [(["Guide"], 1), (["Guide", "Install"], 2)])

    def test_html(self):
        name = f"{self.name}.html"
        text = "The deploy job copies the release to every host and restarts it. " * 10
        page = f"<html><body><article><h1>Runbook</h1><p>{text}</p></article></body></html>"
        doc = self.ingest(name, page.encode())
        self.assertEqual(self.chunks(doc["id"])[0][2], ["Runbook"])

    def test_failure_has_a_reason_and_can_be_retried(self):
        name = f"{self.name}.pdf"
        status, doc = upload(name, b"this is not a pdf")
        self.assertEqual(status, 202)
        self.assertIn("could not read the PDF", wait_for(doc["id"], name, "failed")["error"])

        status, retry = upload(name, b"this is not a pdf")
        self.assertEqual((status, retry["id"], retry["attempt"]), (202, doc["id"], 2))
        wait_for(doc["id"], name, "failed")

    def test_bad_uploads_are_rejected(self):
        self.assertEqual(upload(f"{self.name}.exe", b"x")[0], 415)
        self.assertEqual(upload(f"{self.name}.md", b"")[0], 400)
        self.assertEqual(upload(f"{self.name}.md", b"x" * (25 * 1024 * 1024 + 1))[0], 413)
        # A file of exactly 25 MiB gets through Caddy to the api (which rejects this one for its type).
        self.assertEqual(upload(f"{self.name}.exe", b"x" * (25 * 1024 * 1024))[0], 415)

    def test_proxy_rejects_huge_bodies_without_reading_them(self):
        conn = http.client.HTTPConnection("caddy", 8080, timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/api/documents")
        conn.putheader("content-type", "multipart/form-data; boundary=x")
        conn.putheader("content-length", str(27 * 1024 * 1024))
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
