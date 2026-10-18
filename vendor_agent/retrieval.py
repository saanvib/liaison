"""Document chunking, embedding, and search for the Vendor Agent's KB (Step 2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

from contracts import Citation, IngestDocument

from vendor_agent.clients import Embedder, cosine

CHUNK_SIZE = 800
# v2: structure-aware chunks with Gemini task-typed vectors. The v1 key (fixed 800-char windows,
# random doc ids, duplicates from re-ingests) is simply never read again.
_STORAGE_KEY = "vendor_agent:retrieval_chunks_v2"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class Storage(Protocol):
    def get(self, key: str): ...
    def set(self, key: str, value) -> None: ...


class DictStorage:
    """Plain-dict-backed Storage, for tests and local (non-agent) dev."""

    def __init__(self):
        self._data: dict = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value) -> None:
        self._data[key] = value


@dataclass
class Hit:
    text: str
    doc_id: str
    doc_title: str
    location: Optional[str]
    score: float


@dataclass
class Chunk:
    location: Optional[str]  # the section heading, e.g. "Storage and stability"
    text: str                # what gets embedded and cited: "<heading path>\n<block>"


def _split_long(block: str, size: int) -> list[str]:
    """Split a block over `size` on line ends, then on sentence ends, packing the pieces
    back up to `size`. Only a single sentence over `size` is cut mid-sentence."""
    if len(block) <= size:
        return [block]
    sep = "\n" if "\n" in block else " "
    units = block.split("\n") if sep == "\n" else _SENTENCE_END.split(block)
    if len(units) == 1:
        return [block[i:i + size] for i in range(0, len(block), size)]
    out: list[str] = []
    for unit in units:
        for piece in _split_long(unit, size):
            if out and len(out[-1]) + len(sep) + len(piece) <= size:
                out[-1] += sep + piece
            else:
                out.append(piece)
    return out


def chunk_document(text: str, size: int = CHUNK_SIZE) -> list[Chunk]:
    """One chunk per paragraph, list or FAQ entry (blank-line-separated blocks), each
    prefixed with its heading path so it carries its own context. A fact is never cut
    in half: a block over `size` splits on line or sentence ends, not mid-sentence."""
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []  # the current heading path as (level, text)
    block: list[str] = []

    def flush() -> None:
        body = "\n".join(block).strip()
        block.clear()
        if not body:
            return
        prefix = " > ".join(h for _, h in headings)
        location = headings[-1][1] if len(headings) > 1 else None
        for piece in _split_long(body, size - len(prefix) - 1):
            chunks.append(Chunk(location=location, text=f"{prefix}\n{piece}" if prefix else piece))

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            headings[:] = [h for h in headings if h[0] < level] + [(level, m.group(2))]
        elif not line.strip():
            flush()
        else:
            block.append(line.rstrip())
    flush()
    return chunks


class Retriever:
    def __init__(self, embedder: Embedder, storage: Storage):
        self.embedder = embedder
        self.storage = storage

    def add_document(self, doc: IngestDocument) -> None:
        """Add `doc`'s chunks, replacing any already stored under the same doc_id, so
        re-ingesting a file (stable doc_id from its name, see ingest.py) never duplicates it."""
        chunks = chunk_document(doc.text)
        if not chunks:
            return
        # Documents and questions are embedded differently where the embedder supports it.
        embed_documents = getattr(self.embedder, "embed_documents", self.embedder.embed)
        vectors = embed_documents([c.text for c in chunks])
        records = [r for r in self.storage.get(_STORAGE_KEY) or [] if r["doc_id"] != doc.doc_id]
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            records.append(
                {
                    "doc_id": doc.doc_id,
                    "doc_title": doc.title,
                    "location": chunk.location or f"chunk {i + 1}",
                    "text": chunk.text,
                    "vector": vector,
                }
            )
        self.storage.set(_STORAGE_KEY, records)

    def search(self, query: str, k: int = 4, vector: Optional[list[float]] = None) -> list[Hit]:
        records = self.storage.get(_STORAGE_KEY) or []
        if not records:
            return []
        query_vector = vector if vector is not None else self.embedder.embed([query])[0]
        scored = sorted(
            records, key=lambda r: cosine(query_vector, r["vector"]), reverse=True
        )[:k]
        return [
            Hit(
                text=r["text"],
                doc_id=r["doc_id"],
                doc_title=r["doc_title"],
                location=r["location"],
                score=max(0.0, min(1.0, cosine(query_vector, r["vector"]))),
            )
            for r in scored
        ]

    def confidence(self, hits: list[Hit]) -> float:
        """The top hit's score. `search()` already clamps cosine similarity to
        [0, 1] (cosine is in [-1, 1] in general; negative similarity means "no
        match", so it's floored at 0)."""
        return hits[0].score if hits else 0.0

    def to_citations(self, hits: list[Hit]) -> list[Citation]:
        return [
            Citation(doc_id=h.doc_id, doc_title=h.doc_title, snippet=h.text[:240], location=h.location)
            for h in hits
        ]
