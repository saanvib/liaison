from pathlib import Path

import pytest

from contracts import IngestDocument
from vendor_agent import clients
from vendor_agent.clients import GeminiEmbedder, OfflineEmbedder
from vendor_agent.ingest import load_documents
from vendor_agent.retrieval import _STORAGE_KEY, CHUNK_SIZE, DictStorage, Retriever, chunk_document

DOCS_SEED = Path(__file__).parents[1] / "docs_seed"


def make_retriever() -> Retriever:
    return Retriever(OfflineEmbedder(), DictStorage())


SEED_TEXT = {p.name: p.read_text(encoding="utf-8") for p in DOCS_SEED.glob("*.md")}
SHELF_LIFE = "Unopened kits are stable for 18 months from the date of manufacture"


def test_shelf_life_sentence_stays_whole_in_one_chunk_with_its_heading():
    """Live 2026-10-18: fixed 800-char windows cut this sentence in two, the half without
    "18 months" ranked first, and the answer said the shelf life was "not stated"."""
    [chunk] = [c for c in chunk_document(SEED_TEXT["datasheet.md"]) if "date of manufacture" in c.text]
    assert SHELF_LIFE in chunk.text
    assert chunk.location == "Storage and stability"
    assert chunk.text.startswith("AuroraPrep RUO Nucleic Acid Extraction Kit — Datasheet > Storage and stability\n")


def test_each_faq_entry_is_its_own_chunk():
    chunks = chunk_document(SEED_TEXT["faq.md"])
    entries = [c for c in chunks if "**Q:" in c.text]
    assert all(c.text.count("**Q:") == 1 for c in entries)
    [shelf] = [c for c in entries if "shelf life" in c.text]
    assert "18 months from the date of manufacture" in shelf.text
    assert "35 minutes" not in shelf.text  # the protocol-time entry is a separate chunk


@pytest.mark.parametrize("name", sorted(SEED_TEXT))
def test_seed_chunks_fit_and_lose_no_text(name):
    chunks = chunk_document(SEED_TEXT[name])
    assert chunks and all(len(c.text) <= CHUNK_SIZE for c in chunks)
    body = "\n".join(c.text for c in chunks)
    for line in SEED_TEXT[name].splitlines():
        if line.strip() and not line.startswith("#"):
            assert line.rstrip() in body, line


def test_a_long_paragraph_splits_on_sentence_ends():
    sentence = "The column binds nucleic acid in the presence of chaotropic salt."
    chunks = chunk_document("## Binding\n\n" + " ".join([sentence] * 30))
    assert len(chunks) > 1
    for c in chunks:
        body = c.text.split("\n", 1)[1]
        assert len(c.text) <= CHUNK_SIZE and body.startswith("The column") and body.endswith("salt.")


def test_text_without_headings_is_one_chunk_per_paragraph():
    assert [c.text for c in chunk_document("first paragraph\n\nsecond paragraph")] == ["first paragraph", "second paragraph"]
    assert chunk_document("short text")[0].location is None
    assert chunk_document("") == []


def test_reingesting_a_doc_id_replaces_its_chunks():
    storage = DictStorage()
    retriever = Retriever(OfflineEmbedder(), storage)
    retriever.add_document(IngestDocument(doc_id="faq.md", title="Faq", doc_type="faq", text="old answer\n\nold second"))
    retriever.add_document(IngestDocument(doc_id="ifu.md", title="Ifu", doc_type="ifu", text="ifu text"))
    retriever.add_document(IngestDocument(doc_id="faq.md", title="Faq", doc_type="faq", text="new answer"))
    assert sorted((r["doc_id"], r["text"]) for r in storage.get(_STORAGE_KEY)) == [("faq.md", "new answer"), ("ifu.md", "ifu text")]


def test_old_v1_chunks_are_ignored():
    storage = DictStorage()
    storage.set("vendor_agent:retrieval_chunks", [{"doc_id": "x", "doc_title": "Old", "location": "chunk 1", "text": "shelf life", "vector": [1.0]}])
    assert Retriever(OfflineEmbedder(), storage).search("shelf life") == []


class RecordingEmbedder(OfflineEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = []

    def embed(self, texts):
        self.calls.append(("query", list(texts)))
        return super().embed(texts)

    def embed_documents(self, texts):
        self.calls.append(("document", list(texts)))
        return super().embed(texts)


def test_chunks_embed_as_documents_and_questions_as_queries():
    embedder = RecordingEmbedder()
    retriever = Retriever(embedder, DictStorage())
    retriever.add_document(IngestDocument(doc_id="d", title="D", doc_type="other", text="shelf life is 18 months"))
    retriever.search("shelf life?")
    assert [kind for kind, _ in embedder.calls] == ["document", "query"]


def test_gemini_embedder_sends_task_types(monkeypatch):
    bodies = []

    def fake_post(url, api_key, body, timeout_s):
        bodies.append(body)
        return {"embeddings": [{"values": [1.0, 0.0]} for _ in body["requests"]]}

    monkeypatch.setattr(clients, "_gemini_post", fake_post)
    embedder = GeminiEmbedder(api_key="k")
    embedder.embed_documents(["chunk"])
    embedder.embed(["question"])
    assert [r["taskType"] for b in bodies for r in b["requests"]] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
    assert all(r["outputDimensionality"] == GeminiEmbedder.DIMENSIONS for b in bodies for r in b["requests"])


def test_search_finds_relevant_chunk():
    retriever = make_retriever()
    retriever.add_document(
        IngestDocument(
            doc_id="d1", title="Datasheet", doc_type="datasheet",
            text="The AuroraPrep kit is stable for 18 months unopened, stored at room temperature.",
        )
    )
    retriever.add_document(
        IngestDocument(
            doc_id="d2", title="Unrelated", doc_type="other",
            text="Completely unrelated text about kayaking and mountain weather patterns.",
        )
    )
    hits = retriever.search("How long is the AuroraPrep kit's shelf life?", k=2)
    assert hits
    assert hits[0].doc_id == "d1"
    assert "18 months" in hits[0].text


def test_confidence_uses_top_hit_score_and_handles_empty():
    retriever = make_retriever()
    assert retriever.confidence([]) == 0.0
    retriever.add_document(IngestDocument(doc_id="d1", title="T", doc_type="other", text="shelf life is 18 months"))
    hits = retriever.search("shelf life")
    assert retriever.confidence(hits) == hits[0].score
    assert 0.0 <= retriever.confidence(hits) <= 1.0


def test_to_citations_maps_hits_and_truncates_snippet():
    retriever = make_retriever()
    retriever.add_document(IngestDocument(doc_id="d1", title="Datasheet", doc_type="datasheet", text="x" * 500))
    hits = retriever.search("x")
    citations = retriever.to_citations(hits)
    assert citations[0].doc_id == "d1"
    assert citations[0].doc_title == "Datasheet"
    assert citations[0].location == "chunk 1"
    assert len(citations[0].snippet) <= 240


@pytest.mark.parametrize("filename", ["datasheet.md", "ifu.md", "validation_report.md", "faq.md"])
def test_seed_docs_exist_and_load(filename):
    assert (DOCS_SEED / filename).exists()


def test_direct_ingest_of_seed_docs_answers_a_known_question():
    """Exercises the real acceptance check from the role file ("--direct ingest of
    docs_seed/ makes a known question return the right chunk with a citation")
    at the Retriever level: VendorCore.handle_ingest (Step 4) will delegate to
    this same Retriever, so this proves the retrieval half of that path now."""
    retriever = make_retriever()
    for doc in load_documents(DOCS_SEED):
        retriever.add_document(doc)

    hits = retriever.search("What is the shelf life of an unopened AuroraPrep kit?", k=4)
    assert hits
    assert hits[0].doc_id  # came from a real seeded doc
    assert "18 months" in hits[0].text or any("18 months" in h.text for h in hits)

    citations = retriever.to_citations(hits)
    assert citations and citations[0].doc_title  # a real citation, not a stub
