"""VendorCore: the question-answering flow (D4, D7, D22, D23, D32, D33; A1, A2, A5, A6, A7).

handle_question never talks to Slack or sends an escalation itself — it only
returns a VendorOutput. The agent wiring (Step 5) sends the chat reply and,
when output.escalation is set, calls router.send_escalation.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from contracts import (
    ApprovedAnswer,
    Citation,
    Escalation,
    EscalationReason,
    GuardRequest,
    IngestDocument,
    Source,
    Verdict,
)

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
        internal = sender == self.config.router_address
        counter_key = self._counter_key(sender, asker_ref)

        # Kick off the question's embedding concurrently with the screen call;
        # it's reused for the KB lookup and retrieval below if we get that far.
        embed_task = asyncio.create_task(asyncio.to_thread(self.retriever.embedder.embed, [text]))

        screen_req = GuardRequest(
            request_id=str(uuid.uuid4()), source=Source.VENDOR_AGENT, question=text, draft_answer=""
        )
        verdict = await self.guard.check(screen_req, timeout_s=self.config.guard_timeout_s)

        if verdict is not None and verdict.verdict == Verdict.BLOCKED:
            embed_task.cancel()
            self._reset_miss_count(counter_key)
            return VendorOutput(reply_text=REFUSAL_TEXT, outcome="refused", escalation=None, asker_ref=asker_ref)

        if verdict is None:
            embed_task.cancel()
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
        [vector] = await embed_task

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
            egress_verdict = await self.guard.check(egress_req, timeout_s=self.config.guard_timeout_s)
            if egress_verdict is not None and egress_verdict.verdict == Verdict.APPROVED:
                return VendorOutput(reply_text=answer_with_note, outcome="answered", escalation=None, asker_ref=asker_ref)
            topic = self.llm.classify_topic(text)
            return VendorOutput(reply_text=team_role_reply(topic, self.config), outcome="holding", escalation=None, asker_ref=asker_ref)

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
        draft_verdict = await self.guard.check(draft_req, timeout_s=self.config.guard_timeout_s)

        if draft_verdict is not None and draft_verdict.verdict == Verdict.APPROVED:
            return VendorOutput(
                reply_text=_render_citations(draft, citations), outcome="answered", escalation=None, asker_ref=asker_ref
            )

        if draft_verdict is None:
            # never send the draft (D4); not flagged, since draft_answer is set (A5)
            topic = self.llm.classify_topic(text)
            escalation = Escalation(
                escalation_id=str(uuid.uuid4()), question=text, reason=EscalationReason.GUARD_NEEDS_REVIEW,
                topic=topic, draft_answer=draft, guard_verdict=None, asker_ref=asker_ref,
            ) if internal else None
            return self._escalated_or_holding(internal, asker_ref, escalation, topic)

        # needs_review or blocked
        reason = EscalationReason.GUARD_BLOCKED if draft_verdict.verdict == Verdict.BLOCKED else EscalationReason.GUARD_NEEDS_REVIEW
        topic = draft_verdict.reroute_topic or self.llm.classify_topic(text)
        escalation = Escalation(
            escalation_id=str(uuid.uuid4()), question=text, reason=reason, topic=topic,
            draft_answer=draft, guard_verdict=draft_verdict, asker_ref=asker_ref,
        ) if internal else None
        return self._escalated_or_holding(internal, asker_ref, escalation, topic)

    async def handle_approved_answer(self, msg: ApprovedAnswer, sender: str) -> AcceptResult:
        return self.kb.accept(msg, sender)

    async def handle_ingest(self, doc: IngestDocument, sender: str) -> None:
        if sender not in (self.config.ingest_address, self.config.router_address):
            logger.info("handle_ingest ignored sender=%s doc_id=%s", sender, doc.doc_id)
            return
        self.retriever.add_document(doc)
