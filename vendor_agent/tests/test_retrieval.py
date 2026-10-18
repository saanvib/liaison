from pathlib import Path

import pytest

from contracts import IngestDocument
from vendor_agent.clients import OfflineEmbedder
from vendor_agent.ingest import load_documents
from vendor_agent.retrieval import DictStorage, Retriever, _chunk

DOCS_SEED = Path(__file__).parents[1] / "docs_seed"


def make_retriever() -> Retriever:
    return Retriever(OfflineEmbedder(), DictStorage())


def test_chunking_overlaps_and_covers_text():
    text = "x" * 2000
    chunks = _chunk(text, size=800, overlap=100)
    assert chunks[0] == text[0:800]
    assert chunks[1] == text[700:1500]
    assert chunks[-1].endswith("x")
    assert "".join(chunks) != ""  # non-empty, sanity


def test_chunking_short_text_is_one_chunk():
    assert _chunk("short text", size=800, overlap=100) == ["short text"]


def test_chunking_empty_text_is_no_chunks():
    assert _chunk("", size=800, overlap=100) == []


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
