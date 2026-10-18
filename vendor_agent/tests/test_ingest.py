from pathlib import Path

from vendor_agent.ingest import load_documents, send_spaced

DOCS_SEED = Path(__file__).parents[1] / "docs_seed"


def test_load_documents_reads_md_files_with_guessed_doc_type():
    docs = load_documents(DOCS_SEED)
    # seed_questions.md is team reference material, not a product doc: skipped.
    assert sorted(d.doc_type for d in docs) == ["datasheet", "faq", "ifu", "validation_report"]
    assert sorted(d.doc_id for d in docs) == ["datasheet.md", "faq.md", "ifu.md", "validation_report.md"]
    assert all(d.source == "cli" for d in docs)
    assert all(d.text for d in docs)


def test_load_documents_ignores_non_md_txt_files(tmp_path):
    (tmp_path / "notes.md").write_text("hello")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    docs = load_documents(tmp_path)
    assert len(docs) == 1
    assert docs[0].doc_type == "other"
    assert docs[0].title == "Notes"


async def test_send_spaced_sends_one_at_a_time_with_a_gap_between():
    events = []

    async def send(doc):
        events.append(("send", doc.doc_id))
        return "delivered"

    async def sleep(seconds):
        events.append(("sleep", seconds))

    docs = load_documents(DOCS_SEED)
    assert await send_spaced(send, docs, 30.0, sleep=sleep) == ["delivered"] * len(docs)
    assert events == [
        ("send", "datasheet.md"), ("sleep", 30.0), ("send", "faq.md"), ("sleep", 30.0),
        ("send", "ifu.md"), ("sleep", 30.0), ("send", "validation_report.md"),
    ]
