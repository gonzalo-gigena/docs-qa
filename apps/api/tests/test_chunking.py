"""Extraction and chunking. No network, no database: the tokenizer is baked into the image."""

import io
import unittest

from pypdf import PdfWriter

from app.chunking import MAX_TOKENS, chunk, tokenizer
from app.extract import Document, Unreadable, extract
from tests.support import make_pdf


def prose(sentences: int, tag: str = "") -> str:
    return " ".join(f"The {tag} worker number {i} reads the queue and stores vectors." for i in range(sentences))


def tokens(text: str) -> int:
    return len(tokenizer().encode(text, add_special_tokens=False).ids)


def encrypted(user_password: str) -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(make_pdf([["Read me."]])))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class Chunking(unittest.TestCase):
    def check(self, doc: Document):
        chunks = chunk(doc)
        self.assertEqual([c.ordinal for c in chunks], list(range(len(chunks))))
        covered = [False] * len(doc.text)
        for c in chunks:
            self.assertEqual(c.text, doc.text[c.char_start:c.char_end])
            self.assertLessEqual(tokens(c.text), MAX_TOKENS)
            covered[c.char_start:c.char_end] = [True] * (c.char_end - c.char_start)
        lost = "".join(char for char, seen in zip(doc.text, covered) if not seen and not char.isspace())
        self.assertEqual(lost, "", "text was dropped")
        return chunks

    def test_tokenizer_does_not_truncate(self):
        self.assertGreater(tokens(prose(500)), 5000)

    def test_long_text_is_fully_covered_with_15_percent_overlap(self):
        doc = Document(prose(500), [(0, [])])
        chunks = self.check(doc)
        self.assertGreater(len(chunks), 10)
        for a, b in zip(chunks, chunks[1:]):
            shared = tokens(doc.text[b.char_start:a.char_end]) / tokens(a.text)
            self.assertAlmostEqual(shared, 0.15, delta=0.03)
            self.assertTrue(doc.text[b.char_start - 1].isspace(), "a chunk starts inside a word")

    def test_prefers_paragraph_boundaries(self):
        paragraphs = [prose(13, f"p{i}") for i in range(8)]
        doc = Document("\n\n".join(paragraphs), [(0, [])])
        for c in self.check(doc)[:-1]:
            self.assertTrue(doc.text.startswith("\n\n", c.char_end), "cut inside a paragraph")

    def test_text_without_spaces_still_fits(self):
        self.assertGreater(len(self.check(Document("漢字" * 1000, [(0, [])]))), 1)

    def test_markdown_headings(self):
        source = (
            f"# Guide\n\n## Install\n{prose(120)}\n\n"
            "```sh\n# not a heading\n```\n\n"
            f"### Linux\n{prose(3)}\n\n## Usage ##\nrun it\n\n# Reference\nflags\n"
        )
        chunks = self.check(extract("md", source.encode()))
        paths = list(dict.fromkeys(tuple(c.heading_path) for c in chunks))
        self.assertEqual(paths, [
            ("Guide", "Install"), ("Guide", "Install", "Linux"), ("Guide", "Usage"), ("Reference",),
        ])
        self.assertTrue(chunks[0].text.startswith("# Guide"))

    def test_chunks_never_cross_a_heading(self):
        chunks = self.check(extract("md", b"## One\nfirst body\n\n## Two\nsecond body\n"))
        self.assertEqual([c.heading_path for c in chunks], [["One"], ["Two"]])
        self.assertNotIn("second", chunks[0].text)

    def test_html_keeps_headings(self):
        page = (
            f"<html><body><article><h1>Guide</h1><p>{prose(10, 'intro')}</p><h2>Install</h2>"
            f"<p>{prose(10, 'setup')}</p></article></body></html>"
        )
        chunks = self.check(extract("html", page.encode()))
        self.assertEqual(chunks[-1].heading_path, ["Guide", "Install"])
        self.assertIn("setup", chunks[-1].text)

    def test_pdf_keeps_pages_and_bookmark_paths(self):
        pdf = make_pdf(
            [
                ["Guide", "The introduction."],
                ["The introduction goes on.", "Install", "Run the installer."],
                ["Reference", "All the flags."],
            ],
            [("Guide", 0, None), ("Install", 1, 0), ("Reference", 2, None)],
        )
        chunks = self.check(extract("pdf", pdf))
        self.assertEqual(
            [(c.page, c.heading_path) for c in chunks],
            [(1, ["Guide"]), (2, ["Guide", "Install"]), (3, ["Reference"])],
        )
        self.assertIn("goes on", chunks[0].text)  # the new section starts mid-page, at its title

    def test_encrypted_pdfs(self):
        # An owner password only (common for manuals) opens with no password. A user password doesn't.
        self.assertIn("Read me", extract("pdf", encrypted("")).text)
        with self.assertRaisesRegex(Unreadable, "password-protected"):
            extract("pdf", encrypted("secret"))

    def test_unreadable_files(self):
        for kind, data in (("pdf", b"not a pdf"), ("md", b"\xff\xfe\xfa"), ("md", b"  \n"), ("pdf", make_pdf([[]]))):
            with self.subTest(kind=kind, data=data[:12]), self.assertRaises(Unreadable):
                extract(kind, data)

    def test_nul_is_replaced(self):
        self.assertNotIn("\x00", extract("md", b"a\x00b").text)


if __name__ == "__main__":
    unittest.main()
