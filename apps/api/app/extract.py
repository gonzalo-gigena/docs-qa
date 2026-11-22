"""Bytes in, plain text out, plus where every heading and page starts in that text."""

import io
import re
from bisect import bisect_right
from dataclasses import dataclass, field

KINDS = {".pdf": "pdf", ".html": "html", ".htm": "html", ".md": "md", ".markdown": "md"}

# ponytail: ATX headings (`## Title`) only. Add setext (`Title\n=====`) if real docs need it.
HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


class Unreadable(Exception):
    """The file itself is the problem. The message is shown to the user."""


@dataclass
class Document:
    text: str
    sections: list[tuple[int, list[str]]]  # (start, heading path), ordered by start
    page_starts: list[int] = field(default_factory=list)  # PDFs only

    def page_at(self, pos: int) -> int | None:
        return bisect_right(self.page_starts, pos) if self.page_starts else None


def clean(text: str) -> str:
    # Postgres text rejects NUL, and pypdf can emit lone surrogates.
    text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text.replace("\x00", "�")


def extract(kind: str, data: bytes) -> Document:
    doc = {"pdf": from_pdf, "html": from_html, "md": from_markdown}[kind](data)
    if not doc.text.strip():
        hint = " (a scanned PDF needs OCR first)" if kind == "pdf" else ""
        raise Unreadable(f"no text found{hint}")
    return doc


def from_markdown(data: bytes) -> Document:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Unreadable("the file is not UTF-8 text") from None
    text = clean(text)
    return Document(text, markdown_sections(text))


def from_html(data: bytes) -> Document:
    import trafilatura

    text = trafilatura.extract(
        data,
        output_format="markdown",
        include_formatting=True,
        include_comments=False,
        favor_recall=True,
    )
    if not text:
        raise Unreadable("no text found in the HTML")
    text = clean(text)
    return Document(text, markdown_sections(text))


def markdown_sections(text: str) -> list[tuple[int, list[str]]]:
    sections: list[tuple[int, list[str]]] = [(0, [])]
    stack: list[tuple[int, str]] = []
    fence = ""
    body = False  # anything but headings since the current section started
    pos = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        mark = FENCE.match(line)
        if fence:
            if mark and mark[1].startswith(fence) and not line[mark.end():].strip():
                fence = ""
        elif mark:
            fence, body = mark[1], True
        elif match := HEADING.fullmatch(line):
            level, title = len(match[1]), match[2].strip()
            nested = not stack or level > stack[-1][0]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = [name for _, name in stack]
            if not body and nested:
                sections[-1] = (sections[-1][0], path)  # "# A" straight into "## B": one section, not a title-only chunk
            else:
                sections.append((pos, path))
            body = False
        elif line.strip():
            body = True
        pos += len(raw)
    return sections


def from_pdf(data: bytes) -> Document:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Unreadable("the PDF is password-protected")
        pages = [clean(page.extract_text()) for page in reader.pages]
    except Unreadable:
        raise
    except Exception as exc:
        raise Unreadable(f"could not read the PDF ({type(exc).__name__})") from exc

    starts, pos = [], 0
    for page in pages:
        starts.append(pos)
        pos += len(page) + 2
    return Document("\n\n".join(pages), pdf_sections(reader, pages, starts), starts)


def pdf_sections(reader, pages: list[str], starts: list[int]) -> list[tuple[int, list[str]]]:
    """Heading paths come from the PDF's bookmarks. Without bookmarks there are none."""
    found: list[tuple[int, list[str]]] = []
    seen: dict[int, int] = {}  # page -> where the previous heading was found on it

    def walk(items, parents: list[str]) -> None:
        path = parents
        for item in items:
            if isinstance(item, list):
                walk(item, path)  # children follow their parent
                continue
            title = " ".join(clean(str(item.title)).split())
            path = [*parents, title] if title else parents
            page = reader.get_destination_page_number(item)
            if page is None or not 0 <= page < len(pages):
                continue
            # A bookmark only names a page. Find the title on it, or fall back to the top of the page.
            at = pages[page].find(title, seen.get(page, 0))
            if at >= 0:
                seen[page] = at
            found.append((starts[page] + max(at, 0), path))

    try:
        walk(reader.outline, [])
    except Exception:  # broken bookmarks shouldn't cost us the document
        found.clear()
    by_start = dict(found)  # several bookmarks at one spot: the deepest (last) wins
    by_start.setdefault(0, [])
    return sorted(by_start.items())
