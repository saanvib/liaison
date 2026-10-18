import time

import pytest

from contracts import (
    ApprovedAnswer,
    Escalation,
    EscalationReason,
    GuardCategory,
    GuardVerdict,
    IngestDocument,
    Topic,
    Verdict,
)
from vendor_agent.clients import LLMError, OfflineEmbedder
from vendor_agent.config import Config
from vendor_agent.core import VendorCore
from vendor_agent.kb import KnowledgeBase
from vendor_agent.retrieval import DictStorage, Retriever
from vendor_agent.texts import HOLDING_TEXT, REFUSAL_TEXT, RETRY_TEXT, team_role_reply

ROUTER = "router1"
INTERNAL_SENDER = ROUTER
EXTERNAL_SENDER = "chat:someone"

APPROVED = GuardVerdict(request_id="r", verdict=Verdict.APPROVED, reason="ok")
NEEDS_REVIEW = GuardVerdict(request_id="r", verdict=Verdict.NEEDS_REVIEW, reason="unsure")
BLOCKED = GuardVerdict(request_id="r", verdict=Verdict.BLOCKED, reason="patient data", category=GuardCategory.PATIENT_DATA)


class FakeGuard:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    async def check(self, req, timeout_s=10.0):
        self.calls.append(req)
        return self.verdicts.pop(0)


class FakeRouter:
    def __init__(self):
        self.escalations = []

    async def send_escalation(self, esc):
        self.escalations.append(esc)

    async def reply_chat(self, to_sender, text):
        pass


class FakeLLM:
    def __init__(self, draft_text="drafted answer from the docs", topic=Topic.RND):
        self.draft_text = draft_text
        self.topic = topic
        self.draft_calls = []
        self.classify_calls = []

    def draft(self, question, hits):
        self.draft_calls.append((question, hits))
        return self.draft_text

    def classify_topic(self, question):
        self.classify_calls.append(question)
        return self.topic


def make_core(embedder=None, llm=None, guard_verdicts=(), router=None):
    storage = DictStorage()
    embedder = embedder or OfflineEmbedder()
    config = Config(
        router_address=ROUTER, guard_address="guard1", ingest_address="ingest1",
        approver_allowlist=frozenset({"U1"}),
    )
    kb = KnowledgeBase(storage, embedder, config)
    retriever = Retriever(embedder, storage)
    guard = FakeGuard(guard_verdicts)
    llm = llm or FakeLLM()
    router = router or FakeRouter()
    core = VendorCore(kb, retriever, llm, guard, router, storage, config, clock=lambda: None)
    return core, kb, retriever, storage, guard, llm


def accept_expert_answer(kb, question, answer, topic=Topic.RND):
    escalation_id = "seed-" + question
    kb.add_pending(Escalation(escalation_id=escalation_id, question=question, reason=EscalationReason.LOW_CONFIDENCE), flagged=False)
    result = kb.accept(
        ApprovedAnswer(escalation_id=escalation_id, question=question, answer=answer, topic=topic, approved_by="guard", guard_verdict=APPROVED),
        sender=ROUTER,
    )
    assert result.accepted


# -- screen --

@pytest.mark.asyncio
async def test_screen_blocked_refuses_with_no_llm_call_and_no_escalation():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[BLOCKED])
    out = await core.handle_question("patient Jane Doe DOB 1/1/1990 needs a result", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "refused"
    assert out.reply_text == REFUSAL_TEXT
    assert out.escalation is None
    assert llm.draft_calls == [] and llm.classify_calls == []


@pytest.mark.asyncio
async def test_screen_first_miss_retries_then_second_miss_flags_internal():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[None])
    first = await core.handle_question("what is the shelf life?", INTERNAL_SENDER, "slack:c:t:u1")
    assert first.outcome == "retry"
    assert first.reply_text == RETRY_TEXT

    guard.verdicts = [None]
    second = await core.handle_question("what is the shelf life?", INTERNAL_SENDER, "slack:c:t:u1")
    assert second.outcome == "escalated"
    assert second.escalation.reason == EscalationReason.GUARD_NEEDS_REVIEW
    assert second.escalation.topic is None
    assert second.escalation.draft_answer is None
    assert second.escalation.guard_verdict is None
    pending = kb.get_pending(second.escalation.escalation_id)
    assert pending.flagged is True


@pytest.mark.asyncio
async def test_screen_second_miss_holds_for_external():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[None])
    await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    guard.verdicts = [None]
    second = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert second.outcome == "holding"
    assert second.escalation is None
    assert second.reply_text == team_role_reply(llm.topic, core.config)


# -- counter reset --

@pytest.mark.asyncio
async def test_counter_resets_after_a_successful_screen():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[None])
    await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")  # miss 1 (retry)

    guard.verdicts = [APPROVED]
    hits_core_answer = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert hits_core_answer.outcome in ("holding", "answered")  # approved screen clears the counter regardless of what follows

    guard.verdicts = [None]
    after_reset = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert after_reset.outcome == "retry"  # counter was back at 0, so this is a first miss again, not a second


# -- KB hit --

@pytest.mark.asyncio
async def test_kb_hit_internal_returns_answer_with_attribution_and_only_one_guard_call():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED])
    accept_expert_answer(kb, "what is the shelf life?", "18 months")
    out = await core.handle_question("what is the shelf life?", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.outcome == "answered"
    assert "18 months" in out.reply_text
    assert "Expert answer" in out.reply_text
    assert len(guard.calls) == 1  # only the screen; no egress check for internal


@pytest.mark.asyncio
async def test_kb_hit_external_passes_through_egress_guard_in_expert_mode():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, APPROVED])
    accept_expert_answer(kb, "what is the shelf life?", "18 months")
    out = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "answered"
    assert "18 months" in out.reply_text
    egress_req = guard.calls[1]
    assert egress_req.source.value == "expert"
    assert egress_req.citations == []


@pytest.mark.asyncio
async def test_kb_hit_external_blocked_by_egress_guard_returns_holding():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, NEEDS_REVIEW])
    accept_expert_answer(kb, "what is the shelf life?", "18 months")
    out = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "holding"
    assert out.escalation is None
    assert out.reply_text == team_role_reply(llm.topic, core.config)


# -- low confidence --

@pytest.mark.asyncio
async def test_low_confidence_escalates_internal_and_holds_external():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED])
    out = await core.handle_question("completely unrelated kayaking question", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.outcome == "escalated"
    assert out.escalation.reason == EscalationReason.LOW_CONFIDENCE
    assert out.escalation.topic == llm.topic
    pending = kb.get_pending(out.escalation.escalation_id)
    assert pending.flagged is False

    guard.verdicts = [APPROVED]
    out2 = await core.handle_question("completely unrelated kayaking question", EXTERNAL_SENDER, "chat:x")
    assert out2.outcome == "holding"
    assert out2.escalation is None


# -- draft + guard --

def _seed_retrieval(retriever):
    retriever.add_document(
        IngestDocument(doc_id="d1", title="Datasheet", doc_type="datasheet", text="The kit is stable for 18 months unopened at room temperature. " * 3)
    )


@pytest.mark.asyncio
async def test_draft_approved_returns_draft_with_citations():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, APPROVED])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "answered"
    assert llm.draft_text in out.reply_text
    assert "Sources:" in out.reply_text
    assert "Datasheet" in out.reply_text


@pytest.mark.asyncio
async def test_draft_no_verdict_never_sends_draft_and_escalation_carries_it():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, None])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.outcome == "escalated"
    assert llm.draft_text not in (out.reply_text or "")
    assert out.escalation.draft_answer == llm.draft_text
    assert out.escalation.guard_verdict is None
    assert out.escalation.reason == EscalationReason.GUARD_NEEDS_REVIEW
    pending = kb.get_pending(out.escalation.escalation_id)
    assert pending.flagged is False  # A5: not flagged, draft_answer is set


@pytest.mark.asyncio
async def test_draft_no_verdict_holds_for_external():
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, None])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "holding"
    assert out.escalation is None


@pytest.mark.parametrize("verdict, reason", [(NEEDS_REVIEW, EscalationReason.GUARD_NEEDS_REVIEW), (BLOCKED, EscalationReason.GUARD_BLOCKED)])
@pytest.mark.asyncio
async def test_draft_needs_review_or_blocked_escalates_internal(verdict, reason):
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, verdict])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.outcome == "escalated"
    assert out.escalation.reason == reason
    assert out.escalation.guard_verdict == verdict
    assert out.escalation.draft_answer == llm.draft_text


@pytest.mark.parametrize("verdict", [NEEDS_REVIEW, BLOCKED])
@pytest.mark.asyncio
async def test_draft_needs_review_or_blocked_holds_external(verdict):
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, verdict])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "holding"
    assert out.escalation is None


@pytest.mark.asyncio
async def test_draft_blocked_uses_reroute_topic_when_set():
    rerouted = GuardVerdict(request_id="r", verdict=Verdict.NEEDS_REVIEW, reason="pricing", reroute_topic=Topic.SALES_OPS)
    core, kb, retriever, storage, guard, llm = make_core(guard_verdicts=[APPROVED, rerouted])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.escalation.topic == Topic.SALES_OPS
    assert llm.classify_calls == []  # reroute_topic was used, no need to classify


# -- handle_approved_answer / handle_ingest --

@pytest.mark.asyncio
async def test_handle_approved_answer_delegates_to_kb_accept():
    core, kb, retriever, storage, guard, llm = make_core()
    kb.add_pending(Escalation(escalation_id="e1", question="q", reason=EscalationReason.LOW_CONFIDENCE), flagged=False)
    msg = ApprovedAnswer(escalation_id="e1", question="q", answer="a", topic=Topic.RND, approved_by="guard", guard_verdict=APPROVED)
    result = await core.handle_approved_answer(msg, sender=ROUTER)
    assert result.accepted is True


@pytest.mark.asyncio
async def test_handle_ingest_ignores_untrusted_sender():
    core, kb, retriever, storage, guard, llm = make_core()
    doc = IngestDocument(doc_id="d1", title="T", doc_type="other", text="hello world")
    await core.handle_ingest(doc, sender="some-stranger")
    assert retriever.search("hello") == []


@pytest.mark.asyncio
async def test_handle_ingest_accepts_ingest_or_router_sender():
    core, kb, retriever, storage, guard, llm = make_core()
    doc = IngestDocument(doc_id="d1", title="T", doc_type="other", text="hello world, this is a real document body")
    await core.handle_ingest(doc, sender=core.config.ingest_address)
    assert retriever.search("hello world") != []


# -- concurrency timing --

class SlowEmbedder:
    def __init__(self, delay_s):
        self.delay_s = delay_s

    def embed(self, texts):
        time.sleep(self.delay_s)
        return [[1.0, 0.0] for _ in texts]


class SlowGuard:
    def __init__(self, delay_s, verdict):
        self.delay_s = delay_s
        self.verdict = verdict
        self.calls = []

    async def check(self, req, timeout_s=10.0):
        import asyncio
        self.calls.append(req)
        await asyncio.sleep(self.delay_s)
        return self.verdict


@pytest.mark.asyncio
async def test_embedding_runs_concurrently_with_the_screen():
    delay = 0.2
    storage = DictStorage()
    embedder = SlowEmbedder(delay)
    config = Config(router_address=ROUTER, guard_address="g", ingest_address="i", approver_allowlist=frozenset())
    kb = KnowledgeBase(storage, embedder, config)
    retriever = Retriever(embedder, storage)
    guard = SlowGuard(delay, APPROVED)
    llm = FakeLLM()
    core = VendorCore(kb, retriever, llm, guard, FakeRouter(), storage, config, clock=lambda: None)

    accept_expert_answer(kb, "what is the shelf life?", "18 months")

    start = time.monotonic()
    out = await core.handle_question("what is the shelf life?", INTERNAL_SENDER, "slack:c:t:u1")
    elapsed = time.monotonic() - start

    assert out.outcome == "answered"
    assert elapsed < 1.5 * delay  # concurrent: ~delay total, not ~2*delay serial


# -- LLM unavailable (Gemini HTTP 429, seen live 2026-10-18) --

class DownLLM(FakeLLM):
    def draft(self, question, hits):
        raise LLMError("HTTP 429: You exceeded your current quota")

    def classify_topic(self, question):
        raise LLMError("HTTP 429: You exceeded your current quota")


@pytest.mark.asyncio
async def test_llm_down_at_draft_still_replies_to_external_asker():
    core, kb, retriever, storage, guard, llm = make_core(llm=DownLLM(), guard_verdicts=[APPROVED])
    _seed_retrieval(retriever)
    out = await core.handle_question("how long is the kit stable for?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "holding"
    assert out.reply_text == team_role_reply(Topic.RND, core.config)
    assert len(guard.calls) == 1  # no draft, so no draft check


@pytest.mark.asyncio
async def test_llm_down_escalates_internal_question_for_the_router_to_classify():
    core, kb, retriever, storage, guard, llm = make_core(llm=DownLLM(), guard_verdicts=[APPROVED])
    out = await core.handle_question("completely unrelated kayaking question", INTERNAL_SENDER, "slack:c:t:u1")
    assert out.outcome == "escalated"
    assert out.escalation.topic is None and out.escalation.draft_answer is None
    assert kb.get_pending(out.escalation.escalation_id).flagged is False


class DownEmbedder(OfflineEmbedder):
    def embed(self, texts):
        raise LLMError("HTTP 429: You exceeded your current quota")


@pytest.mark.asyncio
async def test_embeddings_down_still_replies():
    core, kb, retriever, storage, guard, llm = make_core(embedder=DownEmbedder(), guard_verdicts=[APPROVED])
    out = await core.handle_question("what is the shelf life?", EXTERNAL_SENDER, "chat:x")
    assert out.outcome == "holding"
    assert out.reply_text == team_role_reply(Topic.RND, core.config)
