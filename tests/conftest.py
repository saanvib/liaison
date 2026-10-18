"""Loop-test fixtures (D12): the real VendorCore and GuardCore wired together, everything else faked.

`tests/` is the only place allowed to import across agent directories. Nothing here
touches the network: the Guard's judge and the Vendor Agent's LLM are fakes, and the
embedder is the experts' offline bag of words (stopwords and plurals dropped, so a
paraphrase with the same content words scores 1.0 against the 0.85 KB threshold).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pytest

from contracts import ApprovedAnswer, Escalation, GuardRequest, GuardVerdict, IngestDocument, Topic
from experts import load_directory
from experts.embed import BagOfWordsEmbedder
from guard.core import GuardCore, Mode, detect_mode
from guard.judge import Judge

ROUTER_ADDRESS = "agent1q-router"
GUARD_ADDRESS = "agent1q-guard"
INGEST_ADDRESS = "agent1q-ingest"
EXTERNAL = "agent1q-asi-one-user"
SLACK_ASKER = "slack:U_ASKER"
EXPERT = "U0C9YESNKB7|Saanvi"
TODAY = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

# Short documents, one chunk each, so the offline embedder clears LOW_CONFIDENCE (0.5).
DOCS = [
    IngestDocument(
        doc_id="ifu-storage", title="IFU: Storage", doc_type="ifu", source="cli",
        text="Store the extraction kit at 2 to 8 C. Unopened kits are stable for 12 months.",
    ),
    IngestDocument(
        doc_id="faq-orders", title="FAQ: Bulk orders", doc_type="faq", source="cli",
        text="Bulk orders of 500 kits are handled by sales operations.",
    ),
]


class FakeJudgeLLM:
    """The Guard judge's LLM: always approves. Rules still run first and are final."""

    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt, schema=None):
        self.calls += 1
        return json.dumps({"verdict": "approved", "category": "none", "reason": "no policy issue"})


class GuardAdapter:
    """GuardClient around the real GuardCore. Requests in `dead_modes` get no verdict (a timeout)."""

    def __init__(self, core: GuardCore):
        self.core = core
        self.dead_modes: set[Mode] = set()
        self.requests: list[GuardRequest] = []
        self.verdicts: list[Optional[GuardVerdict]] = []

    async def check(self, req: GuardRequest, timeout_s: float = 10.0) -> Optional[GuardVerdict]:
        self.requests.append(req)
        verdict = None if detect_mode(req) in self.dead_modes else self.core.check(req)
        self.verdicts.append(verdict)
        return verdict

    def modes(self) -> list[Mode]:
        return [detect_mode(r) for r in self.requests]


class CollectingRouter:
    """RouterClient fake: records what the Vendor Agent's wiring would send."""

    def __init__(self):
        self.escalations: list[Escalation] = []
        self.replies: list[tuple[str, str]] = []

    async def send_escalation(self, esc: Escalation) -> None:
        self.escalations.append(esc)

    async def reply_chat(self, to_sender: str, text: str) -> None:
        self.replies.append((to_sender, text))


class FakeVendorLLM:
    """Drafts by quoting the top hit (so offline grounding passes) unless a draft is scripted."""

    def __init__(self, topic: Topic = Topic.RND):
        self.topic = topic
        self.drafts: dict[str, str] = {}  # question -> scripted draft
        self.draft_calls: list[str] = []

    def draft(self, question, hits) -> str:
        self.draft_calls.append(question)
        return self.drafts.get(question, hits[0].text)

    def classify_topic(self, question) -> Topic:
        return self.topic


@dataclass
class Loop:
    """The system under test plus the fake 'router driver' (an expert answering in Slack)."""

    vendor: object  # vendor_agent.core.VendorCore
    guard: GuardAdapter
    router: CollectingRouter
    llm: FakeVendorLLM
    storage: object
    judge_llm: FakeJudgeLLM
    outputs: list = field(default_factory=list)

    async def ask(self, text: str, internal: bool = True, asker_ref: Optional[str] = None):
        """What the Vendor Agent's wiring does: handle the question, forward any escalation."""
        sender = ROUTER_ADDRESS if internal else EXTERNAL
        out = await self.vendor.handle_question(text, sender, asker_ref or (SLACK_ASKER if internal else None))
        if out.escalation is not None:
            await self.router.send_escalation(out.escalation)
        self.outputs.append(out)
        return out

    def approved_answer(self, esc: Escalation, answer: str, approved_by: str = EXPERT) -> ApprovedAnswer:
        """The Router's message for an expert reply: no Guard verdict (D17, D18)."""
        return ApprovedAnswer(
            escalation_id=esc.escalation_id, question=esc.question, answer=answer,
            topic=esc.topic or Topic.RND, approved_by=approved_by, guard_verdict=None, asker_ref=esc.asker_ref,
        )

    async def expert_replies(self, esc: Escalation, answer: str, sender: str = ROUTER_ADDRESS):
        return await self.vendor.handle_approved_answer(self.approved_answer(esc, answer), sender)


@pytest.fixture
def loop() -> Loop:
    core_mod = pytest.importorskip("vendor_agent.core", reason="needs Person 1's vendor_agent (not merged yet)")
    from vendor_agent.config import Config
    from vendor_agent.kb import KnowledgeBase
    from vendor_agent.retrieval import DictStorage, Retriever

    storage = DictStorage()
    embedder = BagOfWordsEmbedder()
    config = Config(
        router_address=ROUTER_ADDRESS, guard_address=GUARD_ADDRESS, ingest_address=INGEST_ADDRESS,
        # A12: the Vendor Agent's allowlist is exactly the directory's Slack IDs.
        approver_allowlist=frozenset(e.slack_user_id for e in load_directory()),
    )
    judge_llm = FakeJudgeLLM()
    guard = GuardAdapter(GuardCore(judge=Judge(judge_llm)))
    router = CollectingRouter()
    llm = FakeVendorLLM()
    kb = KnowledgeBase(storage, embedder, config, clock=lambda: TODAY)
    retriever = Retriever(embedder, storage)
    for doc in DOCS:
        retriever.add_document(doc)
    vendor = core_mod.VendorCore(kb, retriever, llm, guard, router, storage, config, clock=lambda: TODAY)
    return Loop(vendor, guard, router, llm, storage, judge_llm)
