"""Document chunking, embedding, and search for the Vendor Agent's KB (Step 2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from contracts import Citation, IngestDocument

from vendor_agent.clients import Embedder, cosine

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
_STORAGE_KEY = "vendor_agent:retrieval_chunks"


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


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    chunks = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start = end - overlap
    return chunks


class Retriever:
    def __init__(self, embedder: Embedder, storage: Storage):
        self.embedder = embedder
        self.storage = storage

    def add_document(self, doc: IngestDocument) -> None:
        chunks = _chunk(doc.text)
        if not chunks:
            return
        vectors = self.embedder.embed(chunks)
        records = self.storage.get(_STORAGE_KEY) or []
        for i, (text, vector) in enumerate(zip(chunks, vectors)):
            records.append(
                {
                    "doc_id": doc.doc_id,
                    "doc_title": doc.title,
                    "location": f"chunk {i + 1}",
                    "text": text,
                    "vector": vector,
                }
            )
        self.storage.set(_STORAGE_KEY, records)

    def search(self, query: str, k: int = 4) -> list[Hit]:
        records = self.storage.get(_STORAGE_KEY) or []
        if not records:
            return []
        [query_vector] = self.embedder.embed([query])
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
