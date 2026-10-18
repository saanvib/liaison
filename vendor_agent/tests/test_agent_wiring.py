"""The wiring answers a question across separate messages, with nothing kept in memory.

Hosted Agentverse keeps no in-memory state between messages (seen live on 2026-10-18: an
awaited Future, then a background task, never got its GuardVerdict). So each step here runs
on a freshly wired agent that shares only storage with the others, and that storage holds
JSON only, like agent storage does.
"""
import json
import logging

from uagents import Agent, Model
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from contracts import Escalation, EscalationReason, GuardRequest, GuardVerdict, Verdict
from vendor_agent.agent import build_reply_envelope, wire
from vendor_agent.clients import OfflineEmbedder
from vendor_agent.config import Config
from vendor_agent.core import VendorCore
from vendor_agent.kb import KnowledgeBase
from vendor_agent.retrieval import DictStorage, Retriever
from vendor_agent.tests.test_flow import FakeLLM, _seed_retrieval
from vendor_agent.texts import REFUSAL_TEXT, RETRY_TEXT

ROUTER = "agent1router"
GUARD = "agent1guard"
ASKER = "agent1asi-one-user"
REF = "slack:C1:1792297350.247049:U1"
CONFIG = Config(router_address=ROUTER, guard_address=GUARD, ingest_address="i", approver_allowlist=frozenset())


class JsonStorage(DictStorage):
    """Agent storage keeps JSON, so anything that doesn't survive a round trip fails here."""

    def set(self, key, value):
        super().set(key, json.loads(json.dumps(value)))


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.logger = logging.getLogger("fake-ctx")

    async def send(self, destination, message):
        self.sent.append((destination, message))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FreshAgent:
    """One wired agent, built from scratch on the shared storage, as if for one message."""

    def __init__(self, storage, clock):
        core = VendorCore(
            KnowledgeBase(storage, OfflineEmbedder(), CONFIG), Retriever(OfflineEmbedder(), storage),
            FakeLLM(), None, None, storage, CONFIG, clock=lambda: None,
        )
        self.agent = Agent(name="vendor-wiring-test", seed="vendor-wiring-test-seed")
        wire(self.agent, CONFIG, core=core, now=clock)
        self.ctx = FakeCtx()

    def _handler(self, model):
        digest = Model.build_schema_digest(model)
        for proto in [self.agent._protocol, *self.agent.protocols.values()]:
            if digest in proto.signed_message_handlers:
                return proto.signed_message_handlers[digest]
        raise AssertionError(f"no {model.__name__} handler")

    async def chat(self, sender, text):
        await self._handler(ChatMessage)(self.ctx, sender, ChatMessage(content=[TextContent(text=text)]))
        return self.ctx.sent

    async def verdict(self, request_id, verdict=Verdict.APPROVED, sender=GUARD):
        msg = GuardVerdict(request_id=request_id, verdict=verdict, reason="test")
        await self._handler(GuardVerdict)(self.ctx, sender, msg)
        return self.ctx.sent

    async def sweep(self):
        [(expire, _period)] = self.agent._protocol.intervals
        await expire(self.ctx)
        return self.ctx.sent


def guard_request(sent) -> GuardRequest:
    [(dest, req)] = [(d, m) for d, m in sent if isinstance(m, GuardRequest)]
    assert dest == GUARD
    return req


def setup():
    storage, clock = JsonStorage(), Clock()
    _seed_retrieval(Retriever(OfflineEmbedder(), storage))
    return storage, clock


async def test_external_answer_takes_three_messages_on_three_fresh_agents():
    storage, clock = setup()

    sent = await FreshAgent(storage, clock).chat(ASKER, "how long is the kit stable for?")
    assert [type(m) for _, m in sent] == [ChatAcknowledgement, GuardRequest]  # returns at once, no reply yet
    screen = guard_request(sent)
    assert screen.draft_answer == ""

    sent = await FreshAgent(storage, clock).verdict(screen.request_id)
    draft = guard_request(sent)
    assert draft.draft_answer == FakeLLM().draft_text
    assert draft.citations  # survived storage as JSON

    sent = await FreshAgent(storage, clock).verdict(draft.request_id)
    [(dest, reply)] = sent
    assert dest == ASKER
    assert FakeLLM().draft_text in reply.text() and "Sources:" in reply.text()


async def test_internal_question_gets_reply_envelope_and_escalation():
    storage, clock = setup()
    sent = await FreshAgent(storage, clock).chat(ROUTER, f"LIAISON-REF: {REF}\n\ncompletely unrelated kayaking question")
    screen = guard_request(sent)

    sent = await FreshAgent(storage, clock).verdict(screen.request_id)
    [(reply_dest, reply), (esc_dest, esc)] = sent
    assert reply_dest == ROUTER
    assert reply.text().startswith(build_reply_envelope(REF, "escalated", ""))
    assert esc_dest == ROUTER and isinstance(esc, Escalation)
    assert esc.reason == EscalationReason.LOW_CONFIDENCE and esc.asker_ref == REF


async def test_blocked_screen_refuses():
    storage, clock = setup()
    screen = guard_request(await FreshAgent(storage, clock).chat(ASKER, "patient Jane Doe DOB 1/1/1990"))
    [(dest, reply)] = await FreshAgent(storage, clock).verdict(screen.request_id, Verdict.BLOCKED)
    assert dest == ASKER and reply.text() == REFUSAL_TEXT


async def test_no_verdict_by_the_deadline_is_a_timeout_and_a_late_verdict_is_ignored():
    storage, clock = setup()
    screen = guard_request(await FreshAgent(storage, clock).chat(ASKER, "how long is the kit stable for?"))

    assert await FreshAgent(storage, clock).sweep() == []  # not due yet

    clock.t += CONFIG.guard_timeout_s
    [(dest, reply)] = await FreshAgent(storage, clock).sweep()
    assert dest == ASKER and reply.text() == RETRY_TEXT

    assert await FreshAgent(storage, clock).verdict(screen.request_id) == []
    assert await FreshAgent(storage, clock).sweep() == []


async def test_verdict_from_anyone_but_the_guard_is_dropped():
    storage, clock = setup()
    screen = guard_request(await FreshAgent(storage, clock).chat(ASKER, "patient Jane Doe DOB 1/1/1990"))

    assert await FreshAgent(storage, clock).verdict(screen.request_id, Verdict.APPROVED, sender="agent1spoof") == []

    [(_, reply)] = await FreshAgent(storage, clock).verdict(screen.request_id, Verdict.BLOCKED)
    assert reply.text() == REFUSAL_TEXT  # the check was still waiting for the real Guard
