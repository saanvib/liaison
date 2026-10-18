"""Local smoke run: the Vendor Agent answers hand-sent ChatMessages against the
seeded docs, with a fake Guard/LLM/Router (no network, no API cost).

    python -m vendor_agent.smoke

Runs the Vendor Agent and a test client in a uAgents Bureau, so messages are
delivered in-process. Exits 0 when a documented (KB) question gets a cited
answer, an unrelated question gets the holding reply, and a patient-data
question is refused; 1 otherwise. Uses a fixed dev seed; never use it for a
real agent.

The "documented" case goes through the KB (a pre-accepted expert answer),
not raw document retrieval: OfflineEmbedder's bag-of-words cosine on real,
hundred-plus-word document chunks scores well below LOW_CONFIDENCE even for
a near-verbatim question (cosine of mismatched-size token sets is bounded by
sqrt(|query|/|chunk|) regardless of word overlap quality) -- a real
calibration note for the free offline fallback, not a bug in this flow.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from uagents import Agent, Bureau, Context
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from contracts import ApprovedAnswer, Escalation, EscalationReason, GuardCategory, GuardVerdict, Topic, Verdict

from vendor_agent.agent import create_agent
from vendor_agent.clients import OfflineEmbedder
from vendor_agent.config import Config
from vendor_agent.core import VendorCore
from vendor_agent.ingest import load_documents
from vendor_agent.kb import KnowledgeBase
from vendor_agent.retrieval import DictStorage, Retriever

DOCS_SEED = Path(__file__).with_name("docs_seed")
DOCUMENTED_QUESTION = "What is the shelf life of an unopened AuroraPrep kit?"
DOCUMENTED_ANSWER = "18 months from the date of manufacture, stored at room temperature."

APPROVED = GuardVerdict(request_id="r", verdict=Verdict.APPROVED, reason="ok")
BLOCKED = GuardVerdict(request_id="r", verdict=Verdict.BLOCKED, reason="patient data", category=GuardCategory.PATIENT_DATA)

TIMEOUT_S = 90.0

# label, question, the Guard verdicts handle_question will consume for it, in order
CASES = [
    ("documented", DOCUMENTED_QUESTION, [APPROVED, APPROVED]),  # screen, then egress (KB hit, external)
    ("unrelated", "What is your favorite kayaking route?", [APPROVED]),  # screen only; low confidence, no draft
    ("patient-data", "My patient Jane Doe, DOB 1/1/1990, needs her AuroraPrep result interpreted.", [BLOCKED]),
]


class FakeGuard:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    async def check(self, req, timeout_s=10.0):
        return self.verdicts.pop(0)


class FakeRouter:
    async def send_escalation(self, esc):
        pass

    async def reply_chat(self, to_sender, text):
        pass


class FakeLLM:
    def draft(self, question, hits):
        return "The kit is stable for 18 months unopened at room temperature."

    def classify_topic(self, question):
        return Topic.RND


def make_fake_core() -> VendorCore:
    storage = DictStorage()
    embedder = OfflineEmbedder()
    config = Config(
        router_address="n/a", guard_address="n/a", ingest_address="n/a", approver_allowlist=frozenset(),
        vendor_seed="liaison-dev-only vendor smoke vendor",
    )
    kb = KnowledgeBase(storage, embedder, config)
    retriever = Retriever(embedder, storage)
    for doc in load_documents(DOCS_SEED):
        if doc.doc_type != "other":  # seed_questions.md is team reference, not a product doc
            retriever.add_document(doc)

    escalation_id = str(uuid.uuid4())
    kb.add_pending(
        Escalation(escalation_id=escalation_id, question=DOCUMENTED_QUESTION, reason=EscalationReason.LOW_CONFIDENCE),
        flagged=False,
    )
    kb.accept(
        ApprovedAnswer(
            escalation_id=escalation_id, question=DOCUMENTED_QUESTION, answer=DOCUMENTED_ANSWER,
            topic=Topic.RND, approved_by="guard", guard_verdict=APPROVED,
        ),
        sender=config.router_address,
    )

    guard = FakeGuard([v for _, _, verdicts in CASES for v in verdicts])
    return VendorCore(kb, retriever, FakeLLM(), guard, FakeRouter(), storage, config, clock=lambda: None)


async def main() -> int:
    quiet = dict(report_events=False, mark_inactive_on_shutdown=False)
    client = Agent(name="vendor-smoke-client", seed="liaison-dev-only vendor smoke client", **quiet)
    core = make_fake_core()
    vendor, _ = create_agent(config=core.config, core=core, **quiet)

    replies: list[str] = []
    acked = asyncio.Event()

    @client.on_message(model=ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        acked.set()

    @client.on_message(model=ChatMessage)
    async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
        replies.append(msg.text())

    @client.on_event("startup")
    async def send_all(ctx: Context):
        for i, (_, question, _) in enumerate(CASES):
            acked.clear()
            await ctx.send(vendor.address, ChatMessage(content=[TextContent(text=question)]))
            try:
                await asyncio.wait_for(acked.wait(), 10.0)
            except asyncio.TimeoutError:
                pass
            while len(replies) <= i:
                await asyncio.sleep(0.1)

    bureau = Bureau(agents=[vendor, client], port=8200, endpoint=[])
    task = asyncio.create_task(bureau.run_async())

    async def wait_for_all():
        while len(replies) < len(CASES):
            await asyncio.sleep(0.1)

    try:
        await asyncio.wait_for(wait_for_all(), TIMEOUT_S)
    except asyncio.TimeoutError:
        pass
    finally:
        task.cancel()

    print("\nVendor smoke run:")
    ok = len(replies) == len(CASES)
    labels = [label for label, _, _ in CASES]
    for label, reply in zip(labels, replies):
        print(f"  {label:<14} {reply[:100]!r}")
    if ok:
        ok = (
            "18 months" in replies[0]
            and "Expert answer" in replies[0]
            and "noted this for" in replies[1]
            and "can't process questions" in replies[2]
        )
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
