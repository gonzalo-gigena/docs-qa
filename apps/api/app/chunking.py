"""Split a document by structure, sized with the embedding model's own tokenizer."""

import functools
from collections.abc import Iterator
from dataclasses import dataclass

from tokenizers import Tokenizer

from app.extract import Document

MAX_TOKENS = 400  # bge-small silently truncates at 512, special tokens included
OVERLAP = 0.15
TOKENIZER_PATH = "/models/tokenizer.json"  # baked into the image from the pinned model revision


@dataclass
class Chunk:
    ordinal: int
    text: str
    heading_path: list[str]
    page: int | None
    char_start: int
    char_end: int


@functools.cache
def tokenizer() -> Tokenizer:
    tok = Tokenizer.from_file(TOKENIZER_PATH)
    # tokenizer.json ships with truncation at 512. Left on, every count stops there and text is lost.
    tok.no_truncation()
    tok.no_padding()
    return tok


def chunk(doc: Document) -> list[Chunk]:
    chunks: list[Chunk] = []
    ends = [start for start, _ in doc.sections[1:]] + [len(doc.text)]
    for (start, path), end in zip(doc.sections, ends):
        for a, b in windows(doc.text[start:end]):
            a, b = start + a, start + b
            chunks.append(Chunk(len(chunks), doc.text[a:b], path, doc.page_at(a), a, b))
    return chunks


def break_rank(text: str, offsets: list[tuple[int, int]], i: int) -> int:
    """How good a place to cut just before token i: paragraph 3, sentence 2, word 1, inside a word 0."""
    before = offsets[i - 1][1]
    gap = text[before:offsets[i][0]]
    if not any(c.isspace() for c in gap):
        return 0
    if gap.count("\n") >= 2:
        return 3
    return 2 if text[before - 1] in ".!?" else 1


def windows(text: str) -> Iterator[tuple[int, int]]:
    # ponytail: tokenizes a whole section at once, about 1 GB for 25 MB of heading-less text
    # (the worker runs out of memory and the document fails). Tokenize in slices if that happens.
    offsets = tokenizer().encode(text, add_special_tokens=False).offsets
    first, n = 0, len(offsets)
    while first < n:
        end = min(first + MAX_TOKENS, n)
        if end < n:
            # The latest best boundary in the second half of the window. max() keeps the first of equals.
            end = max(range(end, first + MAX_TOKENS // 2, -1), key=lambda i: break_rank(text, offsets, i))
        yield offsets[first][0], offsets[end - 1][1]
        if end == n:
            return
        back = end - round((end - first) * OVERLAP)
        first = next((i for i in range(back, end) if break_rank(text, offsets, i)), back)
