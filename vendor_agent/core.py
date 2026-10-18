"""VendorCore: the question-answering flow (D4, D7, D22, D23, D32, D33; A1, A2, A5, A6, A7).

handle_question never talks to Slack or sends an escalation itself — it only
returns a VendorOutput. The agent wiring (Step 5) sends the chat reply and,
when output.escalation is set, calls router.send_escalation.

The flow is split at each Guard call: start() returns the first PendingCheck,
and advance(check, verdict) returns either the next PendingCheck or the final
VendorOutput. A PendingCheck is plain data, so the agent wiring keeps it in
ctx.storage while the verdict is in flight: hosted Agentverse keeps nothing in
memory from one message to the next (seen live 2026-10-18), so the handler that
gets the verdict never saw the question. handle_question runs the same steps in
one call, awaiting each verdict, for tests and in-process use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional, Union

from contracts import (
    ApprovedAnswer,
    Citation,
    Escalation,
    EscalationReason,
    GuardRequest,
    GuardVerdict,
    IngestDocument,
    Source,
    Topic,
    Verdict,
)

from vendor_agent.clients import LLMError
from vendor_agent.models import AcceptResult, VendorOutput
from vendor_agent.texts import HOLDING_TEXT, REFUSAL_TEXT, RETRY_TEXT, team_role_reply

logger = logging.getLogger(__name__)

_MISS_COUNTER_PREFIX = "vendor_agent:guard_miss_count:"


def _render_citations(text: str, citations: list[Citation]) -> str:
    if not citations:
        return text
    lines = "\n".join(
        f"- {c.doc_title}" + (f" ({c.location})" if c.location else "") for c in citations
    )
    return f"{text}\n\nSources:\n{lines}"


@dataclass
class PendingCheck:
    """A question waiting on one Guard verdict: the screen, the egress check of a KB
    answer (external askers only), or the draft check."""

    stage: Literal["screen", "egress", "draft"]
    request: GuardRequest
    question: str
    sender: str
    asker_ref: Optional[str]
    draft: Optional[str] = None
    citations: list[Citation] = field(default_factory=list)
    kb_reply: Optional[str] = None  # egress: the KB answer with its attribution note

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "request": json.loads(self.request.model_dump_json()),
            "question": self.question,
            "sender": self.sender,
            "asker_ref": self.asker_ref,
            "draft": self.draft,
            "citations": [json.loads(c.model_dump_json()) for c in self.citations],
            "kb_reply": self.kb_reply,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingCheck":
        return cls(
            stage=d["stage"],
            request=GuardRequest.model_validate(d["request"]),
            question=d["question"],
            sender=d["sender"],
            asker_ref=d["asker_ref"],
            draft=d["draft"],
            citations=[Citation.model_validate(c) for c in d["citations"]],
            kb_reply=d["kb_reply"],
        )


class VendorCore:
    def __init__(self, kb, retriever, llm, guard, router, storage, config, clock):
        self.kb = kb
        self.retriever = retriever
        self.llm = llm
        self.guard = guard
        self.router = router
        self.storage = storage
        self.config = config
        self.clock = clock

    # -- Guard-miss failure counter: first miss retries, a second escalates flagged --

    def _counter_key(self, sender: str, asker_ref: Optional[str]) -> str:
        return _MISS_COUNTER_PREFIX + (asker_ref or sender)

    def _reset_miss_count(self, key: str) -> None:
        self.storage.set(key, 0)

    def _increment_miss_count(self, key: str) -> int:
        count = (self.storage.get(key) or 0) + 1
        self.storage.set(key, count)
        return count

    def _escalated_or_holding(
        self, internal: bool, asker_ref: Optional[str], escalation: Optional[Escalation], topic
    ) -> VendorOutput:
        if internal:
            self.kb.add_pending(escalation, flagged=False)
            return VendorOutput(reply_text=HOLDING_TEXT, outcome="escalated", escalation=escalation, asker_ref=asker_ref)
        return VendorOutput(
            reply_text=team_role_reply(topic, self.config), outcome="holding", escalation=None, asker_ref=asker_ref
        )

    # -- handle_question (D4, D7, D22, D23) --

    async def handle_question(self, text: str, sender: str, asker_ref: Optional[str]) -> VendorOutput:
        # Kick off the question's embedding concurrently with the screen call;
        # it's reused for the KB lookup and retrieval below if we get that far.
        embed_task = asyncio.create_task(asyncio.to_thread(self.retriever.embedder.embed, [text]))
        step: Union[PendingCheck, VendorOutput] = self.start(text, sender, asker_ref)
        vector = None
        while isinstance(step, PendingCheck):
            verdict = await self.guard.check(step.request, timeout_s=self.config.guard_timeout_s)
            if step.stage == "screen":
                if verdict is None or verdict.verdict == Verdict.BLOCKED:
                    embed_task.cancel()
                else:
                    try:
                        [vector] = await embed_task
                    except LLMError:
                        vector = None  # advance() retries the embedding and handles a second failure
            step = self.advance(step, verdict, vector=vector)
        return step

    def start(self, text: str, sender: str, asker_ref: Optional[str]) -> PendingCheck:
        """The rules-only screen of the question, the first Guard call."""
        screen_req = GuardRequest(
            request_id=str(uuid.uuid4()), source=Source.VENDOR_AGENT, question=text, draft_answer=""
        )
        return PendingCheck(stage="screen", request=screen_req, question=text, sender=sender, asker_ref=asker_ref)

    def advance(
        self, check: PendingCheck, verdict: Optional[GuardVerdict], vector: Optional[list[float]] = None
    ) -> Union[PendingCheck, VendorOutput]:
        """Continue after `check`'s verdict (None = no verdict in time). `vector` is the
        question's embedding if the caller already has it; otherwise it's computed here."""
        try:
            if check.stage == "screen":
                return self._after_screen(check, verdict, vector)
            if check.stage == "egress":
                return self._after_egress(check, verdict)
            return self._after_draft(check, verdict)
        except LLMError as e:
            # Gemini down or out of quota (HTTP 429 live, 2026-10-18): without this the
            # asker got no reply at all. Escalate internally, team-or-role reply externally.
            logger.warning("LLM unavailable at %s for request_id=%s: %s", check.stage, check.request.request_id, e)
            return self._llm_unavailable(check)

    def _llm_unavailable(self, check: PendingCheck) -> VendorOutput:
        internal = check.sender == self.config.router_address
        escalation = Escalation(
            escalation_id=str(uuid.uuid4()), question=check.question, reason=EscalationReason.LOW_CONFIDENCE,
            topic=None, draft_answer=None, guard_verdict=None, asker_ref=check.asker_ref,
        ) if internal else None
        # No topic without the LLM: the Router classifies internal ones (topic=None); R&D otherwise.
        return self._escalated_or_holding(internal, check.asker_ref, escalation, Topic.RND)

    def _after_screen(
        self, check: PendingCheck, verdict: Optional[GuardVerdict], vector: Optional[list[float]]
    ) -> Union[PendingCheck, VendorOutput]:
        text, sender, asker_ref = check.question, check.sender, check.asker_ref
        internal = sender == self.config.router_address
        counter_key = self._counter_key(sender, asker_ref)

        if verdict is not None and verdict.verdict == Verdict.BLOCKED:
            self._reset_miss_count(counter_key)
            return VendorOutput(reply_text=REFUSAL_TEXT, outcome="refused", escalation=None, asker_ref=asker_ref)

        if verdict is None:
            count = self._increment_miss_count(counter_key)
            if count == 1:
                return VendorOutput(reply_text=RETRY_TEXT, outcome="retry", escalation=None, asker_ref=asker_ref)
            # second consecutive miss: flagged forward (A5) -- never written to the KB
            self._reset_miss_count(counter_key)
            if internal:
                escalation = Escalation(
                    escalation_id=str(uuid.uuid4()), question=text, reason=EscalationReason.GUARD_NEEDS_REVIEW,
                    topic=None, draft_answer=None, guard_verdict=None, asker_ref=asker_ref,
                )
                self.kb.add_pending(escalation, flagged=True)
                return VendorOutput(reply_text=HOLDING_TEXT, outcome="escalated", escalation=escalation, asker_ref=asker_ref)
            topic = self.llm.classify_topic(text)
            return VendorOutput(reply_text=team_role_reply(topic, self.config), outcome="holding", escalation=None, asker_ref=asker_ref)

        # approved
        self._reset_miss_count(counter_key)
        if vector is None:
            [vector] = self.retriever.embedder.embed([text])

        # Step 2: KB lookup
        entry = self.kb.lookup(text, vector=vector)
        if entry is not None:
            answer_with_note = f"{entry.answer}\n\n{self.kb.attribution_note(entry)}"
            if internal:
                return VendorOutput(reply_text=answer_with_note, outcome="answered", escalation=None, asker_ref=asker_ref)
            egress_req = GuardRequest(
                request_id=str(uuid.uuid4()), source=Source.EXPERT, question=text,
                draft_answer=entry.answer, citations=[],
            )
            return PendingCheck(
                stage="egress", request=egress_req, question=text, sender=sender, asker_ref=asker_ref,
                kb_reply=answer_with_note,
            )

        # Step 3: retrieve
        hits = self.retriever.search(text, vector=vector)
        conf = self.retriever.confidence(hits)
        if conf < self.config.low_confidence:
            topic = self.llm.classify_topic(text)
            escalation = Escalation(
                escalation_id=str(uuid.uuid4()), question=text, reason=EscalationReason.LOW_CONFIDENCE,
                topic=topic, draft_answer=None, guard_verdict=None, asker_ref=asker_ref,
            ) if internal else None
            return self._escalated_or_holding(internal, asker_ref, escalation, topic)

        # Step 4: draft and Guard
        draft = self.llm.draft(text, hits)
        citations = self.retriever.to_citations(hits)
        draft_req = GuardRequest(
            request_id=str(uuid.uuid4()), source=Source.VENDOR_AGENT, question=text,
            draft_answer=draft, citations=citations, confidence=conf,
        )
        return PendingCheck(
            stage="draft", request=draft_req, question=text, sender=sender, asker_ref=asker_ref,
            draft=draft, citations=citations,
        )

    def _after_egress(self, check: PendingCheck, verdict: Optional[GuardVerdict]) -> VendorOutput:
        if verdict is not None and verdict.verdict == Verdict.APPROVED:
            return VendorOutput(reply_text=check.kb_reply, outcome="answered", escalation=None, asker_ref=check.asker_ref)
        topic = self.llm.classify_topic(check.question)
        return VendorOutput(reply_text=team_role_reply(topic, self.config), outcome="holding", escalation=None, asker_ref=check.asker_ref)

    def _after_draft(self, check: PendingCheck, verdict: Optional[GuardVerdict]) -> VendorOutput:
        text, draft, asker_ref = check.question, check.draft, check.asker_ref
        internal = check.sender == self.config.router_address

        if verdict is not None and verdict.verdict == Verdict.APPROVED:
            return VendorOutput(
                reply_text=_render_citations(draft, check.citations), outcome="answered", escalation=None, asker_ref=asker_ref
            )

        if verdict is None:
            # never send the draft (D4); not flagged, since draft_answer is set (A5)
            topic = self.llm.classify_topic(text)
            escalation = Escalation(
                escalation_id=str(uuid.uuid4()), question=text, reason=EscalationReason.GUARD_NEEDS_REVIEW,
                topic=topic, draft_answer=draft, guard_verdict=None, asker_ref=asker_ref,
            ) if internal else None
            return self._escalated_or_holding(internal, asker_ref, escalation, topic)

        # needs_review or blocked
        reason = EscalationReason.GUARD_BLOCKED if verdict.verdict == Verdict.BLOCKED else EscalationReason.GUARD_NEEDS_REVIEW
        topic = verdict.reroute_topic or self.llm.classify_topic(text)
        escalation = Escalation(
            escalation_id=str(uuid.uuid4()), question=text, reason=reason, topic=topic,
            draft_answer=draft, guard_verdict=verdict, asker_ref=asker_ref,
        ) if internal else None
        return self._escalated_or_holding(internal, asker_ref, escalation, topic)

    async def handle_approved_answer(self, msg: ApprovedAnswer, sender: str) -> AcceptResult:
        return self.kb.accept(msg, sender)

    async def handle_ingest(self, doc: IngestDocument, sender: str) -> None:
        if sender not in (self.config.ingest_address, self.config.router_address):
            logger.info("handle_ingest ignored sender=%s doc_id=%s", sender, doc.doc_id)
            return
        self.retriever.add_document(doc)
