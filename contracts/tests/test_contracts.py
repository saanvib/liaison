from contracts import (
    ApprovedAnswer,
    Citation,
    Escalation,
    EscalationReason,
    GuardCategory,
    GuardRequest,
    GuardVerdict,
    IngestDocument,
    Source,
    Topic,
    Verdict,
)


def round_trip(msg):
    cls = type(msg)
    return cls.model_validate_json(msg.model_dump_json())


def test_citation_round_trip():
    c = Citation(doc_id="d1", doc_title="Datasheet", snippet="some text", location="p.2")
    assert round_trip(c) == c


def test_guard_request_round_trip():
    req = GuardRequest(
        request_id="r1",
        source=Source.VENDOR_AGENT,
        question="what is the shelf life?",
        draft_answer="18 months",
        citations=[Citation(doc_id="d1", doc_title="Datasheet", snippet="18 months")],
        confidence=0.9,
        escalation_id=None,
    )
    out = round_trip(req)
    assert out == req
    assert out.source is Source.VENDOR_AGENT


def test_guard_verdict_round_trip():
    verdict = GuardVerdict(
        request_id="r1",
        verdict=Verdict.NEEDS_REVIEW,
        category=GuardCategory.OFF_LABEL,
        reason="mentions off-label use",
        reroute_topic=Topic.SALES_OPS,
        revised_answer=None,
    )
    out = round_trip(verdict)
    assert out == verdict
    assert out.verdict is Verdict.NEEDS_REVIEW
    assert out.category is GuardCategory.OFF_LABEL
    assert out.reroute_topic is Topic.SALES_OPS


def test_escalation_round_trip():
    esc = Escalation(
        escalation_id="e1",
        question="can this diagnose a patient?",
        reason=EscalationReason.GUARD_BLOCKED,
        topic=Topic.QA_RA,
        draft_answer="no",
        citations=[],
        guard_verdict=GuardVerdict(
            request_id="r1", verdict=Verdict.BLOCKED, reason="patient data"
        ),
        asker_ref="chat:abc",
    )
    out = round_trip(esc)
    assert out == esc
    assert out.reason is EscalationReason.GUARD_BLOCKED
    assert out.topic is Topic.QA_RA


def test_ingest_document_round_trip():
    doc = IngestDocument(
        doc_id="doc1",
        title="FAQ",
        doc_type="faq",
        text="Q: ... A: ...",
        filename="faq.md",
        uploaded_by="U123",
        source="slack",
    )
    out = round_trip(doc)
    assert out == doc


def test_approved_answer_round_trip_with_verdict():
    msg = ApprovedAnswer(
        escalation_id="e1",
        question="what is the shelf life?",
        answer="18 months at -20C",
        topic=Topic.RND,
        approved_by="guard",
        guard_verdict=GuardVerdict(
            request_id="r1", verdict=Verdict.APPROVED, reason="clean"
        ),
        citations=[],
        asker_ref=None,
    )
    out = round_trip(msg)
    assert out == msg
    assert out.guard_verdict is not None
    assert out.guard_verdict.verdict is Verdict.APPROVED


def test_approved_answer_valid_without_verdict():
    msg = ApprovedAnswer(
        escalation_id="e1",
        question="who is the on-call engineer?",
        answer="Jane Doe",
        topic=Topic.ENGINEERING,
        approved_by="U456|Jane Doe",
    )
    assert msg.guard_verdict is None
    out = round_trip(msg)
    assert out == msg
    assert out.guard_verdict is None


def test_list_defaults_not_shared_between_instances():
    a = GuardRequest(request_id="a", source=Source.VENDOR_AGENT, question="q", draft_answer="")
    b = GuardRequest(request_id="b", source=Source.VENDOR_AGENT, question="q", draft_answer="")
    a.citations.append(Citation(doc_id="d1", doc_title="t", snippet="s"))
    assert b.citations == []

    esc_a = Escalation(escalation_id="a", question="q", reason=EscalationReason.LOW_CONFIDENCE)
    esc_b = Escalation(escalation_id="b", question="q", reason=EscalationReason.LOW_CONFIDENCE)
    esc_a.citations.append(Citation(doc_id="d1", doc_title="t", snippet="s"))
    assert esc_b.citations == []

    ans_a = ApprovedAnswer(
        escalation_id="a", question="q", answer="a", topic=Topic.RND, approved_by="guard"
    )
    ans_b = ApprovedAnswer(
        escalation_id="b", question="q", answer="a", topic=Topic.RND, approved_by="guard"
    )
    ans_a.citations.append(Citation(doc_id="d1", doc_title="t", snippet="s"))
    assert ans_b.citations == []


def test_verdict_values_unchanged():
    assert {v.value for v in Verdict} == {"approved", "needs_review", "blocked"}


def test_guard_category_values_unchanged():
    assert {v.value for v in GuardCategory} == {
        "none",
        "ruo_clinical_use",
        "off_label",
        "comparative_claim",
        "pricing_contract",
        "roadmap",
        "patient_data",
        "ungrounded",
    }


def test_topic_values_unchanged():
    assert {v.value for v in Topic} == {"rd", "qa_ra", "engineering", "sales_ops"}


def test_source_values_unchanged():
    assert {v.value for v in Source} == {"vendor_agent", "expert"}


def test_escalation_reason_values_unchanged():
    assert {v.value for v in EscalationReason} == {
        "low_confidence",
        "guard_blocked",
        "guard_needs_review",
    }
