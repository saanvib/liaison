from pathlib import Path

from vendor_agent.ingest import load_documents

DOCS_SEED = Path(__file__).parents[1] / "docs_seed"


def test_load_documents_reads_md_files_with_guessed_doc_type():
    docs = load_documents(DOCS_SEED)
    by_type = {d.doc_type for d in docs}
    # seed_questions.md is team reference material, not a product doc, so it
    # falls into "other" alongside the four real documents.
    assert {"datasheet", "ifu", "validation_report", "faq"}.issubset(by_type)
    assert all(d.source == "cli" for d in docs)
    assert all(d.text for d in docs)


def test_load_documents_ignores_non_md_txt_files(tmp_path):
    (tmp_path / "notes.md").write_text("hello")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    docs = load_documents(tmp_path)
    assert len(docs) == 1
    assert docs[0].doc_type == "other"
    assert docs[0].title == "Notes"
